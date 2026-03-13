import ctypes
import xml.etree.ElementTree as ET
import asyncio
import websockets
import threading
import time
import logging
import pyads
import os
import sys
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
import uvicorn
import logging
import tomllib

logger = logging.getLogger("wwks2")
logger.setLevel(logging.INFO)
ch = logging.StreamHandler()
ch.setFormatter(logging.Formatter("%(asctime)s  %(levelname)s  %(message)s"))
if not logger.handlers:
    logger.addHandler(ch)
logger.propagate = False

# ------------------------------------------------------------------------------
# CONFIG
# ------------------------------------------------------------------------------

WWKS2_LISTEN_IP = "0.0.0.0"
WWKS2_LISTEN_PORT = 6050
PULSE_TIME = 0.1

ADS_AMS_NET_ID = "127.0.0.1.1.1"
ADS_PORT = 48898

# PLC variable names (BOOL)
PLC_VAR_ROBOT_ACTIVE   = "MAIN.RobotActive"
PLC_VAR_DELIVERY_OK    = "MAIN.DeliveryOK"
PLC_VAR_DELIVERY_ERROR = "MAIN.DeliveryError"

# Determine path for config file. When built with PyInstaller, we want the config 
# to be located next to the generated .exe file.
if getattr(sys, 'frozen', False):
    base_dir = os.path.dirname(sys.executable)
else:
    base_dir = os.path.dirname(os.path.abspath(__file__))

CONFIG_FILE = os.path.join(base_dir, "config.toml")

if os.path.exists(CONFIG_FILE) and tomllib is not None:
    try:
        with open(CONFIG_FILE, "rb") as f:
            config = tomllib.load(f)
        
        WWKS2_LISTEN_IP = config.get("WWKS2_LISTEN_IP", WWKS2_LISTEN_IP)
        WWKS2_LISTEN_PORT = config.get("WWKS2_LISTEN_PORT", WWKS2_LISTEN_PORT)
        PULSE_TIME = config.get("PULSE_TIME", PULSE_TIME)
        
        ADS_AMS_NET_ID = config.get("ADS_AMS_NET_ID", ADS_AMS_NET_ID)
        ADS_PORT = config.get("ADS_PORT", ADS_PORT)
        
        PLC_VAR_ROBOT_ACTIVE = config.get("PLC_VAR_ROBOT_ACTIVE", PLC_VAR_ROBOT_ACTIVE)
        PLC_VAR_DELIVERY_OK = config.get("PLC_VAR_DELIVERY_OK", PLC_VAR_DELIVERY_OK)
        PLC_VAR_DELIVERY_ERROR = config.get("PLC_VAR_DELIVERY_ERROR", PLC_VAR_DELIVERY_ERROR)
        
    except Exception as e:
        print(f"Warning: Failed to parse {CONFIG_FILE}: {e}")
elif os.path.exists(CONFIG_FILE) and tomllib is None:
    print(f"Warning: {CONFIG_FILE} found but neither 'tomllib' nor 'tomli' module is available to parse it.")

# ------------------------------------------------------------------------------
# GLOBAL STATE FOR WEB UI
# ------------------------------------------------------------------------------

system_state = {
    "ws_server_running": False,
    "last_message_type": "",
    "last_delivery_ok": None,
    "robot_active": False,
    "ads_connected": False
}

# ------------------------------------------------------------------------------
# PLC INTERFACE (pyads)
# ------------------------------------------------------------------------------

