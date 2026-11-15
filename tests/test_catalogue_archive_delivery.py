"""Scope disclosure and publication of an already verified, immutable catalogue ZIP."""
import hashlib
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from syllafolio.annotation_catalogue_export import _example_roles, _package_inventory
from syllafolio.annotation_desktop_export_files import ExporterAdapter, ExportFileError


class CatalogueArchiveDelivery(unittest.TestCase):
    def test_inventory_matches_media_roles_across_subset_boundary(self):
        items = [{"item_id": ident, "in_scope": ident == "A"} for ident in ("A", "B", "C", "D")]
        cards = [("family1", {"representative_ids": ["A"], "counterexample_ids": ["C"]}),
                 ("family2", {"representative_ids": ["C"], "atypical_ids": ["D"]}),
                 ("undefined", None)]
        view = _package_inventory(items, cards)
        self.assertEqual(view["scope_item_ids"], ["A"])
        self.assertEqual(view["all_item_ids"], ["A", "B", "C", "D"])
        self.assertEqual(view["outside_scope_item_ids"], ["B", "C", "D"])
        self.assertEqual(view["example_item_ids"], ["A", "C", "D"])
        self.assertEqual(view["outside_scope_example_ids"], ["C", "D"])
        self.assertEqual(len(_example_roles(cards)["C"]), 2)
        self.assertEqual(view["audit_scope"], "task-wide")

    def test_failed_copy_can_retry_same_job_without_overwriting(self):
        with TemporaryDirectory() as tmp:
            parent = Path(tmp).resolve()
            project = parent / "project"
            project.mkdir()
            service = SimpleNamespace(run_id="run", work_dir=project / "review", dataset_dir=project / "dataset")
            adapter = ExporterAdapter(SimpleNamespace(metadata={"project_id": "p"}, service=service, project_dir=project))
            with self.assertRaisesRegex(ExportFileError, "outside"):
                adapter.check_zip_destination(project / "inside.zip")
            target = parent / "my catalogue.zip"
            job = "catalogue-" + "a" * 20
            asset = project / "registered.zip"
            original = b"sample verified archive"
            asset.write_bytes(original)
            summary = {"job_id": job, "bundle_bytes": len(original),
                       "bundle_sha256": hashlib.sha256(original).hexdigest()}
            verified = (summary, {"catalogue.zip": asset}, {}, b"")
            with patch.object(adapter, "_verify", return_value=verified):
                def change_after_verify(update):
                    self.assertEqual(update["stage"], "copy")
                    asset.write_bytes(original[::-1])
                with self.assertRaisesRegex(ExportFileError, "changed"):
                    adapter.save_zip(job, target, progress=change_after_verify)
                self.assertFalse(target.exists())
                self.assertEqual(list(parent.glob(".d4-save-*")), [])
                asset.write_bytes(original)
                with self.assertRaisesRegex(ExportFileError, "cancelled"):
                    adapter.save_zip(job, target, cancelled=lambda: True)
                self.assertFalse(target.exists())
                # A destination acquired while saving always wins its original name.
                with self.assertRaisesRegex(ExportFileError, "exists"):
                    adapter.save_zip(job, target, progress=lambda *_a, **_k: target.write_bytes(b"other version"))
                self.assertEqual(target.read_bytes(), b"other version")
                fresh = parent / "another name.zip"
                receipt = adapter.save_zip(job, fresh)
                self.assertEqual(receipt["job_id"], job)
                self.assertEqual(fresh.read_bytes(), original)
                self.assertEqual(list(parent.glob(".d4-save-*")), [])
