"""一次錄好、但還沒存檔的錄音。

錄音結束後分軌其實已經在硬碟上了 —— 放在輸出資料夾底下的 `.pending_<時間戳>`
暫存目錄裡。這樣做有兩個好處：不用把整首歌扛在記憶體裡（五分鐘雙軌就要 200 MB
以上），程式當掉時素材也還在。

按下「確定存檔」之前，混音只存在記憶體中，可以反覆調增益重混、試聽。按下之後
才把混音寫出來、把分軌搬進正式資料夾；選擇捨棄就整個暫存目錄刪掉。
"""

from __future__ import annotations

import os
import shutil

import numpy as np

from . import encode
from .mixer import TrackInfo, build_mix, write_mix

PENDING_PREFIX = ".pending_"


class Take:
    """一次錄音的成果，尚未落地。"""

    def __init__(self, temp_dir: str, stamp: str, tracks: list[TrackInfo],
                 samplerate: int, out_dir: str, subtype: str = "PCM_24",
                 errors: list[str] | None = None):
        self.temp_dir = temp_dir
        self.stamp = stamp
        self.tracks = tracks
        self.samplerate = samplerate
        self.out_dir = out_dir
        self.subtype = subtype
        self.errors = errors or []

        self.mix: np.ndarray | None = None
        self.report: dict = {}
        self.saved_to: str | None = None
        self._cache: dict = {}

    # -- 混音 -------------------------------------------------------------
    def remix(self, gains_db: dict[str, float] | None = None,
              normalize: bool = True,
              offsets_ms: dict[str, float] | None = None) -> dict:
        """依目前（或指定）的增益與時間微調重新混音到記憶體。"""
        if gains_db:
            for t in self.tracks:
                if t.name in gains_db:
                    t.gain_db = gains_db[t.name]
        if offsets_ms:
            for t in self.tracks:
                if t.name in offsets_ms:
                    t.manual_offset_ms = offsets_ms[t.name]
        self.mix, self.report = build_mix(self.tracks, self.samplerate,
                                          normalize=normalize, cache=self._cache)
        return self.report

    @property
    def duration_sec(self) -> float:
        if self.mix is None:
            return 0.0
        return len(self.mix) / self.samplerate

    @property
    def has_audio(self) -> bool:
        return bool(self.tracks)

    # -- 落地 -------------------------------------------------------------
    def save(self, fmt: str = "wav", bitrate: str = "320k",
             keep_stems: bool = True, normalize: bool = True) -> dict:
        """把混音寫出來、分軌搬進正式資料夾。回傳結果 dict。"""
        if self.mix is None:
            self.remix(normalize=normalize)
        assert self.mix is not None

        final_dir = os.path.abspath(os.path.join(self.out_dir, self.stamp))
        os.makedirs(final_dir, exist_ok=True)

        mix_path = os.path.join(final_dir, f"{self.stamp}_mix.wav")
        write_mix(self.mix, self.samplerate, mix_path, self.subtype)

        report = dict(self.report)
        report["path"] = mix_path

        errors = list(self.errors)
        if fmt != "wav":
            try:
                report["converted"] = encode.convert(mix_path, fmt, bitrate)
            except Exception as exc:                        # noqa: BLE001
                errors.append(f"轉檔：{exc}")

        stems: list[str] = []
        if keep_stems:
            for t in self.tracks:
                dst = os.path.join(final_dir, os.path.basename(t.path))
                try:
                    shutil.move(t.path, dst)
                    stems.append(dst)
                except OSError as exc:
                    errors.append(f"搬移分軌 {os.path.basename(t.path)}：{exc}")

        self._cleanup_temp()
        self.saved_to = final_dir
        return {
            "session_dir": final_dir,
            "stems": stems,
            "errors": errors,
            "mix": report,
        }

    def discard(self) -> None:
        """丟掉這次錄音，刪除暫存目錄。"""
        self.mix = None
        self._cache.clear()
        self._cleanup_temp()

    def _cleanup_temp(self) -> None:
        if self.temp_dir and os.path.isdir(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)


def sweep_pending(out_dir: str) -> list[str]:
    """列出輸出資料夾裡殘留的暫存目錄（程式上次沒正常收尾時會留下）。"""
    try:
        entries = os.listdir(out_dir)
    except OSError:
        return []
    return [os.path.join(out_dir, e) for e in entries
            if e.startswith(PENDING_PREFIX)
            and os.path.isdir(os.path.join(out_dir, e))]
