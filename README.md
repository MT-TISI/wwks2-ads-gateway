# WWKS2 ADS Gateway

A Python-based integration service that acts as a bridge between a WWKS2 client (connecting via WebSockets) and a TwinCAT PLC (connecting via ADS). It also features a FastAPI dashboard for system monitoring and control.

## Features

- **WebSocket Server**: Listens for WWKS2 XML messages over a WebSocket connection.
- **TwinCAT ADS Integration**: Uses `pyads` to communicate directly with a TwinCAT PLC, setting boolean variables based on parsed WWKS2 signals (`Ready`, `Completed`, etc.).
- **Web Dashboard**: Provides a simple HTML UI via **FastAPI** to view the current system state and start/stop the WebSocket server.
- **Configurable**: Settings can be easily overridden using a `config.toml` file.

## Requirements

- Python 3.11+ (recommended for built-in `tomllib`, otherwise `tomli` is required)
- `pyads`
- `fastapi`
- `uvicorn`
- `websockets`

To install the required dependencies:

```bash
pip install fastapi uvicorn pyads websockets
```

*(If on Python < 3.11, also install `tomli`)*  