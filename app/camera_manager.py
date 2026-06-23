import csv
import io
import logging
import threading
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("camera_manager")

# Try importing GStreamer — fails gracefully so the API still starts
# even when running without cameras (Phase 3 testing)
try:
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst, GLib
    Gst.init(None)
    GST_AVAILABLE = True
except Exception as e:
    log.warning(f"GStreamer not available: {e}")
    GST_AVAILABLE = False



class CameraPipeline:
    """
    Manages a single GStreamer pipeline with three branches:
      1. Raw YUYV → AVI file (recording, gated by valve)
      2. appsink → frame timestamp CSV (always active when streaming)
      3. YUYV → MJPEG encode → UDP → RTSP server branch
    """

    def __init__(
        self,
        camera_id: str,       # "cam0" or "cam1"
        device: str,          # e.g. "/dev/video4"
        width: int,
        height: int,
        fps: int,
        jpeg_quality: int,
        udp_port: int,        # local UDP port for RTSP branch
    ):
        self.camera_id = camera_id
        self.device = device
        self.width = width
        self.height = height
        self.fps = fps
        self.jpeg_quality = jpeg_quality
        self.udp_port = udp_port

        self.pipeline = None
        self._valve = None
        self._appsink = None
        self._frame_count = 0
        self._csv_file = None
        self._csv_writer = None
        self._lock = threading.Lock()
        self._latest_jpeg: Optional[bytes] = None
        self._streaming = False

    def _build_pipeline_str(self) -> str:
        caps = (
            f"video/x-raw,format=YUY2,"
            f"width={self.width},height={self.height},"
            f"framerate={self.fps}/1"
        )
        return (
            f"v4l2src device={self.device} "
            f"! {caps} "
            f"! tee name=t "

            # Branch 1: recording to AVI (valve closed until armed)
            f"  t. ! queue max-size-buffers=30 leaky=downstream "
            f"     ! valve name=rec_valve drop=true "
            f"     ! avimux "
            f"     ! filesink name=rec_filesink location=/dev/null sync=false "

            # Branch 2: frame timestamps via appsink
            f"  t. ! queue max-size-buffers=2 leaky=downstream "
            f"     ! appsink name=ts_sink emit-signals=true drop=true max-buffers=1 "

            # Branch 3: MJPEG encode → UDP for RTSP server
            f"  t. ! queue max-size-buffers=5 leaky=downstream "
            f"     ! videoconvert "
            f"     ! jpegenc quality={self.jpeg_quality} "
            f"     ! rtpjpegpay "
            f"     ! udpsink host=127.0.0.1 port={self.udp_port} sync=false"
        )

    def start_streaming(self) -> bool:
        """
        Start the pipeline in streaming-only mode.
        Recording branch valve is closed — AVI writes to /dev/null.
        Called at container startup so RTSP is always available.
        """
        if not GST_AVAILABLE:
            log.warning(f"[{self.camera_id}] GStreamer unavailable — streaming disabled")
            return False

        pipeline_str = self._build_pipeline_str()
        log.info(f"[{self.camera_id}] Starting pipeline: {pipeline_str}")

        try:
            self.pipeline = Gst.parse_launch(pipeline_str)
        except Exception as e:
            log.error(f"[{self.camera_id}] Failed to parse pipeline: {e}")
            return False

        self._valve = self.pipeline.get_by_name("rec_valve")
        self._appsink = self.pipeline.get_by_name("ts_sink")

        if self._appsink:
            self._appsink.connect("new-sample", self._on_new_sample)

        ret = self.pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            log.error(f"[{self.camera_id}] Pipeline failed to start")
            self.pipeline = None
            return False

        self._streaming = True
        log.info(f"[{self.camera_id}] Pipeline streaming on UDP :{self.udp_port}")
        return True

    def start_recording(self, session_dir: Path) -> bool:
        """
        Open AVI file and CSV, then open the valve to start recording.
        The pipeline must already be streaming.
        """
        if not self._streaming or self.pipeline is None:
            log.error(f"[{self.camera_id}] Cannot record — pipeline not streaming")
            return False

        # Redirect filesink to actual output file
        avi_path = session_dir / f"{self.camera_id}.avi"
        csv_path = session_dir / f"{self.camera_id}_frames.csv"

        filesink = self.pipeline.get_by_name("rec_filesink")
        if filesink:
            self.pipeline.set_state(Gst.State.PAUSED)
            filesink.set_property("location", str(avi_path))
            self.pipeline.set_state(Gst.State.PLAYING)

        # Open CSV writer
        self._csv_file = open(csv_path, "w", newline="")
        self._csv_writer = csv.writer(self._csv_file)
        self._csv_writer.writerow(["frame_index", "gst_pts_ns", "wall_tai_us"])
        self._frame_count = 0

        # Open the valve
        if self._valve:
            self._valve.set_property("drop", False)

        log.info(f"[{self.camera_id}] Recording started → {avi_path}")
        return True

    def stop_recording(self) -> int:
        """
        Close the recording valve, flush CSV.
        RTSP stream continues uninterrupted.
        Returns final frame count.
        """
        if self._valve:
            self._valve.set_property("drop", True)

        if self._csv_file:
            self._csv_file.flush()
            self._csv_file.close()
            self._csv_file = None
            self._csv_writer = None

        count = self._frame_count
        self._frame_count = 0
        log.info(f"[{self.camera_id}] Recording stopped — {count} frames")
        return count

    def stop_all(self):
        """Full pipeline teardown. Called on container shutdown."""
        if self.pipeline is None:
            return
        self._streaming = False
        self.pipeline.send_event(Gst.Event.new_eos())
        bus = self.pipeline.get_bus()
        bus.timed_pop_filtered(3 * Gst.SECOND, Gst.MessageType.EOS)
        self.pipeline.set_state(Gst.State.NULL)
        self.pipeline = None
        log.info(f"[{self.camera_id}] Pipeline stopped")

    def _on_new_sample(self, appsink):
        """Called by GStreamer for every frame — runs in GStreamer thread."""
        sample = appsink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK

        buf = sample.get_buffer()
        pts_ns = buf.pts
        wall_tai_us = int(time.clock_gettime(time.CLOCK_TAI) * 1e6)

        with self._lock:
            # Write CSV row if recording
            if self._csv_writer is not None:
                self._csv_writer.writerow([self._frame_count, pts_ns, wall_tai_us])
                self._frame_count += 1

            # Cache latest frame as JPEG for snapshot endpoint
            try:
                ok, map_info = buf.map(Gst.MapFlags.READ)
                if ok:
                    self._cache_jpeg(sample.get_caps(), bytes(map_info.data))
                    buf.unmap(map_info)
            except Exception:
                pass

        return Gst.FlowReturn.OK

    def _cache_jpeg(self, caps, yuyv_data: bytes):
        """Convert latest YUYV frame to JPEG and cache for snapshot endpoint."""
        try:
            from PIL import Image
            # YUYV (YUY2): 2 bytes per pixel
            img = Image.frombytes("YCbCr", (self.width, self.height), yuyv_data, "raw", "YCBCR", 0, 1)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=self.jpeg_quality)
            self._latest_jpeg = buf.getvalue()
        except Exception as e:
            log.debug(f"[{self.camera_id}] Snapshot cache failed: {e}")

    def get_latest_jpeg(self) -> Optional[bytes]:
        with self._lock:
            return self._latest_jpeg

    @property
    def frame_count(self) -> int:
        return self._frame_count

    @property
    def is_streaming(self) -> bool:
        return self._streaming


