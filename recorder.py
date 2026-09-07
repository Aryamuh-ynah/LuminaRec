from __future__ import annotations

import os
import select
import shutil
import subprocess
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

import mss
import numpy as np

from portal import PortalError, PortalStream, ScreenCastPortal
from utils import command_exists, is_wayland, pulse_default_sink_monitor, pulse_default_source

Mode = Literal["full", "region", "window"]
AudioMode = Literal["none", "mic", "system", "both"]
Format = Literal["mp4", "webm"]


@dataclass
class RecorderConfig:
    mode: Mode
    fps: int
    format: Format
    audio: AudioMode
    output: Path
    preview: bool
    region: dict[str, int] | None = None

    # Wayland pre-selected portal resources.
    portal: ScreenCastPortal | None = None
    portal_stream: PortalStream | None = None


@dataclass
class RecorderStats:
    state: str = "idle"
    elapsed: float = 0.0
    current_fps: float = 0.0
    bytes_written: int = 0
    output: Path | None = None


class CaptureBackend:
    width: int
    height: int
    pixel_format = "bgra"

    def prepare(self) -> None: ...
    def start_segment(self) -> None: ...
    def grab(self, timeout: float = 0.5) -> np.ndarray | None: ...
    def stop_segment(self) -> None: ...
    def close(self) -> None: ...


class MSSCaptureBackend(CaptureBackend):
    def __init__(self, region: dict[str, int] | None) -> None:
        self.requested_region = region
        self._sct: mss.mss | None = None
        self.region: dict[str, int] | None = None
        self.width = 0
        self.height = 0

    def prepare(self) -> None:
        self._sct = mss.mss()
        if self.requested_region is None:
            mon = self._sct.monitors[0]
            self.region = {k: int(mon[k]) for k in ("left", "top", "width", "height")}
        else:
            self.region = {k: int(self.requested_region[k]) for k in ("left", "top", "width", "height")}
        self.width = self.region["width"]
        self.height = self.region["height"]
        if self.width < 2 or self.height < 2:
            raise RuntimeError("The selected capture area is too small.")

    def start_segment(self) -> None:
        return

    def grab(self, timeout: float = 0.5) -> np.ndarray | None:
        if not self._sct or not self.region:
            raise RuntimeError("MSS capture has not been prepared.")
        return np.asarray(self._sct.grab(self.region))

    def stop_segment(self) -> None:
        return

    def close(self) -> None:
        if self._sct:
            self._sct.close()
            self._sct = None