class PLCInterfaceADS:
    def __init__(self, net_id=ADS_AMS_NET_ID, port=ADS_PORT):
        self.net_id = net_id
        self.port = port
        self.client = pyads.Connection(self.net_id, self.port)
        self._connect()

    def _connect(self):
        try:
            self.client.open()
            system_state["ads_connected"] = True
            logging.info("[PLC] ADS connected")
        except Exception as ex:
            system_state["ads_connected"] = False
            logging.error(f"[PLC] ADS connection failed: {ex}")

    def _write_bool(self, varname, value: bool):
        try:
            self.client.write_by_name(varname, value, pyads.PLCTYPE_BOOL)
            logging.info(f"[PLC] {varname} = {value}")
        except Exception as ex:
            logging.error(f"[PLC] Failed to write {varname}: {ex}")
            
    def _pulse(self, varname):
        self._write_bool(varname, True)
        time.sleep(PULSE_TIME)
        self._write_bool(varname, False)            

    def set_robot_active(self, active: bool):
        system_state["robot_active"] = active
        self._write_bool(PLC_VAR_ROBOT_ACTIVE, active)

    def pulse_delivered_ok(self):
        system_state["last_delivery_ok"] = True
        self._pulse(PLC_VAR_DELIVERY_OK)

    def pulse_delivered_error(self):
        system_state["last_delivery_ok"] = False
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

        for child in root:
            msg_type = child.tag
            attrs = child.attrib.copy()
            return {"type": msg_type, "attributes": attrs, "element": child}

        return {"type": "Unknown"}

# ------------------------------------------------------------------------------
# CORE LOGIC
# ------------------------------------------------------------------------------

class WWKS2SignalEngine:
    def __init__(self, plc: PLCInterfaceADS):
        self.plc = plc
        self.last_processed_output_id = None

    def handle_message(self, msg):
        system_state["last_message_type"] = msg["type"]

        if msg["type"] == "StatusResponse":
            self.handle_status(msg)

        elif msg["type"] == "OutputMessage":
            self.handle_output(msg)

    def handle_status(self, msg):
        state = msg["attributes"].get("State", "NotReady")
        robot_active = state == "Ready"
        self.plc.set_robot_active(robot_active)
   
    def handle_output(self, msg):
        # We use the OutputMessage's Id attribute to detect duplicates, as sometimes the same OutputMessage can be received multiple times.
        # We only process each unique OutputMessage once.
        output_id = msg["attributes"].get("Id")
        if output_id == self.last_processed_output_id:
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

            # We consider an article as delivered if there is at least 1 Pack for that article present
            if pack_count > 0:
                delivered_article_count += 1

        logger.info(
            f"[WWKS2] OutputMessage status={status}, "
            f"articles={article_count}, delivered_articles={delivered_article_count}, "
            f"packs={total_pack_count}, output_destination={output_destination}, output_point={output_point}")
       
        # Evaluation logic:
        # - Completed + at least 1 item/article + each item/article has at least 1 pack => OK
        # - Incomplete / Aborted / no items/articles / no packs => ERROR
        if status == "Completed" and article_count > 0 and delivered_article_count == article_count:
            logger.info("pulse_delivered_ok")
            self.plc.pulse_delivered_ok()
        elif status == "BoxReleased":
            # BoxReleased is a bespoke status, niet noodzakelijk een 'delivery complete'
            logger.info("[WWKS2] BoxReleased received - no OK/ERROR pulse sent")
        else:
            logger.info("pulse_delivered_error")
            self.plc.pulse_delivered_error()    

        if ok:
            logger.info(f"pulse_delivered_ok")
            self.plc.pulse_delivered_ok()
        else:
            logger.info(f"pulse_delivered_error")
            self.plc.pulse_delivered_error()

# ------------------------------------------------------------------------------
# WEBSOCKET CLIENT WRAPPER (Start/Stop)
# ------------------------------------------------------------------------------

