"""原生导入的单调计时sidecar；独立于冻结声学产物和人工/研究计时。"""
from __future__ import annotations
from .annotation_english import system_error

import copy
import hashlib
import json
import math
from pathlib import Path
import uuid

from .annotation_run_info import RunTimer
from .annotation_assistant_contracts import canonical_hash


SCHEMA = "syllafolio-desktop-import-timing-v1"
FILENAME = "import_timing.json"
PHASES = (("inspect", "Input inspection and project setup"), ("freeze", "Input freezing"),
          ("features", "Feature extraction and saving"), ("cluster", "Scaling, reduction and clustering"),
          ("organize", "Candidate organization and saving"), ("validate", "Validation and project finalization"),
          ("replay_validate", "Prepared source validation"), ("replay_copy", "Prepared engineering replay copy"))
STAGES = {"starting": "inspect", "freeze": "freeze", "features": "features",
          "global_cluster": "cluster", "organize": "organize", "validate": "validate", "ready": "validate",
          "replay_validate": "replay_validate", "replay_copy": "replay_copy"}
ORIGINS = {"imported_audio", "prepared_engineering_replay"}


def _empty(origin=None, *, state="unrecorded", diagnostics=None):
    return {"schema_version": SCHEMA, "source": "not_recorded", "state": state, "status": state,
            "origin": origin, "total_seconds": None, "current_phase": None,
            "measurement_complete": False, "observed_lower_bound": False,
            "phases": [{"id": key, "label": label, "seconds": None, "status": "unrecorded"}
                       for key, label in PHASES], "input_identity": {}, "input_summary": {},
            "not_recorded": ["import_timing"], "diagnostics": diagnostics or []}


class _PhaseTimer(RunTimer):
    """复用既有RunTimer的单调差值、finish和snapshot，单独细分原生导入阶段。"""
    def __init__(self, origin, **kwargs):
        super().__init__(**kwargs)
        self.phase = "inspect" if origin == "imported_audio" else "replay_validate"
        self.state = "running"
        applicable = ({"inspect", "freeze", "features", "cluster", "organize", "validate"}
                      if origin == "imported_audio" else {"replay_validate", "replay_copy", "validate"})
        self.rows = {key: {"id": key, "label": label,
                          "seconds": 0.0 if key in applicable else None,
                          "status": "not_started" if key in applicable else "not_applicable"}
                     for key, label in PHASES}
        self.rows[self.phase].update(status="running", started_at=self.wall_clock())

    def transition(self, stage):
        phase = STAGES[stage]
        if phase == self.phase:
            return
        if self.end is not None or self.rows[phase]["status"] == "not_applicable":
            raise ValueError("Import timing phase is not applicable or has already ended")
        stamp, wall = self.clock(), self.wall_clock()
        self.rows[self.phase].update(seconds=self._elapsed(stamp, self.phase_start), status="completed", ended_at=wall)
        self.rows[phase].update(status="running", started_at=wall)
        self.phase, self.phase_start = phase, stamp


