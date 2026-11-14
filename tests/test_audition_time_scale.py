"""Measure syllable widths in the actual linked Qt plot transforms."""
from io import BytesIO
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image
from PySide6.QtCore import QPointF
from PySide6.QtWidgets import QApplication

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from syllafolio import annotation_desktop_widgets as widgets
from syllafolio.annotation_desktop_widgets import AuditionCanvas


class LongWaveform(unittest.TestCase):
    def test_both_peak_signs_keep_their_times_including_partial_bucket(self):
        # Peak-then-valley in the first bucket; valley-then-peak in the second.
        samples = np.zeros(11, dtype=np.float32)
        samples[[1, 2, 5, 6, 9, 10]] = [.8, -.7, -.6, .9, -.5, .4]
        rate = 48000
        with patch.object(widgets, "_MAX_WAVEFORM_POINTS", 8):
            times, values = widgets._waveform_points(samples, rate)
            self.assertLessEqual(len(values), 8)
            frames = np.rint(times * rate).astype(int)
            self.assertEqual((frames[0], frames[-1]), (0, 10))
            self.assertTrue(np.all(np.diff(frames) >= 0))
            self.assertTrue({1, 2, 5, 6, 9, 10}.issubset(frames))
            np.testing.assert_array_equal(values, samples[frames])
            short = samples[:5]
            times, values = widgets._waveform_points(short, rate)
            self.assertIs(values, short)
            np.testing.assert_array_equal(times, np.arange(len(short)) / rate)


class AuditionTimeScale(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.canvas = AuditionCanvas()
        self.canvas.resize(840, 400)
        self.canvas.show()
        self.addCleanup(self.canvas.close)
        self.settle()

    def settle(self):
        for _ in range(4):
            self.app.processEvents()

    def page(self, durations, scale=5):
        segments = []
        end = 0
        for i, duration in enumerate(durations):
            start = end + (.1 if i else 0)
            end = start + duration
            segments.append(dict(item_id=str(i), start_seconds=start, stop_seconds=end))
        page = dict(slots=segments, display={"plot_bounds": dict(left=0, right=1, top=0, bottom=1)})
        if scale is not None:
            page["page_layout"] = {"ms_per_pixel": scale}
        pcm = np.zeros(round(end * 48000), dtype=np.float32)
        with BytesIO() as buf:
            Image.new("RGB", (128, 48)).save(buf, format="PNG")
            buf.seek(0)
            self.canvas.set_page(pcm, 48000, page, buf, gap_seconds=.1)
        self.settle()

    def slot_width(self, plot, seconds=.3):
        vb = plot.getViewBox()
        return vb.mapViewToScene(QPointF(seconds, 0)).x() - vb.mapViewToScene(QPointF(0, 0)).x()

    def assert_width(self, expected):
        for plot in (self.canvas.spec, self.canvas.wave):
            self.assertAlmostEqual(self.slot_width(plot), expected, delta=.15)

    def test_uniform_full_tail_singleton_and_resize(self):
        c = self.canvas
        for durations in ([.3] * 10, [.3, .15], [.3]):
            self.page(durations)
            self.assert_width(60)
            if len(durations) < 10:
                self.assertGreater(c.spec.viewRange()[0][1], c.duration)
            for width in (520, 980, 840):
                c.resize(width, 400)
                self.settle()
                self.assert_width(60)
                self.assertEqual(len(c.slots), len(durations))
            c.spec.setRange(xRange=(0, .25), yRange=(6, 12), padding=0)
            c.fit_page()
            self.settle()
            self.assert_width(60)
            self.assertEqual(c.spec.viewRange()[1], [0, 16])

    def test_long_clip_scroll_playhead_and_full_overview(self):
        c = self.canvas
        self.page([8])
        self.assert_width(60)
        self.assertGreater(c.time_scroll.maximum(), 0)
        c.time_scroll.setValue(c.time_scroll.maximum())
        self.assertAlmostEqual(c.spec.viewRange()[0][1], 8)
        c.set_position(.1)
        self.assertLessEqual(c.spec.viewRange()[0][0], .1)
        c.set_position(7.9)
        self.assertGreaterEqual(c.spec.viewRange()[0][1], 7.9)
        self.assert_width(60)
        c.entire_page.click()
        self.assertEqual(c.spec.viewRange()[0], [0, 8])
        c.resize(590, 420)
        self.settle()
        self.assertEqual(c.spec.viewRange()[0], [0, 8])
        c.fit_page()
        self.settle()
        self.assert_width(60)

    def test_legacy_pages_keep_their_seconds_window(self):
        self.page([.3], scale=None)
        c = self.canvas
        self.assertAlmostEqual(c.spec.viewRange()[0][1], 3.9)
        c.resize(570, 420)
        self.settle()
        self.assertAlmostEqual(c.spec.viewRange()[0][1], 3.9)
        self.assertFalse(c.time_scroll.isVisible())
