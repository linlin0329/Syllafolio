"""Check the resources used when a new checkout imports its first syllable."""
from pathlib import Path
import sys
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from thesis_analysis.annotation_assistant_contracts import ContractBundle
from thesis_analysis.annotation_material_origin import classify_material
from thesis_analysis.annotation_stage_a import _feature_functions
from thesis_analysis.ei_b_assistance import FEATURE_NAMES


class RuntimeInputs(unittest.TestCase):
    def test_load_desktop_contracts_and_material_registry(self):
        self.assertIn("human_decision", ContractBundle().schemas)
        self.assertEqual(classify_material(["0" * 64])["kind"], "unverified")

    def test_extract_acoustic_views_from_memory(self):
        config_type, processor_type, log_mel_patch = _feature_functions()
        config = config_type()
        rate = 48000
        config.audio.sample_rate = rate
        config.feature.feature_types = ["mfcc", "mfcc_delta", "spectral", "temporal", "amplitude"]
        processor = processor_type(config)
        t = np.arange(rate // 4) / rate
        samples = (.05 * np.sin(2 * np.pi * 1800 * t) * np.hanning(t.size)).astype("float32")
        features = processor.extract_features(samples, sr=rate)
        patch = log_mel_patch(samples, rate)
        self.assertEqual(processor.get_feature_names(), FEATURE_NAMES)
        self.assertEqual(features.shape, (105,))
        self.assertEqual(patch.size, 2048)
        self.assertTrue(np.isfinite(features).all() and np.isfinite(patch).all())


if __name__ == "__main__":
    unittest.main()
