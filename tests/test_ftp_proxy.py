"""FTP Proxy tests for debugging client compatibility issues.

These tests help diagnose FTP issues with various clients like OctoApp and Bambuddy.
They test:
- PASV response rewriting
- EPSV command blocking
- SSL session handling
- Connection timing and behavior
"""

import asyncio
import re
import ssl

import pytest


class TestFTPCommands:
    """Test FTP command handling and rewriting."""

    def test_pasv_response_parsing(self):
        """Test that PASV responses are correctly parsed."""
        # Standard PASV response format
        # Port encoding: 195 * 256 + 80 = 50000
        response = "227 Entering Passive Mode (192,168,1,100,195,80)"

        match = re.search(r"227 .*\((\d+),(\d+),(\d+),(\d+),(\d+),(\d+)\)", response)
        assert match is not None

        h1, h2, h3, h4, p1, p2 = map(int, match.groups())
        ip = f"{h1}.{h2}.{h3}.{h4}"
        port = p1 * 256 + p2

        assert ip == "192.168.1.100"
        assert port == 50000  # 195 * 256 + 80 = 50000

    def test_pasv_response_rewriting(self):
        """Test PASV response rewriting for proxy."""
        original = "227 Entering Passive Mode (192,168,1,100,195,88)"
        proxy_ip = "10.0.0.1"
        proxy_port = 60000

        # Parse original
        match = re.search(r"227 .*\((\d+),(\d+),(\d+),(\d+),(\d+),(\d+)\)", original)
        assert match is not None

        # Build new response
        ip_parts = proxy_ip.split(".")
        p1_new = proxy_port // 256
        p2_new = proxy_port % 256
        new_args = f"{ip_parts[0]},{ip_parts[1]},{ip_parts[2]},{ip_parts[3]},{p1_new},{p2_new}"

        prefix = original[: original.find("(") + 1]
        suffix = original[original.find(")") :]
        new_resp = f"{prefix}{new_args}{suffix}"

        assert "10,0,0,1" in new_resp
        assert "234,96" in new_resp  # 60000 = 234 * 256 + 96

    def test_epsv_blocking_response(self):
        """Test that EPSV blocking returns correct FTP error code."""
        # 502 = Command not implemented
        response = "502 Command not implemented\r\n"
        assert response.startswith("502")


class TestSSLSessionReuse:
    """Test SSL session reuse behavior for FTP data connections."""

    @pytest.mark.asyncio
    async def test_ssl_context_session_extraction(self, temp_certs, client_ssl_context):
        """Test that SSL sessions can be extracted from connections."""
        cert_path, key_path = temp_certs

        # Create server
        server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_ctx.load_cert_chain(cert_path, key_path)

        sessions_received = []

        async def handle_client(_reader, writer):
            ssl_obj = writer.get_extra_info("ssl_object")
            if ssl_obj:
                sessions_received.append(ssl_obj.session)
            writer.write(b"OK\n")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(handle_client, "127.0.0.1", 0, ssl=server_ctx)
        port = server.sockets[0].getsockname()[1]

        try:
            # Connect and extract session
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", port, ssl=client_ssl_context
            )
            ssl_obj = writer.get_extra_info("ssl_object")
            session = ssl_obj.session if ssl_obj else None

            await reader.readline()
            writer.close()
            await writer.wait_closed()

            # Session should exist
            assert session is not None
            assert len(sessions_received) == 1
        finally:
            server.close()
            await server.wait_closed()


