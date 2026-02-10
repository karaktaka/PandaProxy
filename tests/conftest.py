"""Shared test fixtures and mock servers for PandaProxy tests."""

import asyncio
import contextlib
import ssl
import tempfile
from pathlib import Path

import pytest

from pandaproxy.helper import generate_self_signed_cert


@pytest.fixture
def temp_certs():
    """Generate temporary TLS certificates for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        cert_path = Path(tmpdir) / "test.crt"
        key_path = Path(tmpdir) / "test.key"

        generate_self_signed_cert(
            common_name="TestProxy",
            san_dns=["localhost"],
            san_ips=["127.0.0.1", "::1"],
            output_cert=cert_path,
            output_key=key_path,
        )

        yield cert_path, key_path


@pytest.fixture
def server_ssl_context(temp_certs):
    """Create server SSL context for mock servers."""
    cert_path, key_path = temp_certs
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_path, key_path)
    return ctx


@pytest.fixture
def client_ssl_context():
    """Create client SSL context that accepts self-signed certs."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class MockFTPServer:
    """Mock FTP server for testing FTP proxy behavior.

    This server simulates BambuLab printer FTP behavior for testing purposes.
    """

    def __init__(self, ssl_context: ssl.SSLContext, bind_address: str = "127.0.0.1"):
        self.ssl_context = ssl_context
        self.bind_address = bind_address
        self.port: int | None = None
        self._server: asyncio.Server | None = None
        self._connections: list[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = []
        self.commands_received: list[str] = []
        self.pasv_ip = "192.168.1.100"  # Simulated printer IP
        self.pasv_port = 50000

    async def start(self) -> int:
        """Start the mock FTP server and return the port."""
        self._server = await asyncio.start_server(
            self._handle_client,
            self.bind_address,
            0,  # Let OS assign port
            ssl=self.ssl_context,
        )
        # Get assigned port
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self):
        """Stop the mock FTP server."""
        if self._server:
            self._server.close()
            await self._server.wait_closed()

        for _, writer in self._connections:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Handle FTP client connection."""
        self._connections.append((reader, writer))

        try:
            # Send welcome message
            writer.write(b"220 MockFTP Ready\r\n")
            await writer.drain()

            while True:
                try:
                    line = await asyncio.wait_for(reader.readline(), timeout=10.0)
                except TimeoutError:
                    break

                if not line:
                    break

                cmd = line.decode("utf-8", errors="replace").strip()
                self.commands_received.append(cmd)

                # Handle commands
                response = await self._handle_command(cmd)
                if response:
                    writer.write(response.encode("utf-8"))
                    await writer.drain()

                if cmd.upper() == "QUIT":
                    break

        except Exception:
            pass
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _handle_command(self, cmd: str) -> str:
        """Generate response for FTP command."""
        cmd_upper = cmd.upper()

        if cmd_upper.startswith("USER "):
            return "331 User name okay, need password\r\n"
        elif cmd_upper.startswith("PASS "):
            return "230 User logged in\r\n"
        elif cmd_upper == "SYST":
            return "215 UNIX Type: L8\r\n"
        elif cmd_upper == "FEAT":
            return "211-Features:\r\n PASV\r\n UTF8\r\n211 End\r\n"
        elif cmd_upper == "PWD":
            return '257 "/" is current directory\r\n'
        elif cmd_upper == "TYPE I":
            return "200 Type set to I\r\n"
        elif cmd_upper == "PASV":
            # Return PASV response with simulated printer IP
            h = self.pasv_ip.split(".")
            p1 = self.pasv_port // 256
            p2 = self.pasv_port % 256
            return f"227 Entering Passive Mode ({h[0]},{h[1]},{h[2]},{h[3]},{p1},{p2})\r\n"
        elif cmd_upper == "EPSV":
            # Extended passive mode
            return f"229 Entering Extended Passive Mode (|||{self.pasv_port}|)\r\n"
        elif cmd_upper == "QUIT":
            return "221 Goodbye\r\n"
        elif cmd_upper.startswith("CWD "):
            return "250 Directory changed\r\n"
        elif cmd_upper.startswith("STOR "):
            return "150 Opening data connection\r\n"
        else:
            return f"502 Command not implemented: {cmd}\r\n"


@pytest.fixture
async def mock_ftp_server(server_ssl_context):
    """Create and start a mock FTP server."""
    server = MockFTPServer(server_ssl_context)
    port = await server.start()
    yield server, port
    await server.stop()
