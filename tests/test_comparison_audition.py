"""Candidate plan audition preserves project PCM and never plays a superseded sample."""
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch

import numpy as np
from PySide6.QtWidgets import QApplication
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from syllafolio import annotation_desktop_comparison_audition as audition
from syllafolio.annotation_desktop_project import create_project


class ComparisonAuditionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_read_only_media_and_latest_selection(self):
        with TemporaryDirectory(prefix="cmp-test-") as directory:
            root = Path(directory)
            sources = root / "sources"
            sources.mkdir()
            rate = 48000
            t = np.arange(4800) / rate
            for ident, frequency in (("one", 1800), ("two", 3500)):
                sf.write(sources / (ident + ".wav"), .25 * np.sin(2*np.pi*frequency*t),
                         rate, subtype="FLOAT")
            with create_project(sources, root / "project") as project:
                first, second = sorted(project.dataset.item_ids)
                before = project.snapshot()
                pcm, actual_rate, page, image = audition.prepare_audition(project, [second, first], .25)
                samples = [project.read_audio(ident)[0] for ident in (second, first)]
                np.testing.assert_array_equal(pcm, np.concatenate((samples[0], np.zeros(12000), samples[1])))
                self.assertEqual(actual_rate, rate)
                self.assertEqual([slot["item_id"] for slot in page["slots"]], [second, first])
                self.assertEqual(page["slots"][1]["start_frame"], len(samples[0]) + 12000)
                self.assertTrue(image.startswith(b"\x89PNG"))

                widget = audition.ComparisonAuditionWidget(project)
                self.addCleanup(widget.close)
                blocked, release = Event(), Event()
                def delayed(ident):
                    if ident == first:
                        blocked.set()
                        if not release.wait(5):
                            raise RuntimeError("Timed out waiting to switch the group")
                    return original_read(ident)

                original_read = project.read_audio
                with patch.object(project, "read_audio", side_effect=delayed), patch.object(widget.player, "play") as play:
                    widget.set_items([first])
                    try:
                        self.assertTrue(blocked.wait(5))
                        widget.set_items([second])
                    finally:
                        release.set()
                    widget._future.result(timeout=8)
                    widget._check_ready()
                    self.assertEqual([slot["item_id"] for slot in widget.canvas.slots], [second])
                    np.testing.assert_array_equal(widget.player.samples, samples[0])
                    play.assert_not_called()
                    widget.play_button.click()
                    play.assert_called_once_with()
                    widget.clear()
                    self.assertFalse(widget.play_button.isEnabled())
                    self.assertEqual(widget.player.samples.size, 0)
                    self.assertFalse(widget.canvas.slots)
                self.assertEqual(project.snapshot()["revision"], before["revision"])


if __name__ == "__main__":
    unittest.main()
