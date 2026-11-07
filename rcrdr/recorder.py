"""GStreamer recording pipeline.

    pipewiresrc ─ videorate ─ videocrop ─ videoconvert ─ encoder ─┐
    pulsesrc (mic) ─────┐                                         ├─ muxer ─ filesink
    pulsesrc (desktop) ─┴─ audiomixer ─ audio encoder ────────────┘

Cropping is always present: for full-screen/window recordings it only trims
an odd trailing pixel, because H.264 requires even frame dimensions.
"""

import os
from dataclasses import dataclass
from typing import Callable

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

from rcrdr.formats import OutputFormat  # noqa: E402

# PipeWire only sends frames on screen damage; resend the last one at least
# this often so videorate can keep a constant frame rate on a static screen.
PIPEWIRE_KEEPALIVE_MS = 1000

# PulseAudio/PipeWire-Pulse alias for "monitor of the current default output".
DESKTOP_AUDIO_DEVICE = "@DEFAULT_MONITOR@"
AUDIO_CAPS = "audio/x-raw,rate=48000,channels=2"

# Encoder threads beyond this give diminishing returns for screen content.
MAX_ENCODER_THREADS = 8

# If the muxer doesn't finalize within this window after Stop, force-close.
EOS_TIMEOUT_S = 5


@dataclass(frozen=True)
class CropRegion:
    """Area to keep, as fractions (0..1) of the captured frame.

    Fractions keep this independent of display scaling: the selector works in
    logical pixels, the stream arrives in physical pixels.
    """

    x: float = 0.0
    y: float = 0.0
    width: float = 1.0
    height: float = 1.0


FULL_FRAME = CropRegion()


@dataclass(frozen=True)
class RecordingConfig:
    pipewire_fd: int
    node_id: int
    output_path: str
    output_format: OutputFormat
    framerate: int
    record_mic: bool
    record_desktop_audio: bool
    crop: CropRegion = FULL_FRAME


def _encoder_threads() -> int:
    return max(1, min(os.cpu_count() or 1, MAX_ENCODER_THREADS))


def describe_video_source(cfg: RecordingConfig) -> str:
    return (
        f"pipewiresrc name=video_src fd={cfg.pipewire_fd} path={cfg.node_id} "
        f"do-timestamp=true keepalive-time={PIPEWIRE_KEEPALIVE_MS} resend-last=true"
    )


def describe_pipeline(cfg: RecordingConfig, video_source: str) -> str:
    threads = _encoder_threads()
    fmt = cfg.output_format
    # The plain video/x-raw caps (no memory:DMABuf feature) make pipewiresrc
    # hand us CPU-mapped frames, which videocrop needs.
    parts = [
        f"{video_source} ! videorate name=rate "
        f"! video/x-raw,framerate={cfg.framerate}/1 "
        f"! videocrop name=crop "
        f"! videoconvert n-threads={threads} chroma-mode=none dither=none "
        f"! video/x-raw,format=I420 ! queue "
        f"! {fmt.video_encoder(threads, cfg.framerate)} ! queue ! mux.",
        f"{fmt.muxer} name=mux ! filesink name=sink",
    ]

    # Device names are set as properties later (see _configure_audio_sources)
    # because "@...@" isn't safe inside gst-launch syntax.
    audio_sources = []
    if cfg.record_mic:
        audio_sources.append("pulsesrc name=mic_src provide-clock=false")
    if cfg.record_desktop_audio:
        audio_sources.append("pulsesrc name=desktop_src provide-clock=false")

    to_encoder = f"{fmt.audio_encoder} ! queue ! mux."
    branch = f"audioconvert ! audioresample ! {AUDIO_CAPS} ! queue"
    if len(audio_sources) == 1:
        parts.append(f"{audio_sources[0]} ! {branch} ! audioconvert ! {to_encoder}")
    elif len(audio_sources) > 1:
        parts.append(f"audiomixer name=amix ! audioconvert ! {to_encoder}")
        parts.extend(f"{src} ! {branch} ! amix." for src in audio_sources)

    return "  ".join(parts)


