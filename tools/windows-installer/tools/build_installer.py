"""Build an offline, current-user Windows Setup.exe with the installed Framework C# compiler.

No downloads, Git, original-payload edits, or administrator access.
"""
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
from zipfile import ZipFile

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.1.0-lite1"
PAYLOAD_SHA = "8c27bab831ed36150f8bd8c94544106425910d71364f91c730d035429225eb24"
FOOTER_MAGIC = b"SYLLAFOLIO_SFX01"


def sha(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def constants(path, *, can_install, archive, launcher_sha, uninstall_sha):
    text = "namespace SyllafolioInstaller { internal static class Build {\n"
    values = {"Version": VERSION, "ZipSha": archive["sha256"], "ZipRoot": archive["root"],
              "IntegritySha": archive["integrity_sha256"], "LauncherSha": launcher_sha,
              "UninstallSha": uninstall_sha, "FooterMagic": FOOTER_MAGIC.decode("ascii")}
    for key, value in values.items():
        text += "internal const string %s = %s;\n" % (key, json.dumps(value))
    text += "internal const bool CanInstall = %s;\n" % str(can_install).lower()
    text += "internal const long ZipLength = %dL;\n" % archive["bytes"]
    text += "internal const long UnpackedBytes = %dL;\n" % archive["uncompressed_bytes"]
    text += "internal const int FileCount = %d;\n" % archive["file_count"]
    path.write_bytes((text + "} }\n").encode("utf-8"))


def inspect_payload(path, expected):
    actual = sha(path)
    if actual != expected:
        raise ValueError("Payload ZIP does not match the explicitly trusted SHA-256")
    with ZipFile(path) as archive:
        names = archive.namelist()
        roots = {name.split("/", 1)[0] for name in names}
        if len(roots) != 1 or len(names) != len(set(names)):
            raise ValueError("Expected one package root without duplicate ZIP entries")
        root = roots.pop()
        raw = archive.read(root + "/INTEGRITY.json")
        integrity = json.loads(raw)
        if set(names) != {root + "/" + name for name in [*integrity, "INTEGRITY.json"]}:
            raise ValueError("Payload ZIP and its integrity inventory differ")
        build = json.loads(archive.read(root + "/BUILD_INFO.json"))
        if build["version"] != VERSION or build.get("default_start") != "blank" or build.get("included_business_data") is not False:
            raise ValueError("Expected the audited blank, data-free Lite version")
        return {"sha256": actual, "bytes": path.stat().st_size, "root": root,
                "integrity_sha256": hashlib.sha256(raw).hexdigest(), "file_count": len(integrity),
                "uncompressed_bytes": sum(info.file_size for info in archive.infolist())}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--payload", required=True, type=Path)
    parser.add_argument("--expected-payload-sha256", default=PAYLOAD_SHA)
    parser.add_argument("--launcher", required=True, type=Path)
    parser.add_argument("--icon", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--work", required=True, type=Path, help="New retained build directory")
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--csc", type=Path, help="Framework C# compiler; defaults to the installed Windows Framework64 compiler")
    args = parser.parse_args()
    if os.name != "nt":
        raise RuntimeError("This build uses the Windows .NET Framework compiler")
    payload, launcher, icon = (p.resolve(strict=True) for p in (args.payload, args.launcher, args.icon))
    output, work = args.output.absolute(), args.work.absolute()
    for path in (output, work, args.receipt):
        if path is not None and (path.exists() or path.is_symlink()):
            raise FileExistsError("Refusing to overwrite an output: " + str(path))
    csc = (args.csc or Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe").resolve(strict=True)
    archive = inspect_payload(payload, args.expected_payload_sha256)
    launcher_sha = sha(launcher)
    source = ROOT / "installer/Installer.cs"
    manifest = ROOT / "installer/installer.manifest"
    source_before = {str(p.relative_to(ROOT)): sha(p) for p in (source, manifest, Path(__file__))}
    work.mkdir(parents=True, exist_ok=False)
    generated = work / "BuildConstants.cs"
    uninstall = work / "Uninstall.exe"
    setup = work / "SetupStub.exe"

    def compile_to(destination, resources):
        command = [str(csc), "/nologo", "/utf8output", "/target:winexe", "/platform:x64", "/optimize+", "/warn:4",
                   "/out:" + str(destination), "/win32manifest:" + str(manifest), "/win32icon:" + str(icon),
                   "/reference:System.Windows.Forms.dll", "/reference:System.Drawing.dll",
                   "/reference:System.Web.Extensions.dll", "/reference:System.IO.Compression.dll", "/reference:System.Core.dll"]
        command += ["/resource:" + str(path) + "," + name for path, name in resources]
        command += [str(source), str(generated)]
        child = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        (work / (destination.stem + ".compile.log")).write_bytes(child.stdout)
        if child.returncode:
            sys.stderr.buffer.write(child.stdout)
            raise RuntimeError("C# compiler failed: " + str(child.returncode))

    constants(generated, can_install=False, archive=archive, launcher_sha=launcher_sha, uninstall_sha="")
    compile_to(uninstall, [])
    uninstall_sha = sha(uninstall)
    constants(generated, can_install=True, archive=archive, launcher_sha=launcher_sha, uninstall_sha=uninstall_sha)
    compile_to(setup, [(launcher, "Syllafolio.Launcher.exe"), (uninstall, "Syllafolio.Uninstall.exe")])
    if any(sha(ROOT / name) != checksum for name, checksum in source_before.items()) or sha(payload) != archive["sha256"] or sha(launcher) != launcher_sha:
        raise RuntimeError("A build input changed during compilation")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("xb") as stream:
        with setup.open("rb") as source_stream:
            shutil.copyfileobj(source_stream, stream, 1024 * 1024)
        with payload.open("rb") as source_stream:
            shutil.copyfileobj(source_stream, stream, 1024 * 1024)
        if len(FOOTER_MAGIC) != 16:
            raise RuntimeError("The shared installer footer magic must be exactly 16 bytes")
        stream.write(FOOTER_MAGIC + struct.pack("<q", archive["bytes"]) + bytes.fromhex(archive["sha256"]))
    receipt = {"schema": "syllafolio-installer-build-v1", "status": "passed", "version": VERSION,
               "output": str(output), "bytes": output.stat().st_size, "sha256": sha(output),
               "payload": archive, "launcher_sha256": launcher_sha, "launcher_bytes": launcher.stat().st_size,
               "uninstaller_sha256": uninstall_sha, "uninstaller_bytes": uninstall.stat().st_size,
               "icon_sha256": sha(icon), "source_sha256": source_before,
               "compiler": str(csc), "compiler_sha256": sha(csc), "signature": "unsigned", "windows_installation_qa": "pending"}
    if args.receipt:
        args.receipt.parent.mkdir(parents=True, exist_ok=True)
        args.receipt.write_bytes((json.dumps(receipt, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
