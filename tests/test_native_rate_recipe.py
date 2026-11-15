"""Native low-rate feature values, version boundaries, and project provenance."""
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

import librosa
import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from syllafolio import annotation_stage_a as stage
from syllafolio.acoustics.audio_processor import AudioProcessor
from syllafolio.acoustics.config import Config
from syllafolio.acoustics.log_mel import log_mel_patch
from syllafolio.annotation_desktop_project import create_project, open_project


class NativeRateRecipe(unittest.TestCase):
    def test_native_reference_bands_on_each_side_of_recipe_boundary(self):
        signal = np.random.default_rng(184).normal(0, .08, 7168).astype(np.float32)
        config = Config()
        config.feature.feature_types = ["mfcc", "spectral"]
        for rate, high, contrast_low in ((16000, 7500, 125), (22050, 10335.9375, 172.265625),
                                         (31999, 14999.53125, 249.9921875), (32000, 15000, 250),
                                         (44100, 15000, 250)):
            with self.subTest(rate=rate):
                vector = AudioProcessor(config).extract_features(signal, sr=rate)
                mfcc = librosa.feature.mfcc(y=signal, sr=rate, n_mfcc=20, n_fft=2048,
                                            hop_length=512, fmin=1000, fmax=high)
                np.testing.assert_allclose(vector[:20], mfcc.mean(axis=1), rtol=1e-6)
                contrast = librosa.feature.spectral_contrast(y=signal, sr=rate, n_fft=2048,
                                hop_length=512, fmin=contrast_low, n_bands=6, quantile=.02, linear=False)
                np.testing.assert_allclose(vector[-7:], contrast.mean(axis=1), rtol=1e-6)
                self.assertTrue(np.all(vector[-7:] > 0))
                mel = librosa.feature.melspectrogram(y=signal, sr=rate, n_fft=1024, hop_length=256,
                                                      n_mels=64, fmin=1000, fmax=high, power=2.0)
                db = librosa.power_to_db(mel, ref=np.max)
                times = np.linspace(0, 1, db.shape[1])
                resized = np.stack([np.interp(np.linspace(0, 1, 32), times, row) for row in db])
                normalized = ((resized - resized.mean()) / resized.std()).astype(np.float32)
                np.testing.assert_array_equal(log_mel_patch(signal, rate), normalized)

    def test_low_rate_recipe_and_filter_survive_reopen(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            clips = root / "clips"
            clips.mkdir()
            rate = 16000
            t = np.arange(4000) / rate
            sf.write(clips / "a.wav", np.sin(2*np.pi*4100*t)*.2, rate)
            filters = {"low_hz": 600, "high_hz": 6800, "order": 4}
            with create_project(clips, root / "project", bandpass=filters) as project:
                summary = project.metadata["preparation_summary"]
                frozen = summary["acoustic_analysis"]
                self.assertEqual(frozen["sample_rate_hz"], rate)
                self.assertEqual(frozen["mfcc_logmel_frequency_band_hz"], [1000, 7500])
                self.assertTrue(frozen["feature_version"].startswith(stage.LOW_RATE_FEATURE_RECIPE + "-bandpass-"))
                self.assertEqual(summary["audio_preprocessing"]["high_hz"], 6800)
                self.assertEqual(project.dataset.features.shape, (1, 105))
                self.assertEqual(project.dataset.logmel.shape, (1, 2048))
                ids = project.dataset.input_versions
                self.assertEqual(ids["feature_version"], frozen["feature_version"])
            marker = (root / "project/project.json").read_bytes()
            with open_project(root / "project") as project:
                self.assertEqual(project.dataset.input_versions, ids)
                self.assertEqual(project.metadata["preparation_summary"]["acoustic_analysis"], frozen)
                y, sr, _ = project.read_audio(project.dataset.item_ids[0])
                self.assertEqual((sr, len(y)), (rate, len(t)))
            self.assertEqual((root / "project/project.json").read_bytes(), marker)
