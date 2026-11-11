"""D4桌面文件边界：核对同owner注册包，原子保存与原样离线预览。"""
from __future__ import annotations

import copy
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import threading
from urllib.parse import unquote, urlsplit
import uuid
import zipfile


SCHEMA = "annotation-desktop-export-files-v1"
JOB = re.compile(r"catalogue-[a-f0-9]{20}\Z")
ASSET = re.compile(r"/api/media/(asset-[a-f0-9]{24})\Z")
SHA = re.compile(r"[a-f0-9]{64}\Z")
REQUIRED = {"catalogue.json", "report.html", "report.css", "audit.json", "README.md", "assessment_status.json",
            "tables/types.csv", "tables/items.csv", "tables/sources.csv", "tables/examples.csv"}


class ExportFileError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(f"{code}: {message}")


def _require(condition, code, message):
    if not condition: raise ExportFileError(code, message)


def _cancel(cancelled):
    _require(cancelled is None or not cancelled(), "CANCELLED", "File delivery was cancelled before publication; no partial package was published.")


def _progress(callback, stage, **fields):
    if callback is not None: callback({"stage": stage, **fields})


def _json(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            _require(key not in value, "EXPORT_JSON", "Duplicate JSON fields are not accepted.")
            value[key] = item
        return value
    def constant(_value): raise ExportFileError("EXPORT_JSON", "Nonfinite JSON values are not accepted.")
    try: return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError) as exc:
        if isinstance(exc, ExportFileError): raise
        raise ExportFileError("EXPORT_JSON", "The registered package contains invalid UTF-8 JSON.") from exc


def _relative(value):
    _require(isinstance(value, str) and bool(value) and "\\" not in value, "EXPORT_PATH", "Use canonical package-relative file names.")
    parsed = urlsplit(value)
    parts = value.split("/")
    _require(not (parsed.scheme or parsed.netloc or parsed.query or parsed.fragment) and not PurePosixPath(value).is_absolute()
             and all(part and part not in {".", ".."} and not part.endswith((".", " "))
                 and not any(ord(c) < 32 or c in '<>:"|?*' for c in part)
                 and part.split(".", 1)[0].upper() not in {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(1, 10)], *[f"LPT{i}" for i in range(1, 10)]}
                 for part in parts), "EXPORT_PATH", "External, ambiguous or escaping package paths are not accepted.")
    return value


def _no_links(path):
    path = Path(path)
    _require(path.is_absolute() and ".." not in path.parts and not str(path).startswith(("\\\\", "//")),
             "EXPORT_PATH", "Choose an absolute local path without parent traversal.")
    for current in (path, *path.parents):
        try: attributes = current.lstat()
        except FileNotFoundError: continue
        _require(not stat.S_ISLNK(attributes.st_mode) and not getattr(attributes, "st_file_attributes", 0) & 0x400,
                 "EXPORT_PATH", "Symbolic links, junctions and other reparse points are not accepted.")
    return path.resolve()


def _digest(stream, *, expected_size=None, cancelled=None, output=None):
    digest = hashlib.sha256(); length = 0
    while True:
        _cancel(cancelled); block = stream.read(1024 * 1024)
        if not block: break
        length += len(block)
        _require(expected_size is None or length <= expected_size, "EXPORT_LENGTH", "Package bytes exceed the recorded length.")
        digest.update(block)
        if output is not None: output.write(block)
    _require(expected_size is None or length == expected_size, "EXPORT_LENGTH", "Package length differs from its manifest.")
    return digest.hexdigest(), length


def _file_hash(path, *, expected_size=None, cancelled=None):
    _no_links(path)
    with Path(path).open("rb") as stream: return _digest(stream, expected_size=expected_size, cancelled=cancelled)


