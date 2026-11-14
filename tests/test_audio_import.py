"""Exercise the import and review paths using small, generated recordings."""
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from syllafolio.annotation_batch_acoustics import prepare_review
from syllafolio.annotation_audio_decode import decode_to_wave
from syllafolio.annotation_desktop_project import DATASET_RELATIVE, DesktopProjectError, create_project, open_project
from syllafolio.annotation_desktop_stage_a import inspect_input
from syllafolio.annotation_stage_a import SOURCE_SCHEMA, StageAError, inspect_source, stage_source


class AudioImport(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "clips"
        self.source.mkdir()
        self.rate = 48000
        time = np.arange(7200) / self.rate
        self.tone = (0.2 * np.sin(2 * np.pi * 3200 * time) * np.hanning(time.size)).astype("float32")

    def manifest(self, entries):
        (self.source / "segments.json").write_text(
            json.dumps({"schema_version": SOURCE_SCHEMA, "items": entries}), encoding="utf-8")

    def read_original(self, path):
        if path.suffix.lower() == ".mp3":
            decoded = self.root / (path.stem + "-reference.wav")
            decode_to_wave(path, decoded)
            path = decoded
        samples, _ = sf.read(path, dtype="float32")
        if samples.ndim == 2:
            return .5 * samples[:, 0] + .5 * samples[:, 1]
        return samples

    def test_mixed_directory_reopens_and_renders_exact_audition_audio(self):
        clips = [("01.WAV", "WAV", "PCM_16"), ("02.flac", "FLAC", "PCM_24"),
                 ("03.MP3", "MP3", "MPEG_LAYER_III"), ("04.ogg", "OGG", "VORBIS"),
                 ("05.aif", "AIFF", "PCM_16"), ("06.AIFF", "AIFF", "PCM_16")]
        expected = []
        for index, (filename, codec, subtype) in enumerate(clips):
            path = self.source / filename
            signal = self.tone / (index + 1)
            if codec in ("FLAC", "OGG") or filename == "06.AIFF":
                signal = np.column_stack((signal, np.roll(signal, 7)))
            sf.write(path, signal, self.rate, format=codec, subtype=subtype)
            expected.append(self.read_original(path))
        (self.source / "unsupported.m4a").touch()
        preview = inspect_input(self.source)
        self.assertEqual([row["filename"] for row in preview["items"]], [c[0] for c in clips])
        self.assertEqual(preview["summary"]["skipped_audio_files"], ["unsupported.m4a"])
        project_dir = self.root / "project"
        with create_project(self.source, project_dir,
                            expected_source_fingerprint=preview["summary"]["source_fingerprint"],
                            expected_inspection_fingerprint=preview["inspection_fingerprint"]):
            pass
        self.source.rename(self.root / "offline")
        with open_project(project_dir) as project:
            dataset_dir = project_dir / DATASET_RELATIVE
            items = json.loads((dataset_dir / "manifest.json").read_text("utf-8"))["items"]
            for row, (name, _, _), pcm in zip(items, clips, expected, strict=True):
                stored = dataset_dir / row["wav_path"]
                suffix = Path(name).suffix.lower()
                if suffix == ".mp3" or sf.info(self.root / "offline" / name).channels == 2:
                    self.assertEqual((sf.info(stored).channels, sf.info(stored).subtype), (1, "FLOAT"))
                else:
                    self.assertEqual(stored.suffix, suffix)
                original = dataset_dir.parent / "coordinator" / "source" / "segments" / (row["item_id"] + suffix)
                self.assertEqual(original.read_bytes(), (self.root / "offline" / name).read_bytes())
                samples, rate, _ = project.read_audio(row["item_id"])
                self.assertEqual(rate, self.rate)
                np.testing.assert_array_equal(samples, pcm)
            folder = self.root / "audition"
            prepare_review(project.dataset, [row["item_id"] for row in items], folder,
                           "direct", 0.05, "per_item")
            review_wav, = folder.rglob("review.wav")
            self.assertTrue(list(folder.rglob("*.png")))
            silence = np.zeros(round(0.05 * self.rate), dtype="float32")
            chunks = [expected[0]]
            for pcm in expected[1:]:
                chunks.extend((silence, pcm))
            np.testing.assert_array_equal(sf.read(review_wav, dtype="float32")[0], np.concatenate(chunks))

    def test_parent_mapping_uses_decoded_frames_and_preserves_container_bytes(self):
        parent = self.source / "recording.mp3"
        sf.write(parent, self.tone, self.rate)
        pcm = self.read_original(parent)
        segment = self.source / "clip.aiff"
        start, stop = 1300, 4500
        sf.write(segment, pcm[start:stop], self.rate, subtype="FLOAT")
        self.manifest([{"file": segment.name, "parent_file": parent.name,
                        "source_frame_start": start, "source_frame_stop": stop}])
        plan = inspect_source(self.source)
        frozen = self.root / "frozen"
        self.assertEqual(stage_source(self.source, frozen)["source_fingerprint"], plan["source_fingerprint"])
        row, = json.loads((frozen / "segments.json").read_text("utf-8"))["items"]
        self.assertEqual((frozen / row["parent_file"]).read_bytes(), parent.read_bytes())
        self.assertEqual((frozen / row["file"]).read_bytes(), segment.read_bytes())
        project_dir = self.root / "mapped-project"
        with create_project(self.source, project_dir):
            pass
        offline = self.source.rename(self.root / "offline")
        with open_project(project_dir) as project:
            ident, = project.dataset.item_ids
            context, rate, bounds = project.read_audio(ident, context=True)
            self.assertEqual(rate, self.rate)
            np.testing.assert_array_equal(context, pcm[bounds["source_frame_start"]:bounds["source_frame_stop"]])
            np.testing.assert_array_equal(project.read_audio(ident)[0], pcm[start:stop])
        offline.rename(self.source)
        sf.write(segment, pcm[start+1:stop+1], self.rate, subtype="FLOAT")
        with self.assertRaises(StageAError) as mismatch:
            inspect_source(self.source)
        self.assertEqual(mismatch.exception.code, "PARENT_PCM_MISMATCH")

    def test_48khz_dual_mono_matches_mono_in_audio_and_both_feature_views(self):
        mono = self.source / "01-mono.wav"
        stereo = self.source / "02-stereo.wav"
        sf.write(mono, self.tone, self.rate, subtype="FLOAT")
        sf.write(stereo, np.column_stack((self.tone, self.tone)), self.rate, subtype="FLOAT")
        project_dir = self.root / "dual-mono"
        with create_project(self.source, project_dir) as project:
            ids = project.dataset.item_ids
            self.assertEqual(len(ids), 2)
            for ident in ids:
                audio, rate, _ = project.read_audio(ident)
                self.assertEqual(rate, self.rate)
                np.testing.assert_array_equal(audio, self.tone)
            features = project.dataset.features
            patches = np.load(project_dir / DATASET_RELATIVE / "logmel.npy")
            np.testing.assert_array_equal(features[0], features[1])
            np.testing.assert_array_equal(patches[0], patches[1])

    def test_legacy_mono_project_reopens_without_rewriting_its_marker(self):
        sf.write(self.source / "clip.wav", self.tone, self.rate, subtype="FLOAT")
        folder = self.root / "legacy-mono"
        with create_project(self.source, folder):
            pass
        marker_file = folder / "project.json"
        marker = json.loads(marker_file.read_bytes())
        requirements = marker["preparation_summary"]["input_requirements"]
        # The v1.0.0 importer stored channels as a scalar and omitted mix metadata.
        requirements["channels"] = 1
        for newer_key in ("source_channels", "analysis_channels", "stereo_mix"):
            del requirements[newer_key]
        legacy = json.dumps(marker, ensure_ascii=False, indent=2).encode("utf-8")
        marker_file.write_bytes(legacy)
        self.source.rename(self.root / "offline")
        with open_project(folder) as reopened:
            samples, rate, _ = reopened.read_audio(reopened.dataset.item_ids[0])
            self.assertEqual(rate, self.rate)
            np.testing.assert_array_equal(samples, self.tone)
        self.assertEqual(marker_file.read_bytes(), legacy)
        requirements["minimum_sample_rate_hz"] = 8000
        marker_file.write_text(json.dumps(marker), encoding="utf-8")
        with self.assertRaises(DesktopProjectError) as mismatch:
            open_project(folder)
        self.assertEqual(mismatch.exception.code, "PROJECT_IDENTITY")

    def test_unindexed_variable_bitrate_mp3_uses_every_frame(self):
        # Valid silent MPEG-1 Layer III frames at 48 kHz, without a Xing duration.
        # The first frame has a higher bitrate; estimating from it truncates the clip.
        frames = []
        for index, kbps in [(9, 128)] + [(1, 32)] * 19:
            header = bytes((0xff, 0xfb, (index << 4) | 4, 0xc0))
            frames.append(header + bytes((144000 * kbps // 48000) - len(header)))
        (self.source / "variable.mp3").write_bytes(b"".join(frames))
        inspection = inspect_source(self.source)
        self.assertEqual(round(inspection["total_duration_s"] * 48000), len(frames) * 1152)

    def test_incompatible_inputs_are_rejected_during_inspection(self):
        for name, fmt, subtype, samples, rate, code in [
            ("wrong.mp3", "FLAC", "PCM_16", self.tone, self.rate, "SOURCE_FORMAT"),
            ("three-channel.flac", "FLAC", "PCM_16", np.column_stack([self.tone] * 3), self.rate, "AUDIO_CHANNELS"),
            ("low.aiff", "AIFF", "PCM_16", self.tone, 22050, "AUDIO_SAMPLE_RATE"),
            ("other.ogg", "OGG", "OPUS", self.tone, self.rate, "SOURCE_FORMAT"),
        ]:
            with self.subTest(name=name):
                sf.write(self.source / name, samples, rate, format=fmt, subtype=subtype)
                self.manifest([{"file": name}])
                with self.assertRaises(StageAError) as failure:
                    inspect_source(self.source)
                self.assertEqual(failure.exception.code, code)
        (self.source / "broken.mp3").write_bytes(b"not an audio file")
        self.manifest([{"file": "broken.mp3"}])
        with self.assertRaisesRegex(StageAError, "broken.mp3"):
            inspect_source(self.source)
