"""FTP Proxy tests for TCP passthrough functionality.

The FTP proxy is now a simple TCP passthrough that forwards raw bytes
(including TLS) to the printer. This allows clients to establish TLS
sessions directly with the printer, enabling proper session reuse.
"""

import asyncio

import pytest

from pandaproxy.ftp_proxy import FTP_DATA_PORT_END, FTP_DATA_PORT_START, FTPProxy

# Use ephemeral ports for testing (avoids privileged port issues)
TEST_CONTROL_PORT = 19990
TEST_DATA_PORT_START = 19000
TEST_DATA_PORT_END = 19010  # Small range for faster tests


@pytest.fixture
def test_proxy():
    """Create an FTPProxy configured for testing with non-privileged ports."""
    proxy = FTPProxy(
        printer_ip="192.168.1.100",
        bind_address="127.0.0.1",
    )
    # Override ports for testing
    proxy.port = TEST_CONTROL_PORT
    return proxy


class TestFTPProxyLifecycle:
    """Test FTP proxy start/stop lifecycle."""

    @pytest.mark.asyncio
    async def test_proxy_start_stop(self, test_proxy, monkeypatch):
        """Test that proxy starts and stops cleanly."""
        # Monkeypatch the data port range for faster tests
        monkeypatch.setattr("pandaproxy.ftp_proxy.FTP_DATA_PORT_START", TEST_DATA_PORT_START)
        monkeypatch.setattr("pandaproxy.ftp_proxy.FTP_DATA_PORT_END", TEST_DATA_PORT_END)

        proxy = test_proxy

        # Should not be running initially
        assert not proxy._running

        await proxy.start()

        # Should be running after start
        assert proxy._running
        assert proxy._control_server is not None
        assert len(proxy._data_servers) > 0

        await proxy.stop()

        # Should be stopped
        assert not proxy._running

    @pytest.mark.asyncio
    async def test_proxy_double_start(self, test_proxy, monkeypatch):
        """Test that double start is idempotent."""
        monkeypatch.setattr("pandaproxy.ftp_proxy.FTP_DATA_PORT_START", TEST_DATA_PORT_START)
        monkeypatch.setattr("pandaproxy.ftp_proxy.FTP_DATA_PORT_END", TEST_DATA_PORT_END)

        proxy = test_proxy

        await proxy.start()
        server_count = len(proxy._data_servers)

        # Second start should be no-op
        await proxy.start()
        assert len(proxy._data_servers) == server_count

        await proxy.stop()

    @pytest.mark.asyncio
    async def test_proxy_listens_on_correct_ports(self, test_proxy, monkeypatch):
        """Test that proxy listens on control and data ports."""
        monkeypatch.setattr("pandaproxy.ftp_proxy.FTP_DATA_PORT_START", TEST_DATA_PORT_START)
        monkeypatch.setattr("pandaproxy.ftp_proxy.FTP_DATA_PORT_END", TEST_DATA_PORT_END)

        proxy = test_proxy

        await proxy.start()

        try:
            # Control server should be listening on configured port
            assert proxy._control_server is not None
            control_port = proxy._control_server.sockets[0].getsockname()[1]
            assert control_port == TEST_CONTROL_PORT

            # Data servers should be listening on the configured range
            data_ports = [s.sockets[0].getsockname()[1] for s in proxy._data_servers]
            assert len(data_ports) > 0

            # All data ports should be in the expected range
            for port in data_ports:
                assert TEST_DATA_PORT_START <= port <= TEST_DATA_PORT_END

        finally:
            await proxy.stop()