class ImportTiming:
    """仅在实际观察点采样；创建项目之前不落盘，计时IO失败不会改声学结果。"""
    def __init__(self, origin="imported_audio", *, clock=None, wall_clock=None):
        if origin not in ORIGINS:
            raise ValueError("Unsupported import origin")
        self.origin, self.root = origin, None
        self.identity, self.input_identity, self.input_summary = {}, {}, {}
        self.operation_id = "import-" + uuid.uuid4().hex
        self.diagnostics, self.invalid, self.last_good = [], False, None
        self.timer = _PhaseTimer(origin, clock=clock, wall_clock=wall_clock)

    def _diagnostic(self, code, error):
        row = {"code": code, "message": system_error(error)}
        if row not in self.diagnostics:
            self.diagnostics.append(row)

    def bind(self, root, marker):
        self.root = Path(root).resolve()
        self.identity = {key: marker[key] for key in ("project_id", "run_id")}
        self.checkpoint()

    def set_input(self, fingerprint, *, summary=None, manifest_sha256=None, input_fingerprint=None):
        self.input_identity["source_fingerprint"] = fingerprint
        if summary is not None:
            self.input_summary = copy.deepcopy(summary)
        if manifest_sha256 is not None:
            self.input_identity["manifest_sha256"] = manifest_sha256
        if input_fingerprint is not None:
            self.input_identity["input_fingerprint"] = input_fingerprint

    def transition(self, stage):
        if not self.invalid and stage in STAGES:
            try:
                self.timer.transition(stage)
            except (ValueError, TypeError, OverflowError) as exc:
                self.invalid = True
                self._diagnostic("IMPORT_CLOCK_INVALID", exc)

    def snapshot(self):
        if not self.invalid:
            try:
                value = self.timer.snapshot()
                self.last_good = copy.deepcopy(value)
            except (ValueError, TypeError, OverflowError) as exc:
                self.invalid = True
                self._diagnostic("IMPORT_CLOCK_INVALID", exc)
        if self.invalid:
            value = _empty(self.origin, state="invalid")
            if self.last_good:
                value["last_observed_total_seconds"] = self.last_good["total_seconds"]
        value.update(schema_version=SCHEMA, status=value["state"], origin=self.origin,
                     operation_id=self.operation_id, **self.identity,
                     input_identity=copy.deepcopy(self.input_identity), input_summary=copy.deepcopy(self.input_summary),
                     observed_lower_bound=False, not_recorded=["import_timing"] if self.invalid else [],
                     diagnostics=copy.deepcopy(self.diagnostics), timing_scope="application computation; not human research time")
        return value

    def checkpoint(self):
        value = self.snapshot()
        if self.root is None:
            return value
        path = self.root / FILENAME
        temporary = self.root / (".import-timing-" + uuid.uuid4().hex + ".tmp")
        try:
            if path.is_symlink():
                raise OSError("Import timing cannot follow an external link")
            temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n", encoding="utf-8")
            temporary.replace(path)
        except (OSError, ValueError, TypeError) as exc:
            self._diagnostic("IMPORT_TIMING_WRITE_FAILED", exc)
            value["diagnostics"] = copy.deepcopy(self.diagnostics)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        return value

    def progress(self, event, callback=None):
        self.transition(event.get("stage"))
        result = {**copy.deepcopy(event), "timing": self.checkpoint()}
        if callback is not None:
            callback(result)
        return result

    def finish(self, state):
        if state not in {"succeeded", "failed", "cancelled"}:
            raise ValueError("An import attempt needs an explicit terminal state")
        if not self.invalid:
            try:
                self.timer.finish(state)
            except (ValueError, TypeError, OverflowError) as exc:
                self.invalid = True
                self._diagnostic("IMPORT_CLOCK_INVALID", exc)
        return self.checkpoint()


