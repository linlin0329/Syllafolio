"""Fine-form counts partition the in-scope family assignments and retain original evidence."""
import csv
from html.parser import HTMLParser
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from syllafolio.annotation_catalogue_report import fine_form_summary, write_catalogue_report
from syllafolio.annotation_form_status import projected_fine_form


class FormRows(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows = {}
        self.status = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.status = dict(attrs).get("data-form-status")
            if self.status:
                self.rows[self.status] = []

    def handle_data(self, text):
        if self.status:
            self.rows[self.status].append(text)

    def handle_endtag(self, tag):
        if tag == "tr":
            self.status = None


class FineFormTotals(unittest.TestCase):
    def test_scoped_counts_and_drilldown_match_frozen_rows(self):
        statuses = ["main", "variant", "candidate_form", "unresolved", "not_assessed",
                    "not_applicable", None, "legacy-other", "main"]
        items = [dict(item_id=f"sound-{i}", in_scope=True, category="assigned", family_id="F",
                      form_status=value, leaf_id=None)
                 for i, value in enumerate(statuses)]
        items[2]["leaf_id"] = "proposed-leaf"
        items.extend([
            dict(item_id="outside", in_scope=False, category="assigned", family_id="F", form_status="variant"),
            dict(item_id="pool", in_scope=True, category="unresolved", form_status="unresolved"),
            dict(item_id="excluded", in_scope=True, category="excluded"),
            dict(item_id="unreviewed", in_scope=True, category="unreviewed", family_id="F"),
        ])
        summary = fine_form_summary(items)
        expected = {"main": 2, "variant": 1, "candidate_form": 1, "unresolved": 1, "not_assessed": 1,
                    "not_applicable": 1, "not_recorded": 1, "other_recorded": 1}
        self.assertEqual(summary, dict(assigned_family_count=9, scope_item_count=12, counts=expected))
        doc = dict(mode="draft", scope={"declared": True}, items=items, counts={"assigned": 9},
                   fine_form_summary=summary)
        with TemporaryDirectory() as temporary:
            folder = Path(temporary)
            write_catalogue_report(folder, doc)
            def csv_rows(name):
                with (folder / "tables" / name).open(encoding="utf-8-sig", newline="") as stream:
                    return list(csv.DictReader(stream))
            counts = csv_rows("fine_form_counts.csv")
            self.assertEqual(expected, {r["form_status"]: int(r["count"]) for r in counts})
            self.assertTrue(all(r["assigned_family_count"] == "9" and r["scope_item_count"] == "12" for r in counts))
            self.assertEqual(json.loads((folder/"catalogue.json").read_text(encoding="utf-8"))["fine_form_summary"], summary)
            raw = {r["item_id"]: r["form_status"] for r in csv_rows("scope_items.csv")}
            self.assertEqual(len(raw), 12)
            self.assertEqual(raw["sound-2"], "candidate_form")
            self.assertEqual(raw["sound-6"], "")
            self.assertEqual(raw["sound-7"], "legacy-other")
            combined = csv_rows("form_unresolved.csv")
            self.assertEqual({r["item_id"] for r in combined}, {"sound-2", "sound-3", "sound-4", "sound-6"})
            page = FormRows()
            page.feed((folder/"report.html").read_text(encoding="utf-8"))
            for i, status in enumerate(statuses):
                bucket = ("other_recorded" if status == "legacy-other" else status) or "not_recorded"
                self.assertIn(f"sound-{i}", " ".join(page.rows[bucket]))
            self.assertFalse(any("outside" in text or "excluded" in text
                                 for row in page.rows.values() for text in row))

    def test_decision_after_reassignment_and_missing_old_form(self):
        decision = {"decision_id": "d", "issue_id": "i", "affected_ids": ["s"],
                    "axes": {"family_status": "assigned", "family_id": "F", "leaf_id": "L", "form_status": "variant"},
                    "reason": "Variant ending."}
        saved = {"legacy_refinement": {"decision_id": "d"}, "form_status": "not_assessed"}
        fine = projected_fine_form("s", [], saved, "F", "L", [decision], {"d"})
        self.assertEqual((fine["form_status"], fine["decision_id"], fine["axes_reason"]), ("variant","d","Variant ending."))
        fine = projected_fine_form("s", ["i"], saved, "other-family", None, [decision], {"d"})
        self.assertEqual((fine["form_status"], fine["decision_id"], fine["axes"]), ("not_assessed", None, None))
        self.assertIsNone(projected_fine_form("s", [], {}, "F", None, [], set())["form_status"])