class TestMockFTPServer:
    """Test the mock FTP server itself to ensure test infrastructure works."""

    @pytest.mark.asyncio
    async def test_mock_server_basic_commands(self, mock_ftp_server, client_ssl_context):
        """Test basic FTP command flow against mock server."""
        server, port = mock_ftp_server

        reader, writer = await asyncio.open_connection("127.0.0.1", port, ssl=client_ssl_context)

        try:
            # Read welcome
            welcome = await asyncio.wait_for(reader.readline(), timeout=5.0)
            assert b"220" in welcome

            # USER command
            writer.write(b"USER bblp\r\n")
            await writer.drain()
            response = await asyncio.wait_for(reader.readline(), timeout=5.0)
            assert b"331" in response

            # PASS command
            writer.write(b"PASS testcode\r\n")
            await writer.drain()
            response = await asyncio.wait_for(reader.readline(), timeout=5.0)
            assert b"230" in response

            # PASV command
            writer.write(b"PASV\r\n")
            await writer.drain()
            response = await asyncio.wait_for(reader.readline(), timeout=5.0)
            assert b"227" in response
            assert b"192,168,1,100" in response  # Mock printer IP

            # QUIT
            writer.write(b"QUIT\r\n")
            await writer.drain()
            response = await asyncio.wait_for(reader.readline(), timeout=5.0)
            assert b"221" in response

        finally:
            writer.close()
            await writer.wait_closed()

        # Verify commands were recorded
        assert "USER bblp" in server.commands_received
        assert "PASV" in server.commands_received

    @pytest.mark.asyncio
    async def test_mock_server_epsv_command(self, mock_ftp_server, client_ssl_context):
        """Test EPSV command handling."""
        server, port = mock_ftp_server

        reader, writer = await asyncio.open_connection("127.0.0.1", port, ssl=client_ssl_context)

        try:
            # Read welcome
            await reader.readline()

            # Login
            writer.write(b"USER bblp\r\n")
            await writer.drain()
            await reader.readline()

            writer.write(b"PASS test\r\n")
            await writer.drain()
            await reader.readline()

            # EPSV command (extended passive)
            writer.write(b"EPSV\r\n")
            await writer.drain()
            response = await asyncio.wait_for(reader.readline(), timeout=5.0)

            # Mock server returns 229 for EPSV
            assert b"229" in response

        finally:
            writer.close()
            await writer.wait_closed()


class TestFTPProxyIntegration:
    """Integration tests for FTP proxy with mock backend.

    These tests verify the proxy correctly handles client connections
    and rewrites PASV responses.

    Note: These tests require careful port management since FTPProxy uses
    the same port for listening and upstream connections by default.
    We also need to patch the SSL context to accept our test certificates.
    """

    @pytest.mark.asyncio
    async def test_proxy_pasv_rewrite(self, mock_ftp_server, temp_certs, client_ssl_context):
        """Test that proxy correctly rewrites PASV responses."""
        from pandaproxy.ftp_proxy import FTPProxy

        server, server_port = mock_ftp_server
        cert_path, key_path = temp_certs

        # Update mock server to use localhost for connection
        server.pasv_ip = "127.0.0.1"

        # Create proxy
        proxy = FTPProxy(
            printer_ip="127.0.0.1",
            access_code="testcode",
            cert_path=cert_path,
            key_path=key_path,
            bind_address="127.0.0.1",
        )

        # Set up: proxy listens on ephemeral port
        proxy.port = 0

        try:
            await proxy.start()
            proxy_port = proxy._server.sockets[0].getsockname()[1]

            # Patch the upstream SSL context to accept our test cert
            # and set upstream port to mock server
            proxy._ssl_context.check_hostname = False
            proxy._ssl_context.verify_mode = ssl.CERT_NONE
            proxy.port = server_port

            # Connect to proxy
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", proxy_port, ssl=client_ssl_context
            )

            try:
                # Read welcome (forwarded from mock server)
                welcome = await asyncio.wait_for(reader.readline(), timeout=5.0)
                assert b"220" in welcome

                # Login
                writer.write(b"USER bblp\r\n")
                await writer.drain()
                await asyncio.wait_for(reader.readline(), timeout=5.0)

                writer.write(b"PASS testcode\r\n")
                await writer.drain()
                await asyncio.wait_for(reader.readline(), timeout=5.0)

                # PASV - should be rewritten by proxy
                writer.write(b"PASV\r\n")
                await writer.drain()
                response = await asyncio.wait_for(reader.readline(), timeout=5.0)
                response_str = response.decode("utf-8")

                # Response should contain 227 and have proxy's IP, not mock server's IP
                assert "227" in response_str
                # The IP should be rewritten to proxy's IP (127.0.0.1 -> 127,0,0,1)
                assert "127,0,0,1" in response_str

            finally:
                writer.close()
                await writer.wait_closed()

        finally:
            await proxy.stop()

    @pytest.mark.asyncio
    async def test_proxy_epsv_blocking(self, mock_ftp_server, temp_certs, client_ssl_context):
        """Test that proxy blocks EPSV commands and returns 502."""
        from pandaproxy.ftp_proxy import FTPProxy

        server, server_port = mock_ftp_server
        cert_path, key_path = temp_certs

        proxy = FTPProxy(
            printer_ip="127.0.0.1",
            access_code="testcode",
            cert_path=cert_path,
            key_path=key_path,
            bind_address="127.0.0.1",
        )

        # Listen on ephemeral port
        proxy.port = 0

        try:
            await proxy.start()
            proxy_port = proxy._server.sockets[0].getsockname()[1]

            # Patch SSL and set upstream port
            proxy._ssl_context.check_hostname = False
            proxy._ssl_context.verify_mode = ssl.CERT_NONE
            proxy.port = server_port

            reader, writer = await asyncio.open_connection(
                "127.0.0.1", proxy_port, ssl=client_ssl_context
            )

            try:
                # Read welcome
                await reader.readline()

                # Login
                writer.write(b"USER bblp\r\n")
                await writer.drain()
                await reader.readline()

                writer.write(b"PASS testcode\r\n")
                await writer.drain()
                await reader.readline()

                # EPSV should be blocked by proxy
                writer.write(b"EPSV\r\n")
                await writer.drain()
                response = await asyncio.wait_for(reader.readline(), timeout=5.0)
                response_str = response.decode("utf-8")

                # Proxy should return 502 (not implemented)
                assert "502" in response_str

                # Verify EPSV was NOT forwarded to mock server
                assert "EPSV" not in server.commands_received

            finally:
                writer.close()
                await writer.wait_closed()

        finally:
            await proxy.stop()


