import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Literal

try:
    import av
except ImportError:
    av = None


def parse_duration_seconds(value: object, *, bare_unit: Literal["minutes", "seconds"] = "minutes") -> float | None:
    """Normalize provider runtime strings while keeping bare-number units explicit."""
    text = str(value or "").strip().lower().replace(",", "")
    if not text:
        return None

    hour = re.search(r"(\d+(?:\.\d+)?)\s*(?:hours?|hrs?|h|時間|小时|小時)", text)
    minute = re.search(r"(\d+(?:\.\d+)?)\s*(?:minutes?|mins?|m|分|分钟|分鐘)", text)
    second = re.search(r"(\d+(?:\.\d+)?)\s*(?:seconds?|secs?|s|秒)", text)
    if hour or minute or second:
        total = float(hour.group(1)) * 3600 if hour else 0.0
        total += float(minute.group(1)) * 60 if minute else 0.0
        total += float(second.group(1)) if second else 0.0
        return total if total > 0 else None

    if ":" in text:
        try:
            parts = [float(part) for part in text.split(":")]
        except ValueError:
            return None
        if len(parts) in (2, 3):
            total = 0.0
            for part in parts:
                total = total * 60 + part
            return total if total > 0 else None
        return None

    match = re.search(r"\d+(?:\.\d+)?", text)
    if not match:
        return None
    number = float(match.group())
    seconds = number * 60 if bare_unit == "minutes" else number
    return seconds if seconds > 0 else None


def get_video_duration_seconds(p: Path | str) -> float | None:
    """Read a video's duration with PyAV when available."""
    if av is None:
        return None
    try:
        with av.open(str(p)) as container:
            if container.duration:
                return float(container.duration) / 1_000_000
            for stream in container.streams.video:
                if stream.duration and stream.time_base:
                    return float(stream.duration * stream.time_base)
    except Exception:
        return None
    return None


def get_video_metadata_pyav(p: Path) -> tuple[int, str]:
    if av is None:
        raise ImportError("Should not be called if pyav is not available")
    height = 0
    codec_fourcc = ""
    with av.open(p) as container:
        # 查找第一个视频流
        video_stream = next((s for s in container.streams.video), None)
        if video_stream:
            height = video_stream.height
            codec_fourcc = video_stream.codec_context.name.upper()
    return height, codec_fourcc


def get_video_metadata_ffmpeg(p: Path) -> tuple[int, str]:
    if shutil.which("ffprobe") is None:
        raise RuntimeError("当前版本无 opencv/pyav. 若想获取视频分辨率请安装 ffprobe 或改用带 opencv/pyav 版本.")
    # Use ffprobe to get video information
    cmd = ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", str(p)]

    # macOS and Linux use default flags
    creationflags = 0
    # Windows use CREATE_NO_WINDOW to suppress the console window
    if os.name == "nt":
        creationflags = subprocess.CREATE_NO_WINDOW

    result = subprocess.run(cmd, capture_output=True, text=True, creationflags=creationflags)

    data = json.loads(result.stdout)

    # Find video stream
    video_stream = next((stream for stream in data["streams"] if stream["codec_type"] == "video"), None)

    if video_stream:
        height = int(video_stream["height"])
        codec_fourcc = video_stream["codec_name"].upper()
    else:
        height = 0
        codec_fourcc = ""
    return height, codec_fourcc


if av is not None:
    VIDEO_BACKEND = "pyav"
    get_video_metadata = get_video_metadata_pyav
else:
    VIDEO_BACKEND = "ffmpeg"
    get_video_metadata = get_video_metadata_ffmpeg
