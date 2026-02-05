"""CLI entry point for PandaProxy."""

import asyncio
import logging
import os
import shutil
import signal
from typing import Annotated

import typer

from pandaproxy.chamber_proxy import ChamberImageProxy
from pandaproxy.detection import detect_camera_type
from pandaproxy.rtsp_proxy import RTSPProxy

app = typer.Typer(
    name="PandaProxy",
    help="BambuLab Camera Fan-Out Proxy - Proxy camera streams from BambuLab printers to multiple clients.",
    add_completion=False,
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def check_dependencies(camera_type: str) -> tuple[bool, list[str]]:
    """Check for required external dependencies based on camera type."""
    missing = []

    if camera_type == "rtsp":
        if not shutil.which("ffmpeg"):
            missing.append("ffmpeg")

        if not shutil.which("mediamtx"):
            missing.append("mediamtx")

    if camera_type == "chamber" and not shutil.which("openssl"):
        missing.append("openssl")

    return len(missing) == 0, missing


async def run_proxy(
    printer_ip: str,
    access_code: str,
    bind: str,
    camera_type: str,
) -> None:
    """Run the camera proxy server based on detected camera type."""
    chamber_proxy: ChamberImageProxy | None = None
    rtsp_proxy: RTSPProxy | None = None

    # Setup signal handlers for graceful shutdown
    stop_event = asyncio.Event()

    def signal_handler() -> None:
        logger.info("Shutdown signal received")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # noinspection PyTypeChecker
        loop.add_signal_handler(sig, signal_handler)

    try:
        if camera_type == "chamber":
            # Start Chamber Image proxy (A1/P1 printers)
            chamber_proxy = ChamberImageProxy(
                printer_ip=printer_ip,
                access_code=access_code,
                bind_address=bind,
            )
            await chamber_proxy.start()

            typer.echo("\n" + "=" * 60)
            typer.echo("PandaProxy is running!")
            typer.echo("=" * 60)
            typer.echo(f"Printer: {printer_ip}")
            typer.echo("Camera Type: Chamber Image (A1/P1 series)")
            typer.echo(f"Proxy: {bind}:6000 (TLS)")
            typer.echo("=" * 60)
            typer.echo("Press Ctrl+C to stop\n")

        elif camera_type == "rtsp":
            # Start RTSP proxy (X1/H2/P2 printers)
            rtsp_proxy = RTSPProxy(
                printer_ip=printer_ip,
                access_code=access_code,
                bind_address=bind,
            )
            await rtsp_proxy.start()

            typer.echo("\n" + "=" * 60)
            typer.echo("PandaProxy is running!")
            typer.echo("=" * 60)
            typer.echo(f"Printer: {printer_ip}")
            typer.echo("Camera Type: RTSP (X1/H2/P2 series)")
            typer.echo(f"Proxy: rtsp://bblp:<access_code>@{bind}:322/stream")
            typer.echo("=" * 60)
            if not (os.path.exists("/.dockerenv") or os.environ.get("RUNNING_IN_DOCKER")):
                typer.echo("Press Ctrl+C to stop\n")

        # Wait for shutdown signal
        await stop_event.wait()

    finally:
        logger.info("Shutting down...")

        if chamber_proxy:
            await chamber_proxy.stop()

        if rtsp_proxy:
            await rtsp_proxy.stop()

        logger.info("Shutdown complete")


@app.command()
def main(
    printer_ip: Annotated[
        str,
        typer.Option(
            "--printer-ip",
            "-p",
            help="IP address of the BambuLab printer",
            envvar="PRINTER_IP",
        ),
    ],
    access_code: Annotated[
        str,
        typer.Option(
            "--access-code",
            "-a",
            help="Access code for the printer (found in printer settings)",
            envvar="ACCESS_CODE",
        ),
    ],
    bind: Annotated[
        str,
        typer.Option(
            "--bind",
            "-b",
            help="Address to bind the proxy servers to",
            envvar="BIND_ADDRESS",
        ),
    ] = "0.0.0.0",
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            "-v",
            help="Enable verbose/debug logging",
        ),
    ] = False,
) -> None:
    """Start the BambuLab camera fan-out proxy.

    This proxy connects to your BambuLab printer's camera stream and serves
    it to multiple clients, preventing connection limit issues.

    The camera type is automatically detected:
    - Chamber Image (port 6000): A1, A1 Mini, P1P, P1S
    - RTSP (port 322): X1, X1C, X1E, H2C, H2D, H2D Pro, H2S, P2S

    Example:
        pandaproxy --printer-ip 192.168.1.100 --access-code 12345678
    """
    # Set log level
    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    typer.echo(f"Connecting to printer at {printer_ip}...")

    # Detect camera type
    try:
        camera_type = asyncio.run(detect_camera_type(printer_ip, access_code))
    except RuntimeError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1) from None

    typer.echo(f"Detected camera type: {camera_type.upper()}")

    # Check dependencies for detected camera type
    dependencies_satisfied, dependencies_missing = check_dependencies(camera_type)
    if not dependencies_satisfied:
        typer.echo("Error: Missing required dependencies:", err=True)
        for dep in dependencies_missing:
            if dep == "ffmpeg":
                typer.echo("  - ffmpeg: Install via your package manager", err=True)
                typer.echo("      Linux: apt install ffmpeg / pacman -S ffmpeg", err=True)
                typer.echo("      macOS: brew install ffmpeg", err=True)
            elif dep == "mediamtx":
                typer.echo(
                    "  - mediamtx: Download from https://github.com/bluenviron/mediamtx/releases",
                    err=True,
                )
            elif dep == "openssl":
                typer.echo("  - openssl: Install via your package manager", err=True)
                typer.echo("      Linux: apt install openssl / pacman -S openssl", err=True)
                typer.echo("      macOS: brew install openssl", err=True)
        raise typer.Exit(1)

    typer.echo("Starting PandaProxy proxy...")

    # Run the async proxy
    asyncio.run(
        run_proxy(
            printer_ip=printer_ip,
            access_code=access_code,
            bind=bind,
            camera_type=camera_type,
        )
    )


if __name__ == "__main__":
    app()
