"""設定檔存取（記住上次選的裝置與音量）。"""

from __future__ import annotations

import json
import os

CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "config.json")

DEFAULTS: dict = {
    "mic_id": None,
    "mic_name": None,
    "loopback_id": None,
    "loopback_name": None,
    "mic_gain_db": 0.0,
    "sys_gain_db": 0.0,
    # 麥克風時間微調（毫秒，負值 = 人聲往前拉）。延遲主要由輸出裝置決定
    # （實測：螢幕喇叭約 64 ms、無線耳機可到 300 ms 以上），所以依裝置分別記住。
    "mic_offset_ms": 0.0,
    "mic_offset_by_device": {},
    "samplerate": 48000,
    "pull_frames": 4800,
    "buffer_frames": 48000,
    "out_dir": "recordings",
    "keep_stems": True,
    "normalize": True,
    "export_format": "wav",
    "mp3_bitrate": "320k",
}


def load() -> dict:
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            cfg.update(json.load(fh))
    except (OSError, ValueError):
        pass
    return cfg


def save(cfg: dict) -> None:
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False, indent=2)
    except OSError:
        pass
