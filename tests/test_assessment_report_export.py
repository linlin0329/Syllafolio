"""A saved assessment produces consistent machine-readable and human-readable files."""
import csv
import hashlib
import io
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from syllafolio.annotation_assessment_export import export_assessment


class AssessmentReportExport(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.job_id = "assessment-demo"
        self.state = {
            "type_cards": {"F1": {"card_id": "card-new", "local_type_id": "F1", "title": "Future name"}},
            "type_card_history": {"card-old": {"local_type_id": "F1", "title": "=Old <name>"}},
        }
        self.controller = Mock()
        self.controller.project = SimpleNamespace(
            project_dir=self.project,
            service=SimpleNamespace(_lock=threading.RLock(), run_id="run",
                                    store=SimpleNamespace(snapshot=lambda run: self.state)))
        self.controller.job.return_value = {"stale_for_current": True, "stale_reasons": ["scope"]}
        self.document = {
            "schema_version": "annotation-label-assessment-report-v1", "job_id": self.job_id,
            "project_identity": {"project_id": "project"}, "created_at": "2026-01-01",
            "snapshot_fingerprint": "frozen", "frozen_snapshot": {
                "scope": {"scope_id": "s1", "purpose": "Listen <again>"}, "catalogue_revision": 5,
                "source_version": 1, "input_versions": {"feature_version": "v1"},
                "definitions": {"F1": {"card_id": "card-old"}}},
            "result": {"status": "not_estimable", "preset": {}, "metrics": None,
                       "coverage": {"n_scope": 8, "n_eligible": 0, "n_families": 1, "n_eligible_families": 0},
                       "support": [{"family_id": "F1", "n_confirmed": 8, "n_source_groups": 1, "eligible": False,
                                    "reasons": [{"message": "Insufficient recording sources."}]}],
                       "reasons": [{"code": "INSUFFICIENT_FAMILIES", "message": "One family is available."}]},
        }
        self.raw = json.dumps(self.document, indent=4).replace("\n", "\r\n").encode("utf-8") + b"\r\n"
        self.controller.report_bytes.return_value = self.raw
        self.output = self.root / "assessment.zip"

    def test_exact_payload_unscored_tables_historical_names_and_status(self):
        receipt = export_assessment(self.controller, self.job_id, self.output)
        self.controller.report_bytes.assert_called_once_with(self.job_id)
        self.assertEqual(receipt["sha256"], hashlib.sha256(self.output.read_bytes()).hexdigest())
        with zipfile.ZipFile(self.output) as archive:
            self.assertEqual(archive.read("assessment.json"), self.raw)
            self.assertEqual(len([n for n in archive.namelist() if n.endswith(".csv")]), 6)
            meta = json.loads(archive.read("manifest.json"))
            self.assertTrue(meta["stale_for_current"])
            for row in meta["files"]:
                self.assertEqual(row["sha256"], hashlib.sha256(archive.read(row["path"])).hexdigest())
            def rows(name):
                content = archive.read("tables/assessment_" + name + ".csv").decode("utf-8-sig")
                return list(csv.DictReader(io.StringIO(content)))
            self.assertEqual(rows("metrics"), [])
            self.assertEqual(rows("predictions"), [])
            family, = rows("support")
            self.assertEqual(family["family_id"], "F1")
            self.assertEqual(family["family_name"], "'=Old <name>")
            html = archive.read("report.html").decode()
            for message in ["Historical assessment", "One family is available.", "Insufficient recording sources.",
                            "Listen &lt;again&gt;", "=Old &lt;name&gt;", "assessment.json", "manifest.json"]:
                self.assertIn(message, html)
            self.assertNotIn("Future name", html)

    def test_protected_destinations_and_failed_write_preserve_previous_file(self):
        with self.assertRaisesRegex(ValueError, "outside"):
            export_assessment(self.controller, self.job_id, self.project / "overwrite.zip")
        self.output.write_bytes(b"existing report")
        with self.assertRaises(FileExistsError):
            export_assessment(self.controller, self.job_id, self.output)
        with patch("syllafolio.annotation_assessment_export._html_report", side_effect=ValueError("test error")):
            with self.assertRaisesRegex(ValueError, "test error"):
                export_assessment(self.controller, self.job_id, self.output, overwrite=True)
        self.assertEqual(self.output.read_bytes(), b"existing report")
        self.assertEqual(list(self.root.glob(".assessment-*")), [])
