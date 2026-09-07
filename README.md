# LuminaRec Recorder — Python screen recorder for Ubuntu/Linux

A modular PySide6 screen recorder that uses **MSS on X11**, the **XDG Desktop Portal + PipeWire on Wayland**, and **FFmpeg for all encoding/muxing**.

## Features

- Full desktop, rectangular region, or a window
- Start / pause / resume / stop
- MP4 (H.264/AAC) and WebM (VP9/Opus)
- 15 / 24 / 30 / 60 FPS
- Optional default microphone, default system output monitor, or both
- Live preview
- Timer, measured capture FPS, and current/estimated file size
- X11 window list via `wmctrl`
- Wayland monitor/window chooser through `xdg-desktop-portal`
- Dark theme by default + light theme
- Ctrl+Shift+R start/stop; Escape cancels region selection
- Pause/resume is implemented as clean temporary segments that are joined losslessly at stop time

## Ubuntu 22.04 / 24.04 installation

Clone the repo:

```bash
git clone https://github.com/Aryamuh-ynah/LuminaRec.git
```

Install system packages:

```bash
sudo apt update
sudo apt install -y \
  ffmpeg wmctrl pulseaudio-utils \
  xdg-desktop-portal xdg-desktop-portal-gnome \
  pipewire gstreamer1.0-tools gstreamer1.0-pipewire \
  gstreamer1.0-plugins-base gstreamer1.0-plugins-good
```

Create a virtual environment and install Python dependencies:

```bash
cd LuminaRec
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Run:

```bash
python main.py
```

## How capture works

### X11

MSS captures BGRA frames directly from the X11 desktop. Region and selected-window capture are simply capture rectangles. Window geometry comes from `wmctrl`.

### Wayland

Wayland intentionally prevents ordinary applications from reading arbitrary screen pixels. LuminaRec Recorder asks `org.freedesktop.portal.ScreenCast` for permission. The compositor returns a permission-scoped PipeWire stream and file descriptor. A small GStreamer bridge converts that stream to raw `BGRx` frames; the frames are then fed to FFmpeg, which performs the actual H.264/VP9 and audio encoding.

For **Window** mode on Wayland, the compositor's portal dialog is the window selector. Applications are not generally permitted to enumerate every other application's Wayland windows.

For **Region** mode on Wayland, choose a region first, then choose the monitor containing that region in the portal dialog. The selected region must fit fully inside that monitor. This is a consequence of the standard ScreenCast portal exposing monitor/window streams rather than a universal arbitrary-region stream.

## Audio

The app uses FFmpeg's PulseAudio input. This also works on Ubuntu systems where PipeWire provides the PulseAudio compatibility server.

- **Microphone:** current default source from `pactl get-default-source`
- **System audio:** monitor source for the current default sink
- **Both:** FFmpeg mixes both sources with `amix`

If the expected source is missing, the app shows an error rather than silently recording without audio.

## Hotkey notes

- The Qt shortcut always works while the application is focused.
- On X11, `pynput` is also used for a system-wide Ctrl+Shift+R shortcut.
- Wayland blocks general-purpose key loggers by design. If your desktop provides a Global Shortcuts portal, a future integration can bind through that portal; on Ubuntu versions/desktops without that support, the application-scoped shortcut remains available.

## Troubleshooting

**Wayland permission denied / cancelled**  
Start again and approve the desktop share dialog. The application does not bypass portal permission.

**`gst-launch-1.0` missing**  
Install `gstreamer1.0-tools gstreamer1.0-pipewire`.

**No system audio**  
Run `pactl list short sources` and verify that a `*.monitor` source exists. Also verify the intended output device is the default sink.

**FFmpeg encoder error**  
Check `ffmpeg -encoders | grep -E 'libx264|libvpx-vp9'`. Ubuntu's normal FFmpeg package includes these encoders.

**X11 Window mode unavailable**  
Install `wmctrl`. On Wayland, use the compositor's window chooser instead.

## Project layout

```text
main.py       Application entry point
ui.py         PySide6 UI, region/window selection, preview, themes
recorder.py   Capture backends, FFmpeg encoder, pause/resume segmentation
portal.py     XDG Desktop Portal + PipeWire D-Bus session handling
utils.py      Window/audio discovery and formatting helpers
requirements.txt
README.md
```

## Design notes

The UI thread never performs capture or encoding. `RecorderController` owns a worker thread, while the portal keeps its own asyncio/D-Bus thread alive for the lifetime of a Wayland recording. Cleanup closes capture processes, FFmpeg stdin/processes, PipeWire descriptors, D-Bus sessions, and temporary segment files.
