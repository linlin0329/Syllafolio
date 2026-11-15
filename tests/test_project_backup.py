"""Verify deep backup paths and recovery from a partial copy failure."""
from pathlib import Path
import hashlib
import shutil
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from syllafolio import annotation_desktop_backup as backups
from syllafolio.annotation_desktop_project import DesktopProjectError, backup_project, create_project, open_project


class ProjectBackupTests(unittest.TestCase):
    def test_deep_paths_and_partial_copy_failure(self):
        with TemporaryDirectory() as temporary:
            # Use an extended path for teardown, including on Windows systems
            # whose normal rmtree cannot descend beyond MAX_PATH.
            with TemporaryDirectory(dir=backups._filesystem_path(Path(temporary))) as private:
                root = Path(temporary) / Path(private).name
                source = root / "project"
                clips = root / "clips"
                clips.mkdir()
                time = np.arange(6000) / 48000
                for i, hz in enumerate((2200, 3100)):
                    sf.write(clips / f"{i}.wav", np.sin(2*np.pi*hz*time)*np.hanning(time.size), 48000)
                with create_project(clips, source):
                    pass

                # A portable final path that would exceed MAX_PATH while
                # copying into the previous staging-directory layout.
                destination = root / "working-copy"
                parent = Path("review/batch_display")
                while len(str(destination / parent)) < 207:
                    parent /= "a"*20
                relative = parent / "spectrogram-0123456789.png"
                asset = backups._filesystem_path(source) / relative
                asset.parent.mkdir(parents=True)
                asset.write_bytes(b"frozen display fixture")
                receipt = backup_project(source, destination)
                self.assertEqual(receipt["project_dir"], str(destination.resolve()))
                self.assertEqual(receipt["identity"]["files"][relative.as_posix()]["sha256"],
                                 hashlib.sha256(asset.read_bytes()).hexdigest())
                self.assertEqual((backups._filesystem_path(destination) / relative).read_bytes(), asset.read_bytes())
                with open_project(destination) as project:
                    self.assertEqual(len(project.dataset.item_ids), 2)
                deep = root / ("field-recordings-"*5) / "backup"
                self.assertGreater(len(str(deep / relative)), 260)
                try:
                    backup_project(source, deep)
                except DesktopProjectError as error:
                    self.assertEqual(error.code, "BACKUP_PATH_LENGTH")
                    self.assertIn("shorter", str(error))
                    self.assertFalse(deep.exists())
                else:
                    self.assertEqual((deep / relative).read_bytes(), asset.read_bytes())
                    with open_project(deep) as project:
                        self.assertEqual(len(project.dataset.item_ids), 2)
                failed = root / "interrupted-copy"
                with patch.object(backups, "_copy_file", side_effect=PermissionError("copy denied")):
                    with self.assertRaises(shutil.Error):
                        backup_project(source, failed)
                self.assertFalse(failed.exists())
                self.assertEqual(list(root.glob(".syllafolio-backup-*")), [])
                self.assertEqual(asset.read_bytes(), b"frozen display fixture")
