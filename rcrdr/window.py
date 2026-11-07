"""Main window: settings form, countdown, and recording status.

Recording flow:
    Record -> portal picker -> [area selector] -> [countdown] -> recording -> saved
Each step can be cancelled; _reset() always returns to the settings page and
releases the portal session.
"""

import os
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from gi.repository import Adw, Gio, GLib, Gtk

from rcrdr import APP_NAME
from rcrdr.area_selector import AreaSelector, find_monitor
from rcrdr.formats import available_formats
from rcrdr.frame_grabber import FrameGrabber
from rcrdr.portal import CursorMode, ScreenCastSession, SourceType
from rcrdr.recorder import FULL_FRAME, CropRegion, Recorder, RecordingConfig
from rcrdr.settings import (
    DELAYS_S,
    FRAMERATES,
    SOURCE_SCREEN,
    SOURCE_SELECTION,
    SOURCE_WINDOW,
    Settings,
)

PAGE_SETUP = "setup"
PAGE_COUNTDOWN = "countdown"
PAGE_RECORDING = "recording"

# Give the compositor time to remove the area overlay before capture begins.
OVERLAY_SETTLE_MS = 250
ELAPSED_REFRESH_MS = 500
QUIT_POLL_MS = 100
RECORDING_TITLE = f"● Recording — {APP_NAME}"
WINDOW_WIDTH = 380


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, application: Adw.Application) -> None:
        super().__init__(application=application, title=APP_NAME, resizable=False)
        self.set_default_size(WINDOW_WIDTH, -1)

        self._settings = Settings.load()
        self._formats = available_formats()
        self._session: Optional[ScreenCastSession] = None
        self._recorder: Optional[Recorder] = None
        # (pipewire fd, node id) for the current attempt. We own the fd until
        # _reset(): pipewiresrc dups it rather than taking ownership.
        self._stream: Optional[tuple[int, int]] = None
        self._grabber: Optional[FrameGrabber] = None
        self._crop: CropRegion = FULL_FRAME
        self._countdown_left = 0
        self._countdown_id = 0
        self._elapsed_id = 0
        self._started_at = 0.0
        # True while this window is hidden so it isn't in the way of (or
        # recorded in) an area selection.
        self._hidden_for_capture = False
        self._quitting = False

        self._toasts = Adw.ToastOverlay()
        self._stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self._stack.add_named(self._build_setup_page(), PAGE_SETUP)
        self._stack.add_named(self._build_countdown_page(), PAGE_COUNTDOWN)
        self._stack.add_named(self._build_recording_page(), PAGE_RECORDING)
        self._toasts.set_child(self._stack)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(self._build_header())
        toolbar.set_content(self._toasts)
        self.set_content(toolbar)

        self.connect("close-request", self._on_close_request)

    # ---- UI construction ------------------------------------------------------

    def _build_header(self) -> Adw.HeaderBar:
        menu = Gio.Menu()
        menu.append("Open Recordings Folder", "win.open-folder")
        menu.append(f"About {APP_NAME}", "app.about")

        open_folder = Gio.SimpleAction.new("open-folder", None)
        open_folder.connect("activate", lambda *_: self._open_save_dir())
        self.add_action(open_folder)

        record = Gio.SimpleAction.new("record", None)
        record.connect("activate", lambda *_: self.toggle_recording())
        self.add_action(record)

        header = Adw.HeaderBar()
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu,
                                       tooltip_text="Main Menu"))
        return header

    def _build_setup_page(self) -> Gtk.Widget:
        s = self._settings

        self._source_group = Adw.ToggleGroup(margin_bottom=6)
        for name, label in ((SOURCE_SELECTION, "Selection"), (SOURCE_WINDOW, "Window"),
                            (SOURCE_SCREEN, "Screen")):
            self._source_group.add(Adw.Toggle(name=name, label=label))
        self._source_group.set_active_name(s.source)
        self._source_group.connect("notify::active-name", self._on_setting_changed)

        self._format_row = Adw.ComboRow(
            title="Format",
            model=Gtk.StringList.new([f.label for f in self._formats]),
        )
        self._format_row.set_selected(self._index_of_format(s.format_id))

        self._fps_row = Adw.ComboRow(
            title="Frame Rate",
            model=Gtk.StringList.new([f"{fps} fps" for fps in FRAMERATES]),
        )
        self._fps_row.set_selected(_index_or_zero(FRAMERATES, s.framerate))

        self._pointer_row = Adw.SwitchRow(title="Show Pointer", active=s.show_pointer)

        self._mic_row = Adw.SwitchRow(title="Microphone", active=s.record_mic)
        self._desktop_row = Adw.SwitchRow(title="Desktop Audio",
                                          active=s.record_desktop_audio)

        self._delay_row = Adw.ComboRow(
            title="Delay",
            model=Gtk.StringList.new(["None" if d == 0 else f"{d} seconds" for d in DELAYS_S]),
        )
        self._delay_row.set_selected(_index_or_zero(DELAYS_S, s.delay_s))

        self._folder_row = Adw.ActionRow(title="Save To", subtitle=_pretty_path(s.save_dir),
                                         activatable=True)
        self._folder_row.add_suffix(Gtk.Image(icon_name="folder-open-symbolic"))
        self._folder_row.connect("activated", self._on_choose_folder)

        for row in (self._format_row, self._fps_row, self._delay_row):
            row.connect("notify::selected", self._on_setting_changed)
        for row in (self._pointer_row, self._mic_row, self._desktop_row):
            row.connect("notify::active", self._on_setting_changed)

        video = Adw.PreferencesGroup(title="Video")
        for row in (self._format_row, self._fps_row, self._pointer_row):
            video.add(row)
        audio = Adw.PreferencesGroup(title="Audio")
        audio.add(self._mic_row)
        audio.add(self._desktop_row)
        general = Adw.PreferencesGroup(title="General")
        general.add(self._delay_row)
        general.add(self._folder_row)

        self._record_button = Gtk.Button(label="Start Recording", halign=Gtk.Align.CENTER,
                                         margin_top=6, action_name="win.record")
        self._record_button.add_css_class("suggested-action")
        self._record_button.add_css_class("pill")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                      margin_top=12, margin_bottom=24, margin_start=18, margin_end=18)
        for child in (self._source_group, video, audio, general, self._record_button):
            box.append(child)

        if not self._formats:
            self._record_button.set_sensitive(False)
            self._format_row.set_subtitle("No encoders found — install GStreamer plugins")
        return box

    def _build_countdown_page(self) -> Gtk.Widget:
        self._countdown_label = Gtk.Label()
        self._countdown_label.add_css_class("title-1")
        self._countdown_label.add_css_class("numeric")

        cancel = Gtk.Button(label="Cancel", halign=Gtk.Align.CENTER)
        cancel.add_css_class("pill")
        cancel.connect("clicked", lambda *_: self._reset())

        page = Adw.StatusPage(title="Recording starts in…", child=_vbox(self._countdown_label,
                                                                      cancel))
        return page

    def _build_recording_page(self) -> Gtk.Widget:
        self._elapsed_label = Gtk.Label(label="00:00")
        self._elapsed_label.add_css_class("title-1")
        self._elapsed_label.add_css_class("numeric")

        self._stop_button = Gtk.Button(label="Stop Recording", halign=Gtk.Align.CENTER,
                                       action_name="win.record")
        self._stop_button.add_css_class("destructive-action")
        self._stop_button.add_css_class("pill")

        page = Adw.StatusPage(icon_name="media-record-symbolic", title="Recording",
                              child=_vbox(self._elapsed_label, self._stop_button))
        page.add_css_class("compact")
        return page

    # ---- settings ---------------------------------------------------------------

    def _index_of_format(self, format_id: str) -> int:
        ids = [f.id for f in self._formats]
        return ids.index(format_id) if format_id in ids else 0

    def _on_setting_changed(self, *_args) -> None:
        s = self._settings
        s.source = self._source_group.get_active_name() or SOURCE_SCREEN
        if self._formats:
            s.format_id = self._formats[self._format_row.get_selected()].id
        s.framerate = FRAMERATES[self._fps_row.get_selected()]
        s.delay_s = DELAYS_S[self._delay_row.get_selected()]
        s.show_pointer = self._pointer_row.get_active()
        s.record_mic = self._mic_row.get_active()
        s.record_desktop_audio = self._desktop_row.get_active()
        s.save()

    def _on_choose_folder(self, _row) -> None:
        dialog = Gtk.FileDialog(title="Save Recordings To",
                                initial_folder=Gio.File.new_for_path(self._settings.save_dir))

        def on_done(dlg, result):
            try:
                folder = dlg.select_folder_finish(result)
            except GLib.Error:
                return  # Dismissed.
            self._settings.save_dir = folder.get_path()
            self._folder_row.set_subtitle(_pretty_path(self._settings.save_dir))
            self._settings.save()

        dialog.select_folder(self, None, on_done)

    # ---- recording flow -----------------------------------------------------------

    def toggle_recording(self) -> None:
        """Bound to the Record/Stop buttons and Ctrl+R."""
        page = self._stack.get_visible_child_name()
        if page == PAGE_RECORDING:
            self._stop_recording()
        elif page == PAGE_COUNTDOWN:
            self._reset()
        elif self._session is None and self._formats:
            self._request_screen()

    def _request_screen(self) -> None:
        s = self._settings
        source = SourceType.WINDOW if s.source == SOURCE_WINDOW else SourceType.MONITOR
        cursor = CursorMode.EMBEDDED if s.show_pointer else CursorMode.HIDDEN
        self._record_button.set_sensitive(False)
        if s.source == SOURCE_SELECTION:
            # Hide before the portal picker so we're gone from the still frame
            # and from the recorded area. Restored in _reset(); meanwhile the
            # user stops via GNOME's sharing indicator or by relaunching us.
            self._hidden_for_capture = True
            self.set_visible(False)
        self._session = ScreenCastSession()
        self._session.start(source, cursor, self._on_portal_started, self._on_portal_failed,
                            on_closed=self._on_session_closed)

    def _on_portal_started(self, fd: int, node_id: int, props: dict) -> None:
        self._stream = (fd, node_id)
        if self._settings.source != SOURCE_SELECTION:
            self._crop = FULL_FRAME
            self._begin_countdown()
            return
        monitor = find_monitor(props)

        def show_selector(background) -> None:
            if self._stream is None:
                return  # Cancelled (e.g. sharing stopped) while grabbing.
            AreaSelector(self.get_application(), monitor, background,
                         on_selected=self._on_area_selected,
                         on_cancelled=self._reset).present()

        self._grabber = FrameGrabber(fd, node_id, show_selector)

    def _on_portal_failed(self, message: Optional[str]) -> None:
        self._reset()
        if message:  # None means the user cancelled the picker.
            self._toast(message)

    def _on_area_selected(self, region: CropRegion) -> None:
        self._crop = region
        if self._hidden_for_capture:
            # Come back minimized: findable in the dock / Alt+Tab to press
            # Stop, but not covering (or recorded in) the selected area.
            # GTK applies the minimize before the window is first shown.
            self.minimize()
            self.set_visible(True)
        GLib.timeout_add(OVERLAY_SETTLE_MS, self._begin_countdown_once)

    def _begin_countdown(self) -> None:
        self._countdown_left = self._settings.delay_s
        if self._countdown_left <= 0:
            self._start_recording()
            return
        self._countdown_label.set_label(str(self._countdown_left))
        self._stack.set_visible_child_name(PAGE_COUNTDOWN)
        self._countdown_id = GLib.timeout_add_seconds(1, self._on_countdown_tick)

    def _on_countdown_tick(self) -> bool:
        self._countdown_left -= 1
        if self._countdown_left > 0:
            self._countdown_label.set_label(str(self._countdown_left))
            return GLib.SOURCE_CONTINUE
        self._countdown_id = 0
        self._start_recording()
        return GLib.SOURCE_REMOVE

    def _begin_countdown_once(self) -> bool:
        self._begin_countdown()
        return GLib.SOURCE_REMOVE

    def _start_recording(self) -> None:
        if self._stream is None or self._recorder is not None:
            return
        fd, node_id = self._stream
        s = self._settings
        fmt = self._formats[self._index_of_format(s.format_id)]

        try:
            Path(s.save_dir).mkdir(parents=True, exist_ok=True)
        except OSError as err:
            self._reset()
            self._toast(f"Cannot write to {s.save_dir}: {err.strerror}")
            return

        filename = f"{APP_NAME} {datetime.now():%Y-%m-%d %H-%M-%S}.{fmt.extension}"
        cfg = RecordingConfig(
            pipewire_fd=fd, node_id=node_id, output_path=str(Path(s.save_dir) / filename),
            output_format=fmt, framerate=s.framerate, record_mic=s.record_mic,
            record_desktop_audio=s.record_desktop_audio, crop=self._crop,
        )
        try:
            self._recorder = Recorder(cfg, self._on_recording_finished,
                                      self._on_recording_failed)
        except GLib.Error as err:
            self._reset()
            self._toast(f"Could not build the recording pipeline: {err.message}")
            return

        self._stack.set_visible_child_name(PAGE_RECORDING)
        # Shown in Alt+Tab / the overview while the window is minimized.
        self.set_title(RECORDING_TITLE)
        self._stop_button.set_sensitive(True)
        self._started_at = time.monotonic()
        self._elapsed_label.set_label("00:00")
        self._elapsed_id = GLib.timeout_add(ELAPSED_REFRESH_MS, self._on_elapsed_tick)
        self._recorder.start()
        # The recorder now holds the shared PipeWire connection; the still
        # frame pipeline can go.
        self._release_grabber()

    def _release_grabber(self) -> None:
        if self._grabber is not None:
            grabber, self._grabber = self._grabber, None
            grabber.stop()

    def _on_elapsed_tick(self) -> bool:
        self._elapsed_label.set_label(_format_elapsed(time.monotonic() - self._started_at))
        return GLib.SOURCE_CONTINUE

    def _stop_recording(self) -> None:
        if self._recorder is None:
            return
        self._stop_button.set_sensitive(False)
        self._stop_button.set_label("Saving…")
        self._recorder.stop()

    def _on_session_closed(self) -> None:
        """User stopped sharing from GNOME's top-bar indicator."""
        if self._recorder is not None:
            self._stop_recording()
        else:
            self._reset()

    def _on_recording_finished(self, path: str) -> None:
        self._recorder = None
        self._reset()
        toast = Adw.Toast(title=f"Saved {Path(path).name}", button_label="Open", timeout=6)
        toast.connect("button-clicked", lambda *_: _launch(Gio.File.new_for_path(path), self))
        self._toasts.add_toast(toast)

    def _on_recording_failed(self, message: str) -> None:
        self._recorder = None
        self._reset()
        self._toast(f"Recording failed: {message}")

    def _reset(self) -> None:
        """Return to the setup page, releasing every resource of the attempt."""
        for source_id in (self._countdown_id, self._elapsed_id):
            if source_id:
                GLib.source_remove(source_id)
        self._countdown_id = self._elapsed_id = 0
        self._release_grabber()
        if self._recorder is not None:
            recorder, self._recorder = self._recorder, None
            recorder.stop()  # No-op if already stopping/finished.
        if self._stream is not None:
            os.close(self._stream[0])
            self._stream = None
        if self._session is not None:
            session, self._session = self._session, None
            session.close()
        self._stop_button.set_label("Stop Recording")
        self._record_button.set_sensitive(bool(self._formats))
        self._stack.set_visible_child_name(PAGE_SETUP)
        self.set_title(APP_NAME)
        if self._hidden_for_capture and not self._quitting:
            self._hidden_for_capture = False
            self.present()

    # ---- misc ---------------------------------------------------------------------

    def _on_close_request(self, _window) -> bool:
        if self._recorder is None:
            self._reset()
            return False
        # Finish the file properly, then quit.
        self._quitting = True
        self._stop_recording()
        self.set_visible(False)
        GLib.timeout_add(QUIT_POLL_MS, self._quit_when_idle)
        return True

    def _quit_when_idle(self) -> bool:
        if self._recorder is not None:
            return GLib.SOURCE_CONTINUE
        self.get_application().quit()
        return GLib.SOURCE_REMOVE

    def _open_save_dir(self) -> None:
        _launch(Gio.File.new_for_path(self._settings.save_dir), self)

    def _toast(self, message: str) -> None:
        self._toasts.add_toast(Adw.Toast(title=message, timeout=6))


# ---- helpers -------------------------------------------------------------------------


def _vbox(*children: Gtk.Widget) -> Gtk.Box:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
    for child in children:
        box.append(child)
    return box


def _index_or_zero(values: tuple, value) -> int:
    return values.index(value) if value in values else 0


def _format_elapsed(seconds: float) -> str:
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02}:{secs:02}" if hours else f"{minutes:02}:{secs:02}"


def _pretty_path(path: str) -> str:
    home = str(Path.home())
    return "~" + path[len(home):] if path.startswith(home) else path


def _launch(file: Gio.File, parent: Gtk.Window) -> None:
    Gtk.FileLauncher.new(file).launch(parent, None, None)
