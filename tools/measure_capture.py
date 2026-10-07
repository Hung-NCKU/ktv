"""量測擷取品質：掉樣本次數與時脈飄移。

對應教學文件第 5.2、5.3 節。

這支腳本**不使用** ktvrec 的 Recorder，而是自己實作擷取迴圈 —— 這樣才能切換
「有問題的設定」與「修好的設定」做對照：

    --buffer-frames 1024 --inline-write    重現掉樣本（原本的錯誤設定）
    （預設）                                大緩衝 + 獨立寫檔執行緒

輸出的三個數字
--------------
掉資料次數
    WASAPI 回報 AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY 的次數。非零代表有聲音
    永遠消失了，而且兩軌從此錯開。

偏差 (ms)
    取樣數對應的時間 減去 牆鐘時間。掉樣本會讓這個值變負。

比例 (%)
    偏差 ÷ 錄音長度。**這是判斷「真飄移」或「掉樣本」的關鍵**：
    真正的時脈差會讓比例在不同長度下保持一致；掉樣本不會。
    用 --duration 跑 10/30/60 秒各一次就看得出來。

用法
----
    python tools/measure_capture.py --duration 30
    python tools/measure_capture.py --duration 60 --buffer-frames 1024 --inline-write
    python tools/measure_capture.py --sweep            # 自動跑 10/30/60 秒
"""

from __future__ import annotations

import argparse
import os
import queue
import shutil
import sys
import tempfile
import threading
import time
import warnings

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ktvrec                                   # noqa: E402  (必須先匯入以設定 COM)
from ktvrec import comapt, devices              # noqa: E402

import numpy as np                              # noqa: E402
import soundcard as sc                          # noqa: E402
import soundfile as sf                          # noqa: E402

SR = 48000

_DROPS: dict[int, int] = {}
_hooked = False


def _install_hook() -> None:
    """soundcard 只用 warnings 回報掉資料；用 thread id 分辨是哪一軌。"""
    global _hooked
    if _hooked:
        return
    prev = warnings.showwarning

    def hook(message, category, filename, lineno, file=None, line=None):
        if "discontinuity" in str(message):
            tid = threading.get_ident()
            if tid in _DROPS:
                _DROPS[tid] += 1
                return
        prev(message, category, filename, lineno, file, line)

    warnings.showwarning = hook
    _hooked = True


class Capture(threading.Thread):
    """單一來源的擷取執行緒。inline_write=True 時刻意把寫入放在迴圈內。"""

    def __init__(self, dev, label, seconds, buffer_frames, pull_frames,
                 inline_write, out_dir):
        super().__init__(name=f"cap-{label}", daemon=True)
        self.dev, self.label, self.seconds = dev, label, seconds
        self.buffer_frames, self.pull_frames = buffer_frames, pull_frames
        self.inline_write = inline_write
        self.out_dir = out_dir
        self.frames = 0
        self.drops = 0
        self.span = 0.0
        self.error: BaseException | None = None

    def run(self) -> None:
        held = comapt.init_mta()
        tid = threading.get_ident()
        _DROPS[tid] = 0
        _install_hook()
        q: queue.Queue = queue.Queue(maxsize=2000)
        writer = None
        path = os.path.join(self.out_dir, f"{self.label}.wav")
        try:
            src = devices.open_recorder_device(self.dev)
            ch = min(2, max(1, self.dev.channels))
            fh = sf.SoundFile(path, mode="w", samplerate=SR, channels=ch,
                              subtype="PCM_24")

            if not self.inline_write:
                def drain():
                    while True:
                        item = q.get()
                        if item is None:
                            break
                        fh.write(item)
                writer = threading.Thread(target=drain, daemon=True)
                writer.start()

            with src.recorder(samplerate=SR, channels=ch,
                              blocksize=self.buffer_frames) as rec:
                rec.record(numframes=self.pull_frames)     # 丟掉暖機那一塊
                _DROPS[tid] = 0
                t_start = time.perf_counter()
                while time.perf_counter() - t_start < self.seconds:
                    data = rec.record(numframes=self.pull_frames)
                    if data.size == 0:
                        continue
                    self.frames += data.shape[0]
                    if self.inline_write:
                        # 原本的錯誤設計：磁碟 I/O 直接擋在擷取迴圈裡
                        fh.write(data)
                        mag = np.abs(data)
                        float(mag.max())
                        float(np.sqrt(np.mean(np.square(data))))
                    else:
                        q.put(data)
                self.span = time.perf_counter() - t_start
        except BaseException as exc:                       # noqa: BLE001
            self.error = exc
        finally:
            if writer is not None:
                q.put(None)
                writer.join(timeout=30)
            try:
                fh.close()
            except BaseException:                          # noqa: BLE001
                pass
            self.drops = _DROPS.pop(tid, 0)
            if held:
                comapt.uninit()


