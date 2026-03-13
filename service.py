import asyncio
import logging
import os
import sys
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from typing import Optional

import pyads
import uvicorn
import websockets
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

try:
    import tomllib
except ModuleNotFoundError:
    try:
        import tomli as tomllib
    except ModuleNotFoundError:
        tomllib = None

# ------------------------------------------------------------------------------
# LOGGING
# ------------------------------------------------------------------------------

logger = logging.getLogger("wwks2")
logger.setLevel(logging.INFO)

if not logger.handlers:
    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter("%(asctime)s  %(levelname)s  %(message)s"))
    logger.addHandler(ch)

logger.propagate = False

# ------------------------------------------------------------------------------
# DEFAULT CONFIG
# ------------------------------------------------------------------------------

WWKS2_LISTEN_IP = "0.0.0.0"
WWKS2_LISTEN_PORT = 6050
PULSE_TIME = 0.1

ADS_AMS_NET_ID = "127.0.0.1.1.1"
ADS_PORT = 48898

PLC_VAR_ROBOT_ACTIVE = "MAIN.RobotActive"
PLC_VAR_DELIVERY_OK = "MAIN.DeliveryOK"
PLC_VAR_DELIVERY_ERROR = "MAIN.DeliveryError"

def get_base_dir() -> str:
    """Determine path for config file.
    When built with PyInstaller, config should be next to the .exe file.
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def load_config():
    global WWKS2_LISTEN_IP
    global WWKS2_LISTEN_PORT
    global PULSE_TIME
    global ADS_AMS_NET_ID
    global ADS_PORT
    global PLC_VAR_ROBOT_ACTIVE
    global PLC_VAR_DELIVERY_OK
    global PLC_VAR_DELIVERY_ERROR

    config_file = os.path.join(get_base_dir(), "config.toml")

    if not os.path.exists(config_file):
        logger.info(f"No config file found at {config_file}, using defaults")
        return

    if tomllib is None:
        logger.warning(
            f"{config_file} found but no TOML parser is available "
            f"(requires Python 3.11+ or package 'tomli'). Using defaults."
        )
        return

    try:
        with open(config_file, "rb") as f:
            config = tomllib.load(f)

        WWKS2_LISTEN_IP = config.get("WWKS2_LISTEN_IP", WWKS2_LISTEN_IP)
        WWKS2_LISTEN_PORT = config.get("WWKS2_LISTEN_PORT", WWKS2_LISTEN_PORT)
        PULSE_TIME = config.get("PULSE_TIME", PULSE_TIME)

        ADS_AMS_NET_ID = config.get("ADS_AMS_NET_ID", ADS_AMS_NET_ID)
        ADS_PORT = config.get("ADS_PORT", ADS_PORT)

        PLC_VAR_ROBOT_ACTIVE = config.get("PLC_VAR_ROBOT_ACTIVE", PLC_VAR_ROBOT_ACTIVE)
        PLC_VAR_DELIVERY_OK = config.get("PLC_VAR_DELIVERY_OK", PLC_VAR_DELIVERY_OK)
        PLC_VAR_DELIVERY_ERROR = config.get("PLC_VAR_DELIVERY_ERROR", PLC_VAR_DELIVERY_ERROR)

        logger.info(f"Loaded config from {config_file}")

    except Exception as ex:
        logger.warning(f"Failed to parse {config_file}: {ex}")

# ------------------------------------------------------------------------------
# THREAD-SAFE SYSTEM STATE
# ------------------------------------------------------------------------------
@dataclass
class SystemState:
    ws_connected: bool = False
    last_message_type: str = ""
    last_delivery_ok: Optional[bool] = None
    robot_active: bool = False
    ads_connected: bool = False

class ThreadSafeState:
    def __init__(self):
        self._lock = threading.Lock()
        self._state = SystemState()

    def update(self, **kwargs):
        with self._lock:
            for key, value in kwargs.items():
                setattr(self._state, key, value)

    def snapshot(self) -> dict:
        with self._lock:
            return asdict(self._state)

# ------------------------------------------------------------------------------
# PLC INTERFACE (pyads)
# ------------------------------------------------------------------------------

class PLCInterfaceADS:
    def __init__(self, state: ThreadSafeState, net_id=ADS_AMS_NET_ID, port=ADS_PORT):
        self.state = state
        self.net_id = net_id
        self.port = port
        self._lock = threading.Lock()
        self.client = pyads.Connection(self.net_id, self.port)
        self._connect()

    def _connect(self):
        with self._lock:
            try:
                try:
                    self.client.close()
                except Exception:
                    pass

                self.client = pyads.Connection(self.net_id, self.port)
                self.client.open()
                self.state.update(ads_connected=True)
                logger.info("[PLC] ADS connected")

            except Exception as ex:
                self.state.update(ads_connected=False)
                logger.error(f"[PLC] ADS connection failed: {ex}")

    def _write_bool(self, varname: str, value: bool):
        with self._lock:
            try:
                self.client.write_by_name(varname, value, pyads.PLCTYPE_BOOL)
                self.state.update(ads_connected=True)
                logger.info(f"[PLC] {varname} = {value}")

            except Exception as ex:
                self.state.update(ads_connected=False)
                logger.error(f"[PLC] Failed to write {varname}: {ex}")

                # Retry once after reconnect
                try:
                    logger.info("[PLC] Attempting reconnect...")
                    try:
                        self.client.close()
                    except Exception:
                        pass

                    self.client = pyads.Connection(self.net_id, self.port)
                    self.client.open()
                    self.state.update(ads_connected=True)
                    logger.info("[PLC] Reconnected successfully")

                    self.client.write_by_name(varname, value, pyads.PLCTYPE_BOOL)
                    logger.info(f"[PLC] Retry successful: {varname} = {value}")

                except Exception as retry_ex:
                    self.state.update(ads_connected=False)
                    logger.error(f"[PLC] Retry failed for {varname}: {retry_ex}")

    def _pulse(self, varname: str):
        self._write_bool(varname, True)
        time.sleep(PULSE_TIME)
        self._write_bool(varname, False)

    def set_robot_active(self, active: bool):
        self.state.update(robot_active=active)
        self._write_bool(PLC_VAR_ROBOT_ACTIVE, active)

    def pulse_delivered_ok(self):
        self.state.update(last_delivery_ok=True)
        self._pulse(PLC_VAR_DELIVERY_OK)

    def pulse_delivered_error(self):
        self.state.update(last_delivery_ok=False)
        self._pulse(PLC_VAR_DELIVERY_ERROR)

# ------------------------------------------------------------------------------
# WWKS2 PARSER
# ------------------------------------------------------------------------------

class WWKS2Parser:
    def parse(self, xml_string: str) -> dict:
        try:
            root = ET.fromstring(xml_string)
        except Exception as ex:
            logger.error(f"Invalid XML: {ex}")
            return {"type": "Invalid"}

        children = list(root)
        if not children:
            return {"type": "Unknown"}

        if len(children) > 1:
            logger.warning(f"[WWKS2] Expected 1 child in WWKS message, got {len(children)}")

        child = children[0]
        return {
            "type": child.tag,
            "attributes": child.attrib.copy(),
            "element": child,
        }

# ------------------------------------------------------------------------------
# CORE LOGIC
# ------------------------------------------------------------------------------

class WWKS2SignalEngine:
    def __init__(self, plc: PLCInterfaceADS, state: ThreadSafeState):
        self.plc = plc
        self.state = state
        self.last_processed_output_id = None

    def handle_message(self, msg: dict):
        msg_type = msg.get("type", "Unknown")
        self.state.update(last_message_type=msg_type)

        if msg_type == "StatusResponse":
            self.handle_status(msg)
        elif msg_type == "OutputMessage":
            self.handle_output(msg)

    def handle_status(self, msg: dict):
        state = msg["attributes"].get("State", "NotReady")
        robot_active = state == "Ready"
        self.plc.set_robot_active(robot_active)

    def handle_output(self, msg: dict):
        # Use OutputMessage Id to detect duplicates
        output_id = msg["attributes"].get("Id")
        if output_id and output_id == self.last_processed_output_id:
            logger.info(f"[WWKS2] Duplicate OutputMessage for ID={output_id} — ignored")
            return

        self.last_processed_output_id = output_id
        element = msg["element"]

        details = element.find("Details")
        if details is None:
            logger.error("[WWKS2] OutputMessage has no Details-element")
            self.plc.pulse_delivered_error()
            return

        status = details.attrib.get("Status", "")
        output_destination = details.attrib.get("OutputDestination")
        output_point = details.attrib.get("OutputPoint")

        article_count = 0
        delivered_article_count = 0
        total_pack_count = 0

        for art in element.findall("Article"):
            article_count += 1
            packs = art.findall("Pack")
            pack_count = len(packs)
            total_pack_count += pack_count

            # Article is considered delivered if it contains at least 1 pack
            if pack_count > 0:
                delivered_article_count += 1

        logger.info(
            f"[WWKS2] OutputMessage status={status}, "
            f"articles={article_count}, delivered_articles={delivered_article_count}, "
            f"packs={total_pack_count}, output_destination={output_destination}, "
            f"output_point={output_point}"
        )

        # Evaluation logic:
        # - Completed + at least 1 article + every article has at least 1 pack => OK
        # - BoxReleased => no pulse
        # - everything else => ERROR
        if status == "Completed" and article_count > 0 and delivered_article_count == article_count:
            logger.info("[WWKS2] pulse_delivered_ok")
            self.plc.pulse_delivered_ok()

        elif status == "BoxReleased":
            logger.info("[WWKS2] BoxReleased received - no OK/ERROR pulse sent")

        else:
            logger.info("[WWKS2] pulse_delivered_error")
            self.plc.pulse_delivered_error()

# ------------------------------------------------------------------------------
# WEBSOCKET CLIENT WRAPPER (Start/Stop)
# ------------------------------------------------------------------------------

class WWKS2ClientThread(threading.Thread):
    def __init__(self, engine: WWKS2SignalEngine, state: ThreadSafeState):
        super().__init__(daemon=True)
        self.engine = engine
        self.state = state
        self.parser = WWKS2Parser()
        self.loop = asyncio.new_event_loop()
        self.stop_event = None

    def stop(self):
        if self.loop.is_running() and self.stop_event is not None:
            self.loop.call_soon_threadsafe(self.stop_event.set)

    def run(self):
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self.async_run())
        finally:
            self.loop.close()
            logger.info("[WS] Event loop closed")

    async def async_run(self):
        self.stop_event = asyncio.Event()
        uri = f"ws://{WWKS2_LISTEN_IP}:{WWKS2_LISTEN_PORT}"
        logger.info(f"[WS] WWKS2 websocket client started, connecting to {uri}")

        while not self.stop_event.is_set():
            try:
                self.state.update(ws_connected=False)

                async with websockets.connect(uri) as websocket:
                    self.state.update(ws_connected=True)
                    logger.info(f"[WS] Connected to {uri}")

                    await self.handle_connection(websocket)

                    self.state.update(ws_connected=False)

            except websockets.exceptions.ConnectionClosed:
                self.state.update(ws_connected=False)
                if not self.stop_event.is_set():
                    logger.warning("[WS] Connection closed, retrying in 5 seconds...")
                    await asyncio.sleep(5)

            except Exception as ex:
                self.state.update(ws_connected=False)
                if not self.stop_event.is_set():
                    logger.error(f"[WS] Connection error: {ex}, retrying in 5 seconds...")
                    await asyncio.sleep(5)

        self.state.update(ws_connected=False)
        logger.info("[WS] WWKS2 client stopped")

    async def handle_connection(self, websocket):
        buffer = ""

        try:
            while not self.stop_event.is_set():
                receive_task = asyncio.create_task(websocket.recv())
                stop_task = asyncio.create_task(self.stop_event.wait())

                done, pending = await asyncio.wait(
                    [receive_task, stop_task],
                    return_when=asyncio.FIRST_COMPLETED
                )

                for task in pending:
                    task.cancel()

                await asyncio.gather(*pending, return_exceptions=True)

                if stop_task in done:
                    if not receive_task.done():
                        receive_task.cancel()
                        await asyncio.gather(receive_task, return_exceptions=True)
                    break

                if receive_task in done:
                    message = receive_task.result()

                    if isinstance(message, bytes):
                        message = message.decode("utf-8")

                    logger.info(f"[WS] Received message chunk (length {len(message)})")
                    buffer += message

                    while "</WWKS>" in buffer:
                        msg, buffer = buffer.split("</WWKS>", 1)
                        xml = msg + "</WWKS>"
                        parsed = self.parser.parse(xml)
                        self.engine.handle_message(parsed)

        except websockets.exceptions.ConnectionClosed:
            logger.info("[WS] Disconnected from server")
            raise

        except Exception as ex:
            logger.error(f"[WS] Error in connection handler: {ex}")
            raise

# ------------------------------------------------------------------------------
# SERVICE CONTROLLER
# ------------------------------------------------------------------------------

class ServiceController:
    def __init__(self, plc: PLCInterfaceADS, state: ThreadSafeState):
        self.plc = plc
        self.state = state
        self.engine = WWKS2SignalEngine(self.plc, self.state)
        self.client_thread = None
        self._lock = threading.Lock()

    def start(self) -> bool:
        with self._lock:
            if self.client_thread and self.client_thread.is_alive():
                logger.info("[Service] Client is already running")
                return False

            self.client_thread = WWKS2ClientThread(self.engine, self.state)
            self.client_thread.start()
            logger.info("[Service] Client started")
            return True

    def stop(self) -> bool:
        with self._lock:
            if not self.client_thread:
                logger.info("[Service] Client is not running")
                return False

            self.client_thread.stop()
            self.client_thread.join(timeout=5)

            if self.client_thread.is_alive():
                logger.warning("[Service] Client thread did not stop within timeout")
                return False

            self.client_thread = None
            self.state.update(ws_connected=False)
            logger.info("[Service] Client stopped")
            return True

# ------------------------------------------------------------------------------
# FASTAPI WEB INTERFACE
# ------------------------------------------------------------------------------

def create_app(controller: ServiceController, state: ThreadSafeState) -> FastAPI:
    app = FastAPI()

    @app.get("/", response_class=HTMLResponse)
    def dashboard():
        html = """
        <html>
        <head>
            <title>WWKS2 Gateway</title>
            <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap" rel="stylesheet">
            <style>
                :root {
                    --bg: #0f172a;
                    --card-bg: rgba(30, 41, 59, 0.7);
                    --text: #f8fafc;
                    --primary: #38bdf8;
                    --success: #22c55e;
                    --error: #ef4444;
                    --warning: #f59e0b;
                    --border: rgba(255, 255, 255, 0.1);
                }
                body {
                    font-family: 'Inter', sans-serif;
                    margin: 0;
                    display: flex;
                    justify-content: center;
                    align-items: center;
                    min-height: 100vh;
                    background: linear-gradient(135deg, #0f172a 0%, #1e1b4b 100%);
                    color: var(--text);
                }
                .container {
                    width: 100%;
                    max-width: 500px;
                    padding: 20px;
                }
                .card {
                    background: var(--card-bg);
                    backdrop-filter: blur(12px);
                    border: 1px solid var(--border);
                    border-radius: 24px;
                    padding: 32px;
                    box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.5);
                }
                h1 {
                    font-size: 24px;
                    margin-bottom: 24px;
                    text-align: center;
                    background: linear-gradient(to right, #38bdf8, #818cf8);
                    -webkit-background-clip: text;
                    -webkit-text-fill-color: transparent;
                    font-weight: 700;
                }
                .status-list {
                    list-style: none;
                    padding: 0;
                    margin: 0 0 24px 0;
                }
                .status-item {
                    display: flex;
                    justify-content: space-between;
                    padding: 12px 0;
                    border-bottom: 1px solid var(--border);
                    transition: all 0.3s ease;
                }
                .status-item:last-child { border-bottom: none; }
                .label { opacity: 0.7; font-size: 14px; }
                .value { font-weight: 600; font-size: 14px; }

                .state-true { color: var(--success); }
                .state-false { color: var(--error); }
                .state-null { color: #94a3b8; }
                .state-active { color: var(--primary); }

                .controls {
                    display: flex;
                    gap: 12px;
                }
                button {
                    flex: 1;
                    padding: 12px;
                    border-radius: 12px;
                    border: none;
                    font-weight: 600;
                    cursor: pointer;
                    transition: transform 0.2s, background 0.2s;
                }
                button:hover { transform: translateY(-2px); }
                button:active { transform: translateY(0); }
                .btn-start { background: var(--success); color: white; }
                .btn-stop { background: var(--error); color: white; }

                #last_msg_type { color: var(--primary); }

                .badge {
                    padding: 2px 8px;
                    border-radius: 12px;
                    font-size: 11px;
                    background: rgba(255,255,255,0.1);
                }
            </style>
        </head>
        <body>
            <div class="container">
                <div class="card">
                    <h1>WWKS2 Gateway</h1>

                    <div class="status-list">
                        <div class="status-item">
                            <span class="label">Client Connection</span>
                            <span id="ws_active" class="value badge">Checking...</span>
                        </div>
                        <div class="status-item">
                            <span class="label">PLC Connection (ADS)</span>
                            <span id="ads_connected" class="value badge">Checking...</span>
                        </div>
                        <div class="status-item">
                            <span class="label">Robot Status</span>
                            <span id="robot_active" class="value">Checking...</span>
                        </div>
                        <div class="status-item">
                            <span class="label">Last Message</span>
                            <span id="last_msg_type" class="value">-</span>
                        </div>
                        <div class="status-item">
                            <span class="label">Last Delivery Result</span>
                            <span id="last_delivery_ok" class="value">None</span>
                        </div>
                    </div>

                    <div class="controls">
                        <form action="/start" method="post" style="flex:1">
                            <button class="btn-start">Start Client</button>
                        </form>
                        <form action="/stop" method="post" style="flex:1">
                            <button class="btn-stop">Stop Client</button>
                        </form>
                    </div>
                </div>
            </div>

            <script>
                async function updateStatus() {
                    try {
                        const response = await fetch('/status');
                        const data = await response.json();

                        const wsEl = document.getElementById('ws_active');
                        wsEl.innerText = data.ws_connected ? 'CONNECTED' : 'DISCONNECTED';
                        wsEl.className = 'value badge ' + (data.ws_connected ? 'state-true' : 'state-false');

                        const adsEl = document.getElementById('ads_connected');
                        adsEl.innerText = data.ads_connected ? 'OK' : 'ERROR';
                        adsEl.className = 'value badge ' + (data.ads_connected ? 'state-true' : 'state-false');

                        const robotEl = document.getElementById('robot_active');
                        robotEl.innerText = data.robot_active ? 'READY' : 'NOT READY';
                        robotEl.className = 'value ' + (data.robot_active ? 'state-true' : 'state-false');

                        document.getElementById('last_msg_type').innerText = data.last_message_type || '-';

                        const deliveryEl = document.getElementById('last_delivery_ok');
                        if (data.last_delivery_ok === true) {
                            deliveryEl.innerText = 'SUCCESS';
                            deliveryEl.className = 'value state-true';
                        } else if (data.last_delivery_ok === false) {
                            deliveryEl.innerText = 'ERROR';
                            deliveryEl.className = 'value state-false';
                        } else {
                            deliveryEl.innerText = 'NONE';
                            deliveryEl.className = 'value state-null';
                        }

                    } catch (err) {
                        console.error('Failed to update status:', err);
                    }
                }

                setInterval(updateStatus, 1000);
                updateStatus();
            </script>
        </body>
        </html>
        """
        return HTMLResponse(content=html)

    @app.post("/start")
    def start_server():
        controller.start()
        return RedirectResponse(url="/", status_code=303)

    @app.post("/stop")
    def stop_server():
        controller.stop()
        return RedirectResponse(url="/", status_code=303)

    @app.get("/status")
    def get_status():
        return JSONResponse(state.snapshot())

    return app

# ------------------------------------------------------------------------------
# MAIN ENTRY
# ------------------------------------------------------------------------------

load_config()

system_state = ThreadSafeState()
plc = PLCInterfaceADS(system_state)
controller = ServiceController(plc, system_state)
app = create_app(controller, system_state)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)