def read_import_timing(project_dir, marker):
    """只读投影。旧running仅是已记录下限；不采样当前时钟，不写恢复状态。"""
    path = Path(project_dir) / FILENAME
    origin = marker.get("origin")
    if not path.exists() and not path.is_symlink():
        return _empty(origin)
    try:
        if path.is_symlink() or not path.resolve().is_relative_to(Path(project_dir).resolve()):
            raise ValueError("Import timing path is outside the project")
        def pairs(values):
            result = {}
            for key, value in values:
                if key in result:
                    raise ValueError("Duplicate import timing key")
                result[key] = value
            return result
        def invalid_number(value):
            raise ValueError("Nonfinite import timing number: " + value)
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs, parse_constant=invalid_number)
        if not isinstance(value, dict) or value.get("schema_version") != SCHEMA:
            raise ValueError("Unsupported import timing schema")
        if value.get("origin") != origin or any(value.get(key) != marker.get(key) for key in ("project_id", "run_id")):
            raise ValueError("Import timing project/run/origin identity mismatch")
        identity = value.get("input_identity", {})
        if not isinstance(identity, dict) or not isinstance(value.get("input_summary"), dict):
            raise ValueError("Import timing input metadata is malformed")
        for key in ("source_fingerprint", "manifest_sha256"):
            if marker.get(key) is not None and identity.get(key) is not None and identity[key] != marker[key]:
                raise ValueError("Import timing input identity mismatch")
        if marker.get("source_fingerprint") is not None and identity.get("source_fingerprint") is None:
            raise ValueError("Import timing lacks frozen source identity")
        state = value.get("state")
        if state not in {"running", "queued", "succeeded", "failed", "cancelled", "interrupted", "invalid"}:
            raise ValueError("Unknown import timing state")
        if value.get("status") != state:
            raise ValueError("Import timing status and state disagree")
        phases = value.get("phases")
        if (not isinstance(phases, list) or not all(isinstance(row, dict) for row in phases)
                or [row.get("id") for row in phases] != [key for key, _ in PHASES]):
            raise ValueError("Import timing phases do not match the contract")
        if type(value.get("measurement_complete")) is not bool:
            raise ValueError("Import timing completion is not explicit")
        if state in {"succeeded", "failed", "cancelled"} and not value["measurement_complete"]:
            raise ValueError("Import timing terminal state has no complete observation")
        if not isinstance(value.get("diagnostics", []), list) or not all(isinstance(row, dict) for row in value.get("diagnostics", [])):
            raise ValueError("Import timing diagnostics are malformed")
        if not isinstance(value.get("not_recorded", []), list):
            raise ValueError("Import timing missing-data declaration is malformed")
        if state != "invalid":
            if value.get("source") != "monotonic_wall_clock":
                raise ValueError("Import timing was not measured with the monotonic clock")
            allowed = {"running", "not_started", "completed", "failed", "cancelled", "interrupted", "not_applicable"}
            if any(row.get("status") not in allowed for row in phases):
                raise ValueError("Import timing contains an unknown phase state")
            applicable = ({"inspect", "freeze", "features", "cluster", "organize", "validate"}
                          if origin == "imported_audio" else {"replay_validate", "replay_copy", "validate"})
            if any((row["id"] not in applicable) != (row["status"] == "not_applicable") for row in phases):
                raise ValueError("Import timing phase applicability does not match its origin")
            if any(row.get("seconds") is not None for row in phases if row["status"] == "not_applicable"):
                raise ValueError("Not-applicable import phases cannot claim measured seconds")
            running = [row["id"] for row in phases if row["status"] == "running"]
            if state in {"succeeded", "failed", "cancelled"}:
                if value.get("current_phase") is not None or running:
                    raise ValueError("A terminal import timing still has a running phase")
                if state == "succeeded" and any(row["status"] != "completed" for row in phases if row["id"] in applicable):
                    raise ValueError("Successful import timing has unobserved required phases")
            if state in {"running", "queued"} and (len(running) != 1 or value.get("current_phase") != running[0]):
                raise ValueError("Active import timing does not identify its single running phase")
            for seconds in [value.get("total_seconds"), *(row.get("seconds") for row in phases if row.get("status") != "not_applicable")]:
                if type(seconds) not in {int, float} or not math.isfinite(seconds) or seconds < 0:
                    raise ValueError("Invalid observed import duration")
            phase_total = sum(row["seconds"] for row in phases if row.get("seconds") is not None)
            if phase_total > value["total_seconds"] + max(1e-6, value["total_seconds"] * 1e-9):
                raise ValueError("Import phase durations exceed the observed total")
        if state in {"running", "queued", "interrupted"}:
            value.update(state="interrupted", status="interrupted", measurement_complete=False,
                         current_phase=None, observed_lower_bound=True)
            for row in phases:
                if row.get("status") == "running":
                    row["status"] = "interrupted"
        if state == "invalid":
            value.update(total_seconds=None, measurement_complete=False, not_recorded=["import_timing"])
        value.setdefault("diagnostics", [])
        value.setdefault("not_recorded", [])
        return value
    except (OSError, UnicodeError, ValueError, TypeError, KeyError) as exc:
        return _empty(origin, state="invalid", diagnostics=[{"code": "IMPORT_TIMING_INVALID", "message": system_error(exc)}])


