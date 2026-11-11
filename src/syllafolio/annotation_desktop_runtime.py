"""Application-owned runtime paths, configured before scientific imports.

This module uses only the standard library. The Windows bundle copies these
exact bytes to ``runtime_paths.py`` so sitecustomize can configure paths without
preloading the syllafolio package or a particular source checkout.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile
import uuid


SCHEMA = "syllafolio-desktop-runtime-v1"
WINDOWS_SAFE_PATH_UNITS = 248
# The observed Numba temporary name was 79 characters. Probe a longer name and
# the same 52-character module-cache folder, not merely an empty directory.
NUMBA_PROBE_FILENAME = ("distances.quantized_int8_alternative_cosine-1966.py312.nbi.tmp."
                        + "f" * 32)
RESERVES = {"numba": 1 + 52 + 1 + len(NUMBA_PROBE_FILENAME),
            "matplotlib": 96, "temp": 150, "projects": 180,
            "diagnostics": 64}
ENV_FIELDS = {"numba_cache_dir": "NUMBA_CACHE_DIR",
              "matplotlib_cache_dir": "MPLCONFIGDIR",
              "temp_dir": "TEMP", "project_dir": "SYLLAFOLIO_PROJECTS_DIR",
              "diagnostics_dir": "SYLLAFOLIO_DIAGNOSTICS_DIR"}


class RuntimePathError(RuntimeError):
    """No validated writable path is available; analysis must not start."""


def windows_path_units(path) -> int:
    """Count Windows UTF-16 code units, including non-BMP characters correctly."""
    return len(str(path).encode("utf-16-le")) // 2


def windows_path_budget_ok(path, kind="numba") -> bool:
    return windows_path_units(path) + RESERVES[kind] <= WINDOWS_SAFE_PATH_UNITS


def probe_directory(directory, kind="temp"):
    """Create, read, and clean an owned nested file; never change existing data."""
    directory = Path(directory)
    token = uuid.uuid4().hex
    directory.mkdir(parents=True, exist_ok=True)
    name = ("syllafolio_probe_" + token + "x" * 52)[:52]
    child = directory / name
    filename = NUMBA_PROBE_FILENAME if kind == "numba" else "nested-write-read-cleanup.txt"
    target = child / filename
    body = b"Syllafolio runtime path probe; no user data.\n"
    try:
        child.mkdir(exist_ok=False)
        with target.open("xb") as stream:
            stream.write(body)
        if target.read_bytes() != body:
            raise OSError("Runtime path probe readback differs")
        target.unlink()
        child.rmdir()
    finally:
        # Clean only this unique probe. A permission error still rejects the
        # candidate; it must not be mistaken for successful TEMP support.
        if target.exists():
            target.unlink()
        if child.exists():
            child.rmdir()
    return {"nested_write_read_cleanup": True, "probe_name_units": len(filename)}


def _absolute(value):
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError("Runtime path must be absolute")
    return path.absolute()


def _roots(root, environment, windows):
    result = []
    if environment.get("SYLLAFOLIO_RUNTIME_ROOT"):
        result.append((environment["SYLLAFOLIO_RUNTIME_ROOT"], "explicit_runtime_root"))
    if windows and environment.get("LOCALAPPDATA"):
        result.append((Path(environment["LOCALAPPDATA"]) / "Syllafolio", "local_app_data"))
    if not windows and environment.get("XDG_CACHE_HOME"):
        result.append((Path(environment["XDG_CACHE_HOME"]) / "Syllafolio", "xdg_cache"))
    portable = (root / "_runtime/python312._pth").is_file()
    result.append((root if portable else root / "tmp/annotation_desktop_runtime", "application_fallback"))
    if windows and environment.get("USERPROFILE"):
        result.append((Path(environment["USERPROFILE"]) / ".syllafolio", "user_profile_fallback"))
    for key in ("TEMP", "TMP"):
        if environment.get(key):
            result.append((Path(environment[key]) / "Syllafolio", key.lower() + "_fallback"))
    return result


def _select(kind, candidates, *, windows, probe, decisions):
    seen = set()
    for value, source in candidates:
        try:
            path = _absolute(value)
            key = str(path).casefold() if windows else str(path)
            if key in seen:
                continue
            seen.add(key)
            if windows and not windows_path_budget_ok(path, kind):
                decisions.append({"kind": kind, "path": str(path), "source": source,
                                  "accepted": False, "reason": "windows_path_budget",
                                  "path_units": windows_path_units(path), "reserved_units": RESERVES[kind]})
                continue
            probe(path, kind)
        except (OSError, ValueError) as error:
            decisions.append({"kind": kind, "path": str(value), "source": source,
                              "accepted": False, "reason": "nested_io_probe_failed",
                              "error_type": type(error).__name__, "error": str(error)})
            continue
        decisions.append({"kind": kind, "path": str(path), "source": source,
                          "accepted": True, "reason": "nested_write_read_cleanup_passed"})
        return path
    raise RuntimePathError(
        "No short writable runtime path is available for " + kind + ". "
        "Set SYLLAFOLIO_RUNTIME_ROOT to a short writable application-owned directory. "
        "No source audio or existing project has been moved or overwritten. Attempts: "
        + json.dumps([d for d in decisions if d["kind"] == kind], ensure_ascii=True))


def _existing_config(root, environment, run_id):
    try:
        config = json.loads(environment.get("SYLLAFOLIO_RUNTIME_CONFIG", ""))
        if not isinstance(config, dict) or config.get("schema") != SCHEMA or config.get("bundle_root") != str(root):
            return None
        if run_id is not None and config.get("run_id") != run_id:
            return None
        for field, variable in ENV_FIELDS.items():
            if environment.get(variable) != config[field] or not Path(config[field]).is_dir():
                return None
        if environment.get("TMP") != config["temp_dir"]:
            return None
        return config
    except (ValueError, TypeError, KeyError):
        return None


def configure_runtime(bundle_root, *, environ=None, platform_name=None,
                      run_id=None, probe=None):
    """Select validated runtime paths and export only this process's environment.

