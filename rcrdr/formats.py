"""Output formats and the GStreamer encoder fragments that produce them.

Each format is a (video encoder, audio encoder, muxer) triple expressed as
gst-launch syntax so the full pipeline stays readable in one place.
"""

from dataclasses import dataclass
from typing import Callable

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

# Seconds between keyframes. Keeps seeking snappy without bloating files.
KEYFRAME_INTERVAL_S = 2

# x264: "superfast" keeps CPU usage low; quantizer 23 is visually near-lossless
# for screen content.
X264_PRESET = "superfast"
X264_QUANTIZER = 23

# VP8 realtime tuning (same approach Kooha uses): fastest preset, and a hard
# quantizer ceiling so text stays sharp regardless of the bitrate target.
VP8_CPU_USED = 16
VP8_CQ_LEVEL = 13
VP8_MAX_QUANTIZER = 17
VP8_STATIC_THRESHOLD = 100
VP8_BUFFER_SIZE_MS = 20000

AAC_BITRATE = 160_000
OPUS_BITRATE = 128_000


def _h264(threads: int, fps: int) -> str:
    return (
        f"x264enc speed-preset={X264_PRESET} pass=qual quantizer={X264_QUANTIZER} "
        f"key-int-max={fps * KEYFRAME_INTERVAL_S} threads={threads} "
        "! video/x-h264,profile=high"
    )


def _vp8(threads: int, fps: int) -> str:
    return (
        f"vp8enc deadline=1 cpu-used={VP8_CPU_USED} cq-level={VP8_CQ_LEVEL} "
        f"max-quantizer={VP8_MAX_QUANTIZER} static-threshold={VP8_STATIC_THRESHOLD} "
        f"buffer-size={VP8_BUFFER_SIZE_MS} keyframe-max-dist={fps * KEYFRAME_INTERVAL_S} "
        f"threads={threads}"
    )


@dataclass(frozen=True)
class OutputFormat:
    id: str
    label: str
    extension: str
    video_encoder: Callable[[int, int], str]
    audio_encoder: str
    muxer: str
    # GStreamer element factories this format needs; used to hide formats
    # whose plugins aren't installed instead of failing at record time.
    required_elements: tuple[str, ...]

    def is_available(self) -> bool:
        return all(Gst.ElementFactory.find(name) for name in self.required_elements)


ALL_FORMATS: tuple[OutputFormat, ...] = (
    OutputFormat(
        id="mp4",
        label="MP4 (H.264 + AAC)",
        extension="mp4",
        video_encoder=_h264,
        audio_encoder=f"avenc_aac bitrate={AAC_BITRATE}",
        muxer="mp4mux",
        required_elements=("x264enc", "avenc_aac", "mp4mux"),
    ),
    OutputFormat(
        id="mkv",
        label="MKV (H.264 + Opus)",
        extension="mkv",
        video_encoder=_h264,
        audio_encoder=f"opusenc bitrate={OPUS_BITRATE}",
        muxer="matroskamux",
        required_elements=("x264enc", "opusenc", "matroskamux"),
    ),
    OutputFormat(
        id="webm",
        label="WebM (VP8 + Opus)",
        extension="webm",
        video_encoder=_vp8,
        audio_encoder=f"opusenc bitrate={OPUS_BITRATE}",
        muxer="webmmux",
        required_elements=("vp8enc", "opusenc", "webmmux"),
    ),
)


def available_formats() -> list[OutputFormat]:
    return [fmt for fmt in ALL_FORMATS if fmt.is_available()]
