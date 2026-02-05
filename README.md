# PandaProxy

BambuLab Camera Fan-Out Proxy - Proxy camera streams from BambuLab printers to multiple clients.

## Overview

BambuLab printers in LAN Mode with Development Mode enabled expose camera streams via:
- **RTSPS** on port 322 (RTSP over TLS) - X1, X1C, X1E, H2C, H2D, H2D Pro, H2S, P2S
- **Chamber Image** on port 6000 (custom binary protocol over TLS) - A1, A1 Mini, P1P, P1S

These streams have limited simultaneous connection support. PandaProxy acts as a transparent proxy that:
1. **Automatically detects** the camera protocol used by your printer
2. Maintains a **single connection** to the printer's camera
3. Serves the stream to **multiple clients** using the same protocol
4. Clients connect to PandaProxy as if they were connecting directly to the printer

## Features

- **Automatic camera type detection** - no manual configuration needed
- Chamber image proxy (port 6000) for A1/P1 printers with TLS and binary protocol
- RTSP proxy (port 322) for X1/H2/P2 printers using FFmpeg + MediaMTX
- Same authentication (access code) as the printer
- Automatic reconnection on connection loss
- Docker support with Alpine-based image

## Requirements

### For Local CLI Usage

- Python 3.14+
- OpenSSL (for chamber image proxy TLS certificate generation)
- FFmpeg (for RTSP proxy only)
- MediaMTX (for RTSP proxy only) - [Download from GitHub](https://github.com/bluenviron/mediamtx/releases)

### For Docker

- Docker & Docker Compose
- All dependencies are included in the image

## Installation

### Using uv (recommended)

```bash
# Clone the repository
git clone https://github.com/karaktaka/pandaproxy.git
cd pandaproxy

# Install with uv
uv sync

# Run
uv run pandaproxy --help
```

### Using pip

```bash
pip install .
pandaproxy --help
```

## Usage

### CLI

```bash
# Basic usage - camera type is automatically detected
pandaproxy --printer-ip 192.168.1.100 --access-code 12345678


# Verbose logging
pandaproxy --printer-ip 192.168.1.100 --access-code 12345678 -v
```

### Environment Variables

All options can be set via environment variables:

```bash
export PRINTER_IP=192.168.1.100
export ACCESS_CODE=12345678
export BIND_ADDRESS=0.0.0.0

pandaproxy
```

### Docker

```bash
# Copy example env file
cp .env.example .env

# Edit with your printer details
nano .env

# Run with Docker Compose
docker compose up -d

# View logs
docker compose logs -f
```

Or run directly:

```bash
docker run -d \
  -e PRINTER_IP=192.168.1.100 \
  -e ACCESS_CODE=12345678 \
  -p 6000:6000 \
  -p 322:322 \
  pandaproxy:latest
```

## Connecting Clients

Once PandaProxy is running, connect your clients to the proxy instead of the printer:

### Chamber Image (A1/P1 printers)

Clients connect to `<proxy-ip>:6000` using TLS with the same binary authentication protocol.
This is typically used by BambuLab apps and compatible third-party software.

### RTSP (X1/H2/P2 printers)

```
rtsp://bblp:<access_code>@<proxy-ip>:322/stream
```

Example with VLC:
```bash
vlc rtsp://bblp:12345678@192.168.1.50:322/stream
```

## Architecture

```
┌─────────────┐     Single      ┌──────────────┐     Multiple     ┌─────────┐
│  BambuLab   │◄───Connection───│  PandaProxy  │◄───Connections───│ Clients │
│   Printer   │                 │              │                  │         │
└─────────────┘                 └──────────────┘                  └─────────┘
    :322 RTSPS                      :322 RTSP      (X1/H2/P2)
    :6000 TLS                       :6000 TLS      (A1/P1)
```

### How It Works

1. PandaProxy probes both ports (322 and 6000) to detect the camera type
2. Based on detection, it starts the appropriate proxy:
   - **Chamber Image Proxy**: Pure Python with asyncio, TLS, binary protocol auth, fan-out broadcasting
   - **RTSP Proxy**: FFmpeg pulls RTSPS from printer, pushes to MediaMTX which serves clients

## Printer Model Support

| Model | Protocol | Port | Proxy |
|-------|----------|------|-------|
| X1, X1C, X1E | RTSPS | 322 | RTSP Proxy |
| H2C, H2D, H2D Pro, H2S | RTSPS | 322 | RTSP Proxy |
| P2S | RTSPS | 322 | RTSP Proxy |
| A1, A1 Mini | Chamber Image | 6000 | Chamber Proxy |
| P1P, P1S | Chamber Image | 6000 | Chamber Proxy |

## Troubleshooting

### Chamber image connection fails

- Verify the printer IP and access code
- Ensure the printer has LAN Mode and Development Mode enabled
- Check if port 6000 is accessible on the printer
- Verify OpenSSL is installed (`openssl version`)

### RTSP connection fails

- Verify FFmpeg and MediaMTX are installed and in PATH
- Check if port 322 is accessible on the printer
- Try running with `-v` for verbose logs

### Privileged ports (322, 6000)

On Linux, binding to ports below 1024 requires root or capabilities:

```bash
# Option 1: Run as root (not recommended)
sudo pandaproxy ...

# Option 2: Use setcap (recommended for production)
sudo setcap 'cap_net_bind_service=+ep' $(which python3)

# Option 3: Use Docker (handles this automatically)
docker compose up -d
```

## License

MIT License