class _LocalHTML(HTMLParser):
    def __init__(self, names):
        super().__init__(convert_charrefs=True); self.names = names; self.references = set()

    def handle_starttag(self, tag, attrs):
        _require(tag not in {"script", "style", "iframe", "frame", "frameset", "object", "embed", "applet", "base", "form", "svg", "math"},
                 "EXPORT_HTML", "Offline reports cannot execute active content or embed external pages.")
        for key, value in attrs:
            _require(not key.lower().startswith("on") and key.lower() not in {"srcset", "action", "formaction", "srcdoc", "style", "background", "poster", "ping", "xlink:href"},
                     "EXPORT_HTML", "Active or alternate resource attributes are not accepted.")
            if tag == "meta" and key.lower() == "http-equiv":
                _require(str(value).lower() != "refresh", "EXPORT_HTML", "Offline report redirects are not accepted.")
            if key not in {"href", "src"}: continue
            if isinstance(value, str) and value.startswith("#"): continue
            name = _relative(unquote(value or ""))
            _require(name in self.names, "EXPORT_HTML", "A report resource is not present in the verified package: " + name)
            self.references.add(name)

    handle_startendtag = handle_starttag


class ExporterAdapter:
    """输入只接受当前项目job ID；不会把URL或任意文件路径当源资产。"""
    def __init__(self, project):
        self.project, self.service = project, project.service
        self.project_id, self.run_id = project.metadata["project_id"], project.service.run_id
        self._lock = threading.RLock()

    def _job(self, job_id):
        _require(isinstance(job_id, str) and JOB.fullmatch(job_id), "EXPORT_IDENTITY", "Select a registered export job in this project.")
        _require(self.project.service is self.service and self.service.run_id == self.run_id
                 and self.project.metadata["project_id"] == self.project_id, "EXPORT_OWNER", "The export file owner changed; reopen this project workspace.")
        with self.service._lock:
            state = self.service.store.snapshot(self.run_id)
            job = state.get("catalogue_exports", {}).get("jobs", {}).get(job_id)
            _require(job is not None and job.get("job_id") == job_id, "EXPORT_NOT_FOUND", "This project has no such export job.")
            _require(job.get("status") == "completed", "EXPORT_NOT_READY", "Only a completed export has readable package assets; failed or cancelled jobs cannot borrow an older package.")
            projected = next((row for row in self.service.catalogue_export.snapshot(state)["jobs"] if row["job_id"] == job_id), None)
            _require(projected is not None, "EXPORT_IDENTITY", "The export history does not contain this selected job.")
            return copy.deepcopy(job), copy.deepcopy(projected), copy.deepcopy(self.service.assets), self.service.record_metadata()

    def _verify(self, job_id, *, cancelled=None, progress=None):
        _cancel(cancelled); job, projected, registry, identity = self._job(job_id)
        mapping = job.get("asset_map")
        _require(isinstance(mapping, dict) and {"manifest.json", "catalogue.zip"} <= set(mapping), "EXPORT_ASSETS", "The completed job has no complete registered package index.")
        paths = {}; root = self.service.work_dir.resolve() / "catalogue_exports" / job_id
        _no_links(root)
        for relative, url in mapping.items():
            _relative(relative); matched = ASSET.fullmatch(url) if isinstance(url, str) else None
            _require(matched is not None, "EXPORT_ASSET_SCOPE", "Only this job's exact registered asset handles are accepted.")
            row = registry.get(matched[1]); expected = root / relative
            _require(isinstance(row, dict) and isinstance(row.get("path"), str)
                     and row["path"].replace("\\", "/") == "catalogue_exports/" + job_id + "/" + relative,
                     "EXPORT_ASSET_SCOPE", "An export asset belongs to another path or project.")
            _no_links(expected)
            path = self.project.media_path(url)
            _require(path.resolve() == expected.resolve(), "EXPORT_ASSET_SCOPE", "The registered export asset escaped its own version directory.")
            paths[relative] = path
        _require(job.get("bundle_url") == mapping["catalogue.zip"] and job.get("manifest_url") == mapping["manifest.json"],
                 "EXPORT_ASSETS", "The selected package handles differ from its frozen registration.")
        raw_manifest = paths["manifest.json"].read_bytes()
        _require(hashlib.sha256(raw_manifest).hexdigest() == job.get("manifest_sha256"), "EXPORT_CHANGED", "The manifest differs from the completed job receipt.")
        manifest = _json(raw_manifest)
        _require(isinstance(manifest, dict) and manifest.get("schema_version") == "annotation-catalogue-package-manifest-v1"
                 and manifest.get("manifest_self_excluded") is True and manifest.get("export_id") == job_id
                 and manifest.get("project_identity") == identity == job.get("document", {}).get("project_identity")
                 and manifest.get("snapshot_fingerprint") == job.get("snapshot_fingerprint")
                 # 历史worker更新把顶层mode覆为项目合同mode；原冻结document及完成metadata仍保留实际导出模式。
                 and manifest.get("mode") in {"draft", "complete"}
                 and manifest.get("mode") == job.get("document", {}).get("mode") == job.get("metadata", {}).get("mode")
                 and manifest.get("assessment_job_id") == job.get("assessment_job_id"),
                 "EXPORT_IDENTITY", "The manifest does not match this run, contract and selected frozen export.")
        entries = manifest.get("files")
        _require(isinstance(entries, list), "EXPORT_MANIFEST", "The package manifest needs an explicit file list.")
        files = {}; folded = set()
        for row in entries:
            _require(isinstance(row, dict) and set(row) == {"path", "size", "sha256"}, "EXPORT_MANIFEST", "Manifest entries must contain exact path, length and SHA-256 fields.")
            name = _relative(row["path"])
            _require(name.casefold() not in folded and name.casefold() not in {"manifest.json", "catalogue.zip"}
                     and type(row["size"]) is int and row["size"] >= 0 and isinstance(row["sha256"], str) and SHA.fullmatch(row["sha256"]),
                     "EXPORT_MANIFEST", "Duplicate, reserved or malformed package entries are not accepted.")
            folded.add(name.casefold()); files[name] = row
        names = set(files) | {"manifest.json"}
        _require(REQUIRED <= set(files) and set(paths) == names | {"catalogue.zip"}, "EXPORT_CLOSURE", "The registered package and manifest file sets are not closed.")
        metadata = job.get("metadata", {})
        _require(metadata.get("file_count") == len(names) and metadata.get("media_count") == manifest.get("media_count"), "EXPORT_CLOSURE", "Package file counts differ from the completed receipt.")
        for number, (name, row) in enumerate(files.items(), 1):
            sha, _size = _file_hash(paths[name], expected_size=row["size"], cancelled=cancelled)
            _require(sha == row["sha256"], "EXPORT_CHANGED", "A registered file differs from its manifest: " + name)
            _progress(progress, "verify", completed=number, total=len(files), message="Verifying the selected registered export files.")
        digest, bundle_bytes = _file_hash(paths["catalogue.zip"], expected_size=metadata.get("bundle_bytes"), cancelled=cancelled)
        _require(digest == job.get("bundle_sha256"), "EXPORT_CHANGED", "The ZIP differs from the selected completed export receipt.")
        try:
            with zipfile.ZipFile(paths["catalogue.zip"]) as archive:
                members = archive.infolist()
                _require(len(members) == len(names) and {row.filename for row in members} == names,
                         "EXPORT_ZIP_CLOSURE", "The ZIP contains missing, additional or duplicate members.")
                for member in members:
                    _relative(member.filename); mode = member.external_attr >> 16
                    _require(not member.is_dir() and not member.flag_bits & 1 and stat.S_IFMT(mode) in {0, stat.S_IFREG},
                             "EXPORT_ZIP_PATH", "ZIP directories, links, encrypted or nonregular members are not accepted.")
                    expected = files.get(member.filename, {"size": len(raw_manifest), "sha256": job["manifest_sha256"]})
                    _require(member.file_size == expected["size"], "EXPORT_LENGTH", "A ZIP entry length differs from the manifest.")
                    with archive.open(member) as source: digest, _size = _digest(source, expected_size=expected["size"], cancelled=cancelled)
                    _require(digest == expected["sha256"], "EXPORT_CHANGED", "A ZIP entry differs from the registered file: " + member.filename)
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
            raise ExportFileError("EXPORT_ZIP", "The registered ZIP failed CRC or format verification.") from exc
        document = _json(paths["catalogue.json"].read_bytes()); frozen = copy.deepcopy(job["document"])
        _require(isinstance(document, dict) and isinstance(document.get("media"), list), "EXPORT_DOCUMENT", "The package catalogue is not a complete frozen document.")
        frozen["media"] = document["media"]
        _require(document == frozen and document.get("versions", {}).get("input_fingerprint") == self.project.dataset.input_fingerprint
                 and manifest.get("counts") == document.get("counts") == metadata.get("counts")
                 and len(document["media"]) == manifest["media_count"], "EXPORT_IDENTITY", "The catalogue differs from the selected frozen source document.")
        parser = _LocalHTML(names)
        try: parser.feed(paths["report.html"].read_text(encoding="utf-8")); parser.close()
        except UnicodeError as exc: raise ExportFileError("EXPORT_HTML", "The offline report is not valid UTF-8.") from exc
        _require("report.css" in parser.references, "EXPORT_HTML", "The offline report does not link its local style sheet.")
        css = paths["report.css"].read_text(encoding="utf-8")
        _require(not re.search(r"@import|url\s*\(|expression\s*\(", css, re.IGNORECASE), "EXPORT_HTML", "Offline styles cannot import resources or active content.")
        for media in document["media"]:
            _require(isinstance(media, dict), "EXPORT_MEDIA", "The catalogue contains an invalid media example.")
            for path_field, sha_field in (("wav_path", "wav_sha256"), ("spectrogram_path", "spectrogram_sha256")):
                name = _relative(media.get(path_field))
                _require(name in files and name in parser.references and files[name]["sha256"] == media.get(sha_field),
                         "EXPORT_MEDIA", "A declared example is missing its exact local audio or image bytes.")
        summary = {"schema": SCHEMA, "job_id": job_id, "project_id": self.project_id, "run_id": self.run_id,
            "mode": manifest["mode"], "snapshot_fingerprint": job["snapshot_fingerprint"],
            "manifest_sha256": job["manifest_sha256"], "bundle_sha256": job["bundle_sha256"],
            "stale_for_current": projected["stale_for_current"], "stale_reasons": projected["stale_reasons"],
            "counts": copy.deepcopy(manifest["counts"]), "media_count": manifest["media_count"], "file_count": len(names),
            "bundle_bytes": bundle_bytes, "assessment_job_id": job["assessment_job_id"], "verified": True}
        _cancel(cancelled)
        return summary, paths, files, raw_manifest

    def inspect(self, job_id, *, cancelled=None, progress=None):
        return self._verify(job_id, cancelled=cancelled, progress=progress)[0]

    @staticmethod
    def _cleanup(staging):
        if staging.exists():
            _no_links(staging); shutil.rmtree(staging)

    @staticmethod
    def _publish(staging, destination, cancelled):
        _cancel(cancelled); _no_links(staging); _no_links(destination)
        _require(not destination.exists(), "EXPORT_DESTINATION_EXISTS", "The delivery destination already exists; select a new version directory.")
        os.rename(staging, destination)

    def save_package(self, job_id, destination_dir, *, cancelled=None, progress=None):
        with self._lock:
            destination = _no_links(Path(destination_dir))
            _require(destination.parent.is_dir() and not destination.exists(), "EXPORT_DESTINATION_EXISTS", "Choose a new directory in an existing local folder; existing deliveries are immutable.")
            protected = [self.project.project_dir.resolve(), self.service.work_dir.resolve(), self.service.dataset_dir.resolve()]
            _require(not any(destination.is_relative_to(root) for root in protected), "EXPORT_DESTINATION_SCOPE", "Save delivery outside the project, frozen inputs and registered asset directory.")
            summary, paths, _files, raw_manifest = self._verify(job_id, cancelled=cancelled, progress=progress)
            staging = destination.parent / (".d4-save-" + uuid.uuid4().hex + ".partial")
            try:
                staging.mkdir()
                for name, expected, size in (("catalogue.zip", summary["bundle_sha256"], summary["bundle_bytes"]),
                                             ("manifest.json", summary["manifest_sha256"], len(raw_manifest))):
                    _cancel(cancelled)
                    with paths[name].open("rb") as source, (staging / name).open("xb") as output:
                        actual, _length = _digest(source, expected_size=size, cancelled=cancelled, output=output)
                        output.flush(); os.fsync(output.fileno())
                    _require(actual == expected, "EXPORT_CHANGED", "A source asset changed while saving; no delivery was published.")
                    _progress(progress, "copy", path=name, message="Saving exact verified ZIP and manifest bytes.")
                self._publish(staging, destination, cancelled)
            finally: self._cleanup(staging)
            return {**summary, "root": str(destination), "zip_path": str(destination / "catalogue.zip"), "manifest_path": str(destination / "manifest.json")}

    @staticmethod
    def _verify_tree(root, files, raw_manifest, cancelled):
        expected = {**files, "manifest.json": {"size": len(raw_manifest), "sha256": hashlib.sha256(raw_manifest).hexdigest()}}
        found = set()
        for parent, dirs, names in os.walk(root, followlinks=False):
            for name in dirs: _no_links(Path(parent) / name)
            for name in names:
                path = Path(parent) / name; relative = path.relative_to(root).as_posix(); found.add(relative)
                _require(relative in expected, "EXPORT_PREVIEW_CHANGED", "The existing offline preview contains an unregistered file.")
                row = expected[relative]; actual, _size = _file_hash(path, expected_size=row["size"], cancelled=cancelled)
                _require(actual == row["sha256"], "EXPORT_PREVIEW_CHANGED", "The existing offline preview was changed; its bytes are retained and cannot be overwritten.")
        _require(found == set(expected), "EXPORT_PREVIEW_CHANGED", "The offline preview is incomplete; existing bytes are retained.")

    def prepare_preview(self, job_id, *, cancelled=None, progress=None):
        with self._lock:
            summary, paths, files, raw_manifest = self._verify(job_id, cancelled=cancelled, progress=progress)
            cache = _no_links(self.project.project_dir / "desktop_export_previews")
            cache.mkdir(exist_ok=True)
            destination = _no_links(cache / (job_id + "-" + summary["bundle_sha256"][:12]))
            reused = destination.exists()
            if reused:
                self._verify_tree(destination, files, raw_manifest, cancelled)
                _cancel(cancelled)
            else:
                staging = cache / (".d4-preview-" + uuid.uuid4().hex + ".partial")
                try:
                    staging.mkdir()
                    with zipfile.ZipFile(paths["catalogue.zip"]) as archive:
                        for name, row in {**files, "manifest.json": {"size": len(raw_manifest), "sha256": summary["manifest_sha256"]}}.items():
                            _cancel(cancelled); path = staging / _relative(name); path.parent.mkdir(parents=True, exist_ok=True)
                            with archive.open(name) as source, path.open("xb") as output:
                                actual, _size = _digest(source, expected_size=row["size"], cancelled=cancelled, output=output)
                            _require(actual == row["sha256"], "EXPORT_CHANGED", "ZIP bytes changed while extracting; no preview was published.")
                            _progress(progress, "extract", path=name, message="Preparing the complete original offline report and local media.")
                    self._verify_tree(staging, files, raw_manifest, cancelled)
                    self._publish(staging, destination, cancelled)
                finally: self._cleanup(staging)
            html = destination / "report.html"
            return {**summary, "root": str(destination), "html_path": str(html), "file_url": html.as_uri(), "reused": reused}