def run_once(mic, lb, seconds, buffer_frames, pull_frames, inline_write) -> list[dict]:
    # 寫到真的磁碟上 —— inline 模式的重點就是磁碟 I/O 擋住擷取迴圈
    out_dir = tempfile.mkdtemp(prefix="ktvrec-measure-")
    workers = [Capture(d, n, seconds, buffer_frames, pull_frames, inline_write, out_dir)
               for d, n in ((mic, "mic"), (lb, "system")) if d is not None]
    for w in workers:
        w.start()
    for w in workers:
        w.join(timeout=seconds + 60)
    shutil.rmtree(out_dir, ignore_errors=True)

    rows = []
    for w in workers:
        if w.error is not None:
            rows.append({"label": w.label, "error": str(w.error)})
            continue
        expected = w.span * SR
        dev_ms = (w.frames - expected) / SR * 1000.0
        rows.append({
            "label": w.label,
            "drops": w.drops,
            "frames": w.frames,
            "span": w.span,
            "dev_ms": dev_ms,
            "ratio": dev_ms / (w.span * 1000.0) * 100.0 if w.span else 0.0,
            "eff_sr": w.frames / w.span if w.span else 0.0,
        })
    return rows


def report(rows: list[dict], seconds: int, tag: str) -> None:
    for r in rows:
        if "error" in r:
            print(f"  {tag} {seconds:>3}s  {r['label']:<7} 錯誤: {r['error']}")
            continue
        print(f"  {tag} {seconds:>3}s  {r['label']:<7} "
              f"掉資料={r['drops']:>3}  偏差={r['dev_ms']:>+8.1f} ms  "
              f"比例={r['ratio']:>+8.4f}%  實測sr={r['eff_sr']:>9.1f} Hz")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--duration", type=int, default=30, help="錄音長度秒數（預設 30）")
    ap.add_argument("--buffer-frames", type=int, default=48000,
                    help="WASAPI 緩衝區 frame 數（預設 48000 = 1 秒；1024 ≈ 21 ms 可重現掉樣本）")
    ap.add_argument("--pull-frames", type=int, default=4800,
                    help="每次讀取的 frame 數（預設 4800 = 100 ms）")
    ap.add_argument("--inline-write", action="store_true",
                    help="把資料處理放在擷取迴圈內（重現原本的錯誤設計）")
    ap.add_argument("--sweep", action="store_true",
                    help="自動跑 10/30/60 秒，用來檢查比例是否隨長度一致")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    mic = devices.default_microphone()
    lb = devices.default_loopback()
    if mic is None and lb is None:
        print("找不到可用裝置。", file=sys.stderr)
        return 2
    print(f"輸入：{mic.name if mic else '（無）'}")
    print(f"輸出：{lb.name if lb else '（無）'}")

    if args.pull_frames > args.buffer_frames:
        # 每次讀的量超過緩衝區容量時，soundcard 會在內部分多次取，
        # 行為仍然正確但不直觀；對齊兩者讓量測結果好解讀。
        print(f"（--pull-frames {args.pull_frames} 大於緩衝區 {args.buffer_frames}，"
              f"自動調整為 {args.buffer_frames}）")
        args.pull_frames = args.buffer_frames

    mode = ("小緩衝 + 迴圈內處理（有問題的設定）" if args.inline_write
            else f"{args.buffer_frames / SR:.2f} 秒緩衝 + 獨立執行緒")
    print(f"設定：{mode}，每次讀 {args.pull_frames / SR * 1000:.0f} ms\n")

    durations = [10, 30, 60] if args.sweep else [args.duration]
    tag = "壞" if args.inline_write else "好"
    for d in durations:
        print(f"錄 {d} 秒…")
        rows = run_once(mic, lb, d, args.buffer_frames, args.pull_frames,
                        args.inline_write)
        report(rows, d, tag)

    print("\n判讀方式：")
    print("  掉資料 > 0            → 有聲音永遠消失，兩軌已錯開，這次錄音不可信")
    print("  比例在不同長度下一致  → 真的時脈差，值得用重新取樣修正")
    print("  比例不一致／符號會變  → 不是時脈差，先去找掉樣本的原因")
    print("  兩軌偏差差距很大      → 同上，掉樣本的典型特徵")
    print()
    print("注意：掉資料警告取決於當下的 CPU 與磁碟負載，不一定每次都觸發。")
    print("      即使警告 0 次，偏差異常放大（或兩軌不對稱）就已經是警訊。")
    print("      想穩定重現，請一邊製造負載（編譯、複製大檔案）一邊量測。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
