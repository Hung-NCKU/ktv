"""用隨 venv 附帶的 ffmpeg 轉檔（mp3 / m4a / flac）。"""

from __future__ import annotations

import os
import subprocess

_EXT = {"mp3": ".mp3", "m4a": ".m4a", "flac": ".flac", "wav": ".wav"}


def ffmpeg_path() -> str | None:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        from shutil import which
        return which("ffmpeg")


def available() -> bool:
    return ffmpeg_path() is not None


def convert(src: str, fmt: str, bitrate: str = "320k",
            delete_src: bool = False) -> str:
    """把 WAV 轉成指定格式，回傳輸出路徑。fmt='wav' 時直接回傳原檔。"""
    fmt = fmt.lower()
    if fmt == "wav":
        return src
    if fmt not in _EXT:
        raise ValueError(f"不支援的格式：{fmt}")
    exe = ffmpeg_path()
    if exe is None:
        raise RuntimeError("找不到 ffmpeg，無法轉檔（請重裝 imageio-ffmpeg）")

    dst = os.path.splitext(src)[0] + _EXT[fmt]
    cmd = [exe, "-y", "-hide_banner", "-loglevel", "error", "-i", src]
    if fmt == "mp3":
        cmd += ["-c:a", "libmp3lame", "-b:a", bitrate]
    elif fmt == "m4a":
        cmd += ["-c:a", "aac", "-b:a", bitrate]
    else:                                   # flac
        cmd += ["-c:a", "flac", "-compression_level", "5"]
    cmd.append(dst)

    proc = subprocess.run(cmd, capture_output=True, text=True,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg 轉檔失敗：{proc.stderr.strip()[:400]}")
    if delete_src:
        try:
            os.remove(src)
        except OSError:
            pass
    return dst