class TestOctoAppBehavior:
    """Tests specifically designed to reproduce OctoApp FTP behavior.

    Based on research:
    - OctoApp may try EPSV before PASV
    - May send commands in rapid succession
    - May have specific SSL/TLS requirements
    """

    @pytest.mark.asyncio
    async def test_epsv_fallback_to_pasv(self, mock_ftp_server, temp_certs, client_ssl_context):
        """Test client behavior when EPSV is blocked and must fallback to PASV.

        This simulates what should happen when OctoApp tries EPSV first
        and the proxy blocks it with 502.
        """
        from pandaproxy.ftp_proxy import FTPProxy

        server, server_port = mock_ftp_server
        cert_path, key_path = temp_certs
        server.pasv_ip = "127.0.0.1"

        proxy = FTPProxy(
            printer_ip="127.0.0.1",
            access_code="testcode",
            cert_path=cert_path,
            key_path=key_path,
            bind_address="127.0.0.1",
        )
        proxy.port = 0

        try:
            await proxy.start()
            proxy_port = proxy._server.sockets[0].getsockname()[1]
            proxy._ssl_context.check_hostname = False
            proxy._ssl_context.verify_mode = ssl.CERT_NONE
            proxy.port = server_port

            reader, writer = await asyncio.open_connection(
                "127.0.0.1", proxy_port, ssl=client_ssl_context
            )

            try:
                # Read welcome
                await reader.readline()

                # Login
                writer.write(b"USER bblp\r\n")
                await writer.drain()
                await reader.readline()

                writer.write(b"PASS testcode\r\n")
                await writer.drain()
                await reader.readline()

                # OctoApp behavior: Try EPSV first
                writer.write(b"EPSV\r\n")
                await writer.drain()
                epsv_response = await asyncio.wait_for(reader.readline(), timeout=5.0)

                # Should get 502 (not implemented)
                assert b"502" in epsv_response

                # Then fallback to PASV
                writer.write(b"PASV\r\n")
                await writer.drain()
                pasv_response = await asyncio.wait_for(reader.readline(), timeout=5.0)

                # PASV should work
                assert b"227" in pasv_response

            finally:
                writer.close()
                await writer.wait_closed()

        finally:
            await proxy.stop()

    @pytest.mark.asyncio
    async def test_rapid_login_sequence(self, mock_ftp_server, temp_certs, client_ssl_context):
        """Test rapid command sequence during login.

        Some clients send USER and PASS very quickly, potentially before
        the proxy has finished processing.
        """
        from pandaproxy.ftp_proxy import FTPProxy

        server, server_port = mock_ftp_server
        cert_path, key_path = temp_certs

        proxy = FTPProxy(
            printer_ip="127.0.0.1",
            access_code="testcode",
            cert_path=cert_path,
            key_path=key_path,
            bind_address="127.0.0.1",
        )
        proxy.port = 0

        try:
            await proxy.start()
            proxy_port = proxy._server.sockets[0].getsockname()[1]
            proxy._ssl_context.check_hostname = False
            proxy._ssl_context.verify_mode = ssl.CERT_NONE
            proxy.port = server_port

            reader, writer = await asyncio.open_connection(
                "127.0.0.1", proxy_port, ssl=client_ssl_context
            )

            try:
                # Read welcome
                await reader.readline()

                # Send USER and PASS rapidly without waiting for responses
                writer.write(b"USER bblp\r\n")
                writer.write(b"PASS testcode\r\n")
                await writer.drain()

                # Now read both responses
                user_resp = await asyncio.wait_for(reader.readline(), timeout=5.0)
                pass_resp = await asyncio.wait_for(reader.readline(), timeout=5.0)

                # Both should succeed
                assert b"331" in user_resp or b"230" in user_resp
                assert b"230" in pass_resp

            finally:
                writer.close()
                await writer.wait_closed()

        finally:
            await proxy.stop()


