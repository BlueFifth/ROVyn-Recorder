import asyncio
import json
import logging
import os
import signal
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from camera_manager import CameraManager
from mavlink_watcher import watch_arm_state
from rtsp_server import RTSPServer
from session import SessionManager, State
from storage import find_data_dir, free_space_gb, estimated_minutes_remaining, list_sessions

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
log = logging.getLogger("main")

# ---------------------------------------------------------------------------
# Load config
# ---------------------------------------------------------------------------
CONFIG_PATH = Path("/config/default_config.json")

def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)

def save_config(cfg: dict):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)

config = load_config()

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------
camera_manager = CameraManager(config)
session_manager = SessionManager()
rtsp_server = RTSPServer(port=config["rtsp_port"])
data_dir: Optional[Path] = None
mavlink_task: Optional[asyncio.Task] = None


# ---------------------------------------------------------------------------
# Recording control
# ---------------------------------------------------------------------------
async def handle_arm(trigger: str):
    global data_dir
    if session_manager.state == State.RECORDING:
        log.warning("Arm received but already recording — ignoring")
        return
    if data_dir is None:
        log.error("Cannot record — no SSD mounted")
        return

    session = session_manager.start(data_dir, trigger)
    camera_manager.start_recording(session.session_dir)
    log.info(f"Session {session.session_id} started (trigger={trigger})")


async def handle_disarm(trigger: str):
    if session_manager.state == State.IDLE:
        return

    camera_manager.stop_recording()
    session = session_manager.stop()
    if session:
        log.info(f"Session {session.session_id} finished — {session.duration_s}s")


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    global data_dir, mavlink_task

    # 1. Find SSD
    data_dir = find_data_dir()
    if data_dir is None:
        log.warning("SSD not found — recording unavailable. Streaming will still work.")
    else:
        log.info(f"SSD data directory: {data_dir}")

    # 2. Detect cameras
    cam_status = camera_manager.detect_cameras()
    log.info(f"Camera detection result: {cam_status}")

    # 3. Start RTSP server
    rtsp_server.start(
        cam0_udp_port=config["rtsp_cam0_udp_port"],
        cam1_udp_port=config["rtsp_cam1_udp_port"],
    )

    # 4. Start GStreamer pipelines (streaming mode — always on)
    camera_manager.start_streaming()

    # 5. Start MAVLink watcher
    mavlink_task = asyncio.create_task(
        watch_arm_state(handle_arm, handle_disarm, port=config["mavlink_port"])
    )

    # 6. SIGTERM handler for graceful shutdown
    def on_sigterm(*_):
        log.info("SIGTERM received — shutting down")
        if session_manager.state == State.RECORDING:
            camera_manager.stop_recording()
            session_manager.stop()
        camera_manager.stop_all()
        rtsp_server.stop()

    signal.signal(signal.SIGTERM, on_sigterm)

    yield

    # Shutdown
    if mavlink_task:
        mavlink_task.cancel()
    if session_manager.state == State.RECORDING:
        camera_manager.stop_recording()
        session_manager.stop()
    camera_manager.stop_all()
    rtsp_server.stop()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="StellarHD Recorder", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="/app/static"), name="static")


# ---------------------------------------------------------------------------
# BlueOS registration
# ---------------------------------------------------------------------------
@app.get("/register_service")
def register_service():
    host = os.environ.get("BLUEOS_HOST", "blueos.local")
    base = f"http://{host}:{config['api_port']}"
    return {
        "name": "StellarHD Recorder",
        "description": "Arm-triggered YUYV recorder for DWE StellarHD stereo cameras",
        "icon": "mdi-video",
        "company": "Custom",
        "version": "0.1.0",
        "new_page": False,
        "webpage": base,
        "api": base,
    }


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------
@app.get("/status")
def status():
    free_gb = free_space_gb(data_dir) if data_dir else 0.0
    cam_status = camera_manager.status()
    return {
        "state": session_manager.state.value,
        "session_id": session_manager.current.session_id if session_manager.current else None,
        "trigger": session_manager.current.trigger if session_manager.current else None,
        "duration_s": session_manager.elapsed_s(),
        "ssd_mounted": data_dir is not None,
        "ssd_path": str(data_dir) if data_dir else None,
        "ssd_free_gb": round(free_gb, 2),
        "estimated_minutes_remaining": round(estimated_minutes_remaining(free_gb), 1),
        "cameras": cam_status,
        "rtsp": {
            "cam0": f"rtsp://{config['pi_ip']}:{config['rtsp_port']}/cam0",
            "cam1": f"rtsp://{config['pi_ip']}:{config['rtsp_port']}/cam1",
        },
    }


# ---------------------------------------------------------------------------
# Manual recording control
# ---------------------------------------------------------------------------
@app.post("/record/start")
async def record_start():
    if session_manager.state == State.RECORDING:
        raise HTTPException(409, "Already recording")
    if data_dir is None:
        raise HTTPException(503, "SSD not mounted — cannot record")
    await handle_arm("manual")
    return {"ok": True, "session_id": session_manager.current.session_id}


@app.post("/record/stop")
async def record_stop():
    if session_manager.state == State.IDLE:
        raise HTTPException(409, "Not recording")
    await handle_disarm("manual")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------
@app.get("/snapshot/{cam_id}")
def snapshot(cam_id: str):
    pipeline = camera_manager.cam0 if cam_id == "cam0" else camera_manager.cam1
    if pipeline is None:
        raise HTTPException(404, f"{cam_id} not available")
    jpeg = pipeline.get_latest_jpeg()
    if jpeg is None:
        raise HTTPException(503, f"No frame available from {cam_id} yet")
    return Response(content=jpeg, media_type="image/jpeg")


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------
@app.get("/sessions")
def get_sessions():
    if data_dir is None:
        return []
    return list_sessions(data_dir)


@app.delete("/sessions/{session_id}")
def delete_session(session_id: str):
    if data_dir is None:
        raise HTTPException(503, "SSD not mounted")
    session_path = data_dir / "sessions" / session_id
    if not session_path.exists():
        raise HTTPException(404, f"Session {session_id} not found")
    shutil.rmtree(session_path)
    return {"ok": True, "deleted": session_id}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
@app.get("/config")
def get_config():
    return config


@app.post("/config")
def update_config(new_config: dict):
    global config
    config.update(new_config)
    save_config(config)
    return {"ok": True}


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
@app.get("/")
def index():
    return FileResponse("/app/static/index.html")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=config["api_port"], log_level="info")
