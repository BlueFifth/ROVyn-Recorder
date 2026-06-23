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
    gi.require_version("GstApp", "1.0")
    from gi.repository import Gst, GLib, GstApp  # noqa: F401  (registers AppSink type)
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
        self._appsink = None
        self._rec_bin = None        # dynamic recording branch (queue ! avimux ! filesink)
        self._tee_recpad = None     # requested tee src pad feeding the record branch
        self._rec_done = None       # threading.Event set when AVI is finalized
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

            # Branch 1 (recording) is added/removed dynamically at arm time.

            # Branch 2: frame timestamps via appsink
            f"  t. ! queue max-size-buffers=2 leaky=downstream "
            f"     ! appsink name=ts_sink emit-signals=false drop=true max-buffers=1 "

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

        self._appsink = self.pipeline.get_by_name("ts_sink")

        if self._appsink:
            # set_callbacks dispatches on the GStreamer streaming thread, so it
            # works without a running GLib main loop (unlike emit-signals).
            self._appsink.set_callbacks(None, None, self._on_new_sample, None)

        ret = self.pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            log.error(f"[{self.camera_id}] Pipeline failed to start")
            self.pipeline = None
            return False

        self._streaming = True
        log.info(f"[{self.camera_id}] Pipeline streaming on UDP :{self.udp_port}")
        return True

    def _request_tee_pad(self, tee):
        """Request a new src pad from the tee (1.20+ vs older API)."""
        if hasattr(tee, "request_pad_simple"):
            return tee.request_pad_simple("src_%u")
        return tee.get_request_pad("src_%u")

    def start_recording(self, session_dir: Path) -> bool:
        """
        Add a fresh AVI record branch (queue ! avimux ! filesink) to the live
        pipeline and link it to the tee. A new branch per session means avimux
        always writes a clean header; the streaming/CSV branches are untouched.
        """
        if not self._streaming or self.pipeline is None:
            log.error(f"[{self.camera_id}] Cannot record — pipeline not streaming")
            return False
        if self._rec_bin is not None:
            log.warning(f"[{self.camera_id}] Already recording")
            return False

        avi_path = session_dir / f"{self.camera_id}.avi"
        csv_path = session_dir / f"{self.camera_id}_frames.csv"

        # CSV writer (frames are logged from _on_new_sample while this is set)
        self._csv_file = open(csv_path, "w", newline="")
        self._csv_writer = csv.writer(self._csv_file)
        self._csv_writer.writerow(["frame_index", "gst_pts_ns", "wall_tai_us"])
        self._frame_count = 0

        # Build and attach the record branch
        desc = (
            "queue name=recq max-size-buffers=0 max-size-bytes=0 max-size-time=0 "
            "! avimux name=recmux "
            f"! filesink name=recsink location=\"{avi_path}\" sync=false"
        )
        try:
            self._rec_bin = Gst.parse_bin_from_description(desc, True)
            self.pipeline.add(self._rec_bin)
            tee = self.pipeline.get_by_name("t")
            self._tee_recpad = self._request_tee_pad(tee)
            self._tee_recpad.link(self._rec_bin.get_static_pad("sink"))
            self._rec_bin.sync_state_with_parent()
        except Exception as e:
            log.error(f"[{self.camera_id}] Failed to start record branch: {e}")
            self._teardown_rec_bin()
            return False

        log.info(f"[{self.camera_id}] Recording started → {avi_path}")
        return True

    def stop_recording(self) -> int:
        """
        Finalize the AVI by sending EOS through the record branch (so avimux
        writes its index), then unlink/remove it. RTSP + CSV branches continue.
        Returns final frame count.
        """
        count = self._frame_count

        if self._rec_bin is not None and self._tee_recpad is not None:
            self._rec_done = threading.Event()
            # IDLE probe fires on the streaming thread when it's safe to unlink
            self._tee_recpad.add_probe(Gst.PadProbeType.IDLE, self._block_and_eos)
            if not self._rec_done.wait(timeout=5.0):
                log.warning(f"[{self.camera_id}] AVI finalize timed out — file may be incomplete")
            self._teardown_rec_bin()

        if self._csv_file:
            self._csv_file.flush()
            self._csv_file.close()
            self._csv_file = None
            self._csv_writer = None

        self._frame_count = 0
        log.info(f"[{self.camera_id}] Recording stopped — {count} frames")
        return count

    def _block_and_eos(self, pad, info):
        """Unlink the record branch from the tee and inject EOS to finalize it."""
        bin_sink = self._rec_bin.get_static_pad("sink")
        # Detect when EOS has passed the filesink → avimux has flushed its index
        recsink = self._rec_bin.get_by_name("recsink")
        recsink.get_static_pad("sink").add_probe(
            Gst.PadProbeType.EVENT_DOWNSTREAM, self._on_eos_at_filesink
        )
        pad.unlink(bin_sink)
        bin_sink.send_event(Gst.Event.new_eos())
        return Gst.PadProbeReturn.REMOVE

    def _on_eos_at_filesink(self, pad, info):
        if info.get_event().type == Gst.EventType.EOS:
            if self._rec_done:
                self._rec_done.set()
            return Gst.PadProbeReturn.DROP
        return Gst.PadProbeReturn.PASS

    def _teardown_rec_bin(self):
        """Release the tee pad and remove/destroy the record branch."""
        try:
            if self._tee_recpad is not None:
                tee = self.pipeline.get_by_name("t")
                if tee:
                    tee.release_request_pad(self._tee_recpad)
            if self._rec_bin is not None:
                self._rec_bin.set_state(Gst.State.NULL)
                self.pipeline.remove(self._rec_bin)
        except Exception as e:
            log.error(f"[{self.camera_id}] Record branch teardown error: {e}")
        finally:
            self._rec_bin = None
            self._tee_recpad = None

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

    def _on_new_sample(self, appsink, _user_data=None):
        """Called by GStreamer for every frame — runs on the streaming thread."""
        sample = appsink.try_pull_sample(0)
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
