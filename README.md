# WWKS2 ADS Gateway

A Python-based integration service that acts as a bridge between a WWKS2 endpoint (via WebSockets) and a TwinCAT PLC (via ADS). It also features a FastAPI dashboard for system monitoring and control.

## Features

- **WebSocket Client**: Connects to a WWKS2 endpoint and receives WWKS2 XML messages over a WebSocket connection.
- **TwinCAT ADS Integration**: Uses `pyads` to communicate directly with a TwinCAT PLC, setting boolean variables based on parsed WWKS2 signals (`Ready`, `Completed`, etc.).
- **Web Dashboard**: Provides a simple HTML UI via **FastAPI** to view the current system state and start/stop the WebSocket client.
- **Configurable**: Settings can be easily overridden using a `config.toml` file.

## Create a Virtual Environment

It is recommended to run this project inside a Python virtual environment (`venv`).

Create a virtual environment:

```powershell
python -m venv .venv
```

## Requirements

- Python 3.11+ (recommended for built-in `tomllib`; otherwise `tomli` is required)
- `pyads`
- `fastapi`
- `uvicorn`
- `websockets`