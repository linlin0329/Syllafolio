"""D3只读声学评估：冻结当前人工目录和来源，异步计算并保留全部审计。

评估结果只能提供复核线索；本模块从不修改成员、类型定义、观察或人工完成记录。
来源登记和评估作业独立于标签检查点，恢复标签后重新判断报告适用性。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
from pathlib import Path
import threading
import uuid

from .annotation_assistant_contracts import canonical_hash, require
from .annotation_manual_protocol import completion_report, definition_fingerprint, item_projection
from .annotation_service import atomic_json, utc
from .annotation_source_registry import (
    build_index, initial_registry, effective_registry, prepare_registration,
    projection as source_projection, apply_registration, LIMITATION,
)

ACTIONS = {"source_validate", "source_register", "assessment_start", "assessment_cancel"}
ACTIVE = {"queued", "running"}
TERMINAL = {"completed", "not_estimable", "failed", "cancelled", "interrupted"}
SCHEMA = "annotation-label-assessment-jobs-v1"


def assessment_state(state):
    return state.get("label_assessment", {"schema_version": SCHEMA, "jobs": {}, "history": []})


def apply_assessment(state, payload):
    """所有写入经既有SQLite事务；不触碰标签字段或目录修订号。"""
    action, record = payload["assessment_action"], payload["record"]
    if action == "source_register":
        apply_registration(state, record)
        return
    data = state.setdefault("label_assessment", {"schema_version": SCHEMA, "jobs": {}, "history": []})
    ident = record["job_id"]
    if action == "start":
        require(not any(job["status"] in ACTIVE for job in data["jobs"].values()), "ASSESSMENT_BUSY", "This run already has an active assessment.")
        require(ident not in data["jobs"], "ASSESSMENT_ID", "The assessment ID already exists.")
        require(record["status"] == "queued", "ASSESSMENT_STATE", "An assessment must start in the queued state.")
        data["jobs"][ident] = copy.deepcopy(record)
    else:
        require(ident in data["jobs"], "ASSESSMENT_NOT_FOUND", "The assessment job does not exist.")
        job = data["jobs"][ident]
        require(job["status"] in ACTIVE, "ASSESSMENT_FINISHED", "The assessment has already ended.")
        if action == "cancel":
            job.update(cancel_requested=True, cancel_reason=record["reason"], cancel_requested_at=record["recorded_at"])
        elif action == "update":
            require(record["status"] in ACTIVE | TERMINAL and not (job["status"] == "running" and record["status"] == "queued"),
                    "ASSESSMENT_STATE", "The assessment state transition is invalid.")
            job.update(copy.deepcopy(record))
        else:
            require(False, "ASSESSMENT_ACTION", "Unknown assessment action.")
    data["history"].append({"job_id": ident, "action": action, "status": data["jobs"][ident]["status"],
                             "recorded_at": record["recorded_at"], "progress": copy.deepcopy(record.get("progress")),
                             "error": copy.deepcopy(record.get("error")), "reason": record.get("reason")})


class LabelAssessmentService:
    def __init__(self, owner):
        self.owner = owner
        self.index = build_index(owner.dataset)
        self.initial = initial_registry(owner.dataset, self.index)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="annotation-d3")
        self.cancel = {}
        self.futures = {}
        self.runtime_progress = {}
        from . import annotation_label_assessment
        paths = [Path(__file__), Path(annotation_label_assessment.__file__)] + [Path(__file__).with_name(name) for name in (
            "annotation_source_registry.py", "annotation_manual_protocol.py", "annotation_manual_service.py", "annotation_decision_history.py",
            "annotation_batch_review.py", "annotation_acoustics.py", "annotation_stage_a.py", "ei_b_assistance.py")]
        self.code_identity = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
        self.numeric_identity = [{"item_id": ident, **{key: copy.deepcopy(owner.dataset._items[ident].get(key)) for key in (
            "feature_id", "feature_row_index", "source_clip_id", "source_frame_start", "source_frame_stop",
            "sample_rate_hz", "parent_audio_sha256")}} for ident in owner.dataset.item_ids]
        self.numeric_blocking = ([] if owner.dataset.logmel.shape[1:] == (2048,) else [{
            "code": "RAW_FEATURES_UNAVAILABLE", "message": "This run lacks the raw 2048D log-mel input required by the fixed preset. A support report is retained; old fused representations or fitted results are not reused."}])

    def close(self):
        for event in self.cancel.values():
            event.set()
        self.executor.shutdown(wait=True, cancel_futures=False)

    def recover(self):
        for job in assessment_state(self.owner.store.snapshot(self.owner.run_id))["jobs"].values():
            if job["status"] in ACTIVE:
                self._update(job["job_id"], status="interrupted", ended_at=utc(),
                             error={"code": "PROCESS_INTERRUPTED", "message": "The previous service ended without a complete report; explicitly start a new assessment if needed."})

    def _context(self, state, report=None, projected=None):
        from .annotation_label_assessment import get_preset, summarize_support
        report = report if report is not None else completion_report(state)
        projected = projected if projected is not None else item_projection(state)
        registry = effective_registry(state, self.initial)
        scope = state.get("catalogue_scope")
        scope_ids = set(scope["scope_ids"] if scope else [])
        families = {r["local_type_id"]: r for r in state["memberships"] if r["parent_type_id"] is None}
        membership = {item: fid for fid, row in families.items() for item in row["member_ids"]}
        items = []
        for ident in self.owner.dataset.item_ids:
            current = projected[ident]
            family = membership.get(ident)
            if current.get("batch_disposition") == "discard":
                label_status = "excluded"
            elif family is not None and current["processing_state"] in {"decided", "reviewed_unresolved"} and not current.get("closure_confirmation_required"):
                label_status = "confirmed"
            else:
                label_status = "unresolved"
            items.append({"item_id": ident, "family_id": family, "in_scope": ident in scope_ids,
                          "label_status": label_status,
                          "source_group_id": registry["mappings"][ident]["raw_recording_id"]})
        support = summarize_support(items, family_ids=sorted(families))
        completion_id = state.get("current_completion_id")
        confirmed = state.get("completion_records", {}).get(completion_id)
        blocking = copy.deepcopy(report["missing"])
        if not confirmed or not scope or confirmed.get("scope", {}).get("scope_id") != scope["scope_id"]:
            blocking.append({"code": "HUMAN_COMPLETION_REQUIRED", "message": "Review the scope and submit human completion confirmation before running quantitative assessment."})
        binding = {"input_fingerprint": self.owner.dataset.input_fingerprint,
            "input_versions": copy.deepcopy(self.owner.dataset.input_versions),
            "input_artifacts": copy.deepcopy(self.owner.dataset._provenance),
            "numeric_row_identity": self.numeric_identity,
            "numeric_input_blocking": self.numeric_blocking,
            "catalogue_revision": state["catalogue_revision"], "scope": copy.deepcopy(scope),
            "items": items, "memberships": copy.deepcopy(state["memberships"]),
            "definitions": {key: {"definition_fingerprint": definition_fingerprint(card),
                                   "membership_fingerprint": card.get("membership_fingerprint"),
                                   "card_id": card["card_id"], "status": card["definition_status"]}
                            for key, card in state.get("type_cards", {}).items()},
            "completion_id": completion_id, "completion": copy.deepcopy(confirmed),
            "quality_gates": {"can_submit": report["can_submit"], "missing": copy.deepcopy(report["missing"])},
            "source_version": registry["version"], "source_mapping": copy.deepcopy(registry["mappings"]),
            "source_identity_fingerprint": self.index["fingerprint"],
            "preset": get_preset(), "code_identity": self.code_identity,
            "source_limitation": LIMITATION, "assessment_level": "family"}
        return binding, support, blocking

    @staticmethod
    def _stale(prior, current):
        fields = ("input_fingerprint", "input_versions", "input_artifacts", "numeric_row_identity", "numeric_input_blocking", "catalogue_revision", "scope", "items", "memberships",
                  "definitions", "completion_id", "completion", "quality_gates", "source_version", "source_mapping",
                  "source_identity_fingerprint", "preset", "code_identity")
        return [field for field in fields if prior.get(field) != current.get(field)]

    def snapshot(self, state, completion=None, projected=None):
        binding, support, blocking = self._context(state, completion, projected)
        data = assessment_state(state)
        # SQLite规范JSON按key排序；随机job ID的字典顺序不能代表运行顺序。
        start_order = {entry["job_id"]: index for index, entry in enumerate(data["history"]) if entry["action"] == "start"}
        ordered_jobs = sorted(data["jobs"].values(), key=lambda job: (
            start_order.get(job["job_id"], -1), job.get("created_at", ""), job["job_id"]))
        jobs = []
        for job in ordered_jobs:
            row = {key: copy.deepcopy(value) for key, value in job.items() if key not in {"frozen_snapshot", "report"}}
            progress = self.runtime_progress.get(job["job_id"])
            if job["status"] in ACTIVE and progress is not None:
                row["progress"] = copy.deepcopy(progress)
            if job.get("report"):
                # 每秒轮询无需重复全部折内2k维缩放数组；完整审计只在不变的任务资产中。
                row["report"] = {key: copy.deepcopy(value) for key, value in job["report"].items() if key != "audit"}
                audit = job["report"].get("audit", {})
                row["report"]["audit"] = {key: copy.deepcopy(audit.get(key)) for key in ("packages", "fit_scope", "fits_started")}
                row["report"]["audit"]["fold_count"] = len(audit.get("folds", []))
                row["report"]["audit"]["full_audit_in_report_asset"] = True
            stale = self._stale(job["frozen_snapshot"], binding)
            row.update(stale_for_current=bool(stale), stale_reasons=stale)
            jobs.append(row)
        active = [job for job in jobs if job["status"] in ACTIVE]
        reports = [job for job in jobs if job.get("report") is not None]
        latest = jobs[-1] if jobs and jobs[-1].get("report") is not None else None
        def report_projection(job):
            return ({k: copy.deepcopy(job.get(k)) for k in
                ("job_id", "status", "stale_for_current", "stale_reasons", "report_url", "report")} if job else None)
        source = source_projection(effective_registry(state, self.initial))
        assessment = {"implemented": True, "schema_version": SCHEMA, "preset": copy.deepcopy(binding["preset"]),
                      **support, "can_start": not blocking and not active and not state["deadline_locked"],
                      "blocking_reasons": blocking + ([{"code": "ASSESSMENT_BUSY", "message": "This run already has an active assessment."}] if active else [])
                          + ([{"code": "DEADLINE_LOCKED", "message": "The run deadline has passed; a new assessment cannot be started."}] if state["deadline_locked"] else []),
                      "current_fingerprint": canonical_hash(binding), "jobs": jobs,
                      "numeric_input_reasons": copy.deepcopy(self.numeric_blocking),
                      "current_job_id": active[0]["job_id"] if active else None,
                      "latest_job_id": jobs[-1]["job_id"] if jobs else None,
                      "latest_report": report_projection(latest),
                      "last_available_report": report_projection(reports[-1] if reports else None),
                      "history": copy.deepcopy(data["history"]),
                      "limitations": [LIMITATION, "This assesses only the acoustic separability of current human family labels under the declared original-recording groups. It does not establish type truth, individual independence or generalization validity.",
                                      "A person must decide whether a finding warrants directed review. Findings do not automatically change labels, reopen reviews or tune parameters to improve scores."],
                      "catalogue_publication_bundle": False}
        return source, assessment

    def execute(self, action, payload, *, expected_revision, idempotency_key):
        owner = self.owner
        require(isinstance(payload, dict), "INVALID_INPUT", "The payload must be an object.")
        require(isinstance(idempotency_key, str) and 0 < len(idempotency_key) <= 160, "IDEMPOTENCY_REQUIRED", "A request identifier is required.")
        request_hash = canonical_hash({"action": action, "payload": payload, "expected_revision": expected_revision})
        with owner._lock:
            prior = owner.store.lookup_event(owner.run_id, idempotency_key)
            if prior:
                require(prior["payload"].get("service_request_hash") == request_hash, "IDEMPOTENCY_CONFLICT", "The same request identifier was used with different parameters.")
                return {"receipt": prior["receipt"], "record": prior["payload"]["record"], "replayed": True}
            owner.command_guard(action, payload)
            state = owner.store.snapshot(owner.run_id)
            valid_revision = (type(expected_revision) is int and
                (0 <= expected_revision <= state["revision"] if action == "assessment_cancel" else state["revision"] == expected_revision))
            require(valid_revision, "REVISION_CONFLICT", "The state has changed; refresh before submitting.")
            ident = hashlib.sha256((owner.run_id + ":" + idempotency_key).encode()).hexdigest()[:20]
            meta = {"recorded_at": utc(), "operator_id": state["contract"]["operator_id"],
                    "source": owner.source, "experiment_enabled": False}
            meta.update(owner.record_metadata())
            if action in {"source_validate", "source_register"}:
                record, preview = prepare_registration(effective_registry(state, self.initial), self.index, payload,
                                                       record_id="source-" + ident, meta=meta)
                if action == "source_validate":
                    return preview
                require(preview["valid"], "SOURCE_CONFLICT", "Source declarations conflict; check the related items marked in the mapping first.")
                subaction = action
            elif action == "assessment_start":
                require(set(payload) == {"reason"} and isinstance(payload["reason"], str) and payload["reason"].strip(),
                        "ASSESSMENT_REASON", "Record the actual reason for starting this assessment.")
                binding, _support, blocking = self._context(state)
                require(not blocking, "ASSESSMENT_NOT_READY", "Review the scope, type definitions and quality requirements, then submit human completion confirmation.")
                require(not any(job["status"] in ACTIVE for job in assessment_state(state)["jobs"].values()),
                        "ASSESSMENT_BUSY", "This run already has an active assessment.")
                record = dict(meta, job_id="assessment-" + ident, status="queued", created_at=utc(), reason=payload["reason"],
                              frozen_snapshot=binding, snapshot_fingerprint=canonical_hash(binding), cancel_requested=False,
                              progress={"stage": "queued", "message": "Human catalogue, sources and fixed assessment preset frozen", "completed_folds": 0, "total_folds": 15})
                subaction = "start"
            else:
                require(set(payload) == {"job_id", "reason"} and isinstance(payload["reason"], str) and payload["reason"].strip(),
                        "ASSESSMENT_REASON", "Cancellation requires a job ID and a reason.")
                require(isinstance(payload["job_id"], str) and payload["job_id"] in assessment_state(state)["jobs"],
                        "ASSESSMENT_NOT_FOUND", "The assessment job does not exist.")
                record = dict(meta, job_id=payload["job_id"], reason=payload["reason"],
                              requested_state_revision=expected_revision, applied_state_revision=state["revision"])
                subaction = "cancel"
            receipt = owner.store.transact(owner.run_id, expected_revision=state["revision"], idempotency_key=idempotency_key,
                operation="record_job" if subaction == "cancel" else "manual_assessment",
                payload={"assessment_event": True, "assessment_action": subaction, "record": record, "service_request_hash": request_hash})
            if subaction == "start":
                event = threading.Event()
                self.cancel[record["job_id"]] = event
                try:
                    self.futures[record["job_id"]] = self.executor.submit(self._worker, copy.deepcopy(record), event)
                except Exception as exc:
                    self._update(record["job_id"], status="failed", ended_at=utc(), error={"code": "WORKER_START_FAILED", "message": str(exc)})
            elif subaction == "cancel":
                event = self.cancel.get(record["job_id"])
                if event is not None:
                    event.set()
            return {"receipt": receipt, "record": record}

    def _update(self, ident, **fields):
        owner = self.owner
        with owner._lock:
            state = owner.store.snapshot(owner.run_id)
            job = assessment_state(state)["jobs"][ident]
            if job["status"] not in ACTIVE:
                return
            record = dict(job_id=ident, recorded_at=utc(), status=job["status"])
            record.update(owner.record_metadata())
            record.update(fields)
            owner.store.transact(owner.run_id, expected_revision=state["revision"],
                idempotency_key="assessment-worker:" + uuid.uuid4().hex, operation="record_job",
                payload={"assessment_event": True, "assessment_action": "update", "record": record})
            if record["status"] in TERMINAL:
                self.runtime_progress.pop(ident, None)

    def _worker(self, job, cancel):
        from .annotation_label_assessment import assess_family_labels
        ident = job["job_id"]
        try:
            if cancel.is_set():
                self._update(ident, status="cancelled", ended_at=utc())
                return
            self._update(ident, status="running", started_at=utc())
            frozen = job["frozen_snapshot"]
            dataset = self.owner.dataset
            rows = [dataset._items[item["item_id"]]["feature_row_index"] for item in frozen["items"]]
            def progress(value):
                # 细粒度计算进度不是人工目录修订；不让高频轮询/折迭代阻塞人工写入。
                with self.owner._lock:
                    self.runtime_progress[ident] = copy.deepcopy(value)
            result = assess_family_labels(dataset.features[rows], dataset.logmel[rows], frozen["items"],
                family_ids=sorted(row["local_type_id"] for row in frozen["memberships"] if row["parent_type_id"] is None),
                blocking_reasons=frozen["numeric_input_blocking"], progress=progress, check_cancelled=cancel.is_set)
            if cancel.is_set():
                self._update(ident, status="cancelled", ended_at=utc())
                return
            require(result.get("status") in {"completed", "not_estimable"}, "ASSESSMENT_RESULT", "The numeric assessment did not return a complete state.")
            document = {"schema_version": "annotation-label-assessment-report-v1", "job_id": ident,
                        "project_identity": self.owner.record_metadata(),
                        "created_at": utc(), "snapshot_fingerprint": job["snapshot_fingerprint"],
                        "frozen_snapshot": frozen, "result": result, "labels_changed": False,
                        "observations_created": False, "human_completion_changed": False,
                        "catalogue_publication_bundle": False}
            with self.owner._lock:
                if cancel.is_set():
                    self._update(ident, status="cancelled", ended_at=utc())
                    return
                folder = self.owner.work_dir / "label_assessments" / ident
                folder.mkdir(parents=True, exist_ok=False)
                path = folder / "report.json"
                atomic_json(path, document)
                url = self.owner._asset(path)
                self._update(ident, status=result["status"], ended_at=utc(), report=result, report_url=url,
                             report_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                             progress={"stage": result["status"], "message": "Report saved; applicable only to its frozen snapshot.",
                                       "completed_folds": 15 if result["status"] == "completed" else 0, "total_folds": 15})
        except Exception as exc:
            cancelled = cancel.is_set() or getattr(exc, "code", None) == "CANCELLED"
            self._update(ident, status="cancelled" if cancelled else "failed", ended_at=utc(),
                         error={"code": "CANCELLED" if cancelled else getattr(exc, "code", "ASSESSMENT_FAILED"), "message": str(exc)})
