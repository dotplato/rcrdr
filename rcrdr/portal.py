"""xdg-desktop-portal ScreenCast client.

On Wayland the only sanctioned way to capture the screen is to ask the portal,
which shows the system picker and hands back a PipeWire fd + node id.

Flow: CreateSession -> SelectSources -> Start -> OpenPipeWireRemote.
Every step except the last answers asynchronously via a Request "Response"
signal, so this module is callback-driven.
"""

from enum import IntEnum
from typing import Callable, Optional
from uuid import uuid4

from gi.repository import Gio, GLib

PORTAL_BUS = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
SCREENCAST_IFACE = "org.freedesktop.portal.ScreenCast"
REQUEST_IFACE = "org.freedesktop.portal.Request"
SESSION_IFACE = "org.freedesktop.portal.Session"

DBUS_TIMEOUT_MS = 10_000

RESPONSE_SUCCESS = 0
RESPONSE_CANCELLED = 1


class SourceType(IntEnum):
    MONITOR = 1
    WINDOW = 2


class CursorMode(IntEnum):
    HIDDEN = 1
    EMBEDDED = 2


# on_started(pipewire_fd, node_id, stream_properties)
StartedCallback = Callable[[int, int, dict], None]
# on_failed(message) — message is None when the user simply cancelled.
FailedCallback = Callable[[Optional[str]], None]


class ScreenCastSession:
    def __init__(self) -> None:
        self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        # Request object paths embed our unique bus name, e.g. ":1.42" -> "1_42".
        self._sender = self._bus.get_unique_name()[1:].replace(".", "_")
        self._session_handle: Optional[str] = None
        self._closed_sub_id: Optional[int] = None
        self._on_closed: Optional[Callable[[], None]] = None

    # ---- public API -----------------------------------------------------

    def start(
        self,
        source_type: SourceType,
        cursor_mode: CursorMode,
        on_started: StartedCallback,
        on_failed: FailedCallback,
        on_closed: Optional[Callable[[], None]] = None,
    ) -> None:
        """Run the portal handshake; exactly one of the callbacks fires."""
        self._on_closed = on_closed

        def after_create(results: dict) -> None:
            self._session_handle = results["session_handle"]
            self._watch_session_closed()
            self._request(
                "SelectSources",
                "(oa{sv})",
                (
                    self._session_handle,
                    {
                        "types": GLib.Variant("u", int(source_type)),
                        "multiple": GLib.Variant("b", False),
                        "cursor_mode": GLib.Variant("u", int(cursor_mode)),
                    },
                ),
                after_select,
                on_failed,
            )

        def after_select(_results: dict) -> None:
            self._request(
                "Start", "(osa{sv})", (self._session_handle, "", {}), after_start, on_failed
            )

        def after_start(results: dict) -> None:
            streams = results.get("streams") or []
            if not streams:
                on_failed("The portal returned no streams.")
                return
            node_id, props = streams[0]
            try:
                fd = self._open_pipewire_remote()
            except GLib.Error as err:
                on_failed(f"Could not connect to PipeWire: {err.message}")
                return
            on_started(fd, node_id, props)

        self._request(
            "CreateSession",
            "(a{sv})",
            ({"session_handle_token": GLib.Variant("s", _new_token())},),
            after_create,
            on_failed,
        )

    def close(self) -> None:
        """End the session; removes GNOME's "screen is being shared" indicator."""
        if self._closed_sub_id is not None:
            self._bus.signal_unsubscribe(self._closed_sub_id)
            self._closed_sub_id = None
        if self._session_handle is None:
            return
        handle, self._session_handle = self._session_handle, None
        try:
            self._bus.call_sync(
                PORTAL_BUS, handle, SESSION_IFACE, "Close", None, None,
                Gio.DBusCallFlags.NONE, DBUS_TIMEOUT_MS, None,
            )
        except GLib.Error:
            pass  # Already closed by the compositor — nothing to clean up.

    # ---- internals ------------------------------------------------------

    def _request(
        self,
        method: str,
        signature: str,
        args: tuple,
        on_success: Callable[[dict], None],
        on_failed: FailedCallback,
    ) -> None:
        """Call a portal method and route its async Response to callbacks.

        The handle_token is injected into the trailing options dict. We
        subscribe to the predictable request path *before* calling so a fast
        response can't be missed.
        """
        token = _new_token()
        request_path = f"{PORTAL_PATH}/request/{self._sender}/{token}"
        args[-1]["handle_token"] = GLib.Variant("s", token)
        sub_id = 0

        def on_response(_conn, _sender, _path, _iface, _signal, params):
            self._bus.signal_unsubscribe(sub_id)
            code, results = params.unpack()
            if code == RESPONSE_SUCCESS:
                on_success(results)
            elif code == RESPONSE_CANCELLED:
                on_failed(None)
            else:
                on_failed(f"The screen cast portal failed during {method}.")

        sub_id = self._bus.signal_subscribe(
            PORTAL_BUS, REQUEST_IFACE, "Response", request_path, None,
            Gio.DBusSignalFlags.NONE, on_response,
        )
        try:
            self._bus.call_sync(
                PORTAL_BUS, PORTAL_PATH, SCREENCAST_IFACE, method,
                GLib.Variant(signature, args), GLib.VariantType("(o)"),
                Gio.DBusCallFlags.NONE, DBUS_TIMEOUT_MS, None,
            )
        except GLib.Error as err:
            self._bus.signal_unsubscribe(sub_id)
            on_failed(f"Screen cast portal unavailable: {err.message}")

    def _open_pipewire_remote(self) -> int:
        result, fd_list = self._bus.call_with_unix_fd_list_sync(
            PORTAL_BUS, PORTAL_PATH, SCREENCAST_IFACE, "OpenPipeWireRemote",
            GLib.Variant("(oa{sv})", (self._session_handle, {})),
            GLib.VariantType("(h)"), Gio.DBusCallFlags.NONE, DBUS_TIMEOUT_MS,
            None, None,
        )
        (fd_index,) = result.unpack()
        return fd_list.get(fd_index)

    def _watch_session_closed(self) -> None:
        def on_closed(*_args):
            self._session_handle = None
            if self._on_closed:
                self._on_closed()

        self._closed_sub_id = self._bus.signal_subscribe(
            PORTAL_BUS, SESSION_IFACE, "Closed", self._session_handle, None,
            Gio.DBusSignalFlags.NONE, on_closed,
        )


def _new_token() -> str:
    return f"rcrdr_{uuid4().hex}"
