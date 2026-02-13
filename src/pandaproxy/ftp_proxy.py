"""FTPS proxy for BambuLab printer file uploads.

This is a simple TCP passthrough proxy for FTP (port 990) and FTP data ports.
It forwards raw TCP bytes including TLS, allowing the client to do TLS session
reuse directly with the printer.

The proxy listens on:
- Port 990: FTP control channel
- Ports 2000-2100: FTP data channels (PASV mode)

Clients connect to the proxy IP, and the proxy forwards to the printer IP
on the same port. This allows clients that only support a single server IP
to work with the printer through the proxy.
"""

import asyncio
import contextlib
import logging

from pandaproxy.helper import close_writer
from pandaproxy.protocol import FTP_PORT

logger = logging.getLogger(__name__)

# FTP data port range used by BambuLab printers (PASV mode)
# Printers typically use ports like 2024, 2025, etc. (high byte 7-8)
FTP_DATA_PORT_START = 2000
FTP_DATA_PORT_END = 2100


class FTPProxy:
    """TCP passthrough proxy for FTPS connections.

    Forwards raw TCP bytes (including TLS) to the printer, allowing
    clients to establish TLS sessions directly with the printer.
    """

    def __init__(
        self,
        printer_ip: str,
        bind_address: str = "0.0.0.0",
    ) -> None:
        self.printer_ip = printer_ip
        self.bind_address = bind_address
        self.port = FTP_PORT

        self._control_server: asyncio.Server | None = None
        self._data_servers: list[asyncio.Server] = []
        self._running = False
        self._active_connections: set[asyncio.Task] = set()

    async def start(self) -> None:
        """Start the FTP proxy servers."""
        if self._running:
            return

        logger.info("Starting FTP passthrough proxy on %s:%d", self.bind_address, self.port)
        self._running = True

        # Start control channel server (port 990)
        self._control_server = await asyncio.start_server(
            lambda r, w: self._handle_connection(r, w, self.port),
            self.bind_address,
            self.port,
        )

        # Start data channel servers (port range for PASV mode)
        for data_port in range(FTP_DATA_PORT_START, FTP_DATA_PORT_END + 1):
            try:
                server = await asyncio.start_server(
                    lambda r, w, p=data_port: self._handle_connection(r, w, p),
                    self.bind_address,
                    data_port,
                )
                self._data_servers.append(server)
            except OSError as e:
                # Port might already be in use, skip it
                logger.debug("Could not bind to port %d: %s", data_port, e)

        logger.info(
            "FTP proxy listening on port %d and data ports %d-%d",
            self.port,
            FTP_DATA_PORT_START,
            FTP_DATA_PORT_END,
        )

    async def stop(self) -> None:
        """Stop the FTP proxy servers."""
        logger.info("Stopping FTP proxy")
        self._running = False

        # Cancel all active connections
        for task in list(self._active_connections):
            task.cancel()
        if self._active_connections:
            await asyncio.gather(*self._active_connections, return_exceptions=True)
        self._active_connections.clear()

        # Close servers
        if self._control_server:
            self._control_server.close()
            await self._control_server.wait_closed()

        for server in self._data_servers:
            server.close()
        for server in self._data_servers:
            await server.wait_closed()
        self._data_servers.clear()

        logger.info("FTP proxy stopped")

    async def _handle_connection(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        port: int,
    ) -> None:
        """Handle a TCP connection by forwarding to the printer."""
        peername = client_writer.get_extra_info("peername")
        port_type = "control" if port == self.port else "data"
        logger.debug("FTP %s connection from %s on port %d", port_type, peername, port)

        # Create task for this connection
        task = asyncio.current_task()
        if task:
            self._active_connections.add(task)

        upstream_writer: asyncio.StreamWriter | None = None

        try:
            # Connect to printer on the same port
            upstream_reader, upstream_writer = await asyncio.wait_for(
                asyncio.open_connection(self.printer_ip, port),
                timeout=10.0,
            )
            logger.debug("Connected to printer %s:%d", self.printer_ip, port)

            # Forward data in both directions
            await self._forward_bidirectional(
                client_reader, client_writer, upstream_reader, upstream_writer
            )

        except TimeoutError:
            logger.warning("Connection to printer %s:%d timed out", self.printer_ip, port)
        except ConnectionRefusedError:
            logger.debug("Printer %s:%d refused connection", self.printer_ip, port)
        except Exception as e:
            logger.debug("FTP %s connection error: %s", port_type, e)
        finally:
            await close_writer(client_writer)
            if upstream_writer:
                await close_writer(upstream_writer)
            if task:
                self._active_connections.discard(task)
            logger.debug("FTP %s connection closed", port_type)

    async def _forward_bidirectional(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        upstream_reader: asyncio.StreamReader,
        upstream_writer: asyncio.StreamWriter,
    ) -> None:
        """Forward data bidirectionally between client and upstream."""

        async def forward(
            src: asyncio.StreamReader,
            dst: asyncio.StreamWriter,
            direction: str,
        ) -> None:
            """Forward data from src to dst."""
            try:
                while True:
                    data = await src.read(65536)
                    if not data:
                        break
                    dst.write(data)
                    await dst.drain()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("Forward %s error: %s", direction, e)

        # Run both directions concurrently
        task1 = asyncio.create_task(forward(client_reader, upstream_writer, "client->printer"))
        task2 = asyncio.create_task(forward(upstream_reader, client_writer, "printer->client"))

        try:
            # Wait for either direction to complete
            done, pending = await asyncio.wait([task1, task2], return_when=asyncio.FIRST_COMPLETED)

            # Cancel the other direction
            for task in pending:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        except asyncio.CancelledError:
            task1.cancel()
            task2.cancel()
            raise
