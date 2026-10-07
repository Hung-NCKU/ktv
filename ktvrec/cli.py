"""命令列介面：互動式裝置選單，或用參數直接指定。

用法：
    python -m ktvrec.cli                 互動選單，Enter 開始、Enter 停止
    python -m ktvrec.cli --list          只列出裝置
    python -m ktvrec.cli --mic 3 --sys 1 --duration 180 --format mp3
"""

from __future__ import annotations

import argparse
import sys
import time

from . import config, devices, encode
from .recorder import RecordSettings, Recorder


def _fix_console() -> None:
    """把主控台切成 UTF-8，裝置名稱才不會因為編碼爆掉。

    這件事刻意在 Python 裡做，而不是在 .bat 裡寫 `chcp 65001` —— 批次檔中途切換
    編碼會讓 cmd.exe 的位元組讀取位置錯位，後面每一行都被打亂（連純 ASCII 的
    `cd /d "%~dp0"` 都會變成 `'~dp0"' is not recognized`）。
    """
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:                                   # noqa: BLE001
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass


def print_devices(mics: list[devices.Device], loops: list[devices.Device]) -> None:
    print("\n輸入裝置（麥克風）")
    print("  0) （不錄麥克風）")
    for i, d in enumerate(mics, 1):
        print(f"  {i}) {d.name}  [{d.channels}ch]")
    print("\n輸出裝置（錄這台電腦正在播放的聲音）")
    print("  0) （不錄系統聲音）")
    for i, d in enumerate(loops, 1):
        print(f"  {i}) {d.name}  [{d.channels}ch]")
    print()


def _ask(prompt: str, n: int, default: int) -> int:
    while True:
        raw = input(f"{prompt} [預設 {default}]: ").strip()
        if not raw:
            return default
        if raw.isdigit() and 0 <= int(raw) <= n:
            return int(raw)
        print(f"  請輸入 0 到 {n} 之間的數字。")


def _default_index(pool: list[devices.Device], dev: devices.Device | None) -> int:
    if dev is None:
        return 0
    for i, d in enumerate(pool, 1):
        if d.id == dev.id:
            return i
    return 1 if pool else 0


def _interactive() -> bool:
    return sys.stdin is not None and sys.stdin.isatty()


def _describe(report: dict) -> None:
    """印出這次錄音的品質資訊（還沒存檔）。"""
    print(f"\n錄好了：{report['duration_sec']:.1f} 秒，峰值 {report['peak']:.3f}"
          + ("（已自動降增益）" if report.get("normalized") else ""))
    for t in report["tracks"]:
        fix = "已修正" if t.get("drift_fixed") else "未修正"
        drops = t.get("discontinuities", 0)
        drop_txt = f"，掉資料 {drops} 次" if drops else ""
        adj = t.get("manual_offset_ms", 0.0)
        adj_txt = f"，手動微調 {adj:+.0f} ms" if adj else ""
        print(f"  [{t['name']}] 起始偏移 {t['offset_ms']:+.0f} ms{adj_txt}，"
              f"時脈飄移 {t['drift_ms']:+.1f} ms（{fix}），"
              f"實測取樣率 {t['effective_sr']:.1f} Hz{drop_txt}")


def _report(result: dict) -> None:
    for e in result.get("errors", []):
        print(f"警告：{e}")
    mix = result["mix"]
    print(f"\n已存檔：{mix.get('converted', mix['path'])}")
    for p in result.get("stems", []):
        print(f"  分軌：{p}")


