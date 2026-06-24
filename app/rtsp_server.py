import logging
import threading

log = logging.getLogger("rtsp_server")

try:
    import gi
    gi.require_version("Gst", "1.0")
    gi.require_version("GstRtspServer", "1.0")
    from gi.repository import Gst, GstRtspServer, GLib
    GST_RTSP_AVAILABLE = True
except Exception as e:
    log.warning(f"GstRtspServer not available: {e}")
    GST_RTSP_AVAILABLE = False


class RTSPServer:
    """
    Hosts two RTSP mounts fed by UDP from the GStreamer pipelines:
      rtsp://192.168.2.2:8554/cam0  — StellarHD Port
      rtsp://192.168.2.2:8554/cam1  — StellarHD Starboard

    Runs GLib main loop in a daemon thread.
    Streams are always live (even when not recording) for positioning use.
    """

    def __init__(self, port: int = 8554):
        self.port = port
        self._server = None
        self._loop = None
        self._thread = None

    def add_stream(self, mount: str, udp_port: int):
        """Register an RTSP mount point pulling from a local UDP source."""
        if not GST_RTSP_AVAILABLE or self._server is None:
            return

        factory = GstRtspServer.RTSPMediaFactory()
        pipeline_str = (
            f"( udpsrc port={udp_port} "
            f"caps=\"application/x-rtp,media=video,clock-rate=90000,encoding-name=H264,payload=96\" "
            f"! rtph264depay ! rtph264pay name=pay0 pt=96 config-interval=1 )"
        )
        factory.set_launch(pipeline_str)
        factory.set_shared(True)  # One pipeline serves all connected clients

        mounts = self._server.get_mount_points()
        mounts.add_factory(mount, factory)
        log.info(f"RTSP mount registered: rtsp://0.0.0.0:{self.port}{mount} ← UDP :{udp_port}")

    def start(self, cam0_udp_port: int, cam1_udp_port: int):
        """Start the RTSP server and GLib loop in a background daemon thread."""
        if not GST_RTSP_AVAILABLE:
            log.warning("RTSP server disabled — GstRtspServer unavailable")
            return

        self._server = GstRtspServer.RTSPServer()
        self._server.set_service(str(self.port))

        self.add_stream("/cam0", cam0_udp_port)
        self.add_stream("/cam1", cam1_udp_port)

        self._server.attach(None)

        self._loop = GLib.MainLoop()
        self._thread = threading.Thread(target=self._loop.run, daemon=True, name="glib-rtsp-loop")
        self._thread.start()
        log.info(f"RTSP server running on port {self.port}")

    def stop(self):
        if self._loop and self._loop.is_running():
            self._loop.quit()
        log.info("RTSP server stopped")
