"""Grab a single still frame from the portal's PipeWire stream.

Used as the area selector's background. GNOME Shell paints black behind
fullscreen windows, so a transparent overlay can't show the desktop;
instead we show a frozen frame of the very stream we're about to record
(the same approach Kooha takes).

Keep the grabber alive until the recorder has started: pipewiresrc shares one
PipeWire connection per fd within a process, so overlapping the two pipelines
reuses that connection instead of reconnecting over the same socket.
"""

from typing import Callable, Optional

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gst, GstVideo  # noqa: E402

# Give up on the still and show a plain dimmed overlay after this long.
FRAME_TIMEOUT_MS = 3000

FrameCallback = Callable[[Optional[Gdk.Texture]], None]


class FrameGrabber:
    """Calls on_frame exactly once: with a texture, or None on failure/timeout."""

    def __init__(self, pipewire_fd: int, node_id: int, on_frame: FrameCallback) -> None:
        self._on_frame = on_frame
        self._delivered = False

        # RGBx keeps rows 4-byte aligned and maps directly to a Gdk format.
        self._pipeline = Gst.parse_launch(
            f"pipewiresrc fd={pipewire_fd} path={node_id} "
            "! videoconvert ! video/x-raw,format=RGBx "
            "! appsink name=sink emit-signals=true max-buffers=1 drop=true sync=false"
        )
        self._pipeline.get_by_name("sink").connect("new-sample", self._on_new_sample)
        bus = self._pipeline.get_bus()
        bus.add_signal_watch()
        self._bus_handler = bus.connect("message::error", self._on_error)
        self._timeout_id = GLib.timeout_add(FRAME_TIMEOUT_MS, self._on_timeout)

        if self._pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            GLib.idle_add(self._deliver, None)

    def stop(self) -> None:
        if self._timeout_id:
            GLib.source_remove(self._timeout_id)
            self._timeout_id = 0
        if self._bus_handler:
            bus = self._pipeline.get_bus()
            bus.disconnect(self._bus_handler)
            bus.remove_signal_watch()
            self._bus_handler = 0
        self._pipeline.set_state(Gst.State.NULL)

    # Runs on a GStreamer streaming thread: copy the frame, hand off to main loop.
    def _on_new_sample(self, sink) -> Gst.FlowReturn:
        sample = sink.emit("pull-sample")
        if sample is None or self._delivered:
            return Gst.FlowReturn.OK
        info = GstVideo.VideoInfo.new_from_caps(sample.get_caps())
        buffer = sample.get_buffer()
        pixels = GLib.Bytes.new(buffer.extract_dup(0, buffer.get_size()))
        GLib.idle_add(self._deliver_pixels, info.width, info.height, info.stride[0], pixels)
        return Gst.FlowReturn.OK

    def _deliver_pixels(self, width: int, height: int, stride: int, pixels: GLib.Bytes) -> bool:
        texture = Gdk.MemoryTexture.new(width, height, Gdk.MemoryFormat.R8G8B8X8, pixels, stride)
        return self._deliver(texture)

    def _on_error(self, _bus, message: Gst.Message) -> None:
        err, _debug = message.parse_error()
        print(f"Frame grab failed: {err.message}")
        self._deliver(None)

    def _on_timeout(self) -> bool:
        self._timeout_id = 0
        self._deliver(None)
        return GLib.SOURCE_REMOVE

    def _deliver(self, texture: Optional[Gdk.Texture]) -> bool:
        if not self._delivered:
            self._delivered = True
            if self._timeout_id:
                GLib.source_remove(self._timeout_id)
                self._timeout_id = 0
            if texture is not None:
                # Stop converting frames while the user selects, but stay
                # connected so the recorder can share the PipeWire connection.
                self._pipeline.set_state(Gst.State.PAUSED)
            self._on_frame(texture)
        return GLib.SOURCE_REMOVE
