"""Unchanged numerical feature methods used by desktop import; no table/corpus IO."""
import warnings
from typing import List

import numpy as np
import librosa
from skimage.transform import resize

from .config import Config


class AudioProcessor:
    """Feature extraction from explicit in-memory PCM."""
    def __init__(self, config: Config = None):
        """
        初始化音频处理器
        
        Parameters
        ----------
        config : Config
            配置对象，如果为 None 则使用默认配置
        """
        self.config = config or Config()
        self._audio_cache: Dict[str, Tuple[np.ndarray, int]] = {}

    def extract_features(self, 
                        waveform: np.ndarray, 
                        sr: int = None) -> np.ndarray:
        """
        从波形中提取多维声学特征
        
        Parameters
        ----------
        waveform : np.ndarray
            音节波形
        sr : int
            采样率，默认使用配置中的值
            
        Returns
        -------
        np.ndarray
            特征向量
        """
        if sr is None:
            sr = self.config.audio.sample_rate
        
        cfg = self.config.feature
        audio_cfg = self.config.audio
        
        # 检查波形长度
        if len(waveform) < cfg.n_fft:
            # 如果太短，进行零填充
            waveform = np.pad(waveform, (0, cfg.n_fft - len(waveform)))
        
        features = []
        
        # ========== 0. 基频 (F0) 特征 ==========
        if "f0" in cfg.feature_types:
            try:
                # 使用 pyin 算法提取基频 (对鸟类鸣声更鲁棒)
                f0, voiced_flag, voiced_probs = librosa.pyin(
                    y=waveform, 
                    sr=sr,
                    fmin=audio_cfg.freq_min,
                    fmax=audio_cfg.freq_max,
                    frame_length=cfg.n_fft,
                    hop_length=cfg.hop_length,
                    fill_na=0.0
                )
                
                # 只保留有声音的部分 (voiced)
                f0_voiced = f0[voiced_flag]
                
                if len(f0_voiced) > 0:
                    # 基本统计
                    features.append(np.mean(f0_voiced))      # 平均基频
                    features.append(np.std(f0_voiced))       # 基频标准差
                    features.append(np.min(f0_voiced))       # 最低音高
                    features.append(np.max(f0_voiced))       # 最高音高
                    features.append(np.max(f0_voiced) - np.min(f0_voiced))  # 音高范围
                    
                    # 基频变化率 (一阶差分的标准差)
                    f0_diff = np.diff(f0_voiced)
                    features.append(np.mean(f0_diff))        # 平均变化率
                    features.append(np.std(f0_diff))         # 变化率标准差
                    
                    # 基频变化方向性
                    if len(f0_diff) > 0:
                        features.append(np.mean(np.sign(f0_diff)))  # 上升/下降倾向 (-1到1)
                    else:
                        features.append(0)
                        
                    # 有声帧比例
                    features.append(len(f0_voiced) / len(f0))
                else:
                    # 没有检测到基频，用零填充
                    features.extend([0] * 9)
                    
            except Exception as e:
                # 基频提取失败，用零填充
                features.extend([0] * 9)
        
        # ========== 1. 振幅包络特征 (新增) ==========
        if "amplitude" in cfg.feature_types:
            # 计算振幅包络
            envelope = librosa.onset.onset_strength(
                y=waveform, 
                sr=sr,
                hop_length=cfg.hop_length
            )
            
            if len(envelope) > 0:
                features.append(np.max(envelope))      # 最大振幅
                features.append(np.mean(envelope))     # 平均振幅
                features.append(np.std(envelope))      # 振幅标准差
                features.append(np.ptp(envelope))      # 峰峰值 (max-min)
                
                # 振幅变化率
                envelope_diff = np.diff(envelope)
                features.append(np.mean(np.abs(envelope_diff)))  # 平均变化率
            else:
                features.extend([0] * 5)
        
        # ========== 2. MFCC 特征 ==========
        if "mfcc" in cfg.feature_types:
            mfcc = librosa.feature.mfcc(
                y=waveform, sr=sr, 
                n_mfcc=cfg.n_mfcc,
                n_fft=cfg.n_fft,
                hop_length=cfg.hop_length,
                fmin=audio_cfg.freq_min,
                fmax=audio_cfg.freq_max
            )
            # 计算每个 MFCC 系数的均值和标准差
            mfcc_mean = np.mean(mfcc, axis=1)
            mfcc_std = np.std(mfcc, axis=1)
            features.extend(mfcc_mean)
            features.extend(mfcc_std)
        
        # ========== 2. MFCC Delta 特征 ==========
        if "mfcc_delta" in cfg.feature_types:
            mfcc = librosa.feature.mfcc(
                y=waveform, sr=sr, 
                n_mfcc=cfg.n_mfcc,
                n_fft=cfg.n_fft,
                hop_length=cfg.hop_length,
                fmin=audio_cfg.freq_min,
                fmax=audio_cfg.freq_max
            )
            # 一阶差分 - 动态调整 width 以适应短音节
            n_frames = mfcc.shape[1]
            
            if n_frames >= 3:
                # width 必须是奇数，且不超过帧数
                delta_width = min(9, n_frames)
                if delta_width % 2 == 0:  # 如果是偶数，减1变成奇数
                    delta_width = max(3, delta_width - 1)
                
                mfcc_delta = librosa.feature.delta(mfcc, width=delta_width)
                mfcc_delta_mean = np.mean(mfcc_delta, axis=1)
                mfcc_delta_std = np.std(mfcc_delta, axis=1)
                features.extend(mfcc_delta_mean)
                features.extend(mfcc_delta_std)
            else:
                # 帧数太少，用零填充
                features.extend([0] * cfg.n_mfcc * 2)
        
        # ========== 3. 频谱特征 ==========
        if "spectral" in cfg.feature_types:
            # 频谱质心 (Spectral Centroid)
            spectral_centroid = librosa.feature.spectral_centroid(
                y=waveform, sr=sr, n_fft=cfg.n_fft, hop_length=cfg.hop_length
            )
            features.append(np.mean(spectral_centroid))
            features.append(np.std(spectral_centroid))
            
            # 频谱带宽 (Spectral Bandwidth)
            spectral_bandwidth = librosa.feature.spectral_bandwidth(
                y=waveform, sr=sr, n_fft=cfg.n_fft, hop_length=cfg.hop_length
            )
            features.append(np.mean(spectral_bandwidth))
            features.append(np.std(spectral_bandwidth))
            
            # 频谱滚降点 (Spectral Rolloff)
            spectral_rolloff = librosa.feature.spectral_rolloff(
                y=waveform, sr=sr, n_fft=cfg.n_fft, hop_length=cfg.hop_length
            )
            features.append(np.mean(spectral_rolloff))
            features.append(np.std(spectral_rolloff))
            
            # 频谱平坦度 (Spectral Flatness)
            spectral_flatness = librosa.feature.spectral_flatness(
                y=waveform, n_fft=cfg.n_fft, hop_length=cfg.hop_length
            )
            features.append(np.mean(spectral_flatness))
            features.append(np.std(spectral_flatness))
            
            # 频谱对比度 (Spectral Contrast)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                try:
                    spectral_contrast = librosa.feature.spectral_contrast(
                        y=waveform, sr=sr, n_fft=cfg.n_fft, hop_length=cfg.hop_length,
                        fmin=audio_cfg.freq_min
                    )
                    features.extend(np.mean(spectral_contrast, axis=1))
                except:
                    # 如果频谱对比度计算失败，填充零
                    features.extend([0] * 7)
        
        # ========== 4. 时域特征 ==========
        if "temporal" in cfg.feature_types:
            # 持续时间
            duration = len(waveform) / sr
            features.append(duration)
            
            # 过零率 (Zero Crossing Rate)
            zcr = librosa.feature.zero_crossing_rate(waveform, hop_length=cfg.hop_length)
            features.append(np.mean(zcr))
            features.append(np.std(zcr))
            
            # RMS 能量
            rms = librosa.feature.rms(y=waveform, hop_length=cfg.hop_length)
            features.append(np.mean(rms))
            features.append(np.std(rms))
        
        # ========== 5. PCEN 图像特征 ==========
        if "pcen_image" in cfg.feature_types:
            # 计算梅尔频谱
            S = librosa.feature.melspectrogram(
                y=waveform, sr=sr,
                n_mels=cfg.n_mels,
                n_fft=cfg.n_fft,
                hop_length=cfg.hop_length,
                fmin=audio_cfg.freq_min,
                fmax=audio_cfg.freq_max
            )
            
            if cfg.use_pcen:
                # PCEN 归一化 - 对低信噪比录音更鲁棒
                S_norm = librosa.pcen(
                    S * (2**31), 
                    sr=sr, 
                    hop_length=cfg.hop_length,
                    gain=cfg.pcen_gain,
                    bias=cfg.pcen_bias,
                    power=cfg.pcen_power,
                    time_constant=cfg.pcen_time_constant
                )
            else:
                # 使用对数梅尔频谱
                S_norm = librosa.power_to_db(S, ref=np.max)
            
            # 缩放到固定尺寸
            S_resized = resize(
                S_norm, 
                (cfg.n_mels, cfg.fixed_width),
                mode='reflect', 
                anti_aliasing=True
            )
            
            # 展平
            features.extend(S_resized.flatten())
        
        return np.array(features, dtype=np.float32)

    def get_feature_names(self) -> List[str]:
        """
        获取特征名称列表
        
        Returns
        -------
        List[str]
            特征名称列表
        """
        cfg = self.config.feature
        names = []
        
        if "f0" in cfg.feature_types:
            names.extend([
                "f0_mean", "f0_std", "f0_min", "f0_max", "f0_range",
                "f0_diff_mean", "f0_diff_std", "f0_trend", "voiced_ratio"
            ])
        
        if "amplitude" in cfg.feature_types:
            names.extend([
                "envelope_max", "envelope_mean", "envelope_std",
                "envelope_ptp", "envelope_diff_rate"
            ])
        
        if "mfcc" in cfg.feature_types:
            for i in range(cfg.n_mfcc):
                names.append(f"mfcc_{i}_mean")
            for i in range(cfg.n_mfcc):
                names.append(f"mfcc_{i}_std")
        
        if "mfcc_delta" in cfg.feature_types:
            for i in range(cfg.n_mfcc):
                names.append(f"mfcc_delta_{i}_mean")
            for i in range(cfg.n_mfcc):
                names.append(f"mfcc_delta_{i}_std")
        
        if "spectral" in cfg.feature_types:
            names.extend([
                "spectral_centroid_mean", "spectral_centroid_std",
                "spectral_bandwidth_mean", "spectral_bandwidth_std",
                "spectral_rolloff_mean", "spectral_rolloff_std",
                "spectral_flatness_mean", "spectral_flatness_std",
            ])
            for i in range(7):
                names.append(f"spectral_contrast_{i}")
        
        if "temporal" in cfg.feature_types:
            names.extend([
                "duration",
                "zcr_mean", "zcr_std",
                "rms_mean", "rms_std",
            ])
        
        if "pcen_image" in cfg.feature_types:
            for i in range(cfg.n_mels * cfg.fixed_width):
                names.append(f"pcen_pixel_{i}")
        
        return names
