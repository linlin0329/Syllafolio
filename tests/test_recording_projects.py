"""End-to-end checks for interval previews, project creation and offline source context."""
import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from syllafolio.annotation_batch_acoustics import prepare_review
from syllafolio.annotation_desktop_project import open_project
from syllafolio.annotation_interval_import import open_preview, create_interval_project


class RecordingImport(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.recording = self.root / "field.wav"
        self.rate = 48000
        self.samples = np.zeros(32 * self.rate, dtype="float32")
        # Put two distinct signals near the end of a recording longer than the clip limit.
        for start, frequency in ((30, 1900), (31, 2600)):
            t = np.arange(self.rate // 4) / self.rate
            self.samples[start*self.rate:start*self.rate+len(t)] = (
                0.15 * np.sin(2*np.pi*frequency*t) * np.hanning(len(t)))
        sf.write(self.recording, self.samples, self.rate, subtype="FLOAT")
        self.labels = self.root / "regions.txt"
        self.labels.write_bytes(b"30\t30.25\tfirst\n31\t31.25\tsecond\n")

    def preview(self, path):
        preview = open_preview(self.recording, path, "auto")
        self.addCleanup(preview.close)
        return preview

    def test_each_format_preserves_the_auditioned_intervals_after_reopen(self):
        textgrid = self.root / "field.TextGrid"
        textgrid.write_text('File type = "ooTextFile short"\n"TextGrid"\n\n0\n32\n<exists>\n1\n'
                            '"IntervalTier"\n"syllables"\n0\n32\n5\n0\n30\n""\n30\n30.25\n"first"\n'
                            '30.25\n31\n""\n31\n31.25\n"second"\n31.25\n32\n""\n', encoding="utf-8")
        raven = self.root / "raven.txt"
        raven.write_bytes(b"Selection\tView\tChannel\tBegin Time (s)\tEnd Time (s)\tAnnotation\n"
                          b"1\tSpectrogram 1\t1\t30\t30.25\tfirst\n1\tWaveform 1\t1\t30\t30.25\tfirst\n"
                          b"2\tSpectrogram 1\t1\t31\t31.25\tsecond\n")
        for table, kind in ((raven,"raven"), (textgrid,"textgrid"), (self.labels,"audacity")):
            with self.subTest(kind=kind):
                preview = self.preview(table)
                self.assertEqual(preview.kind, kind)
                regions = preview.regions(0)
                self.assertEqual([r.label for r in regions], ["first", "second"])
                expected = [self.samples[r.start_frame:r.stop_frame] for r in regions]
                for r, signal in zip(regions, expected, strict=True):
                    np.testing.assert_array_equal(preview.samples(r), signal)
                dest = self.root / kind
                annotation_bytes = table.read_bytes()
                with create_interval_project(preview, 0, False, dest):
                    pass
                preview.close()
                moved_audio = self.recording.rename(self.root / "unavailable.wav")
                moved_table = table.rename(self.root / "unavailable.txt")
                try:
                    provenance = json.loads((dest / "source_annotations/import.json").read_bytes())
                    self.assertEqual(provenance["recording_sha256"], hashlib.sha256(moved_audio.read_bytes()).hexdigest())
                    self.assertEqual((dest / "source_annotations" / provenance["annotation_file"]).read_bytes(), annotation_bytes)
                    self.assertEqual([i["label"] for i in provenance["intervals"]], ["first","second"])
                    with open_project(dest) as reopened:
                        ids = reopened.dataset.item_ids
                        self.assertEqual(len(ids), 2)
                        self.assertEqual(reopened.metadata["preparation_summary"]["human_decisions_created"], 0)
                        self.assertEqual(reopened.metadata["preparation_summary"]["type_memberships_created"], 0)
                        for ident, clip in zip(ids, expected, strict=True):
                            np.testing.assert_array_equal(reopened.read_audio(ident)[0], clip)
                            context, _, meta = reopened.read_audio(ident, context=True)
                            np.testing.assert_array_equal(context, self.samples[meta["source_frame_start"]:meta["source_frame_stop"]])
                        page = self.root / ("review-" + kind)
                        prepare_review(reopened.dataset, ids, page, "direct", .1, "per_item")
                        wav, = page.rglob("review.wav")
                        np.testing.assert_array_equal(sf.read(wav, dtype="float32")[0],
                                                      np.concatenate([expected[0], np.zeros(4800), expected[1]]))
                        self.assertTrue(list(page.rglob("*.png")))
                finally:
                    moved_audio.rename(self.recording)
                    moved_table.rename(table)

    def test_changed_recording_or_table_requires_new_preview(self):
        for changed in (self.recording, self.labels):
            with self.subTest(file=changed.name):
                preview = self.preview(self.labels)
                original = changed.read_bytes()
                changed.write_bytes(original + b"edited")
                try:
                    with self.assertRaisesRegex(ValueError, "changed"):
                        create_interval_project(preview, 0, False, self.root / "rejected")
                    self.assertFalse((self.root / "rejected").exists())
                finally:
                    changed.write_bytes(original)

    def test_selected_textgrid_tier_and_explicit_empty_intervals(self):
        path = self.root / "multiple.TextGrid"
        path.write_text('File type = "ooTextFile"\nObject class = "TextGrid"\n0 32 <exists> 2\n'
                        '"IntervalTier" "phrases" 0 32 1\n0 32 "full phrase"\n'
                        '"IntervalTier" "syllables" 30 30.25 2\n'
                        '30 30.125 ""\n30.125 30.25 "call"\n', encoding="utf-16")
        preview = self.preview(path)
        self.assertEqual([r.label for r in preview.regions(1)], ["call"])
        self.assertEqual([r.label for r in preview.regions(1, True)], ["", "call"])
        project_dir = self.root / "tier"
        with create_interval_project(preview, 1, False, project_dir) as project:
            ident, = project.dataset.item_ids
            fingerprint = project.metadata["source_fingerprint"]
            np.testing.assert_array_equal(project.read_audio(ident)[0],
                                          self.samples[1446000:1452000])
        record = json.loads((project_dir / "source_annotations/import.json").read_bytes())
        self.assertEqual((record["tier"], record["intervals"][0]["label"]), ("syllables", "call"))
        with create_interval_project(preview, 1, False, self.root / "same-input") as repeated:
            self.assertEqual(repeated.metadata["source_fingerprint"], fingerprint)

    def test_out_of_bounds_and_cancellation_do_not_create_project(self):
        self.labels.write_text("31.95\t32.10\tbad end\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "outside"):
            self.preview(self.labels).regions(0)
        self.labels.write_text("30\t30.25\tvalid\n", encoding="utf-8")
        preview = self.preview(self.labels)
        with self.assertRaises(ValueError):
            create_interval_project(preview, 0, False, self.root / "cancelled", cancelled=lambda: True)
        self.assertFalse((self.root / "cancelled").exists())

    def test_mp3_recording_is_previewed_and_frozen_on_the_same_timeline(self):
        from syllafolio.annotation_audio_decode import decode_to_wave
        compressed = self.root / "parent.mp3"
        sf.write(compressed, self.samples[30*self.rate:], self.rate)
        reference = self.root / "reference.wav"
        decode_to_wave(compressed, reference)
        pcm, _ = sf.read(reference, dtype="float32")
        self.labels.write_bytes(b"1.00\t1.25\tsecond\n")
        preview = open_preview(compressed, self.labels)
        self.addCleanup(preview.close)
        region, = preview.regions(0)
        expected = pcm[self.rate:5*self.rate//4]
        np.testing.assert_array_equal(preview.samples(region), expected)
        with create_interval_project(preview, 0, False, self.root / "mp3-project") as project:
            np.testing.assert_array_equal(project.read_audio(project.dataset.item_ids[0])[0], expected)

    def test_temporary_cleanup_cannot_remove_or_strand_a_created_project(self):
        preview = self.preview(self.labels)
        scratch = Path(preview._temporary.name)
        with self.assertRaisesRegex(ValueError, "permanent project folder"):
            create_interval_project(preview, 0, False, scratch / "unsafe")
        self.assertFalse((scratch / "unsafe").exists())
        original_cleanup = TemporaryDirectory.cleanup

        def locked(temporary):
            if Path(temporary.name) == scratch or Path(temporary.name).parent == scratch:
                raise PermissionError("Temporary file still open")
            return original_cleanup(temporary)

        target = self.root / "permanent"
        with patch.object(TemporaryDirectory, "cleanup", locked), self.assertLogs(
                "syllafolio.annotation_interval_import", level="WARNING"):
            with create_interval_project(preview, 0, False, target) as project:
                preview.close()
                self.assertEqual(project.metadata["status"], "ready")
        with open_project(target) as reopened:
            self.assertEqual(len(reopened.dataset.item_ids), 2)