class PortalPipeWireBackend(CaptureBackend):
    """Wayland backend: portal grants PipeWire; GStreamer converts it to BGRA-like BGRx frames."""

    def __init__(
        self,
        mode: Mode,
        fps: int,
        region: dict[str, int] | None,
        portal: ScreenCastPortal | None = None,
        stream: PortalStream | None = None,
    ) -> None:
        self.mode = mode
        self.fps = fps
        self.requested_region = region

        self.portal = portal
        self.stream = stream

        self.proc: subprocess.Popen[bytes] | None = None
        self.width = 0
        self.height = 0
        self._buffer = bytearray()
        self._crop: tuple[int, int, int, int] | None = None


    def prepare(self) -> None:
        if not command_exists("gst-launch-1.0"):
            raise RuntimeError("Wayland capture requires GStreamer. Install gstreamer1.0-tools and gstreamer1.0-pipewire.")
        # Region mode may already have a portal stream because the monitor
        # must be selected before showing our region overlay.
        if self.stream is None:
            if self.portal is None:
                self.portal = ScreenCastPortal()

            source_type = 2 if self.mode == "window" else 1
            self.stream = self.portal.open(source_type)


        if self.mode == "region":
            if not self.requested_region:
                raise RuntimeError("No region was selected.")
            r = self.requested_region
            left = r["left"] - self.stream.x
            top = r["top"] - self.stream.y
            right = left + r["width"]
            bottom = top + r["height"]
            if left < 0 or top < 0 or right > self.stream.width or bottom > self.stream.height:
                raise RuntimeError(
                    "On Wayland, the selected region must fit inside the monitor chosen in the portal dialog."
                )
            self._crop = (left, top, self.stream.width - right, self.stream.height - bottom)
            self.width, self.height = r["width"], r["height"]
        else:
            self.width, self.height = self.stream.width, self.stream.height

    def start_segment(self) -> None:
        if self.stream is None:
            raise RuntimeError("Portal capture is not prepared.")
        parts = [
            "pipewiresrc", f"fd={self.stream.fd}", f"path={self.stream.node_id}", "do-timestamp=true",
            f"keepalive-time={max(10, int(1000 / self.fps))}", "!",
            "videoconvert", "!", "videoscale", "!",
            f"video/x-raw,format=BGRx,width={self.stream.width},height={self.stream.height}", "!",
        ]
        if self._crop is not None:
            left, top, right, bottom = self._crop
            parts += ["videocrop", f"left={left}", f"top={top}", f"right={right}", f"bottom={bottom}", "!"]
        parts += [
            "videorate", "!",
            f"video/x-raw,format=BGRx,width={self.width},height={self.height},framerate={self.fps}/1", "!",
            "fdsink", "fd=1", "sync=false",
        ]
        self._buffer.clear()
        self.proc = subprocess.Popen(
            ["gst-launch-1.0", "-q", *parts],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            pass_fds=(self.stream.fd,),
        )

    def grab(self, timeout: float = 0.5) -> np.ndarray | None:
        if not self.proc or not self.proc.stdout:
            return None
        frame_bytes = self.width * self.height * 4
        fd = self.proc.stdout.fileno()
        deadline = time.monotonic() + timeout
        while len(self._buffer) < frame_bytes:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            readable, _, _ = select.select([fd], [], [], remaining)
            if not readable:
                return None
            chunk = os.read(fd, min(1024 * 1024, frame_bytes - len(self._buffer)))
            if not chunk:
                if self.proc.poll() is not None:
                    raise RuntimeError("The Wayland PipeWire capture pipeline stopped unexpectedly.")
                return None
            self._buffer.extend(chunk)
        raw = bytes(self._buffer[:frame_bytes])
        del self._buffer[:frame_bytes]
        return np.frombuffer(raw, dtype=np.uint8).reshape((self.height, self.width, 4))

    def stop_segment(self) -> None:
        if self.proc:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            self.proc = None

    def close(self) -> None:
        self.stop_segment()
        if self.stream is not None:
            try:
                os.close(self.stream.fd)
            except OSError:
                pass
            self.stream = None
        if self.portal:
            self.portal.close()
            self.portal = None


