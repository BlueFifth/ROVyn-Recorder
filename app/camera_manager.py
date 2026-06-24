import csv
import io
import logging
import threading
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("camera_manager")

# GStreamer imports fail gracefully so the API still boots camera-less
# (e.g. Phase 3 testing on a Pi with no cameras attached).
try:
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst, GLib  # noqa: F401
    Gst.init(None)
    GST_AVAILABLE = True
except Exception as e:  # pragma: no cover
    log.warning(f"GStreamer not available: {e}")
    GST_AVAILABLE = False


class CameraPipeline:
    """
    One V4L2 source, one tee, three branches — the design validated standalone:

      1. stream  (always on): downscaled fps -> I420 -> H264 -> RTP -> udpsink
                              (RTSPServer pulls this UDP feed)
      2. csv     (always on): appsink -> per-frame TAI timestamp CSV
                              (rows written only while recording)
      3. record  (on demand): raw YUY2 -> matroskamux -> .mkv, added/removed
                              dynamically off the tee with a pad offset +
                              block-EOS finalize so each clip is valid.

    Notes from hard-won debugging:
    - matroskamux (NOT avimux) — avimux produces header-only files when its
      branch is added to an already-running pipeline.
    - pad offset on the record tee pad — the branch joins a running pipeline,
      so buffers carry a large running-time PTS the muxer can't reconcile.
    - link the tee pad BEFORE sync_state_with_parent() so sticky caps/segment
      events reach the new branch.
    - appsink uses the new-sample SIGNAL (set_callbacks isn't exposed on this
      GStreamer build); requires a running GLib main loop in the process,
      which RTSPServer provides.
    """

    def __init__(self, camera_id, device, width, height,
                 capture_fps, stream_fps, bitrate, jpeg_quality, udp_port):
        self.camera_id = camera_id
        self.device = device
        self.width = width
        self.height = height
        self.capture_fps = capture_fps
        self.stream_fps = stream_fps
        self.bitrate = bitrate
        self.jpeg_quality = jpeg_quality
        self.udp_port = udp_port

        self.pipeline = None
        self.tee = None
        self.appsink = None
        self._rec_bin = None
        self._tee_pad = None
        self._rec_done = None
        self._csv_file = None
        self._csv_writer = None
        self._frame_count = 0
        self._lock = threading.Lock()
        self._latest_jpeg: Optional[bytes] = None
        self._streaming = False

    def _pipeline_str(self) -> str:
        caps = (
            f"video/x-raw,format=YUY2,"
            f"width={self.width},height={self.height},framerate={self.capture_fps}/1"
        )
        return (
            f"v4l2src device={self.device} ! {caps} ! tee name=t "
            # branch 1: stream (always on)
            f"t. ! queue leaky=downstream "
            f"   ! videorate ! video/x-raw,framerate={self.stream_fps}/1 "
            f"   ! videoconvert ! video/x-raw,format=I420 "
            f"   ! x264enc tune=zerolatency bitrate={self.bitrate} "
            f"          speed-preset=veryfast key-int-max=10 "
            f"   ! rtph264pay config-interval=1 pt=96 "
            f"   ! udpsink host=127.0.0.1 port={self.udp_port} sync=false "
            # branch 2: per-frame timestamp appsink (always on)
            f"t. ! queue max-size-buffers=2 leaky=downstream "
            f"   ! appsink name=ts_sink drop=true max-buffers=1"
        )

    def start_streaming(self) -> bool:
        if not GST_AVAILABLE:
            log.warning(f"[{self.camera_id}] GStreamer unavailable — streaming disabled")
            return False

        pipeline_str = self._pipeline_str()
        log.info(f"[{self.camera_id}] pipeline: {pipeline_str}")
        try:
            self.pipeline = Gst.parse_launch(pipeline_str)
        except Exception as e:
            log.error(f"[{self.camera_id}] parse failed: {e}")
            return False

        self.tee = self.pipeline.get_by_name("t")
        self.appsink = self.pipeline.get_by_name("ts_sink")
        self.appsink.set_property("emit-signals", True)
        self.appsink.connect("new-sample", self._on_new_sample)

        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            log.error(f"[{self.camera_id}] pipeline failed to start")
            self.pipeline = None
            return False

        self._streaming = True
        log.info(f"[{self.camera_id}] streaming on UDP :{self.udp_port}")
        return True

    def start_recording(self, session_dir: Path) -> bool:
        if not self._streaming or self.pipeline is None:
            log.error(f"[{self.camera_id}] cannot record — not streaming")
            return False
        if self._rec_bin is not None:
            log.warning(f"[{self.camera_id}] already recording")
            return False

        mkv = str(session_dir / f"{self.camera_id}.mkv")
        csv_path = session_dir / f"{self.camera_id}_frames.csv"

        with self._lock:
            self._csv_file = open(csv_path, "w", newline="")
            self._csv_writer = csv.writer(self._csv_file)
            self._csv_writer.writerow(["frame_index", "gst_pts_ns", "wall_tai_us"])
            self._frame_count = 0

        try:
            self._rec_bin = Gst.parse_bin_from_description(
                "queue max-size-buffers=0 max-size-bytes=0 max-size-time=0 "
                "! matroskamux name=recmux "
                f"! filesink name=recsink location=\"{mkv}\" sync=false",
                True,
            )
            self.pipeline.add(self._rec_bin)
            if hasattr(self.tee, "request_pad_simple"):
                self._tee_pad = self.tee.request_pad_simple("src_%u")
            else:
                self._tee_pad = self.tee.get_request_pad("src_%u")
            self._tee_pad.link(self._rec_bin.get_static_pad("sink"))
            clock = self.pipeline.get_clock()
            if clock:
                running = clock.get_time() - self.pipeline.get_base_time()
                self._tee_pad.set_offset(-running)
            self._rec_bin.sync_state_with_parent()
        except Exception as e:
            log.error(f"[{self.camera_id}] failed to start record branch: {e}")
            self._teardown_rec_bin()
            return False

        log.info(f"[{self.camera_id}] recording -> {mkv}")
        return True

    def stop_recording(self) -> int:
        if self._rec_bin is not None and self._tee_pad is not None:
            self._rec_done = threading.Event()
            self._tee_pad.add_probe(Gst.PadProbeType.IDLE, self._block_eos)
            if not self._rec_done.wait(5.0):
                log.warning(f"[{self.camera_id}] finalize timed out — clip may be incomplete")
            self._teardown_rec_bin()

        with self._lock:
            count = self._frame_count
            if self._csv_file:
                self._csv_file.flush()
                self._csv_file.close()
                self._csv_file = None
                self._csv_writer = None
            self._frame_count = 0

        log.info(f"[{self.camera_id}] recording stopped — {count} frames")
        return count

    def stop_all(self):
        """Full teardown — MUST run before process exit or the V4L2 device
        stays locked and the next start hits 'device busy'."""
        if self.pipeline is None:
            return
        self._streaming = False
        try:
            if self._rec_bin is not None:
                self.stop_recording()
            self.pipeline.send_event(Gst.Event.new_eos())
            bus = self.pipeline.get_bus()
            bus.timed_pop_filtered(3 * Gst.SECOND, Gst.MessageType.EOS)
            self.pipeline.set_state(Gst.State.NULL)  # releases the V4L2 device
            self.pipeline.get_state(Gst.SECOND)      # block until NULL applied
        except Exception as e:
            log.error(f"[{self.camera_id}] teardown error: {e}")
        finally:
            self.pipeline = None
        log.info(f"[{self.camera_id}] pipeline stopped, device released")

    # --- internals ---
    def _block_eos(self, pad, info):
        bin_sink = self._rec_bin.get_static_pad("sink")
        recsink = self._rec_bin.get_by_name("recsink")
        recsink.get_static_pad("sink").add_probe(
            Gst.PadProbeType.EVENT_DOWNSTREAM, self._eos_done
        )
        pad.unlink(bin_sink)
        bin_sink.send_event(Gst.Event.new_eos())
        return Gst.PadProbeReturn.REMOVE

    def _eos_done(self, pad, info):
        if info.get_event().type == Gst.EventType.EOS:
            if self._rec_done:
                self._rec_done.set()
            return Gst.PadProbeReturn.DROP
        return Gst.PadProbeReturn.PASS

    def _teardown_rec_bin(self):
        try:
            if self._tee_pad is not None and self.tee is not None:
                self.tee.release_request_pad(self._tee_pad)
            if self._rec_bin is not None:
                self._rec_bin.set_state(Gst.State.NULL)
                self.pipeline.remove(self._rec_bin)
        except Exception as e:
            log.error(f"[{self.camera_id}] rec bin teardown error: {e}")
        finally:
            self._rec_bin = None
            self._tee_pad = None

    def _on_new_sample(self, appsink):
        sample = appsink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        pts_ns = buf.pts
        tai_us = int(time.clock_gettime(time.CLOCK_TAI) * 1e6)
        with self._lock:
            if self._csv_writer is not None:
                self._csv_writer.writerow([self._frame_count, pts_ns, tai_us])
                self._frame_count += 1
            try:
                ok, mapinfo = buf.map(Gst.MapFlags.READ)
                if ok:
                    self._cache_jpeg(bytes(mapinfo.data))
                    buf.unmap(mapinfo)
            except Exception:
                pass
        return Gst.FlowReturn.OK

    def _cache_jpeg(self, yuyv: bytes):
        try:
            from PIL import Image
            img = Image.frombytes("YCbCr", (self.width, self.height),
                                  yuyv, "raw", "YCBCR", 0, 1)
            out = io.BytesIO()
            img.save(out, format="JPEG", quality=self.jpeg_quality)
            self._latest_jpeg = out.getvalue()
        except Exception as e:
            log.debug(f"[{self.camera_id}] snapshot cache failed: {e}")

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
    """Manages both camera pipelines. Interface preserved for main.py."""

    def __init__(self, config: dict):
        self.config = config
        self.cam0: Optional[CameraPipeline] = None
        self.cam1: Optional[CameraPipeline] = None
        self._cam0_device: Optional[str] = None
        self._cam1_device: Optional[str] = None

    # config helpers (tolerant of older key names)
    def _cfg(self, *keys, default=None):
        for k in keys:
            if k in self.config:
                return self.config[k]
        return default

    def detect_cameras(self) -> dict:
        d0 = self.config["cam0_device"]
        d1 = self.config["cam1_device"]
        self._cam0_device = d0 if Path(d0).exists() else None
        self._cam1_device = d1 if Path(d1).exists() else None
        status = {
            "cam0": {"device": d0, "found": self._cam0_device is not None},
            "cam1": {"device": d1, "found": self._cam1_device is not None},
        }
        log.info(f"Camera detection: {status}")
        return status

    def _make(self, camera_id, device, udp_port) -> CameraPipeline:
        return CameraPipeline(
            camera_id=camera_id,
            device=device,
            width=self._cfg("width", default=320),
            height=self._cfg("height", default=240),
            capture_fps=self._cfg("framerate", "fps", "capture_fps", default=15),
            stream_fps=self._cfg("stream_fps", default=5),
            bitrate=self._cfg("stream_bitrate", default=1000),
            jpeg_quality=self._cfg("stream_jpeg_quality", default=60),
            udp_port=udp_port,
        )

    def start_streaming(self):
        if self._cam0_device:
            self.cam0 = self._make("cam0", self._cam0_device, self.config["rtsp_cam0_udp_port"])
            self.cam0.start_streaming()
        else:
            log.warning("cam0 not found — skipping")
        if self._cam1_device:
            self.cam1 = self._make("cam1", self._cam1_device, self.config["rtsp_cam1_udp_port"])
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

    def get_latest_jpeg(self, cam_id: str) -> Optional[bytes]:
        cam = self.cam0 if cam_id == "cam0" else self.cam1 if cam_id == "cam1" else None
        return cam.get_latest_jpeg() if cam else None

    def status(self) -> dict:
        def one(cam, dev):
            return {
                "device": dev,
                "found": dev is not None,
                "streaming": cam.is_streaming if cam else False,
                "frames": cam.frame_count if cam else 0,
            }
        return {
            "cam0": one(self.cam0, self._cam0_device),
            "cam1": one(self.cam1, self._cam1_device),
        }
