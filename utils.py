from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class WindowInfo:
    wid: str
    x: int
    y: int
    width: int
    height: int
    title: str

    @property
    def region(self) -> dict[str, int]:
        return {"left": self.x, "top": self.y, "width": self.width, "height": self.height}


def is_wayland() -> bool:
    return os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland" or bool(os.environ.get("WAYLAND_DISPLAY"))


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_output_path(fmt: str = "mp4") -> Path:
    videos = Path.home() / "Videos"
    ensure_dir(videos)
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    return videos / f"Screen Recording {stamp}.{fmt.lower()}"


def human_size(num_bytes: int) -> str:
    value = float(max(0, num_bytes))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024.0 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024.0
    return f"{value:.1f} TB"


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def run_text(args: Iterable[str], timeout: float = 4.0) -> str:
    result = subprocess.run(
        list(args),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"Command failed: {' '.join(args)}")
    return result.stdout


def list_x11_windows() -> list[WindowInfo]:
    if not command_exists("wmctrl"):
        raise RuntimeError("wmctrl is not installed. Install it with: sudo apt install wmctrl")

    windows: list[WindowInfo] = []
    for line in run_text(["wmctrl", "-lG"]).splitlines():
        parts = line.split(None, 7)
        if len(parts) < 8:
            continue
        wid, _desktop, x, y, width, height, _host, title = parts
        try:
            item = WindowInfo(wid, int(x), int(y), int(width), int(height), title.strip())
        except ValueError:
            continue
        if item.width > 1 and item.height > 1 and item.title:
            windows.append(item)
    return windows


def pulse_default_source() -> str | None:
    if not command_exists("pactl"):
        return None
    try:
        value = run_text(["pactl", "get-default-source"]).strip()
        return value or None
    except Exception:
        return None


def pulse_default_sink_monitor() -> str | None:
    if not command_exists("pactl"):
        return None
    try:
        sink = run_text(["pactl", "get-default-sink"]).strip()
        if not sink:
            return None
        expected = sink + ".monitor"
        sources = run_text(["pactl", "list", "short", "sources"])
        names = {line.split("\t", 2)[1] for line in sources.splitlines() if "\t" in line}
        if expected in names:
            return expected
        for name in names:
            if name.endswith(".monitor") and sink in name:
                return name
        return next((name for name in names if name.endswith(".monitor")), None)
    except Exception:
        return None


def safe_filename(name: str) -> str:
    cleaned = re.sub(r"[\x00-\x1f/\\]+", "_", name).strip()
    return cleaned or "recording"