class FFmpegEncoder:
    def __init__(self, config: RecorderConfig, width: int, height: int) -> None:
        self.config = config
        self.width = width
        self.height = height
        self.proc: subprocess.Popen[bytes] | None = None
        self._stderr_tail: deque[str] = deque(maxlen=30)
        self._stderr_thread: threading.Thread | None = None

    def _audio_inputs(self) -> tuple[list[str], int]:
        args: list[str] = []
        count = 0
        mode = self.config.audio
        if mode in ("mic", "both"):
            mic = pulse_default_source()
            if not mic:
                raise RuntimeError("No default microphone source was found. Check PulseAudio/PipeWire and pactl.")
            args += ["-f", "pulse", "-i", mic]
            count += 1
        if mode in ("system", "both"):
            monitor = pulse_default_sink_monitor()
            if not monitor:
                raise RuntimeError("No system-audio monitor source was found. Check your default output device.")
            args += ["-f", "pulse", "-i", monitor]
            count += 1
        return args, count

    def start(self, output: Path) -> None:
        audio_args, audio_count = self._audio_inputs()
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
            "-f", "rawvideo", "-pixel_format", "bgra",
            "-video_size", f"{self.width}x{self.height}",
            "-framerate", str(self.config.fps), "-i", "pipe:0",
            *audio_args,
        ]

        if audio_count == 0:
            maps = ["-map", "0:v:0"]
        elif audio_count == 1:
            maps = ["-map", "0:v:0", "-map", "1:a:0"]
        else:
            maps = [
                "-filter_complex", "[1:a][2:a]amix=inputs=2:normalize=0[aout]",
                "-map", "0:v:0", "-map", "[aout]",
            ]
        cmd += maps
        cmd += ["-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2", "-pix_fmt", "yuv420p"]

        if self.config.format == "mp4":
            cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"]
            if audio_count:
                cmd += ["-c:a", "aac", "-b:a", "160k"]
            cmd += ["-movflags", "+faststart"]
        else:
            cmd += ["-c:v", "libvpx-vp9", "-crf", "30", "-b:v", "0", "-deadline", "realtime", "-cpu-used", "4", "-row-mt", "1"]
            if audio_count:
                cmd += ["-c:a", "libopus", "-b:a", "128k"]

        if audio_count:
            cmd += ["-shortest"]
        cmd += [str(output)]

        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        self._stderr_tail.clear()

        def drain() -> None:
            assert self.proc and self.proc.stderr
            for line in iter(self.proc.stderr.readline, b""):
                self._stderr_tail.append(line.decode("utf-8", "replace").strip())

        self._stderr_thread = threading.Thread(target=drain, daemon=True, name="ffmpeg-stderr")
        self._stderr_thread.start()

    def write(self, frame: np.ndarray) -> None:
        if not self.proc or not self.proc.stdin:
            raise RuntimeError("FFmpeg is not running.")
        if self.proc.poll() is not None:
            details = "\n".join(self._stderr_tail)
            raise RuntimeError("FFmpeg stopped unexpectedly." + (f"\n{details}" if details else ""))
        try:
            self.proc.stdin.write(memoryview(frame).cast("B"))
        except BrokenPipeError as exc:
            details = "\n".join(self._stderr_tail)
            raise RuntimeError("FFmpeg closed its input pipe." + (f"\n{details}" if details else "")) from exc

    def stop(self) -> None:
        if not self.proc:
            return
        if self.proc.stdin:
            try:
                self.proc.stdin.close()
            except Exception:
                pass
        try:
            self.proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        rc = self.proc.returncode
        self.proc = None
        if rc not in (0, None, 255):
            details = "\n".join(self._stderr_tail)
            raise RuntimeError(f"FFmpeg exited with status {rc}." + (f"\n{details}" if details else ""))


