"""音訊裝置列舉與解析。

Windows 下 soundcard 會把每個輸出裝置同時暴露成一個 loopback 錄音裝置，
因此「電腦播放的聲音」就是對應喇叭的 loopback 麥克風，不需要虛擬音效線。
"""

from __future__ import annotations

from dataclasses import dataclass

import soundcard as sc


@dataclass(frozen=True)
class Device:
    """一個可錄音的來源。"""

    id: str
    name: str
    channels: int
    is_loopback: bool

    @property
    def label(self) -> str:
        kind = "系統播放" if self.is_loopback else "麥克風"
        return f"[{kind}] {self.name} ({self.channels}ch)"


def _wrap(dev, is_loopback: bool) -> Device:
    return Device(id=str(dev.id), name=dev.name, channels=int(dev.channels),
                  is_loopback=is_loopback)


def list_microphones() -> list[Device]:
    """實體/虛擬輸入裝置（不含 loopback）。"""
    return [_wrap(m, False) for m in sc.all_microphones(include_loopback=False)]


def list_loopbacks() -> list[Device]:
    """每個輸出裝置對應的 loopback 擷取來源。"""
    return [_wrap(s, True) for s in sc.all_speakers()]


def default_microphone() -> Device | None:
    try:
        return _wrap(sc.default_microphone(), False)
    except Exception:
        mics = list_microphones()
        return mics[0] if mics else None


def default_loopback() -> Device | None:
    try:
        return _wrap(sc.default_speaker(), True)
    except Exception:
        lbs = list_loopbacks()
        return lbs[0] if lbs else None


def open_recorder_device(dev: Device):
    """把 Device 轉回 soundcard 的可錄音物件。"""
    return sc.get_microphone(dev.id, include_loopback=dev.is_loopback)


def resolve(dev_id: str, is_loopback: bool) -> Device | None:
    """依 id 找回裝置；找不到時回傳 None（裝置可能已拔除）。"""
    pool = list_loopbacks() if is_loopback else list_microphones()
    for d in pool:
        if d.id == dev_id:
            return d
    return None