class CameraManager:
    """Manages both camera pipelines."""

    def __init__(self, config: dict):
        self.config = config
        self.cam0: Optional[CameraPipeline] = None
        self.cam1: Optional[CameraPipeline] = None
        self._cam0_device: Optional[str] = None
        self._cam1_device: Optional[str] = None

    def detect_cameras(self) -> dict:
        """Validate configured device paths exist. Returns status dict."""
        self._cam0_device = self.config["cam0_device"] if Path(self.config["cam0_device"]).exists() else None
        self._cam1_device = self.config["cam1_device"] if Path(self.config["cam1_device"]).exists() else None

        status = {
            "cam0": {"device": self.config["cam0_device"], "found": self._cam0_device is not None},
            "cam1": {"device": self.config["cam1_device"], "found": self._cam1_device is not None},
        }
        log.info(f"Camera detection: {status}")
        return status

    def _make_pipeline(self, camera_id: str, device: str, udp_port: int) -> CameraPipeline:
        return CameraPipeline(
            camera_id=camera_id,
            device=device,
            width=self.config["width"],
            height=self.config["height"],
            fps=self.config["framerate"],
            jpeg_quality=self.config["stream_jpeg_quality"],
            udp_port=udp_port,
        )

    def start_streaming(self):
        """Start both pipelines in streaming-only mode."""
        if self._cam0_device:
            self.cam0 = self._make_pipeline("cam0", self._cam0_device, self.config["rtsp_cam0_udp_port"])
            self.cam0.start_streaming()
        else:
            log.warning("cam0 not found — skipping")

        if self._cam1_device:
            self.cam1 = self._make_pipeline("cam1", self._cam1_device, self.config["rtsp_cam1_udp_port"])
            self.cam1.start_streaming()
        else:
            log.warning("cam1 not found — skipping")

    def start_recording(self, session_dir: Path):
        if self.cam0:
            self.cam0.start_recording(session_dir)
        if self.cam1:
            self.cam1.start_recording(session_dir)

    def stop_recording(self) -> dict:
        counts = {}
        if self.cam0:
            counts["cam0"] = self.cam0.stop_recording()
        if self.cam1:
            counts["cam1"] = self.cam1.stop_recording()
        return counts

    def stop_all(self):
        if self.cam0:
            self.cam0.stop_all()
        if self.cam1:
            self.cam1.stop_all()

    def status(self) -> dict:
        return {
            "cam0": {
                "device": self._cam0_device,
                "found": self._cam0_device is not None,
                "streaming": self.cam0.is_streaming if self.cam0 else False,
                "frames": self.cam0.frame_count if self.cam0 else 0,
            },
            "cam1": {
                "device": self._cam1_device,
                "found": self._cam1_device is not None,
                "streaming": self.cam1.is_streaming if self.cam1 else False,
                "frames": self.cam1.frame_count if self.cam1 else 0,
            },
        }
