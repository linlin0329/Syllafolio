"""Export a selected, registered internal assessment as a portable report."""
from __future__ import annotations

import csv
from datetime import datetime, timezone
import hashlib
import html
import io
import json
import os
from pathlib import Path
import tempfile
import zipfile

from . import __version__
from .annotation_analysis_metadata import report_analysis
from .annotation_catalogue_report import (
    CSS, _assessment_html, _csv_value, assessment_family_names, assessment_tables,
    localize_system_document,
)
from .annotation_desktop_export_files import _no_links


def _json(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _csv(fields, rows):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({field: _csv_value(row.get(field)) for field in fields})
    return stream.getvalue().encode("utf-8-sig")


def _html_report(report, assessment, table_paths):
    frozen = report["frozen_snapshot"]
    scope = frozen["scope"] or {}
    e = lambda value: html.escape(str(value), quote=True)
    links = " · ".join(f'<a href="{path}">{path.split("assessment_")[-1][:-4].replace("_", " ").capitalize()}</a>'
                       for path in table_paths)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Internal assessment · {e(report['job_id'])}</title><link rel="stylesheet" href="report.css"></head>
<body><main><header><h1>Internal assessment</h1>
<p>{e(scope.get('purpose') or 'Family-level acoustic assessment')}</p>
<p class="meta">Project {e(report['project_identity']['project_id'])} · Scope {e(scope.get('scope_id', 'unknown'))}
 · Catalogue revision {e(frozen['catalogue_revision'])} · Source version {e(frozen['source_version'])}<br>
Report created {e(report['created_at'])}</p>
</header>{_assessment_html({"assessment": assessment}, {}, standalone=True)}
<section><h2>Analysis files</h2><p>{links}</p>
<p>One prediction is a held-out prediction for one syllable in one repeat. Stable family IDs link the tables. Display names use the matching definition cards.</p>
<p><a href="assessment.json">Original assessment JSON</a> · <a href="manifest.json">File sizes and SHA-256</a></p></section>
</main></body></html>""".encode("utf-8")


def _destination_identity(path):
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    if not path.is_file():
        raise ValueError("Choose a ZIP file, not a directory.")
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


def export_assessment(controller, job_id, destination, *, overwrite=False):
    """Write one offline ZIP from verified saved data; run from the desktop worker."""
    project = controller.project
    path = _no_links(Path(destination))
    if path.suffix.lower() != ".zip" or not path.parent.is_dir():
        raise ValueError("Choose a ZIP file in an existing folder.")
    if path.is_relative_to(project.project_dir.resolve()):
        raise ValueError("Save the report outside the open project.")
    original = _destination_identity(path)
    if original is not None and not overwrite:
        raise FileExistsError("Confirm replacing the selected report ZIP.")

    with project.service._lock:
        raw = controller.report_bytes(job_id)
        document = json.loads(raw)
        job = controller.job(job_id)
        state = project.service.store.snapshot(project.service.run_id)
        names = assessment_family_names(state, document)
    digest = hashlib.sha256(raw).hexdigest()
    result = document["result"]
    readable = {**result, "audit": {"packages": result.get("audit", {}).get("packages", {})}}
    assessment = {
        "job_id": job_id, "status": document["result"]["status"],
        "stale_for_current": bool(job.get("stale_for_current")), "stale_reasons": job.get("stale_reasons", []),
        "snapshot_fingerprint": document["snapshot_fingerprint"], "report_sha256": digest,
        "family_display_names": names, "report_path": "assessment.json",
        "acoustic_analysis": report_analysis(document["frozen_snapshot"]),
        "report": readable,
    }
    if "audio_preprocessing" in document["frozen_snapshot"]:
        assessment["audio_preprocessing"] = document["frozen_snapshot"]["audio_preprocessing"]
    # Match the D4 presentation of legacy system messages; the original JSON stays byte-exact.
    assessment = localize_system_document({"assessment": assessment})["assessment"]
    manifest = {
        "schema_version": "syllafolio-assessment-package-v1", "exported_at": datetime.now(timezone.utc).isoformat(),
        "exporter_version": __version__, "project_identity": document["project_identity"],
        **{key: assessment[key] for key in ("job_id", "status", "stale_for_current", "stale_reasons",
                                             "snapshot_fingerprint", "report_sha256")},
        "family_display_names": names, "acoustic_analysis": assessment["acoustic_analysis"],
        "manifest_self_excluded": True, "files": [],
    }

    with tempfile.TemporaryDirectory(prefix=".assessment-", dir=path.parent) as temp:
        partial = Path(temp) / "report.zip"
        with zipfile.ZipFile(partial, "x", compression=zipfile.ZIP_DEFLATED) as bundle:
            def add(name, data):
                bundle.writestr(name, data)
                manifest["files"].append({"path": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})

            add("assessment.json", raw)
            table_paths = []
            for name, fields, rows in assessment_tables(assessment, assessment["report"]):
                add(name, _csv(fields, rows))
                table_paths.append(name)
            add("report.html", _html_report(document, assessment, table_paths))
            add("report.css", (CSS + "\n").encode("utf-8"))
            bundle.writestr("manifest.json", _json(manifest))

        with partial.open("rb") as reader:
            archive_hash = hashlib.file_digest(reader, "sha256").hexdigest()
        size = partial.stat().st_size
        _no_links(path)
        if _destination_identity(path) != original:
            raise FileExistsError("The destination changed while exporting. Choose a new file and try again.")
        if original is not None:
            os.replace(partial, path)
        elif os.name == "nt":
            os.rename(partial, path)  # Fails if a different file has appeared.
        else:
            os.link(partial, path)
    return {"path": str(path), "sha256": archive_hash, "bytes": size, "job_id": job_id,
            "report_sha256": digest, "stale_for_current": assessment["stale_for_current"]}