def verify_import_inspection(project_dir, marker):
    """每个owner首次使用时核持久审阅附件；只读，不要求旧项目补造记录。"""
    if marker.get("origin") != "imported_audio":
        return {"status": "not_applicable", "diagnostics": []}
    root = Path(project_dir).resolve()
    path = root / "import_inspection.json"
    if not path.exists() and not path.is_symlink():
        return {"status": "unrecorded", "diagnostics": []}
    try:
        def checked_path(relative):
            candidate = root / relative
            if candidate.is_symlink() or not candidate.resolve().is_relative_to(root):
                raise ValueError("Import inspection attachment path is outside the project")
            return candidate
        def read_json(candidate):
            def pairs(values):
                result = {}
                for key, item in values:
                    if key in result:
                        raise ValueError("Duplicate import inspection key")
                    result[key] = item
                return result
            def invalid_number(item):
                raise ValueError("Nonfinite import inspection value: " + item)
            return json.loads(candidate.read_text(encoding="utf-8"), object_pairs_hook=pairs, parse_constant=invalid_number)
        audit = read_json(checked_path("import_inspection.json"))
        if not isinstance(audit, dict) or audit.get("schema_version") != "syllafolio-desktop-import-inspection-v1":
            raise ValueError("Unsupported persisted input inspection")
        if any(audit.get(key) != marker.get(key) for key in ("project_id", "run_id")):
            raise ValueError("Persisted inspection project/run identity mismatch")
        identity = audit.get("inspection_identity")
        if not isinstance(identity, dict) or set(identity) != {"source_fingerprint", "has_sidecar", "segments_json_sha256", "items"}:
            raise ValueError("Persisted input inspection fields do not match the contract")
        if identity["source_fingerprint"] != marker.get("source_fingerprint") or type(identity["has_sidecar"]) is not bool:
            raise ValueError("Persisted inspection source identity mismatch")
        fingerprint = canonical_hash(identity)
        if fingerprint != audit.get("inspection_fingerprint"):
            raise ValueError("Persisted inspection fingerprint mismatch")
        items, mapping = identity["items"], audit.get("original_mapping")
        if (not isinstance(items, list) or not all(isinstance(row, dict) for row in items)
                or not isinstance(mapping, list) or not all(isinstance(row, dict) for row in mapping)
                or len(items) != len(mapping)):
            raise ValueError("Persisted inspection original mapping is malformed")
        receipt = read_json(checked_path("runs/run-0001/coordinator/source/import_receipt.json"))
        frozen = [{key: row.get(key) for key in ("item_id", "original_item_id", "original_file")}
                  for row in receipt.get("original_mapping", [])]
        if mapping != frozen or [row.get("item_id") for row in items] != [row.get("item_id") for row in mapping]:
            raise ValueError("Persisted inspection differs from frozen original ID/file mapping")
        if identity["has_sidecar"]:
            if audit.get("original_segments_relative") != "import_segments.original.json":
                raise ValueError("Original descriptor reference is not supported")
            descriptor = checked_path("import_segments.original.json")
            if hashlib.sha256(descriptor.read_bytes()).hexdigest() != identity["segments_json_sha256"]:
                raise ValueError("Preserved original segments.json bytes do not match the inspected SHA")
            original = read_json(descriptor)
            entries = original.get("items")
            if (not isinstance(entries, list) or len(entries) != len(mapping)
                    or any(not isinstance(row, dict) for row in entries)):
                raise ValueError("Preserved original descriptor mapping is malformed")
            for index, (entry, row) in enumerate(zip(entries, mapping)):
                if (entry.get("item_id", f"input-{index + 1:04d}") != row.get("original_item_id")
                        or Path(entry["file"]).name != Path(row["original_file"]).name):
                    raise ValueError("Preserved original descriptor and frozen mapping disagree")
        elif identity["segments_json_sha256"] is not None or audit.get("original_segments_relative") is not None:
            raise ValueError("Inspection claims an original descriptor that was not supplied")
        return {"status": "verified", "inspection_fingerprint": fingerprint,
                "has_sidecar": identity["has_sidecar"], "segments_json_sha256": identity["segments_json_sha256"],
                "diagnostics": []}
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, AttributeError) as exc:
        return {"status": "invalid", "diagnostics": [{"code": "IMPORT_INSPECTION_INVALID", "message": system_error(exc)}]}


def apply_inspection_integrity(timing, integrity):
    """将缓存的审阅核验与本轮计时引用交叉核对，损坏不阻标签历史。"""
    result = copy.deepcopy(timing)
    result["inspection_integrity"] = integrity["status"]
    result["diagnostics"].extend(copy.deepcopy(integrity.get("diagnostics", [])))
    if integrity["status"] == "not_applicable":
        return result
    recorded = result.get("input_identity", {}).get("inspection_fingerprint")
    invalid = integrity["status"] == "invalid" or (integrity["status"] == "verified" and result.get("state") not in {"unrecorded", "invalid"}
        and (recorded != integrity["inspection_fingerprint"]
             or result.get("input_summary", {}).get("has_sidecar") != integrity["has_sidecar"]
             or result.get("input_summary", {}).get("segments_json_sha256") != integrity["segments_json_sha256"]))
    if integrity["status"] == "unrecorded" or invalid:
        result.setdefault("input_summary", {}).pop("has_sidecar", None)
        if "input_inspection" not in result["not_recorded"]:
            result["not_recorded"].append("input_inspection")
        result["measurement_complete"] = False
    if invalid:
        if not integrity.get("diagnostics"):
            result["diagnostics"].append({"code": "IMPORT_INSPECTION_INVALID", "message": "Timing and persisted input inspection references disagree"})
        result.update(state="invalid", status="invalid", measurement_complete=False, total_seconds=None)
    return result
