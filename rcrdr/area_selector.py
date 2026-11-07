"""Full-screen overlay for dragging out a recording area.

Shown *after* the portal grants a monitor stream, on that same monitor; the
chosen rectangle is converted to a CropRegion (fractions of the monitor).

GNOME Shell paints black behind fullscreen windows, so the overlay can't be
see-through. Instead it shows a still frame of the stream (see
frame_grabber.py) under the selection shade.
"""

from typing import Callable, Optional

from gi.repository import Gdk, Graphene, Gsk, Gtk

from rcrdr.recorder import CropRegion

CSS_CLASS = "rcrdr-area-selector"
CSS = f"window.{CSS_CLASS} {{ background: none; }}"

# Overlay styling. Drawn via Gtk.Snapshot, so these can't be CSS tokens.
SHADE_RGBA = (0.0, 0.0, 0.0, 0.45)
BORDER_RGBA = (1.0, 1.0, 1.0, 0.9)
BORDER_WIDTH = 2.0


def _rgba(values: tuple[float, float, float, float]) -> Gdk.RGBA:
    color = Gdk.RGBA()
    color.red, color.green, color.blue, color.alpha = values
    return color


class _SelectionCanvas(Gtk.Widget):
    """Dims everything except the current selection rectangle.

    Uses Gtk.Snapshot rather than cairo: no python3-gi-cairo needed, and the
    shade is rendered on the GPU.
    """

    def __init__(self) -> None:
        super().__init__(hexpand=True, vexpand=True)
        self.rect: Optional[tuple[float, float, float, float]] = None
        self._shade = _rgba(SHADE_RGBA)
        self._border = _rgba(BORDER_RGBA)

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        width, height = self.get_width(), self.get_height()
        if not self.rect:
            snapshot.append_color(self._shade, Graphene.Rect().init(0, 0, width, height))
            return
        x, y, w, h = self.rect
        # Shade the four bands around the selection, leaving it clear.
        for band in (
            (0, 0, width, y),                    # above
            (0, y + h, width, height - y - h),   # below
            (0, y, x, h),                        # left
            (x + w, y, width - x - w, h),        # right
        ):
            snapshot.append_color(self._shade, Graphene.Rect().init(*band))
        outline = Gsk.RoundedRect()
        outline.init_from_rect(Graphene.Rect().init(x, y, w, h), 0)
        snapshot.append_border(outline, [BORDER_WIDTH] * 4, [self._border] * 4)

# Ignore accidental clicks: anything smaller is treated as "no selection".
MIN_SELECTION_PX = 16

HINT_TEXT = "Drag to select an area · Esc to cancel"

_css_installed = False


def _install_css() -> None:
    global _css_installed
    if _css_installed:
        return
    provider = Gtk.CssProvider()
    provider.load_from_string(CSS)
    Gtk.StyleContext.add_provider_for_display(
        Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )
    _css_installed = True


def find_monitor(stream_props: dict) -> Optional[Gdk.Monitor]:
    """Match the portal stream to a Gdk.Monitor by its logical position."""
    monitors = Gdk.Display.get_default().get_monitors()
    position = stream_props.get("position")
    for i in range(monitors.get_n_items()):
        monitor = monitors.get_item(i)
        geometry = monitor.get_geometry()
        if position and (geometry.x, geometry.y) == tuple(position):
            return monitor
    return monitors.get_item(0) if monitors.get_n_items() else None


class AreaSelector(Gtk.Window):
    def __init__(
        self,
        application: Gtk.Application,
        monitor: Optional[Gdk.Monitor],
        background: Optional[Gdk.Texture],
        on_selected: Callable[[CropRegion], None],
        on_cancelled: Callable[[], None],
    ) -> None:
        super().__init__(application=application, decorated=False, title="Select Area")
        _install_css()
        self.add_css_class(CSS_CLASS)
        self.set_cursor_from_name("crosshair")

        self._on_selected = on_selected
        self._on_cancelled = on_cancelled
        self._done = False
        self._start: Optional[tuple[float, float]] = None
        self._area = _SelectionCanvas()

        self._hint = Gtk.Label(label=HINT_TEXT, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        self._hint.add_css_class("osd")
        self._hint.add_css_class("title-4")
        self._hint.set_can_target(False)

        # Stretch the still to the monitor: the stream *is* this monitor, so
        # only the pixel density differs (physical vs logical pixels).
        overlay = Gtk.Overlay()
        if background is not None:
            overlay.set_child(Gtk.Picture(paintable=background, content_fit=Gtk.ContentFit.FILL,
                                          can_shrink=True))
            overlay.add_overlay(self._area)
        else:
            overlay.set_child(self._area)
        overlay.add_overlay(self._hint)
        self.set_child(overlay)

        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", self._on_drag_end)
        self._area.add_controller(drag)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key_pressed)
        self.add_controller(keys)

        self.connect("close-request", self._on_close_request)

        if monitor is not None:
            self.fullscreen_on_monitor(monitor)
        else:
            self.fullscreen()

    # ---- input --------------------------------------------------------------

    def _on_drag_begin(self, _gesture, x: float, y: float) -> None:
        self._start = (x, y)
        self._area.rect = (x, y, 0.0, 0.0)
        self._area.queue_draw()

    def _on_drag_update(self, _gesture, dx: float, dy: float) -> None:
        x0, y0 = self._start
        self._area.rect = (min(x0, x0 + dx), min(y0, y0 + dy), abs(dx), abs(dy))
        self._hint.set_label(f"{round(abs(dx))} × {round(abs(dy))}")
        self._area.queue_draw()

    def _on_drag_end(self, _gesture, _dx: float, _dy: float) -> None:
        x, y, w, h = self._area.rect
        if w < MIN_SELECTION_PX or h < MIN_SELECTION_PX:
            self._area.rect = None
            self._hint.set_label(HINT_TEXT)
            self._area.queue_draw()
            return
        aw, ah = self._area.get_width(), self._area.get_height()
        self._finish(CropRegion(x / aw, y / ah, w / aw, h / ah))

    def _on_key_pressed(self, _ctrl, keyval: int, _code: int, _state) -> bool:
        if keyval == Gdk.KEY_Escape:
            self._finish(None)
            return True
        return False

    def _on_close_request(self, _window) -> bool:
        self._finish(None)
        return False

    def _finish(self, region: Optional[CropRegion]) -> None:
        if self._done:
            return
        self._done = True
        # Hide first so the overlay isn't captured in the first frames.
        self.close()
        if region is None:
            self._on_cancelled()
        else:
            self._on_selected(region)
