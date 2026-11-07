"""User preferences, persisted as a small JSON file in the XDG config dir.

GSettings would need a compiled schema installed system-wide; a JSON file
keeps the app runnable straight from the source tree.
"""

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from gi.repository import GLib

from rcrdr import APP_NAME

SOURCE_SELECTION = "selection"
SOURCE_WINDOW = "window"
SOURCE_SCREEN = "screen"

FRAMERATES = (24, 30, 60)
DELAYS_S = (0, 3, 5, 10)


def _default_save_dir() -> str:
    videos = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_VIDEOS)
    return videos or str(Path.home() / "Videos")


def _config_path() -> Path:
    return Path(GLib.get_user_config_dir()) / APP_NAME.lower() / "settings.json"


@dataclass
class Settings:
    source: str = SOURCE_SCREEN
    format_id: str = "mp4"
    framerate: int = 30
    delay_s: int = 0
    show_pointer: bool = True
    record_mic: bool = False
    record_desktop_audio: bool = True
    save_dir: str = ""

    def __post_init__(self) -> None:
        if not self.save_dir:
            self.save_dir = _default_save_dir()

    @classmethod
    def load(cls) -> "Settings":
        try:
            raw = json.loads(_config_path().read_text())
        except (OSError, ValueError):
            return cls()
        known = {f.name for f in fields(cls)}
        # Ignore unknown keys so old/new config files never crash startup.
        return cls(**{k: v for k, v in raw.items() if k in known})

    def save(self) -> None:
        path = _config_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(asdict(self), indent=2))
            os.replace(tmp, path)
        except OSError as err:
            # Losing preferences is not worth interrupting the user over.
            print(f"{APP_NAME}: could not save settings: {err}")