``platform_name`` and ``probe`` permit bounded policy/permission tests. A Linux
test with platform_name='win32' is a path-policy test, never native Windows IO.
Valid explicit NUMBA_CACHE_DIR, MPLCONFIGDIR and application overrides are kept.
Invalid paths are recorded before an independently probed fallback is selected.
    """
    environment = os.environ if environ is None else environ
    root = Path(bundle_root).expanduser().resolve()
    selected_id = run_id or environment.get("SYLLAFOLIO_RUN_ID")
    if selected_id is not None and not re.fullmatch(r"[A-Za-z0-9_.-]{1,96}", selected_id):
        raise RuntimePathError("Runtime run_id must contain only letters, digits, dot, underscore or hyphen")
    existing = _existing_config(root, environment, selected_id)
    if existing is not None:
        if environment is os.environ:
            tempfile.tempdir = existing["temp_dir"]
        return existing
    selected_id = selected_id or (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                                  + "-" + uuid.uuid4().hex[:10])
    windows = (platform_name or os.name) in {"nt", "win32", "Windows"}
    test = probe or probe_directory
    roots = _roots(root, environment, windows)
    decisions = []
    selected = {}
    suffix = {"numba": "c/n", "matplotlib": "c/m", "temp": "t", "projects": "p"}
    overrides = {"numba": "NUMBA_CACHE_DIR", "matplotlib": "MPLCONFIGDIR",
                 "temp": "SYLLAFOLIO_TEMP_DIR", "projects": "SYLLAFOLIO_PROJECTS_DIR"}
    for kind in ("numba", "matplotlib", "temp", "projects"):
        candidates = []
        variable = overrides[kind]
        if environment.get(variable):
            candidates.append((environment[variable], "explicit_" + variable))
        if kind == "temp":
            for key in ("TEMP", "TMP"):
                if environment.get(key):
                    candidates.append((environment[key], "existing_" + key))
        candidates.extend((Path(base) / suffix[kind], source) for base, source in roots)
        selected[kind] = _select(kind, candidates, windows=windows, probe=test, decisions=decisions)
    # Keep reports in the bundle when possible, but bind each process to one
    # isolated run. If the bundle is read-only/too deep use validated app data.
    diagnostics = []
    if environment.get("SYLLAFOLIO_DIAGNOSTICS_DIR"):
        previous = runtime_config(environment)
        inherited_old_run = (previous is not None and previous.get("run_id") != selected_id
                             and previous.get("diagnostics_dir") == environment["SYLLAFOLIO_DIAGNOSTICS_DIR"])
        if not inherited_old_run:
            diagnostics.append((environment["SYLLAFOLIO_DIAGNOSTICS_DIR"], "explicit_diagnostics"))
    portable = (root / "_runtime/python312._pth").is_file()
    reports = root / "reports" if portable else root / "tmp/annotation_desktop_runtime/reports"
    diagnostics.append((reports / "runs" / selected_id, "application_reports"))
    diagnostics.extend((Path(base) / "r" / selected_id, source) for base, source in roots)
    diagnostic_dir = _select("diagnostics", diagnostics, windows=windows, probe=test, decisions=decisions)
    config = {"schema": SCHEMA, "bundle_root": str(root), "run_id": selected_id,
              "native_platform": os.name, "path_policy": "windows" if windows else "posix",
              "windows_budget_limit": WINDOWS_SAFE_PATH_UNITS,
              "cache_dir": str(selected["numba"].parent),
              "numba_cache_dir": str(selected["numba"]),
              "matplotlib_cache_dir": str(selected["matplotlib"]),
              "temp_dir": str(selected["temp"]), "project_dir": str(selected["projects"]),
              "diagnostics_dir": str(diagnostic_dir), "decisions": decisions,
              "data_moved_or_deleted": False,
              "scope": "runtime IO policy only; native Windows import/IO acceptance is separate"}
    destination = diagnostic_dir / "runtime_paths.json"
    temporary = destination.with_name(".runtime-paths-" + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    # Export only after the path selection and its diagnostic record succeeded.
    exports = {variable: config[field] for field, variable in ENV_FIELDS.items()}
    exports.update(TMP=config["temp_dir"], SYLLAFOLIO_RUN_ID=selected_id,
                   SYLLAFOLIO_TEMP_DIR=config["temp_dir"])
    for key, value in exports.items():
        environment[key] = value
    environment.setdefault("NUMBA_NUM_THREADS", "1")
    environment.setdefault("PYTHONUTF8", "1")
    environment["SYLLAFOLIO_RUNTIME_CONFIG"] = json.dumps(config, ensure_ascii=True, separators=(",", ":"))
    if environment is os.environ:
        # tempfile caches its first selection; environment changes alone do not
        # correct a previously selected, sandbox-denied Codex TEMP.
        tempfile.tempdir = config["temp_dir"]
    environment.pop("SYLLAFOLIO_BOOTSTRAP_ERROR", None)
    return config


def runtime_config(environ=None):
    """Read existing bootstrap metadata without touching paths or imports."""
    environment = os.environ if environ is None else environ
    try:
        value = json.loads(environment.get("SYLLAFOLIO_RUNTIME_CONFIG", ""))
        return value if isinstance(value, dict) and value.get("schema") == SCHEMA else None
    except (ValueError, TypeError):
        return None
