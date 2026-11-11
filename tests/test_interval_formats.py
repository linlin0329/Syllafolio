"""Small standard annotation files, parsed without Qt, audio IO or float rounding."""
from decimal import Decimal
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from syllafolio.annotation_interval_formats import (
    AnnotationFormatError, detect_format, parse_annotations,
)


LONG_GRID = '''File type = "ooTextFile"
Object class = "TextGrid"
xmin = 0
xmax = 4
tiers? <exists>
size = 3
item []:
    item [91]:
        class = "IntervalTier"
        name = "syllables 中文"
        xmin = 0
        xmax = 4
        intervals: size = 3
        intervals [81]:
            xmin = 0
            xmax = 1
            text = ""
        intervals [82]:
            xmin = 1
            xmax = 2
            text = "He said ""参考_B"""
        intervals [83]:
            xmin = 2
            xmax = 4
            text = "tail"
    item [92]:
        class = "TextTier"
        name = "point track"
        xmin = 0
        xmax = 4
        points: size = 2
        points [1]:
            number = 0.5
            mark = "point one"
        points [2]:
            number = 3
            mark = "point two"
    item [93]:
        class = "IntervalTier"
        name = "context"
        xmin = 0
        xmax = 4
        intervals: size = 1
        intervals [1]:
            xmin = 0
            xmax = 4
            text = "whole context"
'''

SHORT_GRID = '''"ooTextFile short"
"TextGrid"
0 4 ! time domain; 999 is a comment
<exists>
3 tiers
"IntervalTier" "syllables 中文"
0 4
3
0 1 ""
1 2 "He said ""参考_B"""
2 4 "tail"
"TextTier" "point track"
0 4
2
0.5 "point one"
3 "point two"
"IntervalTier" "context"
0 4
1
0 4 "whole context"
'''


def raven(rows, extras="Annotation\tBegin File\tEnd File\tFile Offset (s)"):
    header = "Selection\tView\tChannel\tBegin Time (s)\tEnd Time (s)"
    if extras:
        header += "\t" + extras
    return (header + "\n" + "\n".join(rows) + "\n").encode("utf-8")


