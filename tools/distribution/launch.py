"""Portable entry: blank by default; open only an explicit user project."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import sys
sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "app/src"))


def main():
    if os.name == "nt" and not sys.flags.utf8_mode:
        raise RuntimeError("Use Start.cmd or Python -X utf8.")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blank", action="store_true", help="Open an empty workspace (the default)")
    parser.add_argument("--project", type=Path, help="Explicit existing desktop project")
    parser.add_argument("--smoke-output", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.blank and args.project:
        parser.error("Choose --blank or --project.")
    if args.smoke_output and args.project:
        parser.error("The startup self-check only opens a blank workspace.")
    from runtime_paths import configure_runtime
    config = configure_runtime(ROOT)
    print("Runtime paths ready. Diagnostics: " + config["diagnostics_dir"], flush=True)
    from PySide6.QtCore import QLocale, QSettings, QTimer, Qt
    from PySide6.QtWidgets import QApplication
    QLocale.setDefault(QLocale("en_US"))
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_DontUseNativeDialogs, True)
    application = QApplication.instance() or QApplication(sys.argv[:1])
    application.setApplicationName("Syllafolio")
    token = hashlib.sha256(str(ROOT.resolve()).encode("utf-8")).hexdigest()[:12]
    settings = Path(config["cache_dir"]).parent / "ui_settings" / token
    settings.mkdir(parents=True, exist_ok=True)
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(settings))
    from syllafolio import annotation_desktop as desktop
    if not Path(desktop.__file__).resolve().is_relative_to(ROOT / "app/src/syllafolio"):
        raise RuntimeError("Application source must come from this portable package.")
    build = json.loads((ROOT / "BUILD_INFO.json").read_text(encoding="utf-8"))
    window = desktop.MainWindow()
    application.setApplicationVersion(build["version"])
    window.setWindowTitle("Syllafolio " + build["version"])
    window.show()
    if args.project:
        window.open_project_path(args.project)
    if args.smoke_output:
        def finish():
            from prototype_reports import atomic_json
            try:
                assert window.project is None and not window._busy
                assert application.platformName() == "windows"
                report = {"schema": "syllafolio-blank-startup-v1", "status": "passed",
                    "version": build["version"], "qt_platform": application.platformName(),
                    "actual_dpr": window.devicePixelRatioF(), "project_opened": False,
                    "source_import": desktop.__file__, "python": sys.executable,
                    "workspace_menu": [a.text() for a in window.workspace_menu.actions() if not a.isSeparator()],
                    "audio_played": False, "human_audio_passed": None}
                atomic_json(args.smoke_output, report)
                window.close()
                application.exit(0)
            except Exception as error:
                atomic_json(args.smoke_output, {"schema": "syllafolio-blank-startup-v1", "version": build["version"],
                    "status": "failed", "error": str(error)})
                application.exit(1)
        QTimer.singleShot(800, finish)
    return application.exec()


if __name__ == "__main__":
    from installation_lease import InstallationLease
    with InstallationLease(ROOT, mode="shared"):
        raise SystemExit(main())
