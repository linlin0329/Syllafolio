"""Verify the built EXE, exercise a fresh current-user installation and record results."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def digest(path):
    with path.open("rb") as stream:
        checksum = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(block)
        return checksum.hexdigest()


def check_payload(setup, archive, installer, portable):
    archive_hash = digest(archive)
    archive_size = archive.stat().st_size
    require(portable["status"] == installer["status"] == "passed", "Build did not pass")
    require(portable["sha256"] == installer["payload"]["sha256"] == archive_hash, "ZIP hash mismatch")
    require(portable["bytes"] == installer["payload"]["bytes"] == archive_size, "ZIP size mismatch")
    require(installer["sha256"] == digest(setup), "Setup hash mismatch")
    footer_size = 56
    with setup.open("rb") as executable:
        executable.seek(-footer_size, os.SEEK_END)
        magic, length, checksum = struct.unpack("<16sq32s", executable.read(footer_size))
        require((magic, length, checksum.hex()) == (b"SYLLAFOLIO_SFX01", archive_size, archive_hash),
                "Setup footer differs from the portable ZIP")
        executable.seek(-footer_size - length, os.SEEK_END)
        with archive.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                require(executable.read(len(block)) == block, "Setup payload differs from the portable ZIP")


def run_logged(args, log, **kwargs):
    with log.open("w", encoding="utf-8") as output:
        result = subprocess.run(args, stdout=output, stderr=subprocess.STDOUT, timeout=1200, **kwargs)
    if result.returncode:
        print(log.read_text(encoding="utf-8", errors="replace"), flush=True)
        raise RuntimeError(f"Command failed ({result.returncode}): {args[0]}; see {log.name}")


def exercise(release, evidence, work):
    import winreg

    work.mkdir(exist_ok=False)
    version = "1.0.1"
    archive = release / "Syllafolio-Windows-x64.zip"
    setup = release / f"Syllafolio-Setup-{version}-x64.exe"
    portable = read_json(evidence / "portable.json")
    installer = read_json(evidence / "installer.json")
    require(installer["version"] == portable["version"] == version, "Incorrect release version")
    check_payload(setup, archive, installer, portable)

    install_receipt = evidence / "install.json"
    uninstall_receipt = evidence / "uninstall.json"
    subprocess.run([setup, "--quiet", "--no-desktop-shortcut", "--target", work / "installation",
                    "--receipt", install_receipt], check=True, timeout=900)
    install = read_json(install_receipt)
    require(install["status"] == "passed" and install["version"] == version and
            install["payload_sha256"] == portable["sha256"], "Setup install receipt differs")
    app = Path(install["app_root"])
    manifest = read_json(app / "INSTALLATION.json")
    uninstaller = Path(manifest["cache_path"])
    require(digest(uninstaller) == installer["uninstaller_sha256"], "Installed uninstaller hash differs")
    python = app / "_runtime/python.exe"
    launcher = app / "Syllafolio.exe"
    require(digest(launcher) == installer["launcher_sha256"], "Installed launcher hash differs")
    build = read_json(app / "BUILD_INFO.json")
    require(build["version"] == version and build["built_on"] == "win32", "Wrong application build")
    registry_key = install["registry_key"].removeprefix("HKCU\\")
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, registry_key) as key:
        require(winreg.QueryValueEx(key, "DisplayVersion")[0] == version, "Wrong registered version")

    sentinel = app / "user-added-verification.txt"
    sentinel.write_text("preserve this user file", encoding="utf-8")
    env = dict(os.environ, SYLLAFOLIO_RUNTIME_ROOT=str(work / "runtime"), PYTHONUTF8="1")
    try:
        repo = Path(__file__).resolve().parents[1]
        require((app / "LICENSE").read_bytes() == (repo / "LICENSE").read_bytes(),
                "Installed application license differs from the repository LICENSE")
        log = evidence / "self-check.log"
        run_logged([python, "-B", "-X", "utf8", app / "check_prototype.py"], log, cwd=app, env=env)
        records = [line.removeprefix("Syllafolio self-check passed: ").strip()
                   for line in log.read_text(encoding="utf-8").splitlines()
                   if line.startswith("Syllafolio self-check passed: ")]
        require(len(records) == 1, "Failed to locate the current self-check receipt")
        report = Path(records[0])
        shutil.copyfile(report, evidence / "self-check.json")
        check = read_json(report)
        require(check["version"] == version and check["integrity_passed"] and
                check["blank_startup"]["qt_platform"] == "windows", "Installed self-check failed")

        blank = evidence / "launcher-startup.json"
        subprocess.run([launcher, "--blank", "--smoke-output", blank], env=env, check=True, timeout=300)
        startup = read_json(blank)
        require(startup["status"] == "passed" and startup["version"] == version and
                startup["qt_platform"] == "windows" and startup["project_opened"] is False,
                "Installed desktop launcher failed")
        require(Path(startup["python"]).samefile(python), "Launch used a different Python runtime")

        test_env = dict(env, NUMBA_CACHE_DIR=str(work / "numba"), MPLCONFIGDIR=str(work / "mpl"))
        run_logged([python, "-B", "-X", "utf8", "-m", "unittest", "discover", "-v", "-s", "tests"],
                   evidence / "source-tests.log", cwd=repo, env=test_env)
    finally:
        subprocess.run([uninstaller, "--quiet", "--uninstall", "--target", app, "--receipt", uninstall_receipt],
                       check=True, timeout=900)
        uninstall = read_json(uninstall_receipt)
        require(uninstall["status"] == "passed" and uninstall["registry_removed"] and
                not launcher.exists() and not python.exists() and
                sentinel.read_text(encoding="utf-8") == "preserve this user file", "Uninstall check failed")
    (release / "SHA256SUMS.txt").write_bytes(
        (f"{installer['sha256']}  {setup.name}\n{portable['sha256']}  {archive.name}\n").encode("ascii"))
    receipt = {"version": version, "status": "passed", "source_commit": os.environ.get("GITHUB_SHA"),
               "run_id": os.environ.get("GITHUB_RUN_ID"), "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
               "windows": sys.getwindowsversion().build, "setup_sha256": installer["sha256"],
               "portable_sha256": portable["sha256"], "embedded_payload_matched": True,
               "installed_package_check": "passed", "installed_gui_launcher": "passed",
               "checkout_tests": "see source-tests.log", "uninstall_preserved_user_file": True}
    (evidence / "windows-release.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("release", "evidence", "work"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    exercise(args.release.resolve(), args.evidence.resolve(), args.work.resolve())
