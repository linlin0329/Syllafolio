"""Freeze and export the feature band independently of filter and display limits."""
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4
import zipfile

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from syllafolio.annotation_analysis_metadata import report_analysis
from syllafolio.annotation_assessment_export import export_assessment
from syllafolio.annotation_batch_review import CLOSE_CHECKS
from syllafolio.annotation_desktop_assessment import AssessmentController
from syllafolio.annotation_desktop_organization import OrganizationController
from syllafolio.annotation_desktop_project import create_project
from syllafolio.annotation_manual_protocol import COMPLETION_CHECKS


class NativeRateReports(unittest.TestCase):
    def test_historical_metadata_uses_only_known_frozen_recipe(self):
        frozen = {"input_versions": {"feature_version": "stage-a-existing105-logmel64x32-v1"},
                  "numeric_row_identity": [{"sample_rate_hz": 44100}]}
        self.assertEqual(report_analysis(frozen)["mfcc_logmel_frequency_band_hz"], [1000, 15000])
        frozen["input_versions"]["feature_version"] = "external-feature-dataset"
        self.assertIsNone(report_analysis(frozen)["mfcc_logmel_frequency_band_hz"])
        self.assertEqual(report_analysis(frozen)["sample_rate_hz"], 44100)

    def test_filtered_low_rate_complete_and_assessment_keep_same_analysis(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            clips = root / "clips"
            clips.mkdir()
            rate = 16000
            t = np.arange(3200) / rate
            for i, hz in enumerate((3300, 5500)):
                sf.write(clips / f"call-{i}.wav", .1 * np.sin(2 * np.pi * hz * t), rate)
            bandpass = {"low_hz": 650, "high_hz": 6200, "order": 4}
            with create_project(clips, root / "project", bandpass=bandpass) as project:
                def submit(name, payload):
                    return project.execute(name, payload, expected_revision=project.snapshot()["revision"],
                                           idempotency_key=uuid4().hex)

                org = OrganizationController()
                org.preview(project, generation=0, idempotency_key="preview")
                org.apply(project, generation=0, idempotency_key="apply")
                ids = project.dataset.item_ids
                family = "F-test"
                submit("batch_family", dict(family_id=family, title="Test family", reason="Test fixture",
                                            diagnostic_body="Two test tones"))
                review = submit("batch_prepare_pool", dict(route="pool_cluster", pool_mode="current_pool",
                                normalization="per_item", gap_seconds=.1))["record"]["review"]["review_id"]
                project.service._futures[review].result(timeout=60)
                for page in project.snapshot()["batch_review"]["reviews"][review]["pages"]:
                    submit("batch_submit_v2", dict(review_id=review, page_id=page["page_id"], current_family_id=family,
                           items=[dict(item_id=i, disposition="current", family_id=family) for i in page["item_ids"]]))
                submit("batch_close", dict(review_id=review, checks=dict.fromkeys(CLOSE_CHECKS, True), note="Test."))
                submit("catalogue_scope", dict(scope_ids=ids, target_level="family", purpose="Report test",
                       coverage_plan="Both calls", quality_requirements="Match metadata",
                       unresolved_policy="Retain", exclusions_policy="Retain"))
                submit("type_card", dict(local_type_id=family, title="Test family", diagnostic_body="Two test tones",
                       main_variant_difference="", representative_ids=[ids[0]], atypical_ids=[], counterexample_ids=[],
                       competing_type_ids=[], uncertainties="Technical fixture", reason="Report test", definition_status="reviewed"))
                submit("scoped_complete", dict(scope_id=project.snapshot()["catalogue_scope"]["scope_id"],
                       checks=dict.fromkeys(COMPLETION_CHECKS, True), quality_evidence="Checked the two fixture records.",
                       unresolved_summary="None", exclusions_summary="None", limitations="Two test calls."))
                job = submit("assessment_start", {"reason": "Test support report"})["record"]["job_id"]
                project.service.assessment.futures[job].result(timeout=60)
                standalone = root / "assessment.zip"
                export_assessment(AssessmentController(project), job, standalone)
                eid = submit("catalogue_export_start", dict(mode="complete", assessment_job_id=job,
                             reason="Verify the same analysis record"))["record"]["job_id"]
                project.service.catalogue_export.futures[eid].result(timeout=60)
                saved = project.service.store.read_snapshot(project.service.run_id)["catalogue_exports"]["jobs"][eid]
                self.assertEqual(saved["status"], "completed", saved.get("error"))
                with zipfile.ZipFile(standalone) as d3, zipfile.ZipFile(project.media_path(saved["bundle_url"])) as d4:
                    self.assertEqual(d3.read("assessment.json"), d4.read("assessment.json"))
                    for path in [n for n in d3.namelist() if n.endswith(".csv")]:
                        self.assertEqual(d3.read(path), d4.read(path))
                    document = json.loads(d4.read("catalogue.json"))
                    analysis = document["acoustic_analysis"]
                    self.assertEqual(analysis["mfcc_logmel_frequency_band_hz"], [1000, 7500])
                    self.assertEqual(analysis["sample_rate_hz"], rate)
                    self.assertEqual(analysis["feature_version"], project.dataset.input_versions["feature_version"])
                    self.assertEqual(analysis, json.loads(d3.read("assessment.json"))["frozen_snapshot"]["acoustic_analysis"])
                    self.assertEqual(analysis, json.loads(d4.read("audit.json"))["acoustic_analysis"])
                    self.assertEqual(document["spectrogram_display"]["frequency_max_hz_actual"], 8000)
                    self.assertEqual(document["media"][0]["display"]["frequency_max_hz_actual"], 8000)
                    for package in (d3, d4):
                        html = package.read("report.html").decode()
                        for label in ("16000 Hz", "1000–7500 Hz", "650–6200 Hz", analysis["feature_version"]):
                            self.assertIn(label, html)