class RecorderController:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._thread: threading.Thread | None = None
        self._stop = False
        self._pause = False
        self._state = "idle"
        self._active_elapsed = 0.0
        self._segment_started = 0.0
        self._current_fps = 0.0
        self._segments: list[Path] = []
        self._current_segment: Path | None = None
        self._output: Path | None = None
        self._preview_cb: Callable[[np.ndarray], None] | None = None
        self._state_cb: Callable[[str], None] | None = None
        self._error_cb: Callable[[str], None] | None = None
        self._finished_cb: Callable[[Path], None] | None = None

    def start(
        self,
        config: RecorderConfig,
        preview_cb: Callable[[np.ndarray], None] | None = None,
        state_cb: Callable[[str], None] | None = None,
        error_cb: Callable[[str], None] | None = None,
        finished_cb: Callable[[Path], None] | None = None,
    ) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                raise RuntimeError("A recording is already active.")
            if not command_exists("ffmpeg"):
                raise RuntimeError("FFmpeg was not found. Install it with: sudo apt install ffmpeg")
            config.output.parent.mkdir(parents=True, exist_ok=True)
            self._stop = False
            self._pause = False
            self._state = "starting"
            self._active_elapsed = 0.0
            self._current_fps = 0.0
            self._segments = []
            self._current_segment = None
            self._output = config.output
            self._preview_cb = preview_cb
            self._state_cb = state_cb
            self._error_cb = error_cb
            self._finished_cb = finished_cb
            self._thread = threading.Thread(target=self._run, args=(config,), daemon=True, name="recorder")
            self._thread.start()
        self._emit_state("starting")

    def _emit_state(self, state: str) -> None:
        with self._lock:
            self._state = state
        if self._state_cb:
            self._state_cb(state)

    def pause(self) -> None:
        with self._condition:
            if self._state == "recording":
                self._pause = True
                self._condition.notify_all()

    def resume(self) -> None:
        with self._condition:
            if self._state == "paused":
                self._pause = False
                self._condition.notify_all()

    def stop(self) -> None:
        with self._condition:
            self._stop = True
            self._pause = False
            self._condition.notify_all()

    def stats(self) -> RecorderStats:
        with self._lock:
            elapsed = self._active_elapsed
            if self._state == "recording" and self._segment_started:
                elapsed += max(0.0, time.monotonic() - self._segment_started)
            size = 0
            for path in self._segments:
                if path.exists():
                    size += path.stat().st_size
            if self._current_segment and self._current_segment.exists() and self._current_segment not in self._segments:
                size += self._current_segment.stat().st_size
            return RecorderStats(self._state, elapsed, self._current_fps, size, self._output)

    def _make_backend(self, config: RecorderConfig) -> CaptureBackend:
        if is_wayland():
            return PortalPipeWireBackend(
                mode=config.mode,
                fps=config.fps,
                region=config.region,
                portal=config.portal,
                stream=config.portal_stream,
            )

        return MSSCaptureBackend(config.region)

    def _concat_segments(self, segments: list[Path], output: Path) -> None:
        if not segments:
            raise RuntimeError("No video data was recorded.")
        if len(segments) == 1:
            shutil.move(str(segments[0]), str(output))
            return
        list_file = segments[0].parent / "concat.txt"
        with list_file.open("w", encoding="utf-8") as fh:
            for path in segments:
                escaped = str(path).replace("'", "'\\''")
                fh.write(f"file '{escaped}'\n")
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file), "-c", "copy", str(output)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError("Could not join paused recording segments: " + result.stderr.strip())

    def _run(self, config: RecorderConfig) -> None:
        backend = self._make_backend(config)
        temp_dir_obj = tempfile.TemporaryDirectory(prefix="screenrec-")
        temp_dir = Path(temp_dir_obj.name)
        try:
            backend.prepare()
            segment_index = 0
            while True:
                with self._condition:
                    if self._stop:
                        break
                    while self._pause and not self._stop:
                        self._emit_state("paused")
                        self._condition.wait(timeout=0.5)
                    if self._stop:
                        break

                segment_index += 1
                segment_path = temp_dir / f"segment-{segment_index:04d}.{config.format}"
                encoder = FFmpegEncoder(config, backend.width, backend.height)
                backend.start_segment()
                encoder.start(segment_path)
                with self._lock:
                    self._current_segment = segment_path
                    self._segment_started = time.monotonic()
                self._emit_state("recording")

                frame_times: deque[float] = deque(maxlen=max(config.fps * 2, 30))
                next_frame = time.monotonic()
                try:
                    while True:
                        with self._lock:
                            if self._stop or self._pause:
                                break
                        frame = backend.grab(timeout=0.5)
                        if frame is None:
                            continue
                        encoder.write(frame)
                        now = time.monotonic()
                        frame_times.append(now)
                        if len(frame_times) >= 2:
                            span = frame_times[-1] - frame_times[0]
                            with self._lock:
                                self._current_fps = (len(frame_times) - 1) / span if span > 0 else 0.0
                        if config.preview and self._preview_cb:
                            self._preview_cb(frame)

                        if not is_wayland():
                            next_frame += 1.0 / config.fps
                            delay = next_frame - time.monotonic()
                            if delay > 0:
                                time.sleep(delay)
                            elif delay < -1.0:
                                next_frame = time.monotonic()
                finally:
                    backend.stop_segment()
                    encoder.stop()
                    with self._lock:
                        if self._segment_started:
                            self._active_elapsed += max(0.0, time.monotonic() - self._segment_started)
                        self._segment_started = 0.0
                        self._current_fps = 0.0
                        self._segments.append(segment_path)
                        self._current_segment = None

                with self._condition:
                    if self._stop:
                        break
                    if self._pause:
                        self._emit_state("paused")
                        while self._pause and not self._stop:
                            self._condition.wait(timeout=0.5)
                        if self._stop:
                            break

            self._emit_state("finalizing")
            self._concat_segments(self._segments, config.output)
            self._emit_state("finished")
            if self._finished_cb:
                self._finished_cb(config.output)
        except (PortalError, Exception) as exc:
            self._emit_state("error")
            if self._error_cb:
                self._error_cb(str(exc))
        finally:
            try:
                backend.close()
            except Exception:
                pass
            temp_dir_obj.cleanup()
            with self._lock:
                self._segment_started = 0.0
                self._current_segment = None