class Recorder:
    """Owns one pipeline for the lifetime of one recording."""

    def __init__(
        self,
        cfg: RecordingConfig,
        on_finished: Callable[[str], None],
        on_failed: Callable[[str], None],
        video_source: str | None = None,
    ) -> None:
        self._cfg = cfg
        self._on_finished = on_finished
        self._on_failed = on_failed
        self._stopping = False
        self._torn_down = False
        self._eos_timeout_id = 0

        description = describe_pipeline(cfg, video_source or describe_video_source(cfg))
        self._pipeline: Gst.Pipeline = Gst.parse_launch(description)
        self._pipeline.get_by_name("sink").set_property("location", cfg.output_path)
        self._configure_audio_sources()
        self._install_crop_probe()

        bus = self._pipeline.get_bus()
        bus.add_signal_watch()
        self._bus_handler = bus.connect("message", self._on_bus_message)

    def start(self) -> None:
        if self._pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self._fail("Could not start the recording pipeline.")

    def stop(self) -> None:
        """End every stream so the muxer can write a valid file."""
        if self._stopping or self._torn_down:
            return
        self._stopping = True
        # Sources finish their streams (works for the audio sources)...
        self._pipeline.send_event(Gst.Event.new_eos())
        # ...but pipewiresrc can sit blocked waiting for a frame that never
        # comes (static screen, or the share was stopped from GNOME's top
        # bar), so also end the video stream right after it. Any frame it
        # still pushes afterwards is simply refused.
        rate_sink = self._pipeline.get_by_name("rate").get_static_pad("sink")
        # Only once video has flowed: pushing EOS into a stream that never
        # started (no caps yet) crashes videorate.
        if rate_sink.has_current_caps():
            rate_sink.send_event(Gst.Event.new_eos())
        self._eos_timeout_id = GLib.timeout_add_seconds(EOS_TIMEOUT_S, self._on_eos_timeout)

    # ---- setup helpers ------------------------------------------------------

    def _configure_audio_sources(self) -> None:
        desktop = self._pipeline.get_by_name("desktop_src")
        if desktop is not None:
            desktop.set_property("device", DESKTOP_AUDIO_DEVICE)
        # mic_src keeps the default device: the system's selected input.

    def _install_crop_probe(self) -> None:
        """Set crop margins once the real frame size is known.

        The probe sits on the source pad so the values are in place before the
        caps event reaches videocrop.
        """
        crop = self._pipeline.get_by_name("crop")
        src_pad = self._pipeline.get_by_name("video_src").get_static_pad("src")

        def on_event(_pad, info):
            event = info.get_event()
            if event.type == Gst.EventType.CAPS:
                structure = event.parse_caps().get_structure(0)
                ok_w, width = structure.get_int("width")
                ok_h, height = structure.get_int("height")
                if ok_w and ok_h:
                    _apply_crop(crop, self._cfg.crop, width, height)
            return Gst.PadProbeReturn.OK

        src_pad.add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, on_event)

    # ---- bus / teardown -------------------------------------------------------

    def _on_bus_message(self, _bus, message: Gst.Message) -> None:
        if message.type == Gst.MessageType.EOS:
            self._teardown()
            self._on_finished(self._cfg.output_path)
        elif message.type == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            print(f"GStreamer error: {err.message}\n{debug}")
            if message.src.get_name() == "video_src":
                # The screen stream went away (e.g. sharing stopped from the
                # top bar). Keep what was recorded: finish the file normally.
                self.stop()
                return
            self._fail(err.message)

    def _on_eos_timeout(self) -> bool:
        self._eos_timeout_id = 0
        self._fail("The recording did not finish cleanly; the file may be incomplete.")
        return GLib.SOURCE_REMOVE

    def _fail(self, message: str) -> None:
        self._teardown()
        self._on_failed(message)

    def _teardown(self) -> None:
        self._torn_down = True
        if self._eos_timeout_id:
            GLib.source_remove(self._eos_timeout_id)
            self._eos_timeout_id = 0
        bus = self._pipeline.get_bus()
        if self._bus_handler:
            bus.disconnect(self._bus_handler)
            bus.remove_signal_watch()
            self._bus_handler = 0
        self._pipeline.set_state(Gst.State.NULL)


def _apply_crop(crop: Gst.Element, region: CropRegion, width: int, height: int) -> None:
    left = round(region.x * width)
    top = round(region.y * height)
    # Clamp to the frame, then round down to even sizes for the encoder.
    keep_w = min(round(region.width * width), width - left) & ~1
    keep_h = min(round(region.height * height), height - top) & ~1
    crop.set_property("left", left)
    crop.set_property("top", top)
    crop.set_property("right", width - left - keep_w)
    crop.set_property("bottom", height - top - keep_h)
