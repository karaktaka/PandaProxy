"""Async broadcast manager for fan-out streaming to multiple clients."""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class StreamClient:
    """Represents a connected client with its own message queue."""

    client_id: str
    queue: asyncio.Queue[bytes | list[bytes] | None] = field(
        default_factory=lambda: asyncio.Queue(maxsize=100)
    )
    connected: bool = True

    async def send(self, data: bytes | list[bytes]) -> bool:
        """Queue data for sending to this client. Returns False if queue is full."""
        if not self.connected:
            return False
        try:
            self.queue.put_nowait(data)
            return True
        except asyncio.QueueFull:
            logger.warning("Client %s queue full, dropping frame", self.client_id)
            return False

    async def receive(self) -> bytes | list[bytes] | None:
        """Get next data from queue. Returns None when disconnected."""
        return await self.queue.get()

    def disconnect(self) -> None:
        """Mark client as disconnected and signal queue."""
        self.connected = False
        with contextlib.suppress(asyncio.QueueFull):
            self.queue.put_nowait(None)


class StreamFanout:
    """Manages broadcasting stream data from one source to multiple clients.

    This class handles:
    - Registering/unregistering clients
    - Broadcasting data to all connected clients
    - Graceful handling of slow clients (drops frames if queue full)
    """

    def __init__(self, name: str = "stream") -> None:
        self.name = name
        self._clients: dict[str, StreamClient] = {}
        self._lock = asyncio.Lock()
        self._client_counter = 0
        self._running = False

    @property
    def client_count(self) -> int:
        """Number of currently connected clients."""
        return len(self._clients)

    @property
    def is_running(self) -> bool:
        """Whether the fanout is actively streaming."""
        return self._running

    def start(self) -> None:
        """Mark fanout as running/streaming."""
        self._running = True
        logger.info("[%s] Fanout started", self.name)

    def stop(self) -> None:
        """Stop fanout and disconnect all clients."""
        self._running = False
        for client in list(self._clients.values()):
            client.disconnect()
        logger.info("[%s] Fanout stopped", self.name)

    async def register_client(self, client_id: str | None = None) -> StreamClient:
        """Register a new client and return its StreamClient instance."""
        async with self._lock:
            if client_id is None:
                self._client_counter += 1
                client_id = f"client_{self._client_counter}"

            client = StreamClient(client_id=client_id)
            self._clients[client_id] = client
            logger.info(
                "[%s] Client %s connected (total: %d)", self.name, client_id, len(self._clients)
            )
            return client

    async def unregister_client(self, client: StreamClient) -> None:
        """Unregister a client."""
        async with self._lock:
            client.disconnect()
            self._clients.pop(client.client_id, None)
            logger.info(
                "[%s] Client %s disconnected (total: %d)",
                self.name,
                client.client_id,
                len(self._clients),
            )

    async def broadcast(self, data: bytes | list[bytes]) -> int:
        """Broadcast data to all connected clients. Returns number of successful sends."""
        if not self._clients:
            return 0

        success_count = 0
        for client in list(self._clients.values()):
            if await client.send(data):
                success_count += 1

        return success_count

    async def iter_clients(self) -> AsyncIterator[StreamClient]:
        """Iterate over all connected clients."""
        async with self._lock:
            for client in list(self._clients.values()):
                yield client
