import asyncio
import json
import logging
import os
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
from storage import find_data_dir, free_space_gb, estimated_minutes_remaining, list_sessions, bytes_per_second

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
# Supported capture resolution/framerate presets, and stream (encode) fps
# options. These are independent: capture sets the v4l2 caps, stream fps
# is a separate downstream `videorate` element, so the stream can be
# encoded slower than the camera captures (e.g. 1-2fps to save bandwidth).
# ---------------------------------------------------------------------------
RESOLUTION_PRESETS = [
    {"label": "1600x1200 @ 5fps",  "width": 1600, "height": 1200, "framerate": 5},
    {"label": "1280x720 @ 5fps",   "width": 1280, "height": 720,  "framerate": 5},
    {"label": "640x480 @ 5fps",    "width": 640,  "height": 480,  "framerate": 5},
    {"label": "320x240 @ 15fps",   "width": 320,  "height": 240,  "framerate": 15},
    {"label": "320x240 @ 30fps",   "width": 320,  "height": 240,  "framerate": 30},
]

STREAM_FPS_OPTIONS = [0.2, 0.5, 1, 2, 5, 10, 15, 30]

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

    # NOTE: we deliberately do NOT install a custom SIGTERM handler here.
    # Doing so replaces uvicorn's own handler, so uvicorn never shuts down and
    # the process hangs until supervisord SIGKILLs it — which can interrupt
    # pipeline teardown and leave the V4L2 device locked. Instead, teardown
    # runs in the lifespan shutdown block below, which uvicorn invokes on
    # SIGTERM (bounded by timeout_graceful_shutdown set in uvicorn.run).

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
    bps = bytes_per_second(config["width"], config["height"], config.get("framerate", 15))
    return {
        "state": session_manager.state.value,
        "session_id": session_manager.current.session_id if session_manager.current else None,
        "trigger": session_manager.current.trigger if session_manager.current else None,
        "duration_s": session_manager.elapsed_s(),
        "ssd_mounted": data_dir is not None,
        "ssd_path": str(data_dir) if data_dir else None,
        "ssd_free_gb": round(free_gb, 2),
        "estimated_minutes_remaining": round(estimated_minutes_remaining(free_gb, bps), 1),
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


def _restart_streaming():
    """Stop/recreate both camera pipelines from the current `config`."""
    camera_manager.stop_all()
    camera_manager.detect_cameras()
    camera_manager.start_streaming()


@app.get("/resolutions")
def get_resolutions():
    return RESOLUTION_PRESETS


@app.post("/resolution")
def set_resolution(preset: dict):
    global config

    match = next(
        (p for p in RESOLUTION_PRESETS
         if p["width"] == preset.get("width")
         and p["height"] == preset.get("height")
         and p["framerate"] == preset.get("framerate")),
        None,
    )
    if match is None:
        raise HTTPException(400, "Unsupported resolution/fps combination")
    if session_manager.state == State.RECORDING:
        raise HTTPException(409, "Cannot change resolution while recording")

    config["width"] = match["width"]
    config["height"] = match["height"]
    config["framerate"] = match["framerate"]
    # Stream fps is independent of capture fps (separate `videorate` element
    # downstream of the tee — recording taps the tee directly, unaffected),
    # but it can't exceed what the camera is now capturing.
    config["stream_fps"] = min(config.get("stream_fps", match["framerate"]), match["framerate"])
    save_config(config)

    # Pipelines are built from `config` at start_streaming() time, so a
    # stop/start cycle is required to pick up the new caps — restarting in
    # place also releases and reacquires the V4L2 device cleanly.
    _restart_streaming()

    return {"ok": True, **match, "stream_fps": config["stream_fps"]}


@app.get("/stream-fps-options")
def get_stream_fps_options():
    return STREAM_FPS_OPTIONS


@app.post("/stream-fps")
def set_stream_fps(payload: dict):
    global config

    stream_fps = payload.get("stream_fps")
    if stream_fps not in STREAM_FPS_OPTIONS:
        raise HTTPException(400, "Unsupported stream fps")
    if stream_fps > config["framerate"]:
        raise HTTPException(400, "Stream fps cannot exceed the camera's capture fps")
    if session_manager.state == State.RECORDING:
        raise HTTPException(409, "Cannot change stream fps while recording")

    # Recording always taps the tee at full capture fps (camera_manager.py
    # CameraPipeline._pipeline_str, branch 2/3) — only the stream's
    # `videorate` element downstream of the tee is affected here.
    config["stream_fps"] = stream_fps
    save_config(config)

    _restart_streaming()

    return {"ok": True, "stream_fps": stream_fps}


@app.post("/stream/restart")
def restart_stream():
    """Tear down and rebuild both camera pipelines without changing any
    settings — recovers a stuck/frozen GStreamer stream or V4L2 device."""
    if session_manager.state == State.RECORDING:
        raise HTTPException(409, "Cannot restart stream while recording")

    _restart_streaming()

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
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=config["api_port"],
        log_level="info",
        timeout_graceful_shutdown=10,  # bound shutdown so teardown always runs before supervisord SIGKILL
    )
