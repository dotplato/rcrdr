import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
from gi.repository import Adw, Gio, Gst  # noqa: E402

from rcrdr import APP_ID, APP_NAME, VERSION  # noqa: E402
from rcrdr.window import MainWindow  # noqa: E402

ACCELS = {
    "win.record": ["<Control>r"],
    "win.open-folder": ["<Control>o"],
    "app.quit": ["<Control>q"],
}


class Application(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self._add_action("about", self._on_about)
        self._add_action("quit", lambda *_: self._quit())
        for action, accels in ACCELS.items():
            self.set_accels_for_action(action, accels)

    def do_activate(self) -> None:
        # Single instance: re-launching just raises the existing window.
        window = self.get_active_window() or MainWindow(self)
        window.present()

    def _add_action(self, name: str, callback) -> None:
        action = Gio.SimpleAction.new(name, None)
        action.connect("activate", callback)
        self.add_action(action)

    def _quit(self) -> None:
        # Route through close-request so an active recording is finalized.
        window = self.get_active_window()
        if window is not None:
            window.close()
        else:
            self.quit()

    def _on_about(self, *_args) -> None:
        Adw.AboutDialog(
            application_name=APP_NAME,
            application_icon="media-record-symbolic",
            version=VERSION,
            comments="A lightweight screen recorder.",
        ).present(self.get_active_window())


def main() -> int:
    Gst.init(None)
    return Application().run(sys.argv)
