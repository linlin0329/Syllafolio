"""Check the signal actually heard, measured and exported by both import routes."""
import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

import numpy as np
from scipy import signal
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from syllafolio import annotation_stage_a as stage
from syllafolio.acoustics.audio_processor import AudioProcessor
from syllafolio.acoustics.config import Config
from syllafolio.acoustics.log_mel import log_mel_patch
from syllafolio.annotation_batch_acoustics import prepare_review
from syllafolio.annotation_catalogue_report import render_html
from syllafolio.annotation_audio_decode import decode_to_wave
from syllafolio.annotation_desktop_project import create_project, open_project, DATASET_RELATIVE, RUN_RELATIVE
from syllafolio.annotation_interval_import import open_preview, create_interval_project


class BandpassImport(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.rate = 48000
        self.settings = {"low_hz": 900, "high_hz": 6000, "order": 4}
        t = np.arange(self.rate) / self.rate
        self.raw = (.30*np.sin(2*np.pi*150*t) + .12*np.sin(2*np.pi*3100*t)
                    + .18*np.sin(2*np.pi*12000*t)).astype("float32")
        self.expected = self.reference(self.raw)

    def reference(self, data):
        sos = signal.butter(self.settings["order"], [self.settings["low_hz"], self.settings["high_hz"]],
                            btype="bandpass", fs=self.rate, output="sos")
        return signal.sosfiltfilt(sos, data).astype("float32")

    def assert_features(self, project, expected):
        config = Config()
        config.audio.sample_rate = self.rate
        config.feature.feature_types = ["mfcc", "mfcc_delta", "spectral", "temporal", "amplitude"]
        actual = project.dataset.features
        np.testing.assert_allclose(actual[0], AudioProcessor(config).extract_features(expected, sr=self.rate),
                                   rtol=1e-5, atol=1e-6)
        folder = project.project_dir / DATASET_RELATIVE
        np.testing.assert_allclose(np.load(folder / "logmel.npy")[0], log_mel_patch(expected, self.rate).ravel(),
                                   rtol=1e-5, atol=1e-6)

    def test_segmented_import_preserves_source_and_uses_filtered_media(self):
        clips = self.root / "clips"
        clips.mkdir()
        path = clips / "bird.wav"
        sf.write(path, self.raw, self.rate, subtype="FLOAT")
        original_file = path.read_bytes()
        raw_identity = stage.inspect_source(clips)["source_fingerprint"]
        with create_project(clips, self.root / "plain") as unfiltered:
            original_versions = unfiltered.dataset.input_versions
            raw_index = unfiltered.service.assessment.index
            np.testing.assert_array_equal(unfiltered.read_audio("syllable-0001")[0], self.raw)
            self.assertNotIn("audio_preprocessing", unfiltered.metadata["preparation_summary"])
        with create_project(clips, self.root / "filtered", expected_source_fingerprint=raw_identity,
                            bandpass=self.settings) as project:
            np.testing.assert_allclose(project.read_audio("syllable-0001")[0], self.expected, rtol=0, atol=1e-7)
            self.assert_features(project, self.expected)
            self.assertEqual(project.service.assessment.index, raw_index)
            self.assertEqual(project.metadata["source_fingerprint"], raw_identity)
            self.assertNotEqual(project.dataset.input_versions["feature_version"], original_versions["feature_version"])
            self.assertEqual(project.metadata["preparation_summary"]["audio_preprocessing"]["low_hz"], 900)
        self.assertEqual(path.read_bytes(), original_file)
        stored = self.root / "filtered" / RUN_RELATIVE / "coordinator/source/segments/syllable-0001.wav"
        self.assertEqual(original_file, stored.read_bytes())
        clips.rename(self.root / "offline")
        with open_project(self.root/"filtered") as reopened:
            folder = self.root/"review"
            page = prepare_review(reopened.dataset, reopened.dataset.item_ids, folder, "direct", .05, "per_item")
            self.assertIn("bandpass", page["audio_processing"])
            review_wav, = folder.rglob("review.wav")
            np.testing.assert_array_equal(sf.read(review_wav, dtype="float32")[0], reopened.read_audio("syllable-0001")[0])
            self.assertTrue(list(folder.rglob("*.png")))
            exporter = reopened.service.catalogue_export
            export = self.root / "export"
            export.mkdir()
            ident = "syllable-0001"
            state = reopened.service.store.snapshot(reopened.service.run_id)
            context, preview = exporter._context(state)
            doc, _, _ = exporter._document(state, preview, context, "draft", "qa-export", "Verify filter", None)
            self.assertEqual(doc["audio_preprocessing"]["low_hz"], 900)
            self.assertIn("Import band-pass filter", render_html(doc))
            doc["catalogue"] = [{"local_type_id":"example","card":{"representative_ids":[ident]}}]
            exporter._write_media(doc, export, threading.Event(), "qa-export")
            media, = doc["media"]
            self.assertEqual(media["audio_processing"]["order"], 4)
            np.testing.assert_array_equal(sf.read(export / media["wav_path"], dtype="float32")[0], reopened.read_audio(ident)[0])

    def test_long_recording_preview_context_and_project_match_full_source_filter(self):
        source, table = self.root / "recording.wav", self.root / "labels.txt"
        sf.write(source, self.raw, self.rate, subtype="FLOAT")
        table.write_bytes(b"0.125\t0.375\tOne\n0.5\t0.75\tTwo\n")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        preview = open_preview(source, table, bandpass=self.settings)
        self.addCleanup(preview.close)
        regions = preview.regions(0)
        heard = [preview.samples(region) for region in regions]
        for region, pcm in zip(regions, heard, strict=True):
            np.testing.assert_allclose(pcm, self.expected[region.start_frame:region.stop_frame], rtol=0, atol=1e-7)
        with self.assertRaisesRegex(ValueError,"Preview"):
            create_interval_project(preview, 0, False, self.root/"wrong", bandpass=None)
        self.assertFalse((self.root/"wrong").exists())
        with patch.object(stage.bp, "filter_audio_file", wraps=stage.bp.filter_audio_file) as apply_filter:
            with create_interval_project(preview, 0, False, self.root/"long") as project:
                self.assertEqual(apply_filter.call_count, 1)
                for ident, pcm in zip(project.dataset.item_ids, heard, strict=True):
                    np.testing.assert_array_equal(project.read_audio(ident)[0], pcm)
                    context, _, bounds = project.read_audio(ident, context=True)
                    np.testing.assert_allclose(context, self.expected[bounds["source_frame_start"]:bounds["source_frame_stop"]],
                                               rtol=0, atol=1e-7)
                self.assert_features(project, heard[0])
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), digest)
        evidence = json.loads((self.root/"long/source_annotations/import.json").read_bytes())
        self.assertEqual(evidence["recording_sha256"], digest)
        self.assertEqual(evidence["audio_preprocessing"]["high_hz"], 6000)
        first_raw = self.root/"long"/RUN_RELATIVE/"coordinator/source/segments/syllable-0001.wav"
        np.testing.assert_array_equal(sf.read(first_raw, dtype="float32")[0], self.raw[6000:18000])
        preview.close()
        source.unlink()
        table.unlink()
        with open_project(self.root/"long") as restored:
            np.testing.assert_array_equal(restored.read_audio(restored.dataset.item_ids[0])[0], heard[0])

    def test_bad_settings_stop_before_creating_project_in_both_routes(self):
        src = self.root / "source"
        src.mkdir()
        recording = src / "a.wav"
        sf.write(recording, self.raw, self.rate)
        table = self.root / "times.txt"
        table.write_bytes(b"0.2\t0.5\tCall\n")
        for bad in ({"low_hz": 1000, "high_hz": 24000, "order": 4},
                    {"low_hz": 5000, "high_hz": 2000, "order": 4}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    create_project(src,self.root/"invalid",bandpass=bad)
                self.assertFalse((self.root/"invalid").exists())
                with self.assertRaises(ValueError):
                    open_preview(recording,table,bandpass=bad)

    def test_mp3_uses_sequentially_decoded_pcm_in_both_routes(self):
        source = self.root / "compressed"
        source.mkdir()
        mp3 = source / "clip.mp3"
        t = np.arange(self.raw.size) / self.rate
        right = .24 * np.sin(2*np.pi*4300*t)
        sf.write(mp3, np.column_stack((self.raw, right)), self.rate)
        original_bytes = mp3.read_bytes()
        decoded = self.root / "decoded.wav"
        decode_to_wave(mp3, decoded)
        mono, rate = sf.read(decoded, dtype="float32")
        self.assertEqual((rate, mono.shape), (self.rate, self.raw.shape))
        # Use a second decoder to check both channel gains. Compare Fourier
        # magnitudes so codec-dependent rounding or phase is immaterial.
        reference, _ = sf.read(mp3, dtype="float32", always_2d=True)
        self.assertEqual(reference.shape, (self.raw.size, 2))
        window = np.hanning(len(mono))
        reference_mix = .5 * reference[:, 0] + .5 * reference[:, 1]
        bands = [3100, 4300]  # One second at 48 kHz, giving one bin per Hz.
        np.testing.assert_allclose(np.abs(np.fft.rfft(window * mono))[bands],
                                   np.abs(np.fft.rfft(window * reference_mix))[bands], rtol=.02)
        expected = self.reference(mono)
        with create_project(source, self.root / "mp3-project", bandpass=self.settings) as project:
            np.testing.assert_allclose(project.read_audio("syllable-0001")[0], expected, rtol=0, atol=1e-7)
        annotations = self.root / "labels.txt"
        annotations.write_bytes(b"0.125\t0.375\tcut\n")
        preview = open_preview(mp3, annotations, bandpass=self.settings)
        self.addCleanup(preview.close)
        region, = preview.regions(0)
        np.testing.assert_allclose(preview.samples(region), expected[6000:18000], rtol=0, atol=1e-7)
        self.assertEqual(mp3.read_bytes(), original_bytes)
