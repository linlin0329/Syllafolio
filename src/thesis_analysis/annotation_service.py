"""P1共用服务：工程模式、真实声学计算与持久人工草稿；不接入模型。"""
from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import threading
import time

from .annotation_acoustics import AcousticDataset, AcousticError
from .annotation_state import AnnotationStateStore, AnnotationStateError
from .annotation_assistant_contracts import (
    ContractBundle, ContractViolation, canonical_hash, require,
)
from .annotation_project_contract import (
    CONTRACT_VERSION, DEFAULT_BUDGET, contract_metadata, validate_project_contract,
)


def utc():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".writing")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


class AnnotationService:
    """单机工程服务。P0仅用于内存中的规则校验，持久记录使用独立0.2.0合同。"""

    def __init__(self, dataset_dir, work_dir, *, run_id="p1-engineering", budget_seconds=DEFAULT_BUDGET,
                 preset_path=None, source=None, store=None, assistant_contract=None, project_contract=None):
        self.dataset_dir = Path(dataset_dir).resolve()
        self.work_dir = Path(work_dir).absolute()
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.dataset = AcousticDataset.from_files(
            self.dataset_dir / "manifest.json", self.dataset_dir / "features.npy",
            self.dataset_dir / "logmel.npy", preset_path)
        from .annotation_global_view import load_global_candidates
        self.global_candidates_data = load_global_candidates(self.dataset_dir)
        self.run_id = run_id
        self._lock = threading.RLock()
        self._drain_requested = threading.Event()
        self._closed = False
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="p1-acoustic")
        self._cancel = {}
        self._futures = {}
        self.rules = ContractBundle()
        self.assistant_gateway = None
        self.assets_path = self.work_dir / "assets.json"
        self.assets = json.loads(self.assets_path.read_text()) if self.assets_path.exists() else {}
        self.store = store or AnnotationStateStore(self.work_dir / "state.sqlite3")
        self.feature_identity_compatibility = None
        legacy_contract = {
            "schema_version": "0.2.0", "mode": "engineering", "experiment_enabled": False,
            "execution_enabled": True, "allow_engineering_computation": True,
            "allowed_item_ids": self.dataset.item_ids,
            "input_versions": self.dataset.input_versions,
            "input_fingerprint": self.dataset.input_fingerprint,
            "preset_sha256": canonical_hash(self.dataset.preset),
            "preset_id": self.dataset.preset["preset_id"],
            "budget_seconds": 3600 if budget_seconds is DEFAULT_BUDGET else budget_seconds, "protocol_version": "0.2",
            "operator_id": "local-engineering-operator",
            "model_calls_allowed": False,
        }
        if assistant_contract is not None:
            require(isinstance(assistant_contract, dict) and
                    assistant_contract.get("experiment_enabled") is False,
                    "ASSISTANT_CONTRACT", "Only an independent engineering-run contract is supported here.")
            legacy_contract.update(model_calls_allowed=True, assistant=copy.deepcopy(assistant_contract))
        try:
            previous = None
            try:
                previous = self.store.snapshot(run_id)
            except AnnotationStateError as exc:
                if exc.code != "RUN_NOT_FOUND":
                    raise
            contract = (copy.deepcopy(previous["contract"]) if previous is not None and project_contract is None
                        else copy.deepcopy(project_contract) if project_contract is not None else legacy_contract)
            if contract.get("schema_version") == CONTRACT_VERSION:
                contract = validate_project_contract(contract, run_id=run_id)
                require(assistant_contract is None, "MANUAL_ONLY", "New project contracts do not accept a model or assistant.")
            else:
                require(contract.get("schema_version") == "0.2.0" and contract.get("mode") == "engineering"
                        and contract.get("experiment_enabled") is False, "CONTRACT_VERSION", "Unsupported legacy run contract.")
                require(assistant_contract is not None or not contract.get("model_calls_allowed", False),
                        "IMMUTABLE_RUN", "A legacy assistant contract cannot be opened as a manual-only project.")
            if previous is not None and contract.get("input_fingerprint") == previous["contract"].get("input_fingerprint"):
                from .annotation_feature_identity import existing_run_feature_identity
                compatibility = existing_run_feature_identity(self.dataset, previous["contract"])
                if compatibility is not None:
                    # Keep the original run/draft/cache identity only after the
                    # whole actual input hashes to that exact saved contract.
                    self.dataset.actual_input_fingerprint = self.dataset.input_fingerprint
                    self.dataset.input_fingerprint = compatibility["effective_input_fingerprint"]
                    legacy_contract["input_fingerprint"] = self.dataset.input_fingerprint
                    self.feature_identity_compatibility = compatibility
            for key in ("allowed_item_ids", "input_versions", "input_fingerprint", "preset_sha256", "preset_id"):
                require(contract.get(key) == legacy_contract[key], "IMMUTABLE_RUN", "The run input or preset does not match: " + key)
            if budget_seconds is not DEFAULT_BUDGET:
                require(contract.get("budget_seconds") == budget_seconds, "IMMUTABLE_RUN", "The existing run budget cannot be overwritten.")
            if previous is not None:
                require(previous["contract"] == contract, "IMMUTABLE_RUN", "The existing run contract cannot be overwritten.")
                if assistant_contract is not None:
                    require(contract.get("assistant") == assistant_contract, "IMMUTABLE_RUN", "The existing assistant contract cannot be overwritten.")
            self.project_contract = copy.deepcopy(contract)
            self.source = contract.get("evidence_source", source or "engineering_user")
            require(source is None or source == self.source, "EVIDENCE_SOURCE", "The source does not match the run contract.")
            require(self.source in ({"engineering_user", "engineering_test"} if contract.get("mode") == "engineering"
                                   else {"daily_user"}), "EVIDENCE_SOURCE", self.source)
            if previous is None:
                self.store.create_run(run_id, contract, idempotency_key="create:" + run_id, mode=contract["mode"])
            # 单owner锁证明旧服务已退出；queued也必须终结，不能留下无人执行的队列。
            current = self.store.snapshot(run_id)
            for ident, prior in current["jobs"].items():
                if prior["status"] not in {"queued", "running", "cancel_requested"}:
                    continue
                job = copy.deepcopy(prior)
                job.update(status="failed", error={"code": "PROCESS_INTERRUPTED",
                           "message": "The previous service process ended; explicitly start the task again."}, result_ids=[],
                           output_validation="failed", ended_at=utc())
                self._system_job(job)
        except Exception:
            self._executor.shutdown(wait=False)
            if store is None:
                self.store.close()
            raise

    def close(self):
        if self._closed:
            return
        for event in self._cancel.values():
            event.set()
        self._executor.shutdown(wait=True, cancel_futures=False)
        self.store.close()
        self._closed = True

    @property
    def is_draining(self):
        return self._drain_requested.is_set()

    def begin_drain(self):
        # Like cooperative job cancellation, requesting a drain must not wait
        # behind a long read-only preview that already owns the command lock.
        self._drain_requested.set()

    def end_drain(self):
        require(not self._closed, "STORE_CLOSED", "The service is closed.")
        self._drain_requested.clear()

    def command_guard(self, action, payload=None):
        """在持有owner同一RLock时调用；worker系统收条不经过此入口。"""
        allowed = {"cancel_job", "batch_cancel", "assessment_cancel", "catalogue_export_cancel",
                   "neighbors", "comparison", "export", "batch_preview", "source_validate",
                   "inspect_completion", "batch_display"}
        allowed_timer = action == "timer" and (payload or {}).get("event") in {"pause", "stop"}
        require(not self._drain_requested.is_set() or action in allowed or allowed_timer,
                "PROJECT_DRAINING", "The project is waiting for background tasks to settle; return to the workspace before submitting another action.")

    def worker_status(self):
        with self._lock:
            result = {"local_batch": {key: future.done() for key, future in self._futures.items()}}
            for kind, name in (("assessment", "assessment"), ("catalogue_export", "catalogue_export")):
                child = getattr(self, name, None)
                result[kind] = {key: future.done() for key, future in getattr(child, "futures", {}).items()}
            return result

    def workers_settled(self):
        return all(done for group in self.worker_status().values() for done in group.values())

    def record_metadata(self):
        result = contract_metadata(self.project_contract, source=self.source)
        if "purpose" in result:
            result["project_purpose"] = result["purpose"]
        return result

    def _asset(self, path, expected_sha256=None):
        path = Path(path).resolve()
        require(path.is_relative_to(self.work_dir.resolve()) and path.is_file(),
                "ASSET_SCOPE", "The asset does not belong to the current engineering run.")
        relative = str(path.relative_to(self.work_dir.resolve()))
        ident = "asset-" + hashlib.sha256(relative.encode()).hexdigest()[:24]
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if expected_sha256 is not None:
            require(actual == expected_sha256, "ASSET_CHANGED", "The media does not match its generation receipt.")
        if ident in self.assets:
            require(self.assets[ident] == {"path": relative, "sha256": actual},
                    "ASSET_CHANGED", "The registered asset has changed; it cannot be registered again as a valid cache.")
        self.assets[ident] = {"path": relative, "sha256": actual}
        atomic_json(self.assets_path, self.assets)
        return "/api/media/" + ident

    def media_path(self, ident):
        require(ident in self.assets, "ASSET_NOT_FOUND", "Unknown asset handle.")
        row = self.assets[ident]
        path = (self.work_dir / row["path"]).resolve()
        require(path.is_relative_to(self.work_dir.resolve()) and path.is_file(),
                "ASSET_SCOPE", "The asset is inaccessible.")
        require(hashlib.sha256(path.read_bytes()).hexdigest() == row["sha256"],
                "ASSET_CHANGED", "The asset file has changed.")
        return path

    @staticmethod
    def _safe_result(result):
        result = copy.deepcopy(result)
        result.pop("provenance", None)
        return result

    def snapshot(self):
        state = self.store.snapshot(self.run_id)
        if self.feature_identity_compatibility is not None:
            state["feature_identity_compatibility"] = copy.deepcopy(self.feature_identity_compatibility)
        state.pop("job_runtime", None)
        state["items"] = self.dataset.public_items()
        for item in state["items"]:
            original = self.dataset._items[item["item_id"]]
            if "context_kind" in original:
                item["context_kind"] = original["context_kind"]
        if self.global_candidates_data.get("available"):
            state["global_candidates_available"] = True
        state["source"] = self.source
        state["input_versions"] = self.dataset.input_versions
        state["preset"] = {"preset_id": self.dataset.preset["preset_id"],
                           "purpose": self.dataset.preset["purpose"],
                           "local": self.dataset.preset["local"]}
        state["clock"]["deadline_locked"] = state["deadline_locked"]
        budget = state["clock"]["budget_seconds"]
        state["clock"]["remaining_seconds"] = (
            None if budget is None else max(0, budget - state["clock"]["elapsed_seconds"]))
        state["decisions"] = list(state["decisions"].values())
        for job in state["jobs"].values():
            if job["status"] == "succeeded":
                job["stale_for_current_revision"] = (
                    job.get("catalogue_revision_at_request") != state["catalogue_revision"])
        state["checkpoints"] = [
            {k: row[k] for k in ("checkpoint_id", "reason", "created_at")}
            for row in state["checkpoints"].values()
        ]
        return state

    def global_candidates(self):
        from .annotation_global_view import load_global_candidates
        return load_global_candidates(self.dataset_dir)

    def _validation_context(self, state):
        """只在内存中复用P0关系检查；这些适配记录永不持久化或作为实验输出。"""
        task = {
            "schema_version": "0.1.0", "record_kind": "engineering_simulation",
            "execution_enabled": False, "task_contract_id": self.run_id,
            "parent_contract_id": None, "evaluation_design": "engineering_only",
            **self.dataset.input_versions,
            "material_manifest_sha256": self.dataset.input_fingerprint,
            "feature_manifest_sha256": self.dataset.input_fingerprint,
            "global_fit_member_ids": self.dataset.global_fit_ids, "allowed_item_ids": self.dataset.item_ids,
            "protocol_version": "0.2", "preset_ids": [self.dataset.preset["preset_id"]],
            "condition": {"condition_id": "p1-manual", "assistant_enabled": False,
                          "capabilities": ["global_neighbors", "comparison_bundle",
                                           "local_reorder", "local_recluster", "manual_curation",
                                           "protocol_guidance"]},
            "budgets": dict.fromkeys(["human_seconds", "compute_seconds", "model_calls",
                                     "model_tokens", "model_cost_limit", "currency"]),
            "participants": {"operator_ids": None, "coordinator_id": None, "reference_ids": None},
            "timing_rules": {"include_waiting": True, "hidden_page_is_pause": False,
                             "overlapping_human_intervals": "union",
                             "deadline_policy": "lock_snapshot_no_backfill"}, "revision": 0,
        }
        actor = {"actor_id": state["contract"]["operator_id"], "role": "operator"}
        obs = {ident: {
            "schema_version": "0.1.0", "record_kind": "engineering_simulation",
            "execution_enabled": False, "observation_id": ident,
            "task_contract_id": self.run_id, "item_ids": row["item_ids"],
            "original_text": row["text"], "input_versions": self.dataset.input_versions,
            "actor": actor, "recorded_at": row["recorded_at"],
            "source": "authenticated_human_ui", "is_final_decision": False,
        } for ident, row in state["observations"].items()}
        issues = {}
        for ident, row in state["issues"].items():
            issues[ident] = self._validation_issue(row)
        return task, dict(actor, operator_id=actor["actor_id"],
                         catalogue_revision=state["catalogue_revision"],
                         state_revision=state["revision"], deadline_locked=state["deadline_locked"],
                         observations=obs, issues=issues, memberships=state["memberships"])

    def _validation_issue(self, row):
        return {
            "schema_version": "0.1.0", "record_kind": "engineering_simulation",
            "execution_enabled": False, "issue_id": row["issue_id"],
            "task_contract_id": self.run_id, "revision": row["revision"],
            "parent_issue_id": row.get("parent_issue_id"),
            "question": row["question"], "origin_observation_ids": row["observation_ids"],
            **{k: row[k] for k in ("target_ids", "fit_ids", "comparison_ids",
                                  "eligible_change_ids", "action", "scope_reason", "status")},
            "input_versions": self.dataset.input_versions,
            "catalogue_revision_at_request": row["catalogue_revision_at_request"],
        }

    def _system_job(self, record):
        for _ in range(20):
            state = self.store.snapshot(self.run_id)
            try:
                return self.store.transact(
                    self.run_id, expected_revision=state["revision"],
                    idempotency_key="worker:" + record["job_id"] + ":" + record["status"],
                    operation="record_job", payload={"record": record})
            except AnnotationStateError as exc:
                if exc.code != "REVISION_CONFLICT":
                    raise
        raise AnnotationStateError("REVISION_CONFLICT", "Background state updates repeatedly conflict.")

    def _run_job(self, job):
        event = self._cancel[job["job_id"]]
        started = time.monotonic()
        try:
            latest = self.store.snapshot(self.run_id)
            current = copy.deepcopy(latest["jobs"][job["job_id"]])
            if event.is_set() or latest["deadline_locked"]:
                if current["status"] == "queued":
                    current.update(status="cancel_requested")
                    self._system_job(current)
                current.update(status="cancelled", worker_confirmed_stopped=True,
                               ended_at=utc(), result_ids=[])
                self._system_job(current)
                return
            current.update(status="running", started_at=utc())
            self._system_job(current)
            output = self.work_dir / "jobs" / job["job_id"]
            result = self.dataset.run_local(
                job["action"], job["fit_ids"], job["comparison_ids"], output,
                check_cancelled=event.is_set)
            final = self.store.snapshot(self.run_id)
            current = copy.deepcopy(final["jobs"][job["job_id"]])
            if event.is_set():
                current.update(status="cancelled", worker_confirmed_stopped=True,
                               result_ids=[], output_validation="not_available")
            else:
                current.update(status="succeeded", result=self._safe_result(result),
                               result_ids=[result["result_id"]], output_validation="passed",
                               stale_for_current_revision=(
                                   final["catalogue_revision"] != job["catalogue_revision_at_request"]))
            current.update(ended_at=utc(), compute_wall_seconds=time.monotonic() - started)
            self._system_job(current)
        except Exception as exc:
            final = self.store.snapshot(self.run_id)
            current = copy.deepcopy(final["jobs"][job["job_id"]])
            if current["status"] in {"cancelled", "failed", "succeeded"}:
                return
            cancelled = event.is_set() or getattr(exc, "code", "") == "CANCELLED"
            if cancelled and current["status"] != "cancel_requested":
                current.update(status="cancel_requested")
                self._system_job(current)
            current.update(status="cancelled" if cancelled else "failed",
                           worker_confirmed_stopped=cancelled,
                           error={"code": getattr(exc, "code", "COMPUTATION_FAILED"),
                                  "message": str(exc)},
                           result_ids=[], output_validation="failed",
                           ended_at=utc(), compute_wall_seconds=time.monotonic() - started)
            self._system_job(current)

    def _ids(self, ids, *, nonempty=False):
        require(isinstance(ids, list) and all(isinstance(i, str) for i in ids),
                "INVALID_INPUT", "Material IDs must be a list.")
        require(len(ids) == len(set(ids)), "DUPLICATE_ID", "Materials must be unique.")
        require(not nonempty or bool(ids), "INSUFFICIENT_INPUT", "Select an item.")
        require(set(ids) <= set(self.dataset.item_ids), "OUT_OF_SCOPE", "The materials are outside the allowed scope.")
        return list(ids)

    def execute(self, action, payload, *, expected_revision, idempotency_key):
        require(isinstance(payload, dict), "INVALID_INPUT", "The payload must be an object.")
        require(isinstance(idempotency_key, str) and
                0 < len(idempotency_key) <= 160, "IDEMPOTENCY_REQUIRED", "A request identifier is required.")
        request_hash = canonical_hash({"action": action, "payload": payload,
                                       "expected_revision": expected_revision})
        with self._lock:
            ident = hashlib.sha256((self.run_id + ":" + idempotency_key).encode()).hexdigest()[:20]
            export_meta = self.work_dir / "exports" / ("export-" + ident + ".receipt.json")
            if export_meta.exists():
                prior = json.loads(export_meta.read_text())
                require(action == "export" and prior["request_hash"] == request_hash, "IDEMPOTENCY_CONFLICT",
                        "The same export request identifier was used with different parameters or versions.")
                return dict(prior["result"], replayed=True)
            for event in reversed(self.store.events(self.run_id)):
                if event["idempotency_key"] == idempotency_key:
                    require(event["payload"].get("service_request_hash") == request_hash,
                            "IDEMPOTENCY_CONFLICT", "The same request identifier was used with different parameters.")
                    return {"receipt": event["receipt"],
                            "record": event["payload"].get("record"), "replayed": True}
            self.command_guard(action, payload)
            state = self.store.snapshot(self.run_id)
            require(type(expected_revision) is int and expected_revision == state["revision"],
                    "REVISION_CONFLICT", "The state has changed; refresh before submitting again.")
            task, context = self._validation_context(state)
            stamp = utc()
            record = None
            operation = None
            extra = {}
            if action == "neighbors":
                result = self.dataset.neighbors(self._ids(payload["item_ids"], nonempty=True),
                                               self._ids(payload["scope_ids"], nonempty=True),
                                               payload.get("k", 5))
                result["neighbors"] = result["items"]
                return result
            if action == "comparison":
                ids = self._ids(payload["item_ids"], nonempty=True)
                compares = self._ids(payload.get("comparison_ids", []))
                folder = self.work_dir / "comparisons" / ("comparison-" + canonical_hash(
                    {"input": self.dataset.input_fingerprint, "ids": ids, "compare": compares})[:24])
                if (folder / "bundle.json").exists():
                    result = json.loads((folder / "bundle.json").read_text())
                else:
                    result = self.dataset.build_comparison_bundle(ids, compares, folder)
                result = self._safe_result(result)
                for item in result["items"]:
                    for field, target in (("audio_path", "audio_url"),
                                          ("context_audio_path", "context_audio_url"),
                                          ("image_path", "spectrogram_url")):
                        item[target] = self._asset(folder / item.pop(field),
                                                  item[field.replace("_path", "_sha256")])
                result["group_audio_url"] = self._asset(folder / result.pop("group_audio_path"))
                return result
            if action == "observe":
                ids = self._ids(payload["item_ids"], nonempty=True)
                require(isinstance(payload.get("text"), str) and payload["text"].strip(),
                        "OBSERVATION_REQUIRED", "Enter the actual observation.")
                operation = "record_observation"
                record = {"observation_id": "obs-" + ident, "item_ids": ids,
                          "text": payload["text"], "recorded_at": stamp,
                          "source": self.source, "experiment_enabled": False,
                          "clock_status": state["clock"]["status"],
                          "timing_complete": state["clock"]["timing_complete"]}
            elif action == "issue":
                require(isinstance(payload.get("question"), str) and payload["question"].strip(),
                        "QUESTION_REQUIRED", "Describe the review question.")
                record = {"issue_id": "issue-" + ident, "question": payload["question"],
                          "observation_ids": payload.get("observation_ids", []),
                          "action": payload["action"], "scope_reason": payload["scope_reason"],
                          "status": "open", "revision": 0, "parent_issue_id": None,
                          "catalogue_revision_at_request": state["catalogue_revision"],
                          "source": self.source}
                for key in ("target_ids", "fit_ids", "comparison_ids", "eligible_change_ids"):
                    record[key] = self._ids(payload[key], nonempty=key == "target_ids")
                self.rules.review_issue(self._validation_issue(record), task, context)
                operation = "open_issue"
            elif action == "local_job":
                require(payload["issue_id"] in state["issues"], "ISSUE_NOT_FOUND", "The issue does not exist.")
                issue = state["issues"][payload["issue_id"]]
                require(issue["status"] not in {"decided", "reviewed_unresolved"},
                        "ISSUE_CLOSED", "An ended issue requires a new review issue based on a new question.")
                self.rules.review_issue(self._validation_issue(issue), task, context)
                require(issue["action"] in {"local_reorder", "local_recluster"},
                        "WRONG_ACTION", "This issue does not require local computation.")
                record = {"job_id": "job-" + ident, "issue_id": issue["issue_id"],
                          "action": issue["action"], "status": "queued",
                          "task_contract_id": self.run_id, "config_id": self.dataset.preset["preset_id"],
                          "input_fingerprint": canonical_hash({
                              "dataset": self.dataset.input_fingerprint, "issue": issue,
                              "preset": self.dataset.preset}),
                          "catalogue_revision_at_request": state["catalogue_revision"],
                          "fit_ids": issue["fit_ids"], "comparison_ids": issue["comparison_ids"],
                          "queued_at": stamp, "result_ids": [], "output_validation": "not_available",
                          "source": self.source}
                operation = "record_job"
            elif action == "cancel_job":
                require(payload["job_id"] in state["jobs"], "JOB_NOT_FOUND", "The job does not exist.")
                record = copy.deepcopy(state["jobs"][payload["job_id"]])
                require(record["status"] in {"queued", "running"}, "JOB_STATE", "The job has ended or is being cancelled.")
                record.update(status="cancel_requested", cancel_requested_at=stamp)
                operation = "record_job"
            elif action == "decision":
                record = self._decision(payload, state, task, context, ident, stamp)
                operation = "commit_decision"
                issue = state["issues"][record["issue_id"]]
                done = dict(issue.get("member_processing_states", {}))
                done.update({i: record["processing_state"] for i in record["affected_ids"]})
                completed = all(done.get(i) in {"decided", "reviewed_unresolved"} for i in issue["target_ids"])
                status = ("decided" if all(done.get(i) == "decided" for i in issue["target_ids"])
                          else "reviewed_unresolved") if completed else "under_human_review"
                extra = {"memberships_after": record["memberships_after"],
                         "expected_catalogue_revision": state["catalogue_revision"],
                         "issue_status_after": status}
            elif action == "timer":
                require(payload.get("event") in {"start", "pause", "resume", "stop"},
                        "CLOCK_EVENT", "Unknown timing action.")
                operation = "clock_" + payload["event"]
                extra = {"reason": "explicit_user_action"}
            elif action == "visibility":
                operation = "visibility"
                extra = {"hidden": payload["hidden"]}
            elif action == "checkpoint":
                operation = "save_checkpoint"
                extra = {"checkpoint_id": "checkpoint-" + ident,
                         "reason": payload.get("reason") or "User saved engineering progress"}
            elif action == "restore":
                operation = "restore_checkpoint"
                extra = {"checkpoint_id": payload["checkpoint_id"], "reason": "User explicitly restored progress"}
            elif action == "export":
                dest = self.work_dir / "exports" / ("export-" + ident + ".json")
                dest.parent.mkdir(parents=True, exist_ok=True)
                if not dest.exists():
                    atomic_json(dest, {**self.record_metadata(), "state": state,
                                       "events": self.store.events(self.run_id),
                                       "input_versions": self.dataset.input_versions,
                                       "input_fingerprint": self.dataset.input_fingerprint,
                                       "preset": self.dataset.preset})
                result = {"download_url": self._asset(dest),
                          "catalogue_revision": state["catalogue_revision"],
                          "revision": state["revision"]}
                atomic_json(export_meta, {"request_hash": request_hash, "result": result})
                return result
            else:
                raise ContractViolation("UNKNOWN_ACTION", action)
            data = dict(extra, service_request_hash=request_hash)
            if record is not None:
                record.update(self.record_metadata())
                data["record"] = record
            receipt = self.store.transact(self.run_id, expected_revision=expected_revision,
                                          idempotency_key=idempotency_key, operation=operation, payload=data)
            if action == "decision" and record.get("proposal_id"):
                self.assistant_gateway.link_decision(record["proposal_id"], record)
            if action == "local_job":
                self._cancel[record["job_id"]] = threading.Event()
                try:
                    self._futures[record["job_id"]] = self._executor.submit(self._run_job, record)
                except Exception as exc:
                    failed = copy.deepcopy(record)
                    failed.update(status="failed", error={"code": "WORKER_START_FAILED", "message": str(exc)},
                                  ended_at=utc(), worker_confirmed_stopped=True, output_validation="failed")
                    self._system_job(failed)
            if action == "cancel_job":
                event = self._cancel.get(record["job_id"])
                if event:
                    event.set()
            return {"receipt": receipt, "record": record}

    def _decision(self, payload, state, task, context, ident, stamp):
        proposal_id = payload.get("proposal_id")
        if proposal_id:
            require(self.assistant_gateway is not None, "ASSISTANT_DISABLED",
                    "This run cannot be linked to an assistant proposal.")
            self.assistant_gateway.validate_decision_link(proposal_id, payload, state)
        decision_id = "decision-" + ident
        members = copy.deepcopy(payload["memberships_after"])
        axes_by_item = ({row["item_id"]: row["axes"] for row in payload["item_axes"]}
                        if "item_axes" in payload else
                        {item: payload.get("axes") for item in payload["affected_ids"]})
        for row in members:
            row.setdefault("supporting_decision_ids", [])
            supported = False
            if payload["operation"] != "reject" and payload["processing_state"] != "unfinished":
                for item in set(row["member_ids"]) & set(payload["affected_ids"]):
                    axes = axes_by_item.get(item) or {}
                    if row["parent_type_id"] is None:
                        supported |= (axes.get("family_status") == "assigned" and
                                      axes.get("family_id") == row["local_type_id"])
                    else:
                        supported |= (payload["processing_state"] == "decided" and
                                      axes.get("form_status") in {"main", "variant"} and
                                      axes.get("leaf_id") == row["local_type_id"])
            if supported:
                if decision_id not in row["supporting_decision_ids"]:
                    row["supporting_decision_ids"].append(decision_id)
        draft = {
            "schema_version": "0.1.0", "record_kind": "engineering_simulation",
            "execution_enabled": False, "decision_id": decision_id, "task_contract_id": self.run_id,
            "issue_id": payload["issue_id"], "proposal_id": None,
            "actor": {"actor_id": context["actor_id"], "role": "operator"},
            "observation_ids": payload["observation_ids"], "affected_ids": payload["affected_ids"],
            "operation": payload["operation"], "axes": payload.get("axes"),
            "processing_state": payload["processing_state"], "reason": payload["reason"],
            "expected_catalogue_revision": state["catalogue_revision"],
            "catalogue_revision_after": state["catalogue_revision"] + 1,
            "memberships_before": state["memberships"], "memberships_after": members,
            "input_versions": self.dataset.input_versions, "recorded_at": stamp,
            "idempotency_key": decision_id, "elapsed_person_seconds": None,
        }
        if "item_axes" in payload:
            draft["item_axes"] = payload["item_axes"]
        self.rules.human_decision(draft, task, context)
        draft.update(schema_version="0.2.0", record_kind="engineering_runtime",
                     execution_enabled=True, source=self.source, experiment_enabled=False)
        draft["proposal_id"] = proposal_id
        draft.pop("elapsed_person_seconds")
        draft["clock_status"] = state["clock"]["status"]
        draft["timing_complete"] = state["clock"]["timing_complete"] and state["clock"]["status"] == "running"
        draft["engineering_elapsed_seconds"] = (
            state["clock"]["elapsed_seconds"] if state["clock"]["status"] != "not_started" else None)
        draft.update(self.record_metadata())
        if self.project_contract.get("purpose") == "daily_annotation":
            draft["work_elapsed_seconds"] = draft.pop("engineering_elapsed_seconds")
        return draft
