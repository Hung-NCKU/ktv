"""量測麥克風軌相對系統音軌的時間偏差（硬體延遲）。

對應教學文件第 5.5 節。

原理
----
透過喇叭播一串短脈衝，同時錄下 loopback（系統軌）與麥克風（會收到喇叭的聲音）。
兩軌各自做 1 kHz 帶通 → 取包絡 → 閾值偵測脈衝起始點，套上 mixer 的對齊位移後
比較同一個脈衝落在哪裡，差值就是需要補償的延遲。

用脈衝起始點而不是互相關，是為了讓每一個脈衝的結果都能逐一檢視 —— 一眼看出
一致性，不會被單一個互相關峰值誤導。

限制
----
**必須用喇叭外放。** 戴耳機時麥克風收不到輸出，這個方法會給出看似有數字、
實際不可信的結果（本專案第一次量測就踩過：無線耳機量到 +303 ms，但訊噪比只有
1.34，純屬雜訊）。腳本會檢查脈衝偵測數量與振幅並在不可信時警告。

用法
----
    python tools/measure_latency.py                      # 用預設輸出裝置
    python tools/measure_latency.py --output "ASUS"      # 指定裝置（名稱片段）
    python tools/measure_latency.py --list               # 列出可用裝置
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ktvrec                                   # noqa: E402  (必須先匯入以設定 COM)
from ktvrec import devices                      # noqa: E402
from ktvrec.recorder import RecordSettings, Recorder   # noqa: E402

import numpy as np                              # noqa: E402
import soundcard as sc                          # noqa: E402
import soundfile as sf                          # noqa: E402

SR = 48000
BURST_HZ = 1000
BURST_SEC = 0.006


def band_envelope(x: np.ndarray, centre: float = BURST_HZ,
                  half_width: float = 150.0, smooth_sec: float = 0.004) -> np.ndarray:
    """帶通濾波後取包絡並平滑。用 FFT 遮罩實作，對一次性分析足夠。"""
    spec = np.fft.rfft(x)
    freq = np.fft.rfftfreq(len(x), 1 / SR)
    spec[(freq < centre - half_width) | (freq > centre + half_width)] = 0
    env = np.abs(np.fft.irfft(spec, n=len(x)))
    k = np.ones(max(1, int(smooth_sec * SR)))
    return np.convolve(env, k / k.sum(), mode="same")


def detect_onsets(env: np.ndarray, rel_threshold: float = 0.35,
                  min_gap_sec: float = 0.3) -> list[int]:
    """閾值穿越偵測脈衝起始點（樣本索引）。"""
    if env.max() <= 0:
        return []
    above = env > env.max() * rel_threshold
    starts = np.flatnonzero(above[1:] & ~above[:-1]) + 1
    merged: list[int] = []
    for s in starts:
        if not merged or s - merged[-1] > min_gap_sec * SR:
            merged.append(int(s))
    return merged


def make_burst_train(duration_sec: int) -> np.ndarray:
    """每秒一個 1 kHz 短脈衝的立體聲訊號（首尾各留 1 秒空白）。"""
    sig = np.zeros(SR * duration_sec, dtype=np.float32)
    n = int(BURST_SEC * SR)
    t = np.arange(n) / SR
    burst = (0.7 * np.sin(2 * np.pi * BURST_HZ * t) * np.hanning(n)).astype(np.float32)
    for k in range(1, duration_sec - 1):
        sig[int(k * SR):int(k * SR) + n] = burst
    return np.stack([sig, sig], axis=1)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--output", help="輸出裝置名稱片段；省略則用系統預設")
    ap.add_argument("--input", help="輸入裝置名稱片段；省略則用系統預設")
    ap.add_argument("--duration", type=int, default=10, help="量測長度秒數（預設 10）")
    ap.add_argument("--list", action="store_true", help="列出裝置後離開")
    ap.add_argument("--keep", action="store_true", help="保留錄下的分軌供進一步分析")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    mics = devices.list_microphones()
    loops = devices.list_loopbacks()

    if args.list:
        print("輸入裝置：")
        for d in mics:
            print(f"  {d.name}  [{d.channels}ch]")
        print("輸出裝置：")
        for d in loops:
            print(f"  {d.name}  [{d.channels}ch]")
        return 0

    def pick(pool, frag, fallback):
        if frag:
            hit = [d for d in pool if frag.lower() in d.name.lower()]
            if not hit:
                print(f"找不到名稱含「{frag}」的裝置。用 --list 看可用清單。",
                      file=sys.stderr)
                sys.exit(2)
            return hit[0]
        return fallback

    mic = pick(mics, args.input, devices.default_microphone())
    lb = pick(loops, args.output, devices.default_loopback())
    if mic is None or lb is None:
        print("找不到可用的輸入或輸出裝置。", file=sys.stderr)
        return 2

    print(f"輸入（麥克風）：{mic.name}")
    print(f"輸出（loopback）：{lb.name}")
    print(f"長度：{args.duration} 秒\n")
    print("請確認喇叭已外放、音量適中，麥克風聽得到喇叭。")
    print("量測中…\n")

    play = make_burst_train(args.duration)
    spk = sc.get_speaker(lb.id)
    rec = Recorder(RecordSettings(mic=mic, loopback=lb, out_dir="recordings"))
    rec.start()
    th = threading.Thread(target=lambda: spk.play(play, samplerate=SR), daemon=True)
    th.start()
    time.sleep(args.duration + 0.8)
    th.join(timeout=5)
    take = rec.stop()

    for e in take.errors:
        print(f"警告：{e}")

    by_name = {t.name: t for t in take.tracks}
    if "mic" not in by_name or "system" not in by_name:
        print("兩軌沒有都錄到，無法量測。", file=sys.stderr)
        take.discard()
        return 1

    t0 = min(t.t_start for t in take.tracks)
    results = {}
    for name in ("mic", "system"):
        t = by_name[name]
        data, _ = sf.read(t.path, dtype="float32", always_2d=True)
        env = band_envelope(data.mean(axis=1))
        align = int(round((t.t_start - t0) * SR))
        results[name] = {
            "onsets": [o + align for o in detect_onsets(env)],
            "env_peak": float(env.max()),
            "align_ms": align / SR * 1000.0,
        }

    expected = args.duration - 2
    print(f"{'軌道':<8} {'偵測脈衝':>8} {'包絡峰值':>10} {'對齊位移':>12}")
    for name in ("system", "mic"):
        r = results[name]
        print(f"{name:<8} {len(r['onsets']):>8} {r['env_peak']:>10.4f} "
              f"{r['align_ms']:>+10.1f} ms")
    print(f"（播出了 {expected} 個脈衝）\n")

    trustworthy = True
    for name in ("system", "mic"):
        if len(results[name]["onsets"]) < max(3, expected - 2):
            print(f"!!! {name} 軌偵測到的脈衝太少 —— 量測不可信。")
            trustworthy = False
    if results["mic"]["env_peak"] < 0.002:
        print("!!! 麥克風幾乎沒收到喇叭的聲音 —— 請改用外放喇叭，不要戴耳機。")
        trustworthy = False

    if trustworthy:
        deltas = []
        for s in results["system"]["onsets"]:
            near = [m for m in results["mic"]["onsets"] if abs(m - s) < 0.5 * SR]
            if near:
                deltas.append(min(near, key=lambda m: abs(m - s)) - s)
        if deltas:
            ms = np.array(deltas) / SR * 1000.0
            print("每個脈衝的 mic − system 差值 (ms)：")
            print("  " + "  ".join(f"{v:+.1f}" for v in ms))
            med, sd = float(np.median(ms)), float(ms.std())
            print(f"\n中位數 {med:+.1f} ms，標準差 {sd:.1f} ms")
            if sd > 5.0:
                print("!!! 標準差偏大，結果可能受噪音干擾，建議提高音量或降低環境噪音。")
            print(f"\n→ 建議的「麥克風時間微調」設定值：{-med:+.0f} ms")
            print("  （填進 GUI 的時間微調欄位，或 CLI 的 --mic-offset）")
            print()
            print("重要：單次量測不足以定論。上面的標準差只說明「這一次錄音裡的")
            print("      脈衝彼此一致」，不代表換一次錄音還會得到同樣的值。")
            print("      實測同一台機器跨次變動可達 ±15 ms —— 誤差來自串流啟動")
            print("      時刻的不確定性，不是硬體延遲在變。請至少跑三次取中位數。")
            print("      詳見 docs/TUTORIAL.md 第 5.5 節。")
            print()
            print("註：這個數字含喇叭到麥克風的空氣傳播（每公尺約 2.9 ms）。")
            print("    戴耳機錄音時沒有這一項，實際需要的值會略小。")
        else:
            print("!!! 配對不到脈衝，量測失敗。")

    if args.keep:
        print(f"\n分軌保留在：{take.temp_dir}")
    else:
        take.discard()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
