# Rcrdr

A small, fast screen recorder for Ubuntu / GNOME, built the way Kooha works:
**xdg-desktop-portal ScreenCast → PipeWire → GStreamer**, with a GTK4 +
libadwaita UI. Pure Python, no build step, and only uses packages Ubuntu
ships by default.

## Features

- **Selection**: pick a monitor, then drag out an area
- **Window**: record a single window
- **Screen**: record a whole monitor
- Output formats: **MP4** (H.264 + AAC), **MKV** (H.264 + Opus), **WebM** (VP8 + Opus)
- Show or hide the mouse pointer
- Microphone and desktop audio can each be turned on or off (both are mixed if enabled)
- Frame rate (24 / 30 / 60), start delay (0 / 3 / 5 / 10 s), save folder
- Settings are remembered between launches (`~/.config/rcrdr/settings.json`)
- Stopping from GNOME's top-bar "sharing" indicator also finishes the file cleanly

## Run

```sh
python3 -m rcrdr          # from this directory
./install.sh              # or install for your user (adds app-grid entry + `rcrdr` command)
./install.sh --uninstall
```

| Shortcut | Action |
|---|---|
| `Ctrl+R` | Start / stop recording |
| `Ctrl+O` | Open recordings folder |
| `Ctrl+Q` | Quit (finishes an active recording first) |
| `Esc` | Cancel area selection |

## Requirements

Preinstalled on Ubuntu 24.04+ desktop: `python3-gi`, `gir1.2-gtk-4.0`,
`gir1.2-adw-1` (libadwaita ≥ 1.7 for the source toggle),
`gstreamer1.0-pipewire`, `gstreamer1.0-plugins-{good,ugly}`,
`gstreamer1.0-libav`, `gstreamer1.0-pulseaudio`. Any format whose encoder is
missing is hidden from the list.

## Layout

```
rcrdr/
  application.py   Adw.Application, actions, shortcuts
  window.py        UI and the record flow (portal → selector → countdown → record)
  portal.py        ScreenCast portal D-Bus client
  area_selector.py full-screen drag-to-select overlay
  recorder.py      GStreamer pipeline builder + lifecycle
  formats.py       encoder/muxer settings per output format
  settings.py      JSON-backed preferences
```

## Notes

- Files are named `Rcrdr YYYY-MM-DD HH-MM-SS.<ext>` and saved to `~/Videos` by default.
- H.264 needs even frame sizes, so an odd-sized window or selection loses at
  most one pixel on the right/bottom edge.
- Encoding is software only (x264 `superfast`, VP8 realtime).
