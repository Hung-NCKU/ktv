"""試聽播放器：對記憶體裡的混音做播放／暫停／拖曳定位。

跑一條餵食執行緒，每次送一塊資料進 WASAPI，主執行緒改動播放位置時下一塊就會
生效 —— 拖曳定位才不會卡。

**每塊的大小不能小於 WASAPI 緩衝區**，這是 soundcard 的實作逼出來的限制。它的
`_Player.play()` 長這樣：

    towrite = self._render_available_frames()   # 緩衝區目前的空位
    bytes   = data[:towrite].ravel().tobytes()  # 但 data 可能比空位短
    buffer  = self._render_buffer(towrite)      # 仍然跟 WASAPI 要 towrite 個 frame
    _ffi.memmove(buffer[0], bytes, len(bytes))  # 只填了 len(bytes)
    self._render_release(towrite)               # 卻宣告 towrite 個 frame 都有效

餵進去的資料比空位短時，沒填到的部分會把緩衝區裡的殘留內容當成音訊播出去，
播放速率也會因此掉到即時的八成左右。只要每次都餵滿一個緩衝區的量，
`data[:towrite]` 就一定拿得到 towrite 個 frame，最後不足的一塊補零即可。
"""

from __future__ import annotations

import threading
import time

import numpy as np
import soundcard as sc

from . import comapt


class Player:
    """播放一段 (frames, 2) 的 float32 音訊。"""

    def __init__(self, data: np.ndarray, samplerate: int, device_id: str,
                 buffer_frames: int = 4800):
        self.data = data
        self.samplerate = samplerate
        self.device_id = device_id
        self.buffer = buffer_frames
        # 見模組說明：每塊必須餵滿整個緩衝區，否則會播出殘留資料。
        self.chunk = buffer_frames

        self._lock = threading.Lock()
        self._pos = 0                       # 已經「餵進去」的位置
        self._playing = threading.Event()
        self._quit = threading.Event()
        self._thread: threading.Thread | None = None
        self.error: BaseException | None = None
        self.on_finish = None               # 播到結尾時呼叫（在餵食執行緒上）

    # -- 控制 -------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="ktvrec-player",
                                        daemon=True)
        self._thread.start()

    def play(self) -> None:
        if self.frames == 0:
            return
        with self._lock:
            if self._pos >= self.frames:
                self._pos = 0
        self.start()
        self._playing.set()

    def pause(self) -> None:
        self._playing.clear()

    def toggle(self) -> None:
        if self.is_playing:
            self.pause()
        else:
            self.play()

    def seek(self, frame: int) -> None:
        with self._lock:
            self._pos = int(max(0, min(frame, self.frames)))

    def close(self) -> None:
        self._playing.clear()
        self._quit.set()
        th = self._thread
        if th is not None:
            th.join(timeout=3.0)
        self._thread = None

    # -- 狀態 -------------------------------------------------------------
    @property
    def frames(self) -> int:
        return 0 if self.data is None else len(self.data)

    @property
    def is_playing(self) -> bool:
        return self._playing.is_set()

    @property
    def position(self) -> int:
        """目前「聽得到」的位置。扣掉還積在緩衝區裡、尚未出聲的部分。"""
        with self._lock:
            pos = self._pos
        if self._playing.is_set():
            pos -= self.buffer
        return int(max(0, min(pos, self.frames)))

    @property
    def position_sec(self) -> float:
        return self.position / self.samplerate

    @property
    def duration_sec(self) -> float:
        return self.frames / self.samplerate

    # -- 餵食執行緒 -------------------------------------------------------
    def _run(self) -> None:
        held = comapt.init_mta()
        try:
            spk = sc.get_speaker(self.device_id)
            with spk.player(samplerate=self.samplerate, channels=2,
                            blocksize=self.buffer) as p:
                while not self._quit.is_set():
                    if not self._playing.is_set():
                        time.sleep(0.02)
                        continue
                    with self._lock:
                        start = self._pos
                        end = min(start + self.chunk, self.frames)
                        self._pos = end
                    if start >= self.frames:
                        self._playing.clear()
                        if self.on_finish:
                            self.on_finish()
                        continue
                    block = self.data[start:end]
                    if len(block) < self.chunk:
                        # 最後一塊補零到整個緩衝區，避免殘留資料被當成音訊播出
                        block = np.pad(block, ((0, self.chunk - len(block)), (0, 0)))
                    p.play(block)
        except BaseException as exc:                        # noqa: BLE001
            self.error = exc
            self._playing.clear()
        finally:
            if held:
                comapt.uninit()
