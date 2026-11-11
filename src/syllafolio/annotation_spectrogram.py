"""人工显示专用声谱：按固定频率和时间坐标绘制，独立于冻结的声学计算预设。"""
from __future__ import annotations

import math
from pathlib import Path
import numpy as np
from scipy.signal.windows import hann
from PIL import Image, ImageDraw, ImageFont
from matplotlib import colormaps

VERSION = "manual-paper-spectrogram-v1"
DEFAULTS = {"frequency_max_hz": 16000, "dynamic_range_db": 45,
            "time_max_s": 0.7, "window_samples": 512, "hop_samples": 96}
PARAMETERS = set(DEFAULTS)


def validate_display(values):
    if not isinstance(values, dict) or set(values) - PARAMETERS:
        raise ValueError("The spectrogram display setting fields do not match.")
    result = dict(DEFAULTS)
    result.update(values)
    for key, value in result.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("Spectrogram display settings must be finite numbers.")
        if key != "time_max_s" and int(value) != value:
            raise ValueError("Spectrogram settings must be integers.")
    if not (2000 <= result["frequency_max_hz"] <= 24000 and
            20 <= result["dynamic_range_db"] <= 100 and
            0.1 <= result["time_max_s"] <= 31 and
            result["window_samples"] in {256, 512, 1024, 2048} and
            32 <= result["hop_samples"] <= 512 and
            result["hop_samples"] <= result["window_samples"]):
        raise ValueError("The spectrogram display settings are outside the allowed range.")
    return {key: float(value) if key == "time_max_s" else int(value) for key, value in result.items()}


def spectrum(samples, rate, params):
    """周期Hann，无端点补齐；频段内峰值相对功率。短于窗长明确无完整窗。"""
    p = validate_display(params)
    y = np.asarray(samples, dtype=np.float64)
    if y.ndim != 1 or not len(y) or not np.isfinite(y).all() or rate <= 0:
        raise ValueError("Spectrogram input must contain finite mono samples.")
    win, hop, nfft = p["window_samples"], p["hop_samples"], 2 * p["window_samples"]
    frequencies = np.fft.rfftfreq(nfft, d=1 / rate)
    max_hz = min(p["frequency_max_hz"], rate / 2)
    mask = frequencies <= max_hz
    if len(y) < win:
        return {"db": np.empty((int(mask.sum()), 0)), "frequencies_hz": frequencies[mask],
                "centers_s": np.array([]), "peak_power": 0.0, "last_window_stop": 0}
    if ((len(y) - win) // hop + 1) * int(mask.sum()) > 4_000_000:
        raise ValueError("Spectrogram rendering exceeds the work limit; increase the hop size or reduce the window length.")
    frames = np.lib.stride_tricks.sliding_window_view(y, win)[::hop]
    power = np.abs(np.fft.rfft(frames * hann(win, sym=False), n=nfft, axis=1)[:, mask]).T ** 2
    peak = float(power.max())
    db = np.full(power.shape, -float(p["dynamic_range_db"]))
    if peak > 0:
        db = np.clip(10 * np.log10(np.maximum(power / peak, np.finfo(float).tiny)),
                     -p["dynamic_range_db"], 0)
    return {"db": db, "frequencies_hz": frequencies[mask],
            "centers_s": (np.arange(frames.shape[0]) * hop + win / 2) / rate,
            "peak_power": peak, "last_window_stop": (len(frames) - 1) * hop + win}


def render(samples, rate, params, path, *, target_interval=None):
    p = validate_display(params)
    data = spectrum(samples, rate, p)
    width, height = 1000, 440
    left, top, right, bottom = 85, 34, 25, 68
    pw, ph = width - left - right, height - top - bottom
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 22)
        small = ImageFont.truetype("DejaVuSans.ttf", 18)
    except OSError:
        font = small = ImageFont.load_default()
    max_hz = min(p["frequency_max_hz"], rate / 2)
    time_max, duration = p["time_max_s"], len(samples) / rate
    x_times = (np.arange(pw) + .5) * time_max / pw
    y_freq = (1 - (np.arange(ph) + .5) / ph) * max_hz
    rgb = np.full((ph, pw, 3), 255, dtype=np.uint8)
    if data["db"].shape[1]:
        # 以STFT中心 +/- hop/2定义显示单元，不拉伸时轴。
        t0 = data["centers_s"][0] - p["hop_samples"] / rate / 2
        indices = np.floor((x_times - t0) * rate / p["hop_samples"]).astype(int)
        valid = (indices >= 0) & (indices < data["db"].shape[1])
        freq_indices = np.clip(np.floor(y_freq * (2 * p["window_samples"]) / rate + .5).astype(int),
                               0, data["db"].shape[0] - 1)
        db = data["db"][freq_indices[:, None], indices[valid][None, :]]
        rgb[:, valid] = colormaps["Greys"]((db + p["dynamic_range_db"]) / p["dynamic_range_db"], bytes=True)[:, :, :3]
    image.paste(Image.fromarray(rgb), (left, top))
    if duration < time_max:
        x = left + int(pw * duration / time_max)
        draw.line((x, top, x, top + ph), fill="#9ca5aa", width=1)
    if target_interval:
        for t in target_interval:
            if 0 <= t <= time_max:
                x = left + int(pw * t / time_max)
                draw.line((x, top, x, top + ph), fill="#168c91", width=2)
    draw.rectangle((left, top, left + pw, top + ph), outline="#333333", width=2)
    for fraction in (0, .25, .5, .75, 1):
        y = top + int((1 - fraction) * ph)
        draw.text((left - 12, y), f"{fraction * max_hz / 1000:g}", fill="black", font=small, anchor="rm")
        x = left + int(fraction * pw)
        draw.text((x, top + ph + 16), f"{fraction * time_max:.2f}", fill="black", font=small, anchor="mt")
    draw.text((left, 3), "Frequency (kHz)", fill="black", font=small)
    draw.text((left + pw / 2, height - 8), "Time (s)", fill="black", font=font, anchor="mb")
    draw.text((left + pw, 3), f"-{p['dynamic_range_db']} to 0 dB (relative peak)", fill="#555555", font=small, anchor="rt")
    if not data["db"].shape[1]:
        draw.text((left + pw / 2, top + ph / 2), "No complete STFT window", fill="#666666", font=font, anchor="mm")
    if duration > time_max:
        draw.text((left + pw - 12, top + ph - 12), "Time window cropped", fill="#a34b13", font=small, anchor="rb")
    image.save(path)
    return {"renderer": VERSION, "parameters": dict(p, fft_samples=p["window_samples"] * 2),
            "sample_rate_hz": rate, "duration_s": duration, "frequency_max_hz_actual": max_hz,
            "db_reference": "peak power within displayed frequency band, independently per panel",
            "stft_frames": int(data["db"].shape[1]), "endpoint_padding": False,
            "uncovered_tail_frames": len(samples) - data["last_window_stop"],
            "view_cropped": duration > time_max, "audio_processing": "none"}