class IntervalFormats(unittest.TestCase):
    def parse(self, data, kind):
        if isinstance(data, str):
            data = data.encode("utf-8")
        return parse_annotations(data, kind, "recording.wav")

    def test_textgrid_long_short_and_intermediate_have_identical_data(self):
        long = self.parse(LONG_GRID, "textgrid")
        short = self.parse(SHORT_GRID, "textgrid")
        self.assertEqual(long, short)
        self.assertEqual([tier.name for tier in long], ["syllables 中文", "context"])
        self.assertEqual(len(long[0].intervals), 3)
        self.assertEqual(long[0].intervals[0].label, "")
        self.assertEqual(long[0].intervals[1].label, 'He said "参考_B"')
        self.assertEqual(long[0].info["blank_count"], 1)
        self.assertEqual(sum(t.info["skipped_point_count"] for t in long), 2)
        self.assertEqual(sum(t.info["skipped_point_tier_count"] for t in long), 1)

    def test_textgrid_comments_accept_cr_crlf_and_lf(self):
        reference = self.parse(SHORT_GRID, "textgrid")
        for ending in ("\r", "\r\n", "\n"):
            with self.subTest(ending=repr(ending)):
                self.assertEqual(self.parse(SHORT_GRID.replace("\n", ending), "textgrid"), reference)

    def test_all_supported_text_encodings_preserve_unicode_and_quotes(self):
        reference = self.parse(LONG_GRID, "textgrid")
        for encoding in ("utf-8", "utf-8-sig", "utf-16", "utf-16-le", "utf-16-be"):
            with self.subTest(encoding=encoding):
                data = LONG_GRID.encode(encoding)
                self.assertEqual(self.parse(data, "textgrid"), reference)
                self.assertEqual(detect_format(data, "marks.TextGrid"), "textgrid")

    def test_raven_and_audacity_share_utf8_utf16_decoding(self):
        inputs = {
            "raven": "Begin Time (s)\tEnd Time (s)\tAnnotation\n0.1\t0.2\t参考_B\n",
            "audacity": "0.1\t0.2\t参考_B\n",
        }
        for kind, text in inputs.items():
            reference = self.parse(text, kind)
            for encoding in ("utf-8-sig", "utf-16", "utf-16-le", "utf-16-be"):
                with self.subTest(kind=kind, encoding=encoding):
                    data = text.encode(encoding)
                    self.assertEqual(self.parse(data, kind), reference)
                    self.assertEqual(detect_format(data, "marks.txt"), kind)

    def test_textgrid_decimal_precision_and_literal_text_are_preserved(self):
        precise = "0.123456789012345678901234567890123"
        text = f'''"ooTextFile" "TextGrid"
0 1 <exists> 1
"IntervalTier" "literal" 0 1 1
{precise} 0.9 "space ! and \\ct\nnext line"
'''
        item = self.parse(text, "textgrid")[0].intervals[0]
        self.assertEqual(item.start, Decimal(precise))
        self.assertEqual(item.label, "space ! and \\ct\nnext line")
        self.assertIsInstance(item.end, Decimal)

    def test_textgrid_duplicate_tier_names_remain_separate(self):
        text = SHORT_GRID.replace('"context"', '"syllables 中文"')
        tiers = self.parse(text, "textgrid")
        self.assertEqual(len(tiers), 2)
        self.assertEqual(tiers[0].name, tiers[1].name)
        self.assertNotEqual(tiers[0].intervals[0].source_id, tiers[1].intervals[0].source_id)

    def test_textgrid_point_only_track_returns_no_syllable_tier(self):
        text = '"ooTextFile" "TextGrid" 0 1 <exists> 1 "TextTier" "points" 0 1 1 .5 "mark"'
        self.assertEqual(self.parse(text, "textgrid"), [])

    def test_textgrid_rejects_truncation_extra_data_bad_counts_and_bounds(self):
        cases = [
            SHORT_GRID.replace('"whole context"', ''),
            SHORT_GRID + "99\n",
            SHORT_GRID.replace("3 tiers", "200001 tiers"),
            SHORT_GRID.replace('1 2 "He said', '0.5 2 "He said'),
            SHORT_GRID.replace('2 4 "tail"', '2 5 "tail"'),
            SHORT_GRID.replace('2 4 "tail"', '-1 4 "tail"'),
        ]
        for text in cases:
            with self.subTest(text=text[-80:]):
                with self.assertRaises(AnnotationFormatError):
                    self.parse(text, "textgrid")

    def test_binary_textgrid_has_specific_conversion_guidance(self):
        for data in (b"ooBinaryFile\x08TextGrid\x00\xff", "ooBinaryFile".encode("utf-16")):
            with self.subTest(data=data):
                for function in (lambda: detect_format(data, "a.TextGrid"), lambda: self.parse(data, "textgrid")):
                    with self.assertRaisesRegex(AnnotationFormatError, "Praat.*Save as text file"):
                        function()

    def test_raven_distinct_views_deduplicate_selection_but_not_independent_ids(self):
        rows = [
            "1\tWaveform 1\t1\t0.25\t0.75\t\trecording.wav\trecording.wav\t0.25",
            "1\tSpectrogram 1\t1\t0.25\t0.75\t参考_B\trecording.wav\trecording.wav\t0.250000",
            "2\tSpectrogram 1\t1\t0.25\t0.75\t参考_B\trecording.wav\trecording.wav\t0.25",
            "3\tWaveform 1\t1\t1\t1\tpoint\trecording.wav\trecording.wav\t1",
        ]
        tier = self.parse(raven(rows), "raven")[0]
        self.assertEqual([item.source_id for item in tier.intervals], ["1", "2"])
        self.assertEqual([item.label for item in tier.intervals], ["参考_B", "参考_B"])
        self.assertEqual(tier.info["deduplicated_view_rows"], 1)
        self.assertEqual(tier.info["skipped_point_count"], 1)

    def test_raven_official_two_column_minimum_has_independent_row_ids(self):
        text = "Begin Time (s)\tEnd Time (s)\n0.1\t0.2\n0.1\t0.2\n"
        tier = self.parse(text, "raven")[0]
        self.assertEqual(len(tier.intervals), 2)
        self.assertNotEqual(tier.intervals[0].source_id, tier.intervals[1].source_id)
        self.assertEqual(tier.intervals[0].label, "")
        self.assertEqual(tier.info["recording_binding"], "selected_recording")

    def test_raven_numeric_looking_ids_and_label_quotes_are_not_rewritten(self):
        rows = ['1\tA\t1\t0\t1\t"literal"', '01\tA\t1\t0\t1\t"literal"']
        tier = self.parse(raven(rows, "Label"), "raven")[0]
        self.assertEqual([item.source_id for item in tier.intervals], ["1", "01"])
        self.assertEqual(tier.intervals[0].label, '"literal"')

    def test_raven_begin_end_paths_bind_to_selected_recording(self):
        rows = ["1\tA\t1\t1\t2\tC:\\old folder\\RECORDING.WAV\tc:/old folder/recording.wav\t1"]
        data = raven(rows, "Begin Path\tEnd Path\tFile Offset (s)")
        self.assertEqual(len(self.parse(data, "raven")[0].intervals), 1)
        for old in (b"RECORDING.WAV", b"c:/old folder/recording.wav"):
            with self.subTest(old=old):
                with self.assertRaisesRegex(AnnotationFormatError, "does not match"):
                    self.parse(data.replace(old, b"other.wav"), "raven")

    def test_raven_same_basename_different_absolute_paths_is_a_sequence_conflict(self):
        rows = ["1\tA\t1\t1\t2\tC:\\one\\recording.wav",
                "2\tA\t1\t3\t4\tD:\\two\\recording.wav"]
        with self.assertRaisesRegex(AnnotationFormatError, "multiple absolute recording paths"):
            self.parse(raven(rows, "Begin Path"), "raven")
        consistent = raven(["1\tA\t1\t1\t2\t/old/location/recording.wav"], "Begin Path")
        moved = parse_annotations(consistent, "raven", "C:/new location/recording.wav")
        self.assertEqual(len(moved[0].intervals), 1)

    def test_raven_accepts_display_rounding_without_retiming(self):
        data = raven(["1\tA\t1\t30.123\t31\t30.12304"], "File Offset (s)")
        tier = self.parse(data, "raven")[0]
        self.assertEqual(tier.intervals[0].start, Decimal("30.123"))
        self.assertEqual(tier.info["offset_precision_matches"], 1)

    def test_raven_rejects_sequence_offsets_and_conflicting_views(self):
        cases = [
            raven(["1\tA\t1\t30.123\t31\t0.12304"], "File Offset (s)"),
            raven(["1\tA\t1\t30.123\t31\t30.124"], "File Offset (s)"),
            raven(["1\tA\t1\t1000\t1001\t999.9"], "File Offset (s)"),
            raven(["1\tA\t1\t1\t2", "1\tB\t1\t1\t3"], ""),
            raven(["1\tA\t1\t1\t2\tX", "1\tB\t1\t1\t2\tY"], "Annotation"),
            raven(["1\tA\t1\t1\t2", "1\tA\t1\t1\t2"], ""),
        ]
        for data in cases:
            with self.subTest(data=data):
                with self.assertRaises(AnnotationFormatError):
                    self.parse(data, "raven")

    def test_raven_rejects_wrong_file_channel_ragged_rows_and_duplicate_headers(self):
        cases = [
            raven(["1\tA\t1\t1\t2\tother.wav"], "Begin File"),
            raven(["1\tA\t2\t1\t2"], ""),
            raven(["1\tA\t1\t1"], ""),
            b"Begin Time (s)\tEnd Time (s)\t BeginTime(s) \n1\t2\t1\n",
            raven(["\tA\t1\t1\t2"], ""),
        ]
        for data in cases:
            with self.subTest(data=data):
                with self.assertRaises(AnnotationFormatError):
                    self.parse(data, "raven")

    def test_audacity_regions_points_spectral_rows_and_blank_labels(self):
        text = ("0.25\t0.75\t参考_B\n\\\t-1.000\t7000\n"
                "1\t1\tpoint\n\\\t3000\t3000\n"
                "2\t3\t\n\\\t2000\t-1\n")
        tier = self.parse(text, "audacity")[0]
        self.assertEqual(len(tier.intervals), 2)
        self.assertEqual(tier.intervals[0].start, Decimal("0.25"))
        self.assertEqual(tier.intervals[0].label, "参考_B")
        self.assertEqual(tier.intervals[1].label, "")
        self.assertIn("low=-1.000", tier.intervals[0].notes[0])
        self.assertEqual(tier.info["skipped_point_count"], 1)
        self.assertEqual(tier.info["frequency_line_count"], 3)
        self.assertEqual(tier.info["blank_count"], 1)

    def test_audacity_duplicate_regions_are_independent_and_tabs_stay_in_label(self):
        tier = self.parse("0\t1\tA\tB\n0\t1\tA\tB\n", "audacity")[0]
        self.assertEqual([item.label for item in tier.intervals], ["A\tB", "A\tB"])
        self.assertNotEqual(tier.intervals[0].source_id, tier.intervals[1].source_id)

    def test_audacity_two_numeric_columns_and_point_only_files(self):
        tier = self.parse("0\t1\n2\t2\n", "audacity")[0]
        self.assertEqual(tier.intervals[0].label, "")
        self.assertEqual(tier.info["skipped_point_count"], 1)
        self.assertEqual(self.parse("2\t2\tpoint\n", "audacity")[0].intervals, [])

    def test_audacity_rejects_bad_times_and_orphan_or_repeated_frequency_rows(self):
        cases = ["1\t0\tbad", "NaN\t1\tbad", "-1\t0\tbad", "0 1 text",
                 "\\\t1\t2", "0\t1\tX\n\\\t2\t1", "0\t1\tX\n\\\t-2\t3",
                 "0\t1\tX\n\\\t1\t2\n\\\t1\t2"]
        for text in cases:
            with self.subTest(text=text):
                with self.assertRaises(AnnotationFormatError):
                    self.parse(text, "audacity")

    def test_content_detection_and_auto_use_the_shared_decoder(self):
        self.assertEqual(detect_format(LONG_GRID.encode("utf-16"), "a.txt"), "textgrid")
        self.assertEqual(detect_format(b"Begin Time (s)\tEnd Time (s)\n0\t1", "a.txt"), "raven")
        self.assertEqual(detect_format(b"0\t1\tlabel", "a.txt"), "audacity")
        self.assertEqual(self.parse("0\t1\tlabel", "auto"), self.parse("0\t1\tlabel", "audacity"))
        with self.assertRaises(AnnotationFormatError):
            detect_format(b"0\t1\tlabel", "misnamed.TextGrid")

    def test_bad_encoding_unknown_format_and_empty_input_fail_clearly(self):
        for data in (b"", b"\xff\xfe\x00", b"\x80bad", "0\t1".encode("utf-32")):
            with self.subTest(data=data):
                with self.assertRaises(AnnotationFormatError):
                    self.parse(data, "audacity")
        with self.assertRaises(AnnotationFormatError):
            self.parse("0\t1\tX", "csv")
        with self.assertRaises(AnnotationFormatError):
            detect_format(b"unrecognized text", "a.txt")


if __name__ == "__main__":
    unittest.main()
