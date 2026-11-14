"""Build a data-free Windows release from this repository and a locked binary runtime.

Standard library only. No pip, downloads, Git, or application data are used.
"""
from __future__ import annotations

import argparse
from contextlib import nullcontext
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import sys
import tempfile
from zipfile import ZIP_DEFLATED, ZipFile

sys.dont_write_bytecode = True

VERSION = "1.0.0"
TOOLS = Path(__file__).resolve().parent
RUNTIME_PREFIXES = ("_runtime/", "site-packages/")
APP_FOLDERS = ("src/syllafolio", "configs/annotation_assistant",
               "configs/annotation_manual")


def io_path(path):
    value = str(Path(path).absolute())
    if os.name == "nt" and not value.startswith("\\\\?\\"):
        value = "\\\\?\\UNC\\" + value[2:] if value.startswith("\\\\") else "\\\\?\\" + value
    return Path(value)


def digest(path):
    value = hashlib.sha256()
    with io_path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def json_read(path):
    return json.loads(io_path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    io_path(path).parent.mkdir(parents=True, exist_ok=True)
    io_path(path).write_bytes((json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def relative_name(value):
    pure = PurePosixPath(value)
    if not value or "\\" in value or pure.is_absolute() or any(part in {"", ".", ".."} or ":" in part for part in pure.parts):
        raise ValueError("Unsafe package member: " + value)
    return pure.as_posix()


def regular_file(path, root):
    """Reject linked/reparse inputs and traversal, including linked parent folders."""
    path, root = Path(path), Path(root)
    if not path.absolute().is_relative_to(root.absolute()):
        raise ValueError("Input is outside its declared root: " + str(path))
    current = path
    while True:
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Linked/reparse build inputs are refused: " + str(current))
        if current == root:
            break
        current = current.parent
    if not path.is_file():
        raise ValueError("Expected a regular file: " + str(path))


def load_protocol(repository):
    path = repository / "src/syllafolio/annotation_desktop_installation.py"
    regular_file(path, repository)
    spec = importlib.util.spec_from_file_location("_syllafolio_build_lease", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RuntimeSeed:
    """Expose only members named by the committed runtime lock, never old app/metadata."""
    def __init__(self, path):
        self.path = Path(path).resolve(strict=True)
        self.archive = None
        self.members = {}
        if self.path.is_file():
            self.archive = ZipFile(self.path)
            infos = self.archive.infolist()
            names = [row.filename for row in infos]
            if len(names) != len(set(names)):
                raise ValueError("Duplicate ZIP entries are refused")
            candidates = [name[:-len("_runtime/python.exe")] for name in names if name.endswith("_runtime/python.exe")]
            if len(candidates) != 1:
                raise ValueError("Runtime ZIP must contain exactly one _runtime/python.exe")
            prefix = candidates[0]
            for info in infos:
                name = info.filename
                if name.startswith(prefix):
                    relative = name[len(prefix):]
                    if relative.startswith(RUNTIME_PREFIXES) and not info.is_dir():
                        relative_name(relative)
                        mode = info.external_attr >> 16
                        if stat.S_ISLNK(mode):
                            raise ValueError("Linked runtime ZIP member is refused: " + name)
                        self.members[relative] = info
        elif not self.path.is_dir():
            raise ValueError("Runtime seed must be an extracted runtime root or ZIP")

    def close(self):
        if self.archive:
            self.archive.close()

    def stream(self, name):
        relative_name(name)
        if not name.startswith(RUNTIME_PREFIXES):
            raise ValueError("Only binary runtime files may come from the seed")
        if self.archive:
            return self.archive.open(self.members[name])
        path = self.path / name
        regular_file(path, self.path)
        return io_path(path).open("rb")

    def copy_verified(self, name, expected, destination=None):
        value = hashlib.sha256()
        target = None
        if destination is not None:
            io_path(destination).parent.mkdir(parents=True, exist_ok=True)
            target = io_path(destination).open("xb")
        try:
            with self.stream(name) as source:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    value.update(block)
                    if target:
                        target.write(block)
        finally:
            if target:
                target.close()
        if value.hexdigest() != expected:
            raise ValueError("Runtime seed hash differs from the committed lock: " + name)


def application_inputs(repository):
    result = {}
    for folder in APP_FOLDERS:
        base = repository / folder
        if not base.is_dir():
            raise ValueError("Missing repository runtime folder: " + folder)
        suffixes = {".py"} if folder.startswith("src/") else {".json", ".md"}
        for path in sorted(base.rglob("*")):
            if path.is_file() and path.suffix.lower() in suffixes:
                relative = path.relative_to(repository).as_posix()
                regular_file(path, repository)
                if any(part in {"__pycache__", "examples", "fixtures", "test_data", "materials", "results"} for part in path.relative_to(base).parts):
                    raise ValueError("Non-runtime content is not allowed in the source whitelist: " + relative)
                if path.name in {"protocol_source_map.json"} or path.name.startswith("demo_"):
                    raise ValueError("Private/source-excerpt or demo config must be removed before building: " + relative)
                result[relative] = digest(path)
    for required in ("src/syllafolio/annotation_desktop.py", "src/syllafolio/acoustics/__init__.py",
                     "src/syllafolio/acoustics/config.py", "src/syllafolio/acoustics/audio_processor.py",
                     "src/syllafolio/acoustics/log_mel.py", "configs/annotation_manual/test_material_hashes.json",
                     "configs/annotation_assistant/p1/preset.json"):
        if required not in result:
            raise ValueError("Missing required runtime input: " + required)
    return result


def assemble(repository, seed, destination, runtime_lock, dependencies, inputs, protocol):
    published = {}

    def copy(source, name, expected=None):
        relative_name(name)
        path = destination / name
        io_path(path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(io_path(source), io_path(path))
        actual = digest(path)
        if expected is not None and actual != expected:
            raise ValueError("Source changed during build: " + name)
        published[name] = actual

    def data(name, value):
        write_json(destination / name, value)
        published[name] = digest(destination / name)

    for index, (name, checksum) in enumerate(sorted(runtime_lock["files"].items()), 1):
        seed.copy_verified(name, checksum, destination / name)
        published[name] = checksum
        if index % 2000 == 0:
            print(json.dumps({"stage": "runtime_copy", "files": index}), flush=True)
    # The sole runtime configuration rewrite adapts the locked interpreter to the public source layout.
    pth = "python312.zip\n.\n../site-packages\n../app/src\n../app\n..\nimport site\n"
    io_path(destination / "_runtime/python312._pth").write_bytes(pth.encode("utf-8"))
    published["_runtime/python312._pth"] = digest(destination / "_runtime/python312._pth")
    for name, checksum in inputs.items():
        copy(repository / name, "app/" + name, checksum)
    for source, target in (("annotation_desktop_installation.py", "installation_lease.py"),
                           ("annotation_desktop_runtime.py", "runtime_paths.py"),
                           ("annotation_desktop_reports.py", "prototype_reports.py")):
        copy(repository / "src/syllafolio" / source, target)
    for source in sorted((TOOLS / "distribution").iterdir()):
        if source.is_file():
            regular_file(source, TOOLS)
            copy(source, source.name)
    for name in ("Start.cmd", "Run_Checks.cmd", "Record_Feedback.cmd"):
        path = destination / name
        text = io_path(path).read_text("utf-8").replace("\r\n", "\n")
        io_path(path).write_bytes(text.replace("\n", "\r\n").encode("utf-8"))
        published[name] = digest(path)
    io_path(destination / protocol.LOCK_FILE).touch(exist_ok=False)
    published[protocol.LOCK_FILE] = digest(destination / protocol.LOCK_FILE)
    for folder in ("reports", "projects", "cache"):
        io_path(destination / folder).mkdir()
    data("DEPENDENCIES.json", dependencies)
    data("APP_SOURCE_MANIFEST.json", inputs)
    data("RUNTIME_PROFILE.json", {
        "schema": "syllafolio-runtime-profile-v1", "runtime_lock_sha256": digest(TOOLS / "runtime-lock.json"),
        "runtime_files": len(runtime_lock["files"]), "runtime_policy": runtime_lock["policy"],
        "runtime_configuration_rewrite": "_runtime/python312._pth; app/src and app relative paths only",
        "application_materials": False, "included_user_projects": False, "included_acceptance_outputs": False,
        "application_source": "This repository's src/syllafolio and configs whitelist only; no seed application files."})
    build = {"schema": "syllafolio-windows-prototype-build-v1", "version": VERSION, "target": "Windows x64",
        "python_version": runtime_lock["python_version"], "edition": "Desktop", "default_start": "blank",
        "included_business_data": False, "built_on": sys.platform, "windows_execution_verified": False,
        "manual_windows_acceptance": "pending; separate receipts required", "source_sha256": inputs,
        "runtime_reuse": {"type": "hash-locked external binary runtime", "dependency_count": len(dependencies),
                          "runtime_lock_sha256": digest(TOOLS / "runtime-lock.json"), "pip_or_download_performed": False},
        "installation_lease": {"schema": protocol.PROTOCOL, "lock_file": protocol.LOCK_FILE,
                               "bootstrap_sha256": {name: digest(destination / name) for name in protocol.BOOTSTRAPS}}}
    data("BUILD_INFO.json", build)
    if application_inputs(repository) != inputs:
        raise ValueError("Repository runtime inputs changed during assembly")
    write_json(destination / "INTEGRITY.json", dict(sorted(published.items())))
    protocol.validate_installation_protocol(destination, build)
    return published


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=TOOLS.parent)
    parser.add_argument("--runtime-seed", required=True, type=Path, help="External extracted runtime/package root or ZIP; selected files must match runtime-lock.json")
    parser.add_argument("--output", type=Path, help="New release ZIP path; existing ZIP is never overwritten")
    parser.add_argument("--build-dir", type=Path, help="Optional new retained assembly directory; existing directory is refused")
    parser.add_argument("--receipt", type=Path, help="Optional new build receipt outside the release ZIP")
    parser.add_argument("--check-runtime-only", action="store_true", help="Verify the external seed against all committed runtime hashes; do not build")
    args = parser.parse_args()
    repository = args.repository.resolve(strict=True)
    protocol = load_protocol(repository)
    lock = json_read(TOOLS / "runtime-lock.json")
    dependencies = json_read(TOOLS / "dependencies.lock.json")
    if lock.get("schema") != "syllafolio-binary-runtime-lock-v1" or len(dependencies) != lock["dependency_count"]:
        raise ValueError("Unexpected committed dependency/runtime lock")
    for name in lock["files"]:
        relative_name(name)
        if not name.startswith(RUNTIME_PREFIXES):
            raise ValueError("Runtime lock contains a non-runtime member")
    if not args.check_runtime_only and not args.output:
        parser.error("--output is required for an actual build")
    output = args.output.absolute() if args.output else None
    for path in (output, args.build_dir, args.receipt):
        if path is not None and path.exists():
            raise FileExistsError("Refusing to overwrite an existing output: " + str(path))
    if output and output.suffix.lower() != ".zip":
        raise ValueError("--output must name a .zip file")
    inputs = application_inputs(repository)
    seed = RuntimeSeed(args.runtime_seed)
    # A package root participates in the public shared lease. A standalone runtime root has no app/lease.
    lease = protocol.InstallationLease(seed.path, mode="shared") if seed.path.is_dir() and (seed.path / protocol.LOCK_FILE).exists() else nullcontext()
    try:
        with lease:
            if args.check_runtime_only:
                for name, checksum in lock["files"].items():
                    seed.copy_verified(name, checksum)
                print(json.dumps({"status": "passed", "scope": "runtime hash lock only", "files": len(lock["files"])}))
                return 0
            output.parent.mkdir(parents=True, exist_ok=True)
            workspace = nullcontext(None) if args.build_dir else tempfile.TemporaryDirectory(prefix="syl-")
            with workspace as temporary:
                destination = args.build_dir.absolute() if args.build_dir else Path(temporary) / output.stem
                io_path(destination).mkdir(parents=True, exist_ok=False)
                published = assemble(repository, seed, destination, lock, dependencies, inputs, protocol)
                with ZipFile(output, "x", ZIP_DEFLATED, compresslevel=6) as archive:
                    for name in sorted([*published, "INTEGRITY.json"]):
                        archive.write(io_path(destination / name), output.stem + "/" + name)
                with ZipFile(output) as archive:
                    failed = archive.testzip()
                    if failed:
                        raise ValueError("ZIP CRC differs: " + failed)
                receipt = {"schema": "syllafolio-windows-build-v1", "status": "passed", "version": VERSION,
                    "archive": str(output), "bytes": output.stat().st_size, "sha256": digest(output),
                    "uncompressed_bytes": sum(io_path(destination / name).stat().st_size for name in [*published, "INTEGRITY.json"]),
                    "integrity_files": len(published), "zip_crc_passed": True, "source_sha256": inputs,
                    "runtime_lock_sha256": digest(TOOLS / "runtime-lock.json"), "builder_sha256": digest(__file__),
                    "windows_execution_verified": False, "business_data_included": False,
                    "build_dir": str(destination) if args.build_dir else None}
                if args.receipt:
                    write_json(args.receipt, receipt)
                print(json.dumps(receipt, ensure_ascii=False, indent=2), flush=True)
    finally:
        seed.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
