"""Compute the fixed 64 x 32 log-mel patch used in syllable review."""
from __future__ import annotations

import librosa
import numpy as np


N_FFT = 1024

HOP_LENGTH = 256

FMIN = 1000.0

FMAX = 15000.0

N_MELS = 64

PATCH_WIDTH = 32

EPSILON = 1e-9

def log_mel_patch(waveform: np.ndarray, sample_rate: int) -> np.ndarray:
    """提取逐音节标准化的定长 log-mel patch。"""
    if len(waveform) < N_FFT:
        waveform = np.pad(waveform, (0, N_FFT - len(waveform)))
    mel = librosa.feature.melspectrogram(
        y=waveform,
        sr=sample_rate,
        n_fft=N_FFT,
        hop_length=HOP_LENGTH,
        n_mels=N_MELS,
        fmin=FMIN,
        fmax=FMAX,
        power=2.0,
    )
    log_mel = librosa.power_to_db(mel, ref=np.max)
    source_time = np.linspace(0.0, 1.0, num=log_mel.shape[1])
    target_time = np.linspace(0.0, 1.0, num=PATCH_WIDTH)
    resized = np.vstack(
        [
            np.interp(target_time, source_time, log_mel[band])
            for band in range(N_MELS)
        ]
    )
    scale = float(resized.std())
    return ((resized - resized.mean()) / max(scale, EPSILON)).astype(np.float32)