class WWKS2ClientThread(threading.Thread):
    def __init__(self, engine):
        super().__init__()
        self.engine = engine
        self.parser = WWKS2Parser()
        self.loop = asyncio.new_event_loop()
        self.stop_event = None

    def stop(self):
        if self.loop.is_running() and self.stop_event:
            self.loop.call_soon_threadsafe(self.stop_event.set)

    def run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self.async_run())

    async def async_run(self):
        # ws_server_running is used by the frontend to show connection status
        self.stop_event = asyncio.Event()
        uri = f"ws://{WWKS2_LISTEN_IP}:{WWKS2_LISTEN_PORT}"
        logger.info(f"[WS] WWKS2 websocket client started, connecting to {uri}")
        
        while not self.stop_event.is_set():
            try:
                # Set to False before attempting connection
                system_state["ws_server_running"] = False
                async with websockets.connect(uri) as websocket:
                    # Successfully connected
                    system_state["ws_server_running"] = True
                    logger.info(f"[WS] Connected to {uri}")
                    await self.handle_connection(websocket)
                    # If handle_connection returns normally, it means connection was closed gracefully
                    system_state["ws_server_running"] = False
            except websockets.exceptions.ConnectionClosed:
                system_state["ws_server_running"] = False
                logger.warning(f"[WS] Connection closed, retrying in 5 seconds...")
                await asyncio.sleep(5)
            except Exception as ex:
                system_state["ws_server_running"] = False
                logger.error(f"[WS] Connection error: {ex}, retrying in 5 seconds...")
                await asyncio.sleep(5)
                
        system_state["ws_server_running"] = False
        logger.info("[WS] WWKS2 client stopped")

    async def handle_connection(self, websocket):
        buffer = ""
        try:
            while not self.stop_event.is_set():
                # We use asyncio.wait to be able to respond to stop_event while waiting for messages
                receive_task = asyncio.create_task(websocket.recv())
                stop_task = asyncio.create_task(self.stop_event.wait())
                
                done, pending = await asyncio.wait(
                    [receive_task, stop_task],
                    return_when=asyncio.FIRST_COMPLETED
                )
                
                if stop_task in done:
                    receive_task.cancel()
                    break
                    
                if receive_task in done:
                    message = receive_task.result()
                    logger.info(f"[WS] Received message chunk (length {len(message)})")
                    if isinstance(message, bytes):
                        message = message.decode('utf-8')
                    buffer += message
                    while "</WWKS>" in buffer:
                        msg, buffer = buffer.split("</WWKS>", 1)
                        xml = msg + "</WWKS>"
                        # logger.info(f"[WS] Extracted WWKS message: {xml[:200]}...")
                        parsed = self.parser.parse(xml)
                        # logger.info(f"[WS] Parsed WWKS message type: {parsed.get('type')}")
                        self.engine.handle_message(parsed)
                        
        except websockets.exceptions.ConnectionClosed:
            logger.info(f"[WS] Disconnected from server")
            raise  # bubble up to trigger reconnect
        except Exception as ex:
            logger.error(f"[WS] Error in connection handler: {ex}")
            raise

# ------------------------------------------------------------------------------
# SERVICE CONTROLLER
# ------------------------------------------------------------------------------

class ServiceController:
    def __init__(self, plc):
        self.plc = plc
        self.engine = WWKS2SignalEngine(self.plc)
        self.client_thread = None

    def start(self):
        if self.client_thread and self.client_thread.is_alive():
            return False
        self.client_thread = WWKS2ClientThread(self.engine)
        self.client_thread.start()
        return True

    def stop(self):
        if not self.client_thread:
            return False
        self.client_thread.stop()
        self.client_thread.join()
        self.client_thread = None
        return True

# ------------------------------------------------------------------------------
# FASTAPI WEB INTERFACE
# ------------------------------------------------------------------------------

app = FastAPI()
plc = PLCInterfaceADS()
controller = ServiceController(plc)

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
                    wsEl.innerText = data.ws_server_running ? 'CONNECTED' : 'DISCONNECTED';
                    wsEl.className = 'value badge ' + (data.ws_server_running ? 'state-true' : 'state-false');

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

            // Update every second
            setInterval(updateStatus, 1000);
            updateStatus(); // Initial call
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
    return JSONResponse(system_state)

# ------------------------------------------------------------------------------
# MAIN ENTRY
# ------------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(levelname)s  %(message)s")

    uvicorn.run(app, host="0.0.0.0", port=8080)