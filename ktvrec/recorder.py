"""雙軌同步錄音核心：麥克風 + 系統播放（WASAPI loopback）。

兩條串流各跑一個執行緒、各自寫入獨立 WAV，互不干擾；停止後再依時間戳
對齊混音。這樣任一軌爆音或後悔了，原始素材都還在。

為了不掉樣本，這裡有兩個關鍵設計：

1. WASAPI 緩衝區開得比每次讀取的量大很多。soundcard 的 `blocksize` 參數直接
   決定 WASAPI 的 buffer duration（見 mediafoundation.py 的 Initialize 呼叫），
   預設 1024 frames 只有 21 ms，任何排程停頓都會造成 overrun。

2. 檔案寫入交給獨立的 writer 執行緒，擷取迴圈只負責把資料丟進佇列。磁碟一旦
   卡頓，擷取迴圈不會跟著停下來。
"""

from __future__ import annotations

import queue
import threading
import time
import warnings
from dataclasses import dataclass
from datetime import datetime
import os

import numpy as np
import soundfile as sf

from . import comapt, devices
from .mixer import TrackInfo
from .take import PENDING_PREFIX, Take

# soundcard 只用 warnings 回報 WASAPI 的 data discontinuity，沒有其他管道。
# 警告是在擷取執行緒上發出的，所以用 thread id 當 key 就能分辨是哪一軌掉的。
_DISCONTINUITIES: dict[int, int] = {}
_HOOK_LOCK = threading.Lock()
_hook_installed = False


def _install_warning_hook() -> None:
    global _hook_installed
    with _HOOK_LOCK:
        if _hook_installed:
            return
        prev = warnings.showwarning

        def hook(message, category, filename, lineno, file=None, line=None):
            if "discontinuity" in str(message):
                tid = threading.get_ident()
                if tid in _DISCONTINUITIES:
                    _DISCONTINUITIES[tid] += 1
                    return
            prev(message, category, filename, lineno, file, line)

        warnings.showwarning = hook
        # soundcard 用 simplefilter('always')，重複的警告不會被壓掉。
        _hook_installed = True


@dataclass
class Levels:
    """給 UI 用的即時音量。"""

    mic_rms: float = 0.0
    mic_peak: float = 0.0
    sys_rms: float = 0.0
    sys_peak: float = 0.0


@dataclass
class RecordSettings:
    mic: devices.Device | None
    loopback: devices.Device | None
    samplerate: int = 48000
    pull_frames: int = 4800        # 每次從 WASAPI 讀多少（100 ms）
    buffer_frames: int = 48000     # WASAPI 緩衝區大小（1 秒，留足排程餘裕）
    queue_blocks: int = 600        # writer 佇列深度（約 60 秒的緩衝）
    mic_gain_db: float = 0.0
    sys_gain_db: float = 0.0
    out_dir: str = "recordings"
    keep_stems: bool = True        # 保留分軌原始檔
    normalize: bool = True
    subtype: str = "PCM_24"


def _drain(q: "queue.Queue", fh: sf.SoundFile) -> list[BaseException]:
    """把佇列裡的資料寫進檔案，直到收到 None。"""
    errors: list[BaseException] = []
    try:
        while True:
            item = q.get()
            if item is None:
                break
            try:
                fh.write(item)
            except BaseException as exc:      # noqa: BLE001
                errors.append(exc)
                break
    finally:
        try:
            fh.close()
        except BaseException:                 # noqa: BLE001
            pass
    return errors


class _StreamWorker(threading.Thread):
    """單一來源的擷取執行緒。"""

    def __init__(self, dev: devices.Device, path: str, settings: RecordSettings,
                 stop_evt: threading.Event, label: str):
        super().__init__(name=f"ktvrec-{label}", daemon=True)
        self.dev = dev
        self.path = path
        self.settings = settings
        self.stop_evt = stop_evt
        self.label = label

        self.t_start = 0.0
        self.t_stop = 0.0
        self.frames = 0
        self.discontinuities = 0
        self.error: BaseException | None = None
        self.ready = threading.Event()
        self.rms = 0.0
        self.peak = 0.0
        self._tid = 0
        self._channels = min(2, max(1, dev.channels))

    @property
    def drops(self) -> int:
        """錄音進行中也讀得到的掉資料次數（結束後用收尾時存下的值）。"""
        return _DISCONTINUITIES.get(self._tid, self.discontinuities)

    def run(self) -> None:
        # 主執行緒是 STA（給 Shell 對話框用），擷取執行緒要自己進 MTA，
        # WASAPI 物件才會建立在這條執行緒自己的 apartment 裡。
        held = comapt.init_mta()
        tid = threading.get_ident()
        self._tid = tid
        _DISCONTINUITIES[tid] = 0
        _install_warning_hook()

        s = self.settings
        q: queue.Queue = queue.Queue(maxsize=s.queue_blocks)
        writer: threading.Thread | None = None
        drain_errors: list[BaseException] = []

        try:
            src = devices.open_recorder_device(self.dev)
            fh = sf.SoundFile(self.path, mode="w", samplerate=s.samplerate,
                              channels=self._channels, subtype=s.subtype)

            def run_writer() -> None:
                drain_errors.extend(_drain(q, fh))

            writer = threading.Thread(target=run_writer,
                                      name=f"ktvrec-{self.label}-writer", daemon=True)
            writer.start()

            with src.recorder(samplerate=s.samplerate, channels=self._channels,
                              blocksize=s.buffer_frames) as rec:
                # 丟掉第一塊：串流剛開時常有暖機延遲，會污染時間基準
                rec.record(numframes=s.pull_frames)
                _DISCONTINUITIES[tid] = 0
                self.t_start = time.perf_counter()
                self.ready.set()
                while not self.stop_evt.is_set():
                    data = rec.record(numframes=s.pull_frames)
                    if data.size == 0:
                        continue
                    self.frames += data.shape[0]
                    q.put(data)
                    mag = np.abs(data)
                    self.peak = float(mag.max())
                    self.rms = float(np.sqrt(np.mean(np.square(data))))
                self.t_stop = time.perf_counter()
        except BaseException as exc:      # noqa: BLE001 - 回報給主執行緒
            self.error = exc
            self.t_stop = self.t_stop or time.perf_counter()
            self.ready.set()
        finally:
            if writer is not None:
                q.put(None)
                writer.join(timeout=60.0)
            if drain_errors and self.error is None:
                self.error = drain_errors[0]
            self.discontinuities = _DISCONTINUITIES.pop(tid, 0)
            if held:
                comapt.uninit()


