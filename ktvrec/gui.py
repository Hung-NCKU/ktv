"""圖形介面：選擇輸入/輸出裝置、看即時音量、錄音、試聽、確認後才存檔。"""

from __future__ import annotations

import math
import os
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import comapt, config, devices, encode
from .player import Player
from .recorder import RecordSettings, Recorder
from .take import Take, sweep_pending
from .waveform import WaveformView

METER_W, METER_H = 240, 13


def _db(x: float) -> float:
    """線性振幅轉 dBFS，下限 -60。"""
    return -60.0 if x <= 1e-6 else max(-60.0, 20.0 * math.log10(x))


def _mmss(sec: float) -> str:
    sec = max(0.0, sec)
    return f"{int(sec // 60):02d}:{sec % 60:04.1f}"


class App(ttk.Frame):
    def __init__(self, master: tk.Tk):
        super().__init__(master, padding=10)
        self.grid(sticky="nsew")
        master.columnconfigure(0, weight=1)
        master.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

        self.cfg = config.load()
        self.recorder: Recorder | None = None
        self.take: Take | None = None
        self.player: Player | None = None
        self.mics: list[devices.Device] = []
        self.loops: list[devices.Device] = []
        self._remixing = False

        self._build_devices()
        self._build_levels()
        self._build_output()
        self._build_controls()
        self._build_review()
        self._build_log()

        self.refresh_devices()
        # 裝置選好之後才知道要載入哪個裝置記住的時間微調
        self._last_offset_key = self._offset_key()
        self.offset_var.set(self._saved_offset())
        if not comapt.is_sta():
            # 只會在有人搶在 ktvrec 之前 import soundcard 時發生；見 comapt.py。
            self.log("警告：主執行緒不是 STA，「瀏覽…」和「開啟錄音資料夾」會卡住。")
        self._sweep_leftovers()
        self._tick()
        master.protocol("WM_DELETE_WINDOW", self.on_close)
        master.bind("<F9>", lambda e: self.toggle())
        master.bind("<space>", self._on_space)

    # -- 版面 -------------------------------------------------------------
    def _build_devices(self) -> None:
        box = ttk.LabelFrame(self, text=" 裝置選擇 ", padding=8)
        box.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        box.columnconfigure(1, weight=1)

        ttk.Label(box, text="輸入（麥克風）：").grid(row=0, column=0, sticky="w", pady=2)
        self.mic_var = tk.StringVar()
        self.mic_cb = ttk.Combobox(box, textvariable=self.mic_var, state="readonly", width=48)
        self.mic_cb.grid(row=0, column=1, sticky="ew", padx=6, pady=2)

        ttk.Label(box, text="輸出（要錄的播放裝置）：").grid(row=1, column=0, sticky="w", pady=2)
        self.sys_var = tk.StringVar()
        self.sys_cb = ttk.Combobox(box, textvariable=self.sys_var, state="readonly", width=48)
        self.sys_cb.grid(row=1, column=1, sticky="ew", padx=6, pady=2)
        self.sys_cb.bind("<<ComboboxSelected>>", self._on_output_changed)

        ttk.Button(box, text="重新掃描", command=self.refresh_devices).grid(
            row=0, column=2, rowspan=2, sticky="ns", padx=(4, 0))

        ttk.Label(box, text="輸出裝置錄的是「這台電腦正在播放的聲音」，也是試聽時的播放裝置。",
                  foreground="#666").grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))

    def _build_levels(self) -> None:
        box = ttk.LabelFrame(self, text=" 音量 ", padding=8)
        box.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        box.columnconfigure(2, weight=1)

        self.meters: dict[str, tuple] = {}
        self.gain_vars: dict[str, tk.DoubleVar] = {}
        for i, (key, text, cfg_key) in enumerate(
                [("mic", "麥克風", "mic_gain_db"), ("sys", "系統播放", "sys_gain_db")]):
            ttk.Label(box, text=text + "：").grid(row=i, column=0, sticky="w", pady=3)
            cv = tk.Canvas(box, width=METER_W, height=METER_H, bg="#1b1b1b",
                           highlightthickness=1, highlightbackground="#444")
            cv.grid(row=i, column=1, padx=6, pady=3)
            bar = cv.create_rectangle(0, 0, 0, METER_H, fill="#3ddc84", width=0)
            pk = cv.create_line(0, 0, 0, METER_H, fill="#ff5252", width=2)
            self.meters[key] = (cv, bar, pk)

            var = tk.DoubleVar(value=float(self.cfg.get(cfg_key, 0.0)))
            self.gain_vars[key] = var
            lbl = ttk.Label(box, width=9)
            scale = ttk.Scale(box, from_=-24, to=12, variable=var, orient="horizontal",
                              command=lambda v, l=lbl: l.config(text=f"{float(v):+.1f} dB"))
            scale.grid(row=i, column=2, sticky="ew", padx=6)
            scale.bind("<ButtonRelease-1>", self._on_mix_param_change)
            lbl.config(text=f"{var.get():+.1f} dB")
            lbl.grid(row=i, column=3, sticky="w")

        ttk.Label(box, text="增益只影響混音檔，分軌原始檔永遠是原音。試聽中調整會即時重新混音。",
                  foreground="#666").grid(row=2, column=0, columnspan=4, sticky="w", pady=(4, 0))

    def _build_output(self) -> None:
        box = ttk.LabelFrame(self, text=" 輸出設定 ", padding=8)
        box.grid(row=2, column=0, sticky="ew", pady=(0, 6))
        box.columnconfigure(1, weight=1)

        ttk.Label(box, text="儲存資料夾：").grid(row=0, column=0, sticky="w", pady=2)
        self.dir_var = tk.StringVar(value=self.cfg.get("out_dir", "recordings"))
        ttk.Entry(box, textvariable=self.dir_var).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(box, text="瀏覽…", command=self._pick_dir).grid(row=0, column=2)

        row = ttk.Frame(box)
        row.grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 0))
        ttk.Label(row, text="混音格式：").pack(side="left")
        self.fmt_var = tk.StringVar(value=self.cfg.get("export_format", "wav"))
        fmts = ["wav", "mp3", "m4a", "flac"] if encode.available() else ["wav"]
        ttk.Combobox(row, textvariable=self.fmt_var, values=fmts, state="readonly",
                     width=7).pack(side="left", padx=(0, 16))
        self.stems_var = tk.BooleanVar(value=bool(self.cfg.get("keep_stems", True)))
        ttk.Checkbutton(row, text="保留分軌", variable=self.stems_var).pack(side="left", padx=(0, 16))
        self.norm_var = tk.BooleanVar(value=bool(self.cfg.get("normalize", True)))
        ttk.Checkbutton(row, text="爆音時自動降增益", variable=self.norm_var,
                        command=self._on_mix_param_change).pack(side="left")

    def _build_controls(self) -> None:
        bar = ttk.Frame(self)
        bar.grid(row=3, column=0, sticky="ew", pady=(0, 6))
        bar.columnconfigure(1, weight=1)

        self.rec_btn = ttk.Button(bar, text="● 開始錄音  (F9)", command=self.toggle, width=20)
        self.rec_btn.grid(row=0, column=0)

        self.time_lbl = ttk.Label(bar, text="00:00.0", font=("Consolas", 20))
        self.time_lbl.grid(row=0, column=1, padx=14)

        ttk.Button(bar, text="開啟錄音資料夾", command=self._open_dir).grid(row=0, column=2)

    def _build_review(self) -> None:
        box = ttk.LabelFrame(self, text=" 試聽（尚未存檔） ", padding=8)
        box.grid(row=4, column=0, sticky="ew", pady=(0, 6))
        box.columnconfigure(0, weight=1)
        self.review_box = box

        self.wave = WaveformView(box, height=110, on_seek=self._on_seek)
        self.wave.grid(row=0, column=0, sticky="ew")

        adj = ttk.Frame(box)
        adj.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        adj.columnconfigure(1, weight=1)
        ttk.Label(adj, text="麥克風時間微調：").grid(row=0, column=0, sticky="w")
        self.offset_var = tk.DoubleVar(value=self._saved_offset())
        self.offset_scale = ttk.Scale(
            adj, from_=-500, to=500, variable=self.offset_var, orient="horizontal",
            command=lambda v: self.offset_var.set(round(float(v))))
        self.offset_scale.grid(row=0, column=1, sticky="ew", padx=6)
        self.offset_scale.bind("<ButtonRelease-1>", self._on_mix_param_change)
        self.offset_spin = ttk.Spinbox(adj, from_=-500, to=500, increment=1, width=7,
                                       textvariable=self.offset_var,
                                       command=self._on_mix_param_change)
        self.offset_spin.grid(row=0, column=2)
        self.offset_spin.bind("<Return>", self._on_mix_param_change)
        self.offset_spin.bind("<FocusOut>", self._on_mix_param_change)
        ttk.Label(adj, text="ms").grid(row=0, column=3, padx=(3, 6))
        ttk.Button(adj, text="歸零", width=6,
                   command=self._reset_offset).grid(row=0, column=4)

        bar = ttk.Frame(box)
        bar.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        bar.columnconfigure(3, weight=1)

        self.play_btn = ttk.Button(bar, text="▶ 播放", width=10,
                                   command=self._toggle_play, state="disabled")
        self.play_btn.grid(row=0, column=0)
        self.home_btn = ttk.Button(bar, text="⏮", width=4,
                                   command=lambda: self._on_seek(0.0), state="disabled")
        self.home_btn.grid(row=0, column=1, padx=(4, 8))

        self.pos_lbl = ttk.Label(bar, text="00:00.0 / 00:00.0", font=("Consolas", 11))
        self.pos_lbl.grid(row=0, column=2)

        self.save_btn = ttk.Button(bar, text="✓ 確定存檔", width=14,
                                   command=self._save_take, state="disabled")
        self.save_btn.grid(row=0, column=4, padx=(0, 6))
        self.drop_btn = ttk.Button(bar, text="✗ 捨棄", width=10,
                                   command=self._discard_take, state="disabled")
        self.drop_btn.grid(row=0, column=5)

        self.review_hint = ttk.Label(
            box, text="錄完之後在這裡試聽，拖曳波形可以移動播放位置；滿意再按「確定存檔」。",
            foreground="#666")
        self.review_hint.grid(row=3, column=0, sticky="w", pady=(6, 0))
        ttk.Label(box, foreground="#666",
                  text="時間微調：負值把人聲往前拉。喇叭或耳機到麥克風之間的硬體延遲會讓人聲"
                       "比伴奏晚，這個值依輸出裝置分別記住。").grid(
            row=4, column=0, sticky="w")

    def _build_log(self) -> None:
        box = ttk.LabelFrame(self, text=" 狀態 ", padding=5)
        box.grid(row=5, column=0, sticky="nsew")
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)
        self.rowconfigure(5, weight=1)
        self.log_txt = tk.Text(box, height=7, wrap="word", font=("Consolas", 9))
        self.log_txt.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(box, command=self.log_txt.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.log_txt.config(yscrollcommand=sb.set, state="disabled")

    # -- 行為 -------------------------------------------------------------
    def log(self, msg: str) -> None:
        self.log_txt.config(state="normal")
        self.log_txt.insert("end", msg + "\n")
        self.log_txt.see("end")
        self.log_txt.config(state="disabled")

    def _sweep_leftovers(self) -> None:
        left = sweep_pending(os.path.abspath(self.dir_var.get() or "recordings"))
        if left:
            self.log(f"發現 {len(left)} 個上次沒收尾的暫存錄音，留在："
                     + "、".join(os.path.basename(p) for p in left))

    def refresh_devices(self) -> None:
        if self.recorder and self.recorder.running:
            self.log("錄音中無法重新掃描裝置。")
            return
        self.mics = devices.list_microphones()
        self.loops = devices.list_loopbacks()
        self.mic_cb["values"] = ["（不錄麥克風）"] + [d.name for d in self.mics]
        self.sys_cb["values"] = ["（不錄系統聲音）"] + [d.name for d in self.loops]

        self._select(self.mic_cb, self.mics, self.cfg.get("mic_id"),
                     devices.default_microphone())
        self._select(self.sys_cb, self.loops, self.cfg.get("loopback_id"),
                     devices.default_loopback())
        self.log(f"找到 {len(self.mics)} 個輸入裝置、{len(self.loops)} 個輸出裝置。")

    @staticmethod
    def _select(cb: ttk.Combobox, pool: list[devices.Device],
                want_id: str | None, fallback: devices.Device | None) -> None:
        for i, d in enumerate(pool):
            if want_id and d.id == want_id:
                cb.current(i + 1)
                return
        if fallback is not None:
            for i, d in enumerate(pool):
                if d.id == fallback.id:
                    cb.current(i + 1)
                    return
        cb.current(1 if pool else 0)

    def _chosen(self, cb: ttk.Combobox, pool: list[devices.Device]) -> devices.Device | None:
        idx = cb.current()
        if idx <= 0 or idx > len(pool):
            return None
        return pool[idx - 1]

    # -- 時間微調 ---------------------------------------------------------
    def _offset_key(self) -> str:
        """時間微調依輸出裝置記住 —— 延遲主要由它決定（實測螢幕喇叭約 64 ms、
        無線耳機可到 300 ms 以上）。"""
        lb = self._chosen(self.sys_cb, self.loops) if hasattr(self, "sys_cb") else None
        return lb.id if lb else "default"

    def _saved_offset(self) -> float:
        """沒有記錄過的裝置一律從 0 開始。

        不沿用上一個裝置的值 —— 延遲本來就因裝置而異（螢幕喇叭約 64 ms、無線耳機
        可到 300 ms 以上），沿用等於給一個幾乎肯定錯、而且使用者不會察覺的數字。
        """
        by_dev = self.cfg.get("mic_offset_by_device") or {}
        key = self._offset_key()
        if key in by_dev:
            return float(by_dev[key])
        if key == "default":
            return float(self.cfg.get("mic_offset_ms", 0.0))
        return 0.0

    def _remember_offset(self) -> None:
        """把目前的微調值記在（上次載入時的）那個裝置名下。

        存進 self.cfg 而不是等 _save_cfg —— 使用者可能調完就換裝置，
        來回切換時值不該消失。
        """
        key = getattr(self, "_last_offset_key", None) or self._offset_key()
        by_dev = dict(self.cfg.get("mic_offset_by_device") or {})
        by_dev[key] = self._offset_ms()
        self.cfg["mic_offset_by_device"] = by_dev

    def _offset_ms(self) -> float:
        try:
            return float(self.offset_var.get())
        except (tk.TclError, ValueError):
            return 0.0

    def _reset_offset(self) -> None:
        self.offset_var.set(0.0)
        self._on_mix_param_change()

    def _on_output_changed(self, event=None) -> None:
        """換輸出裝置 → 先保住舊裝置的值，再載入新裝置記住的值。"""
        self._remember_offset()
        self._last_offset_key = self._offset_key()
        self.offset_var.set(self._saved_offset())
        self.log(f"輸出裝置改為「{self.sys_cb.get()}」，時間微調 {self._offset_ms():+.0f} ms。")
        if self.take is not None:
            self._on_mix_param_change()

    def _current_dir(self) -> str:
        """目前的輸出資料夾；不存在就退回程式所在目錄（不存在的 initialdir 會讓對話框卡住）。"""
        d = os.path.abspath(self.dir_var.get() or "recordings")
        return d if os.path.isdir(d) else os.path.abspath(".")

    def _pick_dir(self) -> None:
        d = filedialog.askdirectory(initialdir=self._current_dir(), parent=self)
        if d:
            self.dir_var.set(os.path.normpath(d))

    def _open_dir(self) -> None:
        d = os.path.abspath(self.dir_var.get() or "recordings")
        try:
            os.makedirs(d, exist_ok=True)
            os.startfile(d)                                # noqa: S606 - Windows only
        except OSError as exc:
            self.log(f"無法開啟資料夾：{exc}")

    # -- 錄音 -------------------------------------------------------------
    def toggle(self) -> None:
        if self.recorder and self.recorder.running:
            self.stop()
        else:
            self.start()

    def start(self) -> None:
        if self.take is not None:
            if not messagebox.askyesno(
                    "還有未存檔的錄音",
                    "上一段錄音還沒存檔，開始新錄音會把它丟掉。要繼續嗎？"):
                return
            self._teardown_take()

        mic = self._chosen(self.mic_cb, self.mics)
        lb = self._chosen(self.sys_cb, self.loops)
        if mic is None and lb is None:
            messagebox.showwarning("沒有來源", "請至少選一個輸入或輸出裝置。")
            return

        settings = RecordSettings(
            mic=mic, loopback=lb,
            samplerate=int(self.cfg.get("samplerate", 48000)),
            pull_frames=int(self.cfg.get("pull_frames", 4800)),
            buffer_frames=int(self.cfg.get("buffer_frames", 48000)),
            mic_gain_db=self.gain_vars["mic"].get(),
            sys_gain_db=self.gain_vars["sys"].get(),
            out_dir=self.dir_var.get() or "recordings",
            keep_stems=self.stems_var.get(),
            normalize=self.norm_var.get(),
        )
        self.recorder = Recorder(settings)
        try:
            self.recorder.start()
        except Exception as exc:                            # noqa: BLE001
            self.recorder = None
            messagebox.showerror("開始錄音失敗", str(exc))
            self.log(f"錯誤：{exc}")
            return

        self._set_controls(False)
        self.rec_btn.config(text="■ 停止錄音  (F9)")
        self.log("開始錄音…")
        if mic:
            self.log(f"  輸入：{mic.name}")
        if lb:
            self.log(f"  輸出：{lb.name}")

    def stop(self) -> None:
        rec = self.recorder
        if not rec or not rec.running:
            return
        rec.settings.mic_gain_db = self.gain_vars["mic"].get()
        rec.settings.sys_gain_db = self.gain_vars["sys"].get()
        self.rec_btn.config(text="處理中…", state="disabled")
        self.log("停止中，正在對齊並混音…")
        threading.Thread(target=self._finish, args=(rec,), daemon=True).start()

    def _finish(self, rec: Recorder) -> None:
        try:
            take = rec.stop()
            if take.has_audio:
                take.remix(normalize=self.norm_var.get(),
                           offsets_ms={"mic": self._offset_ms()})
        except Exception as exc:                            # noqa: BLE001
            msg = str(exc)
            self.after(0, lambda: self._finish_ui(None, msg))
            return
        self.after(0, lambda: self._finish_ui(take, None))

    def _finish_ui(self, take: Take | None, err: str | None) -> None:
        self.recorder = None
        self._set_controls(True)
        self.rec_btn.config(text="● 開始錄音  (F9)", state="normal")
        self.time_lbl.config(text="00:00.0")
        for key in self.meters:
            self._draw_meter(key, 0.0, 0.0)

        if err:
            self.log(f"錯誤：{err}")
            messagebox.showerror("停止錄音失敗", err)
            return
        assert take is not None
        for e in take.errors:
            self.log(f"警告：{e}")
        if not take.has_audio:
            self.log("沒有錄到任何音訊。")
            take.discard()
            return

        self.take = take
        self._show_take()

    # -- 試聽 -------------------------------------------------------------
    def _show_take(self) -> None:
        take = self.take
        assert take is not None and take.mix is not None
        rep = take.report

        self.wave.set_data(take.mix, take.samplerate)
        self.review_box.config(text=f" 試聽（尚未存檔）— {take.stamp} ")
        for w in (self.play_btn, self.home_btn, self.save_btn, self.drop_btn):
            w.config(state="normal")
        self.pos_lbl.config(text=f"00:00.0 / {_mmss(take.duration_sec)}")
        self.review_hint.config(
            text="拖曳波形可以移動播放位置，空白鍵播放／暫停。滿意再按「確定存檔」。")

        self.log(f"錄好了：{_mmss(rep['duration_sec'])}，峰值 {_db(rep['peak']):.1f} dBFS"
                 + ("（已自動降增益）" if rep.get("normalized") else ""))
        for t in rep["tracks"]:
            fix = "已修正" if t.get("drift_fixed") else "未修正"
            drops = t.get("discontinuities", 0)
            drop_txt = f"，掉資料 {drops} 次" if drops else ""
            adj = t.get("manual_offset_ms", 0.0)
            adj_txt = f"，手動微調 {adj:+.0f} ms" if adj else ""
            self.log(f"  [{t['name']}] 起始偏移 {t['offset_ms']:+.0f} ms{adj_txt}，"
                     f"時脈飄移 {t['drift_ms']:+.1f} ms（{fix}），"
                     f"實測取樣率 {t['effective_sr']:.1f} Hz{drop_txt}")
        self.log("尚未存檔 — 試聽後按「確定存檔」才會寫入。")

    def _playback_device(self) -> str | None:
        lb = self._chosen(self.sys_cb, self.loops)
        if lb is not None:
            return lb.id
        d = devices.default_loopback()
        return d.id if d else None

    def _ensure_player(self) -> Player | None:
        if self.take is None or self.take.mix is None:
            return None
        if self.player is not None:
            return self.player
        dev = self._playback_device()
        if dev is None:
            self.log("找不到可用的播放裝置。")
            return None
        self.player = Player(self.take.mix, self.take.samplerate, dev)
        self.player.on_finish = lambda: self.after(0, self._on_play_end)
        return self.player

    def _toggle_play(self) -> None:
        p = self._ensure_player()
        if p is None:
            return
        p.toggle()
        if p.error:
            self.log(f"播放失敗：{p.error}")
            self.player = None
            return
        self.play_btn.config(text="⏸ 暫停" if p.is_playing else "▶ 播放")

    def _on_space(self, event) -> None:
        if isinstance(event.widget, (ttk.Entry, tk.Entry, ttk.Combobox)):
            return
        if str(self.save_btn["state"]) == "normal":
            self._toggle_play()

    def _on_play_end(self) -> None:
        self.play_btn.config(text="▶ 播放")

    def _on_seek(self, sec: float) -> None:
        if self.take is None:
            return
        p = self._ensure_player()
        if p is not None:
            p.seek(int(sec * self.take.samplerate))
        self.wave.set_position(sec)
        self.pos_lbl.config(text=f"{_mmss(sec)} / {_mmss(self.take.duration_sec)}")

    def _on_mix_param_change(self, event=None) -> None:
        """試聽中調整增益或時間微調 → 重新混音。錄音中或沒有 take 時不做事。"""
        if self.take is None or self._remixing:
            return
        if self.recorder and self.recorder.running:
            return
        self._remember_offset()
        self._remixing = True
        was_playing = self.player is not None and self.player.is_playing
        pos = self.player.position if self.player else 0
        if self.player:
            self.player.pause()
        threading.Thread(target=self._remix, args=(was_playing, pos),
                         daemon=True).start()

    def _remix(self, resume: bool, pos: int) -> None:
        take = self.take
        if take is None:
            self._remixing = False
            return
        try:
            take.remix({"mic": self.gain_vars["mic"].get(),
                        "sys": self.gain_vars["sys"].get()},
                       normalize=self.norm_var.get(),
                       offsets_ms={"mic": self._offset_ms()})
        except Exception as exc:                            # noqa: BLE001
            msg = str(exc)
            self.after(0, lambda: self.log(f"重新混音失敗：{msg}"))
            self._remixing = False
            return
        self.after(0, lambda: self._remix_ui(resume, pos))

    def _remix_ui(self, resume: bool, pos: int) -> None:
        take = self.take
        self._remixing = False
        if take is None or take.mix is None:
            return
        self.wave.set_data(take.mix, take.samplerate)
        if self.player is not None:
            self.player.data = take.mix
            self.player.seek(pos)
        self.wave.set_position(pos / take.samplerate)
        peak = take.report.get("peak", 0.0)
        self.log(f"重新混音：峰值 {_db(peak):.1f} dBFS"
                 + ("（已自動降增益）" if take.report.get("normalized") else ""))
        if resume and self.player is not None:
            self.player.play()
            self.play_btn.config(text="⏸ 暫停")

    # -- 存檔／捨棄 -------------------------------------------------------
    def _save_take(self) -> None:
        take = self.take
        if take is None:
            return
        self._stop_player()
        take.out_dir = self.dir_var.get() or "recordings"
        self.save_btn.config(state="disabled", text="存檔中…")
        self.drop_btn.config(state="disabled")
        threading.Thread(target=self._do_save, args=(take,), daemon=True).start()

    def _do_save(self, take: Take) -> None:
        try:
            result = take.save(fmt=self.fmt_var.get(),
                               bitrate=self.cfg.get("mp3_bitrate", "320k"),
                               keep_stems=self.stems_var.get(),
                               normalize=self.norm_var.get())
        except Exception as exc:                            # noqa: BLE001
            msg = str(exc)
            self.after(0, lambda: self._save_ui(None, msg))
            return
        self.after(0, lambda: self._save_ui(result, None))

    def _save_ui(self, result: dict | None, err: str | None) -> None:
        self.save_btn.config(text="✓ 確定存檔")
        if err:
            self.save_btn.config(state="normal")
            self.drop_btn.config(state="normal")
            self.log(f"存檔失敗：{err}")
            messagebox.showerror("存檔失敗", err)
            return
        assert result is not None
        for e in result.get("errors", []):
            self.log(f"警告：{e}")
        mix = result["mix"]
        self.log(f"已存檔：{mix.get('converted', mix['path'])}")
        for p in result.get("stems", []):
            self.log(f"  分軌：{p}")
        self._teardown_take()
        self._save_cfg()

    def _discard_take(self) -> None:
        if self.take is None:
            return
        if not messagebox.askyesno("捨棄錄音", "這段錄音還沒存檔，確定要丟掉嗎？"):
            return
        self.log(f"已捨棄 {self.take.stamp}。")
        self._teardown_take()

    def _teardown_take(self) -> None:
        self._stop_player()
        if self.take is not None:
            self.take.discard()
            self.take = None
        self.wave.set_data(None, 48000)
        self.review_box.config(text=" 試聽（尚未存檔） ")
        self.pos_lbl.config(text="00:00.0 / 00:00.0")
        for w in (self.play_btn, self.home_btn, self.save_btn, self.drop_btn):
            w.config(state="disabled")
        self.review_hint.config(
            text="錄完之後在這裡試聽，拖曳波形可以移動播放位置；滿意再按「確定存檔」。")

    def _stop_player(self) -> None:
        if self.player is not None:
            self.player.close()
            self.player = None
        self.play_btn.config(text="▶ 播放")

    # -- 繪製與計時 -------------------------------------------------------
    def _set_controls(self, enabled: bool) -> None:
        state = "readonly" if enabled else "disabled"
        self.mic_cb.config(state=state)
        self.sys_cb.config(state=state)

    def _draw_meter(self, key: str, rms: float, peak: float) -> None:
        cv, bar, pk = self.meters[key]

        def pos(v: float) -> float:
            return max(0.0, (_db(v) + 60.0) / 60.0) * METER_W

        w = pos(rms)
        color = "#ff5252" if peak > 0.98 else ("#ffb300" if peak > 0.7 else "#3ddc84")
        cv.coords(bar, 0, 0, w, METER_H)
        cv.itemconfig(bar, fill=color)
        x = pos(peak)
        cv.coords(pk, x, 0, x, METER_H)

    def _tick(self) -> None:
        rec = self.recorder
        if rec and rec.running:
            self.time_lbl.config(text=_mmss(rec.elapsed))
            lv = rec.levels()
            self._draw_meter("mic", lv.mic_rms, lv.mic_peak)
            self._draw_meter("sys", lv.sys_rms, lv.sys_peak)

        p = self.player
        if p is not None and self.take is not None and not self.wave.dragging:
            if p.is_playing:
                sec = p.position_sec
                self.wave.set_position(sec)
                self.pos_lbl.config(text=f"{_mmss(sec)} / {_mmss(self.take.duration_sec)}")
        self.after(60, self._tick)

    # -- 設定與收尾 -------------------------------------------------------
    def _save_cfg(self) -> None:
        mic = self._chosen(self.mic_cb, self.mics)
        lb = self._chosen(self.sys_cb, self.loops)
        self.cfg.update({
            "mic_id": mic.id if mic else None,
            "mic_name": mic.name if mic else None,
            "loopback_id": lb.id if lb else None,
            "loopback_name": lb.name if lb else None,
            "mic_gain_db": self.gain_vars["mic"].get(),
            "sys_gain_db": self.gain_vars["sys"].get(),
            "out_dir": self.dir_var.get(),
            "keep_stems": self.stems_var.get(),
            "normalize": self.norm_var.get(),
            "export_format": self.fmt_var.get(),
            "mic_offset_ms": self._offset_ms(),
        })
        self._remember_offset()
        config.save(self.cfg)

    def on_close(self) -> None:
        if self.recorder and self.recorder.running:
            if not messagebox.askyesno("還在錄音", "錄音尚未停止，確定要關閉？"):
                return
            try:
                self.recorder.stop().discard()
            except Exception:                               # noqa: BLE001
                pass
        if self.take is not None:
            if not messagebox.askyesno(
                    "還有未存檔的錄音",
                    f"{self.take.stamp} 還沒存檔，關閉就會丟掉。確定要關閉？"):
                return
            self._teardown_take()
        self._stop_player()
        self._save_cfg()
        self.master.destroy()


def main() -> int:
    root = tk.Tk()
    root.title("KTV 雙軌錄音機 — 麥克風 + 系統播放")
    root.minsize(820, 880)
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
