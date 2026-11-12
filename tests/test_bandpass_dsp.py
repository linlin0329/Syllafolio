"""Compare file-streamed filtering against an independent whole-signal reference."""
import hashlib
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import numpy as np
from scipy.io import wavfile
from scipy.signal import butter, sosfiltfilt, sosfreqz
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from syllafolio.acoustics import bandpass


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class BandpassDsp(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = {"low_hz": 1000, "high_hz": 15000}

    def source(self, values, rate=48000, name="source.wav"):
        path = self.root / name
        wavfile.write(path, rate, values)
        return path

    def reference(self, source, settings):
        samples, rate = sf.read(source, dtype="float32")
        normalized = bandpass.validate_bandpass(settings, rate)
        sos = butter(normalized["order"], [normalized["low_hz"], normalized["high_hz"]],
                     btype="bandpass", fs=rate, output="sos")
        # Independent transcription of scipy's documented default padding.
        taps = 2 * len(sos) + 1
        taps -= min(np.count_nonzero(sos[:, 2] == 0), np.count_nonzero(sos[:, 5] == 0))
        edge = min(3 * taps, len(samples) - 1)
        return sosfiltfilt(sos, samples, padlen=edge).astype("float32"), edge

    def test_validate_off_normalizes_defaults_and_does_not_mutate_input(self):
        self.assertIsNone(bandpass.validate_bandpass(None, 48000))
        supplied = dict(self.settings)
        self.assertEqual(bandpass.validate_bandpass(supplied, 48000),
                         {"low_hz": 1000.0, "high_hz": 15000.0, "order": 4})
        self.assertEqual(supplied, self.settings)

    def test_invalid_cutoffs_orders_rates_and_unknown_fields_are_rejected(self):
        for settings, rate in [
            ({}, 48000), ({"low_hz": 0, "high_hz": 15000}, 48000),
            ({"low_hz": 1000, "high_hz": 24000}, 48000),
            ({"low_hz": 15000, "high_hz": 1000}, 48000),
            ({"low_hz": True, "high_hz": 15000}, 48000),
            ({"low_hz": "1000", "high_hz": 15000}, 48000),
            ({"low_hz": float("nan"), "high_hz": 15000}, 48000),
            ({"low_hz": 1000, "high_hz": float("inf")}, 48000),
            ({**self.settings, "order": 0}, 48000), ({**self.settings, "order": 9}, 48000),
            ({**self.settings, "order": True}, 48000), ({**self.settings, "order": 4.0}, 48000),
            ({**self.settings, "gain": 1}, 48000), (None, 0), (None, True),
            (None, float("nan")), (None, 48000.5),
        ]:
            with self.subTest(settings=settings, rate=rate):
                with self.assertRaises(ValueError):
                    bandpass.validate_bandpass(settings, rate)
        with self.assertRaisesRegex(ValueError, "below 16000 Hz for 32000 Hz audio"):
            bandpass.validate_bandpass({"low_hz": 1000, "high_hz": 18000}, 32000)

    def test_multi_block_results_match_scipy_for_orders_and_sample_rates(self):
        rng = np.random.default_rng(38)
        values = (rng.standard_normal(300_031) * .1).astype("float32")
        for index, (rate, order) in enumerate([(32000, 1), (44100, 4), (48000, 8), (96000, 4)]):
            with self.subTest(rate=rate, order=order):
                source = self.source(values, rate, f"source-{index}.wav")
                destination = self.root / f"filtered-{index}.wav"
                settings = {**self.settings, "order": order}
                before = sha(source)
                with patch.object(bandpass, "_CHUNK_FRAMES", 4093):
                    metadata = bandpass.filter_audio_file(source, destination, settings)
                actual, actual_rate = sf.read(destination, dtype="float32")
                expected, edge = self.reference(source, settings)
                np.testing.assert_array_equal(actual, expected)
                self.assertEqual(actual_rate, rate)
                self.assertEqual((len(actual), metadata["padlen"]), (len(values), edge))
                self.assertEqual(sha(source), before)

    def test_short_inputs_match_scipy_with_recorded_adaptive_padding(self):
        rng = np.random.default_rng(52)
        for size in [1, 2, 7, 8, 26, 27, 28, 52]:
            with self.subTest(size=size):
                source = self.source(rng.normal(size=size).astype("float32"), name=f"short-{size}.wav")
                destination = self.root / f"short-out-{size}.wav"
                metadata = bandpass.filter_audio_file(source, destination, self.settings)
                expected, edge = self.reference(source, self.settings)
                actual, _ = sf.read(destination, dtype="float32")
                np.testing.assert_array_equal(actual, expected)
                self.assertEqual(metadata["padlen"], edge)

    def test_off_and_pcm32_input_follow_the_app_float32_definition(self):
        integers = np.array([1, 17, 33554431, 1073741825, -1073741823] * 41, dtype="int32")
        source = self.source(integers)
        decoded, _ = sf.read(source, dtype="float32")
        off = self.root / "off.wav"
        metadata = bandpass.filter_audio_file(source, off, None)
        np.testing.assert_array_equal(sf.read(off, dtype="float32")[0], decoded)
        self.assertEqual(metadata["phase"], "off")
        destination = self.root / "on.wav"
        bandpass.filter_audio_file(source, destination, self.settings)
        np.testing.assert_array_equal(sf.read(destination, dtype="float32")[0], self.reference(source, self.settings)[0])

    def test_full_recording_filter_matches_adjacent_and_overlapping_slices(self):
        rate = 48000
        time = np.arange(rate * 3) / rate
        values = (.2*np.sin(2*np.pi*2000*time)+.1*np.sin(2*np.pi*100*time)).astype("float32")
        source = self.source(values)
        destination = self.root / "full.wav"
        bandpass.filter_audio_file(source, destination, self.settings)
        reference, _ = self.reference(source, self.settings)
        for start, stop in [(12000, 24000), (24000, 36000), (18000, 30000)]:
            with self.subTest(start=start, stop=stop):
                actual = sf.read(destination, start=start, stop=stop, dtype="float32")[0]
                np.testing.assert_array_equal(actual, reference[start:stop])

    def test_frequency_gain_matches_forward_backward_response_without_normalization(self):
        rate = 48000
        frequencies = [100, 3000, 22000]
        time = np.arange(rate * 4) / rate
        amplitudes = [.17, .03, .11]
        source = self.source(sum(a*np.sin(2*np.pi*f*time) for a,f in zip(amplitudes, frequencies)).astype("float32"))
        destination = self.root / "response.wav"
        bandpass.filter_audio_file(source, destination, self.settings)
        values = sf.read(destination, dtype="float32")[0][rate:3*rate]
        local_time = np.arange(len(values)) / rate
        sos = butter(4, [1000, 15000], btype="bandpass", fs=rate, output="sos")
        _, response = sosfreqz(sos, worN=frequencies, fs=rate)
        for f, amplitude, gain in zip(frequencies, amplitudes, abs(response)**2):
            measured = abs(np.sum(values*np.exp(-2j*np.pi*f*local_time))) * 2/len(values)
            self.assertAlmostEqual(measured, amplitude*gain, delta=1e-6)
        self.assertLess(float(np.max(np.abs(values))), .04)

    def test_wav_bytes_are_deterministic_across_runs_and_chunk_sizes(self):
        source = self.source(np.linspace(-.8, .8, 5003, dtype="float32"))
        outputs = []
        for index, chunk in enumerate([53, 4096, 131072]):
            destination = self.root / f"repeat-{index}.wav"
            with patch.object(bandpass, "_CHUNK_FRAMES", chunk):
                bandpass.filter_audio_file(source, destination, self.settings)
            outputs.append(destination.read_bytes())
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(outputs[1], outputs[2])
        self.assertNotIn(b"PEAK", outputs[0][:100])
        info = sf.info(self.root / "repeat-0.wav")
        self.assertEqual((info.format, info.subtype, info.channels, info.frames), ("WAV", "FLOAT", 1, 5003))

    def test_cancellation_in_each_pass_and_bypass_removes_only_owned_partials(self):
        source = self.source(np.linspace(-1, 1, 20_000, dtype="float32"))
        before = sha(source)
        for index, (settings, threshold) in enumerate([(self.settings, 1), (self.settings, 8),
                                                     (self.settings, 55), (None, 6)]):
            with self.subTest(settings=settings, threshold=threshold):
                destination = self.root / f"cancel-{index}.wav"
                calls = 0
                def cancelled():
                    nonlocal calls
                    calls += 1
                    return calls >= threshold
                with patch.object(bandpass, "_CHUNK_FRAMES", 1000), \
                        patch.object(bandpass, "_backward", wraps=bandpass._backward) as backward:
                    with self.assertRaises(bandpass.BandpassCancelled) as error:
                        bandpass.filter_audio_file(source, destination, settings, cancelled=cancelled)
                self.assertEqual(backward.call_count, int(threshold == 55))
                self.assertEqual(error.exception.code, "CANCELLED")
                self.assertFalse(destination.exists())
                self.assertEqual(list(self.root.glob(".bandpass-*.tmp")), [])
        self.assertEqual(sha(source), before)

    def test_existing_destination_and_publish_race_never_overwrite_other_files(self):
        source = self.source(np.ones(3000, dtype="float32"))
        destination = self.root / "existing.wav"
        destination.write_bytes(b"OWNED_SENTINEL")
        with self.assertRaises(FileExistsError):
            bandpass.filter_audio_file(source, destination, self.settings)
        self.assertEqual(destination.read_bytes(), b"OWNED_SENTINEL")
        destination.unlink()
        original = bandpass._publish
        def raced(temporary, target):
            target.write_bytes(b"RACE_SENTINEL")
            original(temporary, target)
        with patch.object(bandpass, "_publish", side_effect=raced):
            with self.assertRaises(FileExistsError):
                bandpass.filter_audio_file(source, destination, self.settings)
        self.assertEqual(destination.read_bytes(), b"RACE_SENTINEL")
        self.assertEqual(list(self.root.glob(".bandpass-*.tmp")), [])

    def test_stereo_empty_and_nonfinite_sources_fail_without_publication(self):
        cases = [np.zeros((200, 2), dtype="float32"), np.array([], dtype="float32"),
                 np.array([0, 1, np.nan, 2], dtype="float32")]
        for index, values in enumerate(cases):
            with self.subTest(index=index):
                source = self.source(values, name=f"bad-{index}.wav")
                destination = self.root / f"bad-out-{index}.wav"
                with self.assertRaises(ValueError):
                    bandpass.filter_audio_file(source, destination, self.settings)
                self.assertFalse(destination.exists())
                self.assertEqual(list(self.root.glob(".bandpass-*.tmp")), [])

    def test_stream_reads_are_bounded_and_io_failure_cleans_partials(self):
        source = self.source(np.zeros(300_000, dtype="float32"))
        sizes = []
        original = bandpass._read
        def observe(stream, frames):
            sizes.append(frames)
            return original(stream, frames)
        with patch.object(bandpass, "_read", side_effect=observe):
            bandpass.filter_audio_file(source, self.root/"bounded.wav", self.settings)
        self.assertGreater(len(sizes), 2)
        self.assertLessEqual(max(sizes), bandpass._CHUNK_FRAMES)
        with patch.object(bandpass, "_backward", side_effect=OSError("INDEPENDENT QA disk-write failure")):
            with self.assertRaises(OSError):
                bandpass.filter_audio_file(source, self.root/"write-error.wav", self.settings)
        self.assertFalse((self.root/"write-error.wav").exists())
        self.assertEqual(list(self.root.glob(".bandpass-*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