class Recorder:
    """啟動/停止一次錄音工作。"""

    def __init__(self, settings: RecordSettings):
        self.settings = settings
        self._stop = threading.Event()
        self._workers: list[_StreamWorker] = []
        self._session_dir = ""
        self._base = ""
        self._t_begin = 0.0
        self.running = False

    # -- 生命週期 ---------------------------------------------------------
    def start(self) -> str:
        if self.running:
            raise RuntimeError("已經在錄音中")
        s = self.settings
        if s.mic is None and s.loopback is None:
            raise ValueError("至少要選一個來源（麥克風或系統播放）")

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        # 先錄進暫存目錄，等使用者按「確定存檔」才搬進正式資料夾。
        # 放在同一個輸出資料夾底下，搬移才是同磁碟改名而不是複製。
        os.makedirs(os.path.abspath(s.out_dir), exist_ok=True)
        self._session_dir = os.path.abspath(
            os.path.join(s.out_dir, f"{PENDING_PREFIX}{stamp}"))
        os.makedirs(self._session_dir, exist_ok=True)
        self._base = stamp
        self._stop.clear()
        self._workers = []

        if s.mic is not None:
            self._workers.append(_StreamWorker(
                s.mic, os.path.join(self._session_dir, "mic.wav"), s,
                self._stop, "mic"))
        if s.loopback is not None:
            self._workers.append(_StreamWorker(
                s.loopback, os.path.join(self._session_dir, "system.wav"), s,
                self._stop, "system"))

        for w in self._workers:
            w.start()
        for w in self._workers:
            w.ready.wait(timeout=15.0)

        failed = [w for w in self._workers if w.error is not None]
        if failed:
            self._stop.set()
            for w in self._workers:
                w.join(timeout=5.0)
            raise RuntimeError(
                "無法開啟音訊串流：\n" +
                "\n".join(f"  - {w.label}: {w.error}" for w in failed))

        self._t_begin = time.perf_counter()
        self.running = True
        return self._session_dir

    def stop(self) -> Take:
        """停止錄音，回傳一個「還沒存檔」的 Take。"""
        if not self.running:
            raise RuntimeError("目前沒有在錄音")
        self._stop.set()
        for w in self._workers:
            w.join(timeout=90.0)
        self.running = False

        errs = [f"{w.label}: {w.error}" for w in self._workers if w.error]
        for w in self._workers:
            if w.discontinuities:
                errs.append(
                    f"{w.label}: 擷取中掉了 {w.discontinuities} 次資料，"
                    "這一軌的時脈修正已停用（見 README「時脈飄移修正」）")

        tracks: list[TrackInfo] = []
        for w in self._workers:
            if w.error is not None or w.frames == 0:
                continue
            gain = (self.settings.mic_gain_db if w.label == "mic"
                    else self.settings.sys_gain_db)
            tracks.append(TrackInfo(path=w.path, t_start=w.t_start,
                                    t_stop=w.t_stop, gain_db=gain,
                                    name=w.label,
                                    discontinuities=w.discontinuities))

        return Take(temp_dir=self._session_dir, stamp=self._base, tracks=tracks,
                    samplerate=self.settings.samplerate,
                    out_dir=self.settings.out_dir,
                    subtype=self.settings.subtype, errors=errs)

    # -- 狀態 -------------------------------------------------------------
    @property
    def elapsed(self) -> float:
        return time.perf_counter() - self._t_begin if self.running else 0.0

    def levels(self) -> Levels:
        lv = Levels()
        for w in self._workers:
            if w.label == "mic":
                lv.mic_rms, lv.mic_peak = w.rms, w.peak
            else:
                lv.sys_rms, lv.sys_peak = w.rms, w.peak
        return lv

    @property
    def dropped(self) -> int:
        """目前為止所有軌道的掉資料次數。"""
        return sum(w.drops for w in self._workers)

    @property
    def session_dir(self) -> str:
        return self._session_dir
