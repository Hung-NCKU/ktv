"""波形圖，可以用滑鼠拖曳定位。

整首歌有幾百萬個取樣點，不可能每點都畫。這裡把資料切成「每個像素一欄」，
算出每欄的最大／最小值畫成一條直線 —— 這是音訊軟體的標準做法，短促的尖峰
才不會因為取樣而消失。
"""

from __future__ import annotations

import tkinter as tk
from typing import Callable

import numpy as np

BG = "#141414"
WAVE = "#3ddc84"
WAVE_DIM = "#2a7d4f"
MID = "#333333"
HEAD = "#ff5252"
TEXT = "#888888"


def envelope(data: np.ndarray, n_cols: int) -> tuple[np.ndarray, np.ndarray]:
    """算出每一欄的 (最小值, 最大值)。"""
    mono = data.mean(axis=1) if data.ndim > 1 else data
    n = len(mono)
    if n == 0 or n_cols <= 0:
        return np.zeros(0), np.zeros(0)
    if n <= n_cols:
        return mono.copy(), mono.copy()

    # 每欄的起點；用 reduceat 一次算完，比迴圈快上兩個數量級
    idx = np.linspace(0, n, n_cols + 1).astype(np.int64)[:-1]
    idx = np.maximum.accumulate(idx)              # reduceat 要求非遞減
    lo = np.minimum.reduceat(mono, idx)
    hi = np.maximum.reduceat(mono, idx)
    return lo, hi


class WaveformView(tk.Canvas):
    """畫波形 + 播放頭，點擊或拖曳可定位。"""

    def __init__(self, master, height: int = 110,
                 on_seek: Callable[[float], None] | None = None, **kw):
        super().__init__(master, height=height, bg=BG, highlightthickness=1,
                         highlightbackground="#3a3a3a", **kw)
        self.on_seek = on_seek
        self._data: np.ndarray | None = None
        self._samplerate = 48000
        self._duration = 0.0
        self._pos_sec = 0.0
        self._dragging = False
        self._items: list[int] = []
        self._head = None
        self._cw = 0
        self._ch = height

        self.bind("<Configure>", self._on_resize)
        self.bind("<Button-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Enter>", lambda e: self.config(cursor="sb_h_double_arrow"))
        self.bind("<Leave>", lambda e: self.config(cursor=""))

    # -- 資料 -------------------------------------------------------------
    def set_data(self, data: np.ndarray | None, samplerate: int) -> None:
        self._data = data
        self._samplerate = samplerate
        self._duration = 0.0 if data is None else len(data) / samplerate
        self._pos_sec = 0.0
        self._redraw()

    def set_position(self, sec: float) -> None:
        self._pos_sec = max(0.0, min(sec, self._duration))
        self._move_head()

    @property
    def dragging(self) -> bool:
        return self._dragging

    # -- 繪製 -------------------------------------------------------------
    def _on_resize(self, event) -> None:
        self._cw, self._ch = event.width, event.height
        self._redraw()

    def _redraw(self) -> None:
        self.delete("all")
        self._items = []
        self._head = None
        w, h = self._cw, self._ch
        if w <= 1 or h <= 1:
            return

        mid = h / 2
        self.create_line(0, mid, w, mid, fill=MID)

        if self._data is None or len(self._data) == 0:
            self.create_text(w / 2, mid, text="錄音後這裡會顯示波形",
                             fill=TEXT, font=("Segoe UI", 10))
            return

        lo, hi = envelope(self._data, w)
        scale = (h / 2) * 0.92
        for x in range(len(lo)):
            y1 = mid - float(hi[x]) * scale
            y2 = mid - float(lo[x]) * scale
            if abs(y1 - y2) < 1:
                y1, y2 = mid - 0.5, mid + 0.5
            self.create_line(x, y1, x, y2, fill=WAVE)

        # 每 10 秒一條刻度
        if self._duration > 0:
            step = 10.0 if self._duration > 30 else 5.0
            t = step
            while t < self._duration:
                x = t / self._duration * w
                self.create_line(x, 0, x, 6, fill=TEXT)
                self.create_line(x, h - 6, x, h, fill=TEXT)
                t += step

        self._head = self.create_line(0, 0, 0, h, fill=HEAD, width=2)
        self._move_head()

    def _move_head(self) -> None:
        if self._head is None or self._duration <= 0:
            return
        x = self._pos_sec / self._duration * self._cw
        self.coords(self._head, x, 0, x, self._ch)

    # -- 滑鼠 -------------------------------------------------------------
    def _x_to_sec(self, x: int) -> float:
        if self._cw <= 0:
            return 0.0
        return max(0.0, min(x / self._cw, 1.0)) * self._duration

    def _on_press(self, event) -> None:
        if self._duration <= 0:
            return
        self._dragging = True
        self._seek_to(event.x)

    def _on_drag(self, event) -> None:
        if self._dragging:
            self._seek_to(event.x)

    def _on_release(self, event) -> None:
        if self._dragging:
            self._seek_to(event.x)
            self._dragging = False

    def _seek_to(self, x: int) -> None:
        sec = self._x_to_sec(x)
        self.set_position(sec)
        if self.on_seek:
            self.on_seek(sec)
