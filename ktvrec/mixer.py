"""把多條獨立錄下的軌道對齊、混音成一個檔案。

麥克風與喇叭是兩個獨立的硬體時脈，各自跑一段時間後取樣數會有些微偏差
（幾分鐘下來可能差到幾十毫秒）。這裡用每軌實際的牆鐘時間長度回推真實
取樣率，重新取樣到標稱取樣率後再依起始時間對齊，藉此消除飄移。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import soundfile as sf


@dataclass
class TrackInfo:
    """一條待混音的軌道。"""

    path: str
    t_start: float   # perf_counter，串流開始的時刻
    t_stop: float    # perf_counter，串流結束的時刻
    gain_db: float = 0.0
    muted: bool = False
    name: str = ""
    discontinuities: int = 0   # 擷取中掉資料的次數；>0 時時脈推算不可信
    # 手動時間微調（毫秒，負值 = 把這一軌往前拉）。
    # 用來補償硬體延遲：loopback 取樣點到喇叭實際發聲之間有輸出延遲，
    # 麥克風進來也有擷取延遲，兩者相加會讓人聲比伴奏晚。見 README。
    manual_offset_ms: float = 0.0


def _to_stereo(data: np.ndarray) -> np.ndarray:
    if data.shape[1] == 1:
        return np.repeat(data, 2, axis=1)
    if data.shape[1] > 2:
        return data[:, :2]
    return data


# 低於這個長度時，起停時間的量測誤差會大於真實時脈飄移，修正反而有害。
MIN_DRIFT_FIX_SEC = 15.0
# 真實硬體時脈差不會超過這個比例；超過代表量測出錯，不要動它。
MAX_DRIFT_RATIO = 0.005


def _resample_to(data: np.ndarray, target_n: int) -> np.ndarray:
    """線性重新取樣到 target_n 個 frame（用於修正時脈飄移，比例極接近 1）。"""
    n = data.shape[0]
    if target_n <= 0 or n < 2 or abs(target_n - n) <= 1:
        return data
    x_old = np.linspace(0.0, 1.0, n, dtype=np.float64)
    x_new = np.linspace(0.0, 1.0, target_n, dtype=np.float64)
    out = np.empty((target_n, data.shape[1]), dtype=np.float32)
    for c in range(data.shape[1]):
        out[:, c] = np.interp(x_new, x_old, data[:, c]).astype(np.float32)
    return out


def _prepare(t: TrackInfo, samplerate: int) -> tuple[np.ndarray, int, dict]:
    """讀檔、轉立體聲、修正時脈，回傳 (資料, 起始偏移 frame, 這一軌的報告)。

    這一步跟增益無關，所以結果可以被快取起來重複使用 —— 試聽時拉增益滑桿要
    即時重新混音，不能每次都重讀檔案。
    """
    data, sr = sf.read(t.path, dtype="float32", always_2d=True)
    data = _to_stereo(data)
    n_raw = data.shape[0]

    span = t.t_stop - t.t_start
    target_n = int(round(span * samplerate))
    drift_ms = (n_raw - target_n) / samplerate * 1000.0
    ratio = abs(target_n - n_raw) / n_raw if n_raw else 0.0
    # 掉過資料的軌道，取樣數少掉的是「真的不見的時間」，不是時脈跑得慢。
    # 這時候拉伸整軌等於把一個局部的斷點抹散到整首歌，只會更糟。
    fixed = (t.discontinuities == 0
             and span >= MIN_DRIFT_FIX_SEC
             and 0 < ratio <= MAX_DRIFT_RATIO)
    if fixed:
        data = _resample_to(data, target_n)

    info = {
        "name": t.name or t.path,
        "frames": n_raw,
        "drift_ms": drift_ms,
        "drift_fixed": fixed,
        "discontinuities": t.discontinuities,
        "effective_sr": (n_raw / span) if span > 0 else float(sr),
    }
    return data, 0, info


def build_mix(tracks: list[TrackInfo], samplerate: int, normalize: bool = True,
              cache: dict | None = None) -> tuple[np.ndarray, dict]:
    """混音到記憶體，回傳 (立體聲陣列, 報告)。不寫檔。

    傳入 `cache`（任意 dict）可以讓重複混音省下讀檔與重新取樣的成本。
    """
    live = [t for t in tracks if not t.muted]
    if not live:
        raise ValueError("沒有可混音的軌道（全部靜音）")

    t0 = min(t.t_start for t in live)
    report: dict = {"tracks": [], "samplerate": samplerate}

    # 先把每一軌準備好並算出各自的起點（手動微調可能是負的，所以起點可能為負）
    prepared: list[tuple[TrackInfo, np.ndarray, dict, int]] = []
    for t in live:
        key = (t.path, samplerate)
        if cache is not None and key in cache:
            data, info = cache[key]
        else:
            data, _, info = _prepare(t, samplerate)
            if cache is not None:
                cache[key] = (data, info)
        start = (int(round((t.t_start - t0) * samplerate))
                 + int(round(t.manual_offset_ms / 1000.0 * samplerate)))
        prepared.append((t, data, info, start))

    # 以最早的起點當混音的原點，往前拉的軌道才不會被切掉頭
    origin = min(start for _, _, _, start in prepared)
    n_out = max(1, max(start - origin + data.shape[0]
                       for _, data, _, start in prepared))
    acc = np.zeros((n_out, 2), dtype=np.float32)

    for t, data, info, start in prepared:
        gain = 10.0 ** (t.gain_db / 20.0)
        offset = start - origin
        n = min(data.shape[0], n_out - offset)
        if n > 0:
            acc[offset:offset + n] += data[:n] * gain

        entry = dict(info)
        entry["gain_db"] = t.gain_db
        entry["offset_ms"] = (t.t_start - t0) * 1000.0
        entry["manual_offset_ms"] = t.manual_offset_ms
        entry["mix_start_ms"] = offset / samplerate * 1000.0
        report["tracks"].append(entry)

    peak = float(np.abs(acc).max()) if acc.size else 0.0
    report["peak"] = peak
    report["normalized"] = False
    if peak > 0.999:
        if normalize:
            acc *= 0.99 / peak
            report["normalized"] = True
        else:
            np.clip(acc, -1.0, 1.0, out=acc)
            report["clipped"] = True

    report["duration_sec"] = n_out / samplerate
    return acc, report


def write_mix(acc: np.ndarray, samplerate: int, out_path: str,
              subtype: str = "PCM_24") -> None:
    sf.write(out_path, acc, samplerate, subtype=subtype)


def mix_tracks(tracks: list[TrackInfo], samplerate: int, out_path: str,
               normalize: bool = True, subtype: str = "PCM_24") -> dict:
    """混音並寫檔，回傳含 peak / 是否削峰等資訊的 dict。"""
    acc, report = build_mix(tracks, samplerate, normalize)
    write_mix(acc, samplerate, out_path, subtype)
    report["path"] = out_path
    return report