def main(argv: list[str] | None = None) -> int:
    _fix_console()
    ap = argparse.ArgumentParser(
        prog="ktvrec", description="同時錄製麥克風與電腦播放的聲音")
    ap.add_argument("--list", action="store_true", help="列出所有裝置後離開")
    ap.add_argument("--mic", type=int, help="輸入裝置編號（0 = 不錄）")
    ap.add_argument("--sys", type=int, dest="sysdev", help="輸出裝置編號（0 = 不錄）")
    ap.add_argument("--duration", type=float, help="錄幾秒後自動停止；省略則按 Enter 停止")
    ap.add_argument("--format", default=None, choices=["wav", "mp3", "m4a", "flac"],
                    help="混音檔格式")
    ap.add_argument("--out", default=None, help="儲存資料夾")
    ap.add_argument("--mic-gain", type=float, default=None, help="麥克風增益 dB")
    ap.add_argument("--sys-gain", type=float, default=None, help="系統音增益 dB")
    ap.add_argument("--no-stems", action="store_true", help="混音後刪掉分軌檔")
    ap.add_argument("--mic-offset", type=float, default=None,
                    help="麥克風時間微調毫秒（負值把人聲往前拉，補償硬體延遲）")
    ap.add_argument("--yes", "-y", action="store_true",
                    help="錄完直接存檔，不詢問（給腳本用）")
    args = ap.parse_args(argv)

    cfg = config.load()
    mics = devices.list_microphones()
    loops = devices.list_loopbacks()

    if args.list:
        print_devices(mics, loops)
        return 0

    if args.mic is None or args.sysdev is None:
        print_devices(mics, loops)

    mic_i = args.mic
    if mic_i is None:
        mic_i = _ask("選擇輸入裝置", len(mics),
                     _default_index(mics, devices.default_microphone()))
    sys_i = args.sysdev
    if sys_i is None:
        sys_i = _ask("選擇輸出裝置", len(loops),
                     _default_index(loops, devices.default_loopback()))

    mic = mics[mic_i - 1] if 0 < mic_i <= len(mics) else None
    lb = loops[sys_i - 1] if 0 < sys_i <= len(loops) else None
    if mic is None and lb is None:
        print("錯誤：至少要選一個來源。", file=sys.stderr)
        return 2

    fmt = args.format or cfg.get("export_format", "wav")
    settings = RecordSettings(
        mic=mic, loopback=lb,
        samplerate=int(cfg.get("samplerate", 48000)),
        pull_frames=int(cfg.get("pull_frames", 4800)),
        buffer_frames=int(cfg.get("buffer_frames", 48000)),
        mic_gain_db=args.mic_gain if args.mic_gain is not None else float(cfg.get("mic_gain_db", 0.0)),
        sys_gain_db=args.sys_gain if args.sys_gain is not None else float(cfg.get("sys_gain_db", 0.0)),
        out_dir=args.out or cfg.get("out_dir", "recordings"),
        keep_stems=not args.no_stems,
        normalize=bool(cfg.get("normalize", True)),
    )

    rec = Recorder(settings)
    try:
        session = rec.start()
    except Exception as exc:                                # noqa: BLE001
        print(f"開始錄音失敗：{exc}", file=sys.stderr)
        return 1

    print(f"\n● 錄音中 → {session}")
    print(f"  輸入：{mic.name if mic else '（無）'}")
    print(f"  輸出：{lb.name if lb else '（無）'}")

    try:
        if args.duration:
            end = time.perf_counter() + args.duration
            while time.perf_counter() < end:
                _print_status(rec)
                time.sleep(0.1)
        else:
            print("  按 Enter 停止…\n")
            stopper = _EnterWatcher()
            while not stopper.hit:
                _print_status(rec)
                time.sleep(0.1)
    except KeyboardInterrupt:
        pass

    print("\n停止中，正在對齊並混音…")
    take = rec.stop()
    for e in take.errors:
        print(f"警告：{e}")
    if not take.has_audio:
        print("沒有錄到任何音訊。")
        take.discard()
        return 1

    normalize = bool(cfg.get("normalize", True))
    mic_offset = (args.mic_offset if args.mic_offset is not None
                  else float(cfg.get("mic_offset_ms", 0.0)))
    _describe(take.remix(normalize=normalize,
                         offsets_ms={"mic": mic_offset}))

    # 存檔前先確認。腳本用 --yes，或在沒有終端機時自動存。
    if not args.yes and _interactive():
        if input("\n要存檔嗎？[Y/n] ").strip().lower() in ("n", "no"):
            take.discard()
            print("已捨棄，沒有留下任何檔案。")
            return 0

    result = take.save(fmt=fmt, bitrate=cfg.get("mp3_bitrate", "320k"),
                       keep_stems=not args.no_stems, normalize=normalize)
    _report(result)

    cfg.update({
        "mic_id": mic.id if mic else None,
        "loopback_id": lb.id if lb else None,
        "export_format": fmt,
        "mic_offset_ms": mic_offset,
        "out_dir": settings.out_dir,
    })
    config.save(cfg)
    return 0


class _EnterWatcher:
    """背景等 Enter，讓主迴圈可以同時畫音量條。"""

    def __init__(self) -> None:
        import threading
        self.hit = False
        threading.Thread(target=self._wait, daemon=True).start()

    def _wait(self) -> None:
        try:
            input()
        except (EOFError, OSError):
            pass
        self.hit = True


def _bar(v: float, width: int = 24) -> str:
    import math
    db = -60.0 if v <= 1e-6 else max(-60.0, 20.0 * math.log10(v))
    n = int((db + 60.0) / 60.0 * width)
    return "#" * n + "-" * (width - n)


def _print_status(rec: Recorder) -> None:
    lv = rec.levels()
    t = rec.elapsed
    sys.stdout.write(
        f"\r  {int(t // 60):02d}:{t % 60:04.1f}  "
        f"MIC [{_bar(lv.mic_rms)}]  SYS [{_bar(lv.sys_rms)}]  ")
    sys.stdout.flush()


if __name__ == "__main__":
    raise SystemExit(main())
