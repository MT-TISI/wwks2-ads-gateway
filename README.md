# WWKS2 ADS Gateway

A Python-based integration service that acts as a bridge between a WWKS2 endpoint (via WebSockets) and a TwinCAT PLC (via ADS). It also features a FastAPI dashboard for system monitoring and control.

## Features

- **WebSocket Client**: Connects to a WWKS2 endpoint and receives WWKS2 XML messages over a WebSocket connection.
- **TwinCAT ADS Integration**: Uses `pyads` to communicate directly with a TwinCAT PLC, setting boolean variables based on parsed WWKS2 signals (`Ready`, `Completed`, etc.).
- **Web Dashboard**: Provides a simple HTML UI via **FastAPI** to view the current system state and start/stop the WebSocket client.
- **Configurable**: Settings can be easily overridden using a `config.toml` file.

## Setup and Installation

### 1. Create a Virtual Environment

It is recommended to run this project inside a Python virtual environment (`venv`).

```powershell
python -m venv .venv
```

### 2. Activate the Virtual Environment

```powershell
.\.venv\Scripts\Activate.ps1
```

### 3. Install Dependencies

Install the required packages using the `requirements.txt` file:

```powershell
pip install -r requirements.txt
```

## Configuration

Settings can be overridden by creating a `config.toml` file in the project directory. If no config file is found, the service will use hardcoded defaults.

## Running the Service

Once dependencies are installed, you can start the service by running:

```powershell
python service.py
```

The web dashboard will be available at `http://localhost:8080`.

## Building for Production (EXE)

To create a standalone executable for Windows, you can use PyInstaller.

### 1. Install PyInstaller

If not already installed via `requirements.txt`:
```powershell
pip install pyinstaller
```

### 2. Run the Build

Use the provided `.spec` file to create the build:
```powershell
pyinstaller wwks2-ads-gateway.spec
```

The resulting executable will be located in the `dist/` directory.