class TestFTPProxyPassthrough:
    """Test TCP passthrough functionality."""

    @pytest.mark.asyncio
    async def test_passthrough_connection_to_mock_server(self, monkeypatch):
        """Test that proxy forwards connections to the backend server."""
        monkeypatch.setattr("pandaproxy.ftp_proxy.FTP_DATA_PORT_START", TEST_DATA_PORT_START)
        monkeypatch.setattr("pandaproxy.ftp_proxy.FTP_DATA_PORT_END", TEST_DATA_PORT_END)

        # Create a simple echo server to act as the "printer"
        received_data = []

        async def echo_handler(reader, writer):
            data = await reader.read(100)
            received_data.append(data)
            writer.write(b"ECHO:" + data)
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        # Start mock server (backend "printer") on an ephemeral port
        mock_server = await asyncio.start_server(echo_handler, "127.0.0.1", 0)
        backend_port = mock_server.sockets[0].getsockname()[1]

        # Create proxy that listens on a different ephemeral port
        # but forwards to the mock server's port
        proxy_listen_server = await asyncio.start_server(lambda _r, _w: None, "127.0.0.1", 0)
        proxy_port = proxy_listen_server.sockets[0].getsockname()[1]
        proxy_listen_server.close()
        await proxy_listen_server.wait_closed()

        # Small delay to ensure port is released
        await asyncio.sleep(0.1)

        # Create proxy pointing to the mock server
        proxy = FTPProxy(
            printer_ip="127.0.0.1",
            bind_address="127.0.0.1",
        )
        # Proxy listens on proxy_port, forwards to backend_port
        proxy.port = proxy_port
        # We need to make the proxy forward to the backend port
        # The proxy forwards to printer_ip:port, so we set printer port via a custom attribute
        original_handle = proxy._handle_connection

        async def patched_handle(reader, writer, _port):
            # Always forward to the backend port
            await original_handle(reader, writer, backend_port)

        proxy._handle_connection = patched_handle

        await proxy.start()

        try:
            # Connect to proxy
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)

            # Send test data
            writer.write(b"Hello FTP")
            await writer.drain()

            # Should receive echoed response from mock server through proxy
            response = await asyncio.wait_for(reader.read(100), timeout=2.0)
            assert response == b"ECHO:Hello FTP"
            assert received_data == [b"Hello FTP"]

            writer.close()
            await writer.wait_closed()

        finally:
            await proxy.stop()
            mock_server.close()
            await mock_server.wait_closed()

    @pytest.mark.asyncio
    async def test_proxy_handles_connection_refused(self, monkeypatch):
        """Test that proxy handles connection refused gracefully."""
        monkeypatch.setattr("pandaproxy.ftp_proxy.FTP_DATA_PORT_START", TEST_DATA_PORT_START)
        monkeypatch.setattr("pandaproxy.ftp_proxy.FTP_DATA_PORT_END", TEST_DATA_PORT_END)

        proxy = FTPProxy(
            printer_ip="127.0.0.1",
            bind_address="127.0.0.1",
        )

        # Use ephemeral port for control, but backend points to a closed port
        proxy.port = TEST_CONTROL_PORT

        await proxy.start()

        try:
            # Temporarily point printer to a port that's not listening
            original_ip = proxy.printer_ip
            proxy.printer_ip = "127.0.0.1"

            # Try to connect to the proxy
            reader, writer = await asyncio.open_connection("127.0.0.1", TEST_CONTROL_PORT)

            # Connection to proxy succeeds, but upstream should fail (no server on TEST_CONTROL_PORT on printer side)
            # The proxy should close the connection gracefully
            try:
                data = await asyncio.wait_for(reader.read(100), timeout=2.0)
                # Connection should be closed by proxy (empty read)
                assert data == b""
            except (ConnectionResetError, TimeoutError):
                # Also acceptable - connection was terminated
                pass

            writer.close()
            await writer.wait_closed()
            proxy.printer_ip = original_ip

        finally:
            await proxy.stop()


class TestFTPProxyDataPorts:
    """Test data port handling."""

    def test_data_port_range_constants(self):
        """Test that data port range constants are sensible."""
        assert FTP_DATA_PORT_START < FTP_DATA_PORT_END
        assert FTP_DATA_PORT_START >= 1024  # Not privileged
        assert FTP_DATA_PORT_END <= 65535  # Valid port range

        # Range should cover typical BambuLab PASV ports (around 2024-2025)
        assert FTP_DATA_PORT_START <= 2024
        assert FTP_DATA_PORT_END >= 2025

    @pytest.mark.asyncio
    async def test_data_port_servers_created(self, test_proxy, monkeypatch):
        """Test that data port servers are created during startup."""
        monkeypatch.setattr("pandaproxy.ftp_proxy.FTP_DATA_PORT_START", TEST_DATA_PORT_START)
        monkeypatch.setattr("pandaproxy.ftp_proxy.FTP_DATA_PORT_END", TEST_DATA_PORT_END)

        proxy = test_proxy

        await proxy.start()

        try:
            # Should have created servers for the data port range
            # Some ports might fail if already in use, but most should succeed
            expected_count = TEST_DATA_PORT_END - TEST_DATA_PORT_START + 1
            assert len(proxy._data_servers) > 0
            assert len(proxy._data_servers) <= expected_count

        finally:
            await proxy.stop()


class TestFTPProxyEdgeCases:
    """Test edge cases and error handling."""

    @pytest.mark.asyncio
    async def test_stop_without_start(self, test_proxy):
        """Test that stop works even if never started."""
        proxy = test_proxy

        # Should not raise
        await proxy.stop()

    @pytest.mark.asyncio
    async def test_multiple_stop_calls(self, test_proxy, monkeypatch):
        """Test that multiple stop calls are safe."""
        monkeypatch.setattr("pandaproxy.ftp_proxy.FTP_DATA_PORT_START", TEST_DATA_PORT_START)
        monkeypatch.setattr("pandaproxy.ftp_proxy.FTP_DATA_PORT_END", TEST_DATA_PORT_END)

        proxy = test_proxy

        await proxy.start()
        await proxy.stop()

        # Second stop should be safe
        await proxy.stop()

    def test_proxy_configuration(self):
        """Test proxy configuration is stored correctly."""
        proxy = FTPProxy(
            printer_ip="192.168.1.100",
            bind_address="10.0.0.1",
        )

        assert proxy.printer_ip == "192.168.1.100"
        assert proxy.bind_address == "10.0.0.1"
        assert proxy.port == 990  # Default FTP port