class TestClientBehaviorSimulation:
    """Simulate different client behaviors to debug compatibility issues.

    These tests help identify what might be causing issues with specific clients
    like OctoApp and Bambuddy.
    """

    @pytest.mark.asyncio
    async def test_rapid_command_sequence(self, mock_ftp_server, client_ssl_context):
        """Test rapid command sequence (some clients send commands quickly)."""
        server, port = mock_ftp_server

        reader, writer = await asyncio.open_connection("127.0.0.1", port, ssl=client_ssl_context)

        try:
            # Read welcome
            await reader.readline()

            # Send multiple commands rapidly without waiting for responses
            commands = [
                b"USER bblp\r\n",
                b"PASS testcode\r\n",
                b"TYPE I\r\n",
                b"PWD\r\n",
            ]

            for cmd in commands:
                writer.write(cmd)
            await writer.drain()

            # Now read all responses
            responses = []
            for _ in range(len(commands)):
                resp = await asyncio.wait_for(reader.readline(), timeout=5.0)
                responses.append(resp)

            # All commands should have responses
            assert len(responses) == len(commands)

        finally:
            writer.close()
            await writer.wait_closed()

    @pytest.mark.asyncio
    async def test_connection_timeout_behavior(self, temp_certs, client_ssl_context):
        """Test behavior when server doesn't respond quickly."""
        cert_path, key_path = temp_certs

        server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_ctx.load_cert_chain(cert_path, key_path)

        async def slow_handler(_reader, writer):
            # Simulate slow server - just wait
            await asyncio.sleep(10)
            writer.close()

        server = await asyncio.start_server(slow_handler, "127.0.0.1", 0, ssl=server_ctx)
        port = server.sockets[0].getsockname()[1]

        try:
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", port, ssl=client_ssl_context
            )

            # Try to read with short timeout - should timeout
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(reader.readline(), timeout=1.0)

            writer.close()
            await writer.wait_closed()
        finally:
            server.close()
            await server.wait_closed()

    @pytest.mark.asyncio
    async def test_partial_command_handling(self, mock_ftp_server, client_ssl_context):
        """Test handling of commands sent in chunks (network fragmentation)."""
        server, port = mock_ftp_server

        reader, writer = await asyncio.open_connection("127.0.0.1", port, ssl=client_ssl_context)

        try:
            # Read welcome
            await reader.readline()

            # Send USER command in chunks
            writer.write(b"US")
            await writer.drain()
            await asyncio.sleep(0.1)
            writer.write(b"ER ")
            await writer.drain()
            await asyncio.sleep(0.1)
            writer.write(b"bblp\r\n")
            await writer.drain()

            # Should still get proper response
            response = await asyncio.wait_for(reader.readline(), timeout=5.0)
            assert b"331" in response

        finally:
            writer.close()
            await writer.wait_closed()
