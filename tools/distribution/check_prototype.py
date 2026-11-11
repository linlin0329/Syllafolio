"""Syllafolio package self-check: identities, imports and optional blank Windows startup; no sample data."""
from pathlib import Path
import argparse
import hashlib
import importlib
import importlib.metadata
import json
import os
import subprocess
import sys
import traceback
sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parent


def digest(path):
    value = str(path.absolute())
    if os.name == "nt" and not value.startswith("\\\\?\\"):
        value = "\\\\?\\UNC\\" + value[2:] if value.startswith("\\\\") else "\\\\?\\" + value
    result = hashlib.sha256()
    with open(value, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""): result.update(block)
    return result.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--static-only", action="store_true", help="Check identities/imports without opening the blank window")
    args = parser.parse_args()
    from runtime_paths import configure_runtime
    from prototype_reports import atomic_json, begin_run, new_run_id
    from installation_lease import validate_installation_protocol
    config = configure_runtime(ROOT, run_id=new_run_id("self_check"))
    folder = begin_run(ROOT, "self_check", config["run_id"], config)
    print("Syllafolio self-check report: " + str(folder), flush=True)
    report = {"schema": "syllafolio-self-check-v1", "status": "starting", "runtime_paths": config,
              "scope": "Package integrity/imports and blank startup only", "p1_suite_executed": False,
              "business_workflows_exercised": False, "audio_played": False, "human_audio_passed": None}
    code = 1
    try:
        build = json.loads((ROOT / "BUILD_INFO.json").read_text(encoding="utf-8"))
        expected = json.loads((ROOT / "INTEGRITY.json").read_text(encoding="utf-8"))
        validate_installation_protocol(ROOT, build)
        mismatches = [name for name, checksum in expected.items() if digest(ROOT / name) != checksum]
        if mismatches: raise RuntimeError("Package identity differs: " + ", ".join(mismatches[:10]))
        report.update(version=build["version"], integrity_files=len(expected), integrity_passed=True)
        if "RUNTIME_PROFILE.json" not in expected:
            raise RuntimeError("The runtime profile is missing from the package inventory.")
        report["runtime_profile"] = {"file": "RUNTIME_PROFILE.json", "sha256": digest(ROOT / "RUNTIME_PROFILE.json")}
        imported = {}
        for name in ("numpy", "scipy", "sklearn", "hdbscan", "umap", "soundfile", "PIL", "matplotlib",
                     "PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets", "PySide6.QtMultimedia",
                     "PySide6.QtNetwork", "PySide6.QtSvg", "PySide6.QtOpenGLWidgets", "PySide6.QtTest",
                     "syllafolio.annotation_desktop", "syllafolio.acoustics.config",
                     "syllafolio.acoustics.audio_processor", "syllafolio.acoustics.log_mel"):
            module = importlib.import_module(name); imported[name] = str(getattr(module, "__file__", "built-in"))
        for name, path in imported.items():
            if name.startswith("syllafolio."):
                assert Path(path).resolve().is_relative_to(ROOT / "app/src/syllafolio")
        registry = ROOT / "app/configs/annotation_manual/test_material_hashes.json"
        assert registry.is_file()
        report["imports"] = imported
        report["dependencies"] = {row["name"]: importlib.metadata.version(row["name"])
                                  for row in json.loads((ROOT / "DEPENDENCIES.json").read_text(encoding="utf-8"))}
        report["startup_smoke_executed"] = not args.static_only
        if not args.static_only:
            command = [sys.executable, "-B", "-X", "utf8", str(ROOT / "launch.py"), "--blank", "--smoke-output", str(folder / "blank_startup.json")]
            with (folder / "blank_startup.log").open("w", encoding="utf-8") as log:
                child = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
            report["startup_exit_code"] = child.returncode
            if child.returncode != 0: raise RuntimeError("Blank startup failed; read blank_startup.log")
            startup = json.loads((folder / "blank_startup.json").read_text(encoding="utf-8"))
            assert startup["schema"] == "syllafolio-blank-startup-v1"
            assert startup["version"] == build["version"]
            assert startup["status"] == "passed" and startup["project_opened"] is False
            report["blank_startup"] = startup
        report["status"] = "passed"; code = 0
    except Exception:
        report["status"] = "failed"; report["error"] = traceback.format_exc(); print(report["error"], file=sys.stderr)
    atomic_json(folder / "self_check.json", report)
    print("Syllafolio self-check " + report["status"] + ": " + str(folder / "self_check.json"), flush=True)
    return code


if __name__ == "__main__":
    from installation_lease import InstallationLease
    with InstallationLease(ROOT, mode="shared"):
        raise SystemExit(main())
