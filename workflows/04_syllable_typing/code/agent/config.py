"""Audio/feature defaults used by the desktop import kernel."""
from dataclasses import dataclass, field
from typing import List


@dataclass
class AudioConfig:
    """音频处理参数"""
    sample_rate: int = 48000          # 采样率 (Hz)
    freq_min: float = 1000            # 最低频率 (Hz) - 麻雀鸣声下限
    freq_max: float = 15000           # 最高频率 (Hz) - 麻雀鸣声上限
    min_syllable_duration: float = 0.03  # 最短音节时长 (秒) - 提高到30ms以确保足够的帧数
    max_syllable_duration: float = 1.0   # 最长音节时长 (秒)

@dataclass
class FeatureConfig:
    """特征提取参数"""
    # 梅尔频谱参数
    n_mels: int = 128                 # 梅尔频带数量
    n_fft: int = 2048                 # FFT 窗口大小
    hop_length: int = 512             # 步长
    
    # MFCC 参数
    n_mfcc: int = 20                  # MFCC 系数数量
    
    # PCEN 参数 (Per-Channel Energy Normalization)
    use_pcen: bool = True             # 是否使用 PCEN (对低信噪比有效)
    pcen_gain: float = 0.98
    pcen_bias: float = 2.0
    pcen_power: float = 0.5
    pcen_time_constant: float = 0.4
    
    # 图像特征参数 (用于统一音节长度)
    fixed_width: int = 128            # 将所有音节缩放到统一的时间宽度
    
    # 特征类型选择
    feature_types: List[str] = field(default_factory=lambda: [
        "f0",             # 基频特征 (音高)
        "mfcc",           # MFCC 均值和标准差
        "spectral",       # 频谱特征 (质心、带宽、滚降点等)
        "temporal",       # 时域特征 (持续时间、ZCR等)
        "amplitude",      # 振幅包络特征 (新增)
    ])


@dataclass
class Config:
    """Only the two configuration views used by desktop feature extraction."""
    audio: AudioConfig = field(default_factory=AudioConfig)
    feature: FeatureConfig = field(default_factory=FeatureConfig)
