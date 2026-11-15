"""Regression checks for spectral contrast in the frozen 105-column import."""
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import librosa
import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from syllafolio import annotation_stage_a as stage
from syllafolio.acoustics.audio_processor import AudioProcessor
from syllafolio.acoustics.config import Config


class SpectralContrastTests(unittest.TestCase):
    def test_seven_bands_at_supported_rates(self):
        cfg = Config()
        cfg.feature.feature_types = ["spectral"]
        processor = AudioProcessor(cfg)
        samples = np.random.default_rng(678).normal(0, .03, 6144).astype(np.float32)
        for rate, boundary in [(32000, 250), (44100, 250), (48000, 250), (64000, 250),
                               (96000, 250), (192000, 250), (384000, 375)]:
            with self.subTest(rate=rate):
                # Pin the intended band boundaries independently of the configuration helper.
                reference = librosa.feature.spectral_contrast(
                    y=samples, sr=rate, n_fft=2048, hop_length=512,
                    fmin=boundary, n_bands=6, quantile=.02, linear=False).mean(axis=1)
                actual = processor.extract_features(samples, rate)[8:15]
                self.assertTrue((actual > 0).all())
                np.testing.assert_allclose(actual, reference, rtol=1e-6)

    def test_contrast_failure_aborts_candidate_publication(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "clips"
            source.mkdir()
            sf.write(source / "bird.wav", np.ones(4096) * .1, 48000)
            with patch("librosa.feature.spectral_contrast", side_effect=ValueError("contrast failed")):
                with self.assertRaises(stage.StageAError) as caught:
                    stage.prepare_stage_a(source, root / "candidate")
            self.assertEqual(caught.exception.code, "FEATURE_EXTRACTION_FAILED")
            self.assertIn("syllable-0001", str(caught.exception))
            self.assertFalse((root / "candidate/dataset/manifest.json").exists())

    def test_recipe_change_versions_features_and_global_but_preserves_source(self):
        for processing in (None, stage.bandpass_metadata({"low_hz": 500, "high_hz": 12000,
                                                          "order": 4}, 48000)):
            new = stage._versions("a" * 64, processing)
            old = stage._versions("a" * 64, processing, feature_recipe=stage.LEGACY_FEATURE_RECIPE)
            for key in ("boundary_version", "material_version"):
                self.assertEqual(new[key], old[key])
            for key in ("feature_version", "global_representation_version"):
                self.assertNotEqual(new[key], old[key])
