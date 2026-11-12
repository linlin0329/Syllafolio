'Versioned catalogue delivery; freeze human records without classification or new acoustic assessment.'
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import threading
import uuid
import zipfile

import numpy as np
import soundfile as sf

from .annotation_assistant_contracts import canonical_hash, require
from .annotation_manual_protocol import completion_report, item_projection
from .annotation_service import atomic_json, utc
from .annotation_spectrogram import DEFAULTS, VERSION, render
from .annotation_english import system_error

ACTIONS = {"catalogue_export_start", "catalogue_export_cancel"}
ACTIVE = {"queued", "running"}
TERMINAL = {"completed", "failed", "cancelled", "interrupted"}
SCHEMA = "annotation-catalogue-export-jobs-v1"


def export_state(state):
    return state.get("catalogue_exports", {"schema_version": SCHEMA, "jobs": {}, "history": []})


def apply_export(state, payload):
    'Export lifecycle only; independent of catalogue, human completion and label checkpoints.'
    data = state.setdefault("catalogue_exports", {"schema_version": SCHEMA, "jobs": {}, "history": []})
    action, record = payload["catalogue_export_action"], payload["record"]
    ident = record["job_id"]
    if action == "start":
        require(not any(row["status"] in ACTIVE for row in data["jobs"].values()), "EXPORT_BUSY", 'This task already has an active export')
        require(ident not in data["jobs"] and record["status"] == "queued", "EXPORT_STATE", 'An export must start queued with a new ID')
        data["jobs"][ident] = copy.deepcopy(record)
    else:
        require(ident in data["jobs"], "EXPORT_NOT_FOUND", 'Export job not found')
        job = data["jobs"][ident]
        require(job["status"] in ACTIVE, "EXPORT_FINISHED", 'This export has already ended')
        if action == "cancel":
            job.update(cancel_requested=True, cancel_reason=record["reason"], cancel_requested_at=record["recorded_at"])
        elif action == "update":
            require(record["status"] in ACTIVE | TERMINAL and not (job["status"] == "running" and record["status"] == "queued"),
                    "EXPORT_STATE", 'Invalid export state transition')
            job.update(copy.deepcopy(record))
        else:
            require(False, "EXPORT_ACTION", 'Invalid export action')
    data["history"].append({"job_id": ident, "action": action, "status": data["jobs"][ident]["status"],
                             "recorded_at": record["recorded_at"], "error": copy.deepcopy(record.get("error"))})


def _sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _ordered_jobs(data):
    order = {row["job_id"]: i for i, row in enumerate(data["history"]) if row["action"] == "start"}
    return sorted(data["jobs"].values(), key=lambda row: (order.get(row["job_id"], -1), row["created_at"], row["job_id"]))


def _audit_events(events):
    'Preserve human and source audits; reference earlier exports to avoid recursive snapshots.'
    output = []
    for event in events:
        row = copy.deepcopy(event)
        payload = row.get("payload", {})
        if payload.get("catalogue_export_event") is True or payload.get("assessment_event") is True and payload.get("assessment_action") != "source_register":
            original_hash = canonical_hash(payload)
            original_serialized = row.get("payload_json")
            record = payload.get("record", {})
            payload["record"] = {key: value for key, value in record.items()
                                 if key not in {"document", "audit", "frozen_context", "assessment_document", "asset_map", "frozen_snapshot", "report"}}
            payload["portable_event_policy"] = "Computational/export snapshot omitted; original payload hash and job/report fingerprints identify the unchanged SQLite event. Human/source events are unchanged."
            payload["original_payload_sha256"] = original_hash
            if original_serialized is not None:
                payload["original_payload_json_sha256"] = hashlib.sha256(original_serialized.encode()).hexdigest()
                row["payload_json"] = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            row["portable_payload_transformed"] = True
        output.append(row)
    return output


class CatalogueExportService:
    def __init__(self, owner, preset_path=None):
        self.owner = owner
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="annotation-d4")
        self.cancel = {}
        self.futures = {}
        self.runtime_progress = {}
        from .annotation_acoustics import DEFAULT_PRESET
        self.input_paths = {"manifest_sha256": owner.dataset_dir / "manifest.json",
                            "features_sha256": owner.dataset_dir / "features.npy",
                            "logmel_sha256": owner.dataset_dir / "logmel.npy",
                            "preset_sha256": Path(preset_path or DEFAULT_PRESET)}
        self.code_identity = {name: _sha(Path(__file__).with_name(name)) for name in
            ("annotation_catalogue_export.py", "annotation_catalogue_report.py", "annotation_spectrogram.py", "annotation_decision_history.py")}

    def close(self):
        for event in self.cancel.values():
            event.set()
        self.executor.shutdown(wait=True, cancel_futures=False)

    def recover(self):
        for row in export_state(self.owner.store.snapshot(self.owner.run_id))["jobs"].values():
            if row["status"] == "completed":
                self._discard_publication_journal(row["job_id"])
                continue
            cleanup = self._cleanup_unpublished(row["job_id"], remove_target=True, verify_target=True)
            if row["status"] in ACTIVE:
                self._update(row["job_id"], status="interrupted", ended_at=utc(),
                             error={"code": "PROCESS_INTERRUPTED", "message": 'The export did not finish before the service restarted; explicitly export a new version.',
                                    "cleanup_required": bool(cleanup), "cleanup_errors": cleanup})
            elif cleanup:
                # 不返回仍可能从磁盘注册表恢复未完成资产的服务；原记录保持不变。
                require(False, "EXPORT_RECOVERY_CLEANUP_FAILED", "; ".join(cleanup))

    def _publication_paths(self, ident):
        require(isinstance(ident, str) and re.fullmatch(r"catalogue-[a-f0-9]{20}", ident),
                "EXPORT_IDENTITY", 'Invalid export job identity')
        work = self.owner.work_dir.resolve()
        root = work / "catalogue_exports"
        staging, target = root / ("." + ident + ".partial"), root / ident
        journal = work / ("." + ident + ".publication.json")
        backup = work / ("." + ident + ".assets.rollback")
        for path in (root, staging, target, journal, backup):
            require(path.resolve().is_relative_to(work) and not path.is_symlink(),
                    "EXPORT_PATH", 'The publication path must remain inside this run and cannot use symbolic links')
        return root, staging, target, journal, backup

    def _begin_publication(self, ident):
        _root, _staging, _target, journal, backup = self._publication_paths(ident)
        require(not journal.exists() and not backup.exists(), "EXPORT_IMMUTABLE", 'An unfinished publication record exists; reopen the project to recover it')
        existed = self.owner.assets_path.exists()
        if existed:
            require(not self.owner.assets_path.is_symlink(), "EXPORT_PATH", 'The asset registry cannot point outside this run')
            with backup.open("xb") as stream:
                stream.write(self.owner.assets_path.read_bytes())
        atomic_json(journal, {"schema_version": "annotation-export-publication-v1",
                             "job_id": ident, "registry_existed": existed})

    def _discard_publication_journal(self, ident):
        'A sidecar after completion only supports retryable cleanup; the successful package stays unchanged.'
        _root, _staging, _target, journal, backup = self._publication_paths(ident)
        errors = []
        for path in (journal, backup, journal.with_suffix(journal.suffix + ".writing")):
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                errors.append(system_error(exc))
        return errors

    def _cleanup_unpublished(self, ident, *, remove_target, verify_target=False):
        'Remove this job handles before its directory; restore registry preimages under the same owner lock.'
        with self.owner._lock:
            try:
                _root, staging, target, journal, backup = self._publication_paths(ident)
            except Exception as exc:
                return [system_error(exc)]
            prefix = "catalogue_exports/" + ident + "/"
            revoked = [key for key, row in self.owner.assets.items()
                       if isinstance(row, dict) and isinstance(row.get("path"), str)
                       and row["path"].replace("\\", "/").startswith(prefix)]
            for key in revoked:
                self.owner.assets.pop(key, None)
            errors, registry_restored = [], False
            try:
                saved = json.loads(journal.read_text()) if journal.is_file() else None
                if saved is not None:
                    require(saved.get("schema_version") == "annotation-export-publication-v1"
                            and saved.get("job_id") == ident and type(saved.get("registry_existed")) is bool,
                            "EXPORT_PUBLICATION", 'Publication recovery record identity mismatch')
                    if saved["registry_existed"] and backup.is_file():
                        # 前像与注册表同卷；不依赖再次写入可能已满的磁盘。
                        before = json.loads(backup.read_text())
                        if before == self.owner.assets:
                            os.replace(backup, self.owner.assets_path)
                        else:
                            # 旧回滚曾失败后可能已有别的成功作业；不能用旧前像抹掉它们。
                            atomic_json(self.owner.assets_path, self.owner.assets)
                    elif not saved["registry_existed"]:
                        if self.owner.assets:
                            atomic_json(self.owner.assets_path, self.owner.assets)
                        else:
                            self.owner.assets_path.unlink(missing_ok=True)
                    else:
                        atomic_json(self.owner.assets_path, self.owner.assets)
                    registry_restored = True
                elif revoked:
                    atomic_json(self.owner.assets_path, self.owner.assets)
                    registry_restored = True
                else:
                    registry_restored = True
            except Exception as exc:
                errors.append("Asset registry rollback: " + system_error(exc))
            # 即使清理失败，内存句柄已经撤回，不会从media_path公开本次半包。
            for path in (staging, target) if remove_target else (staging,):
                try:
                    if not path.exists():
                        continue
                    require(path.resolve().is_relative_to(self.owner.work_dir.resolve()) and not path.is_symlink(),
                            "EXPORT_PATH", 'An export path outside this run cannot be cleaned up')
                    if path == target and verify_target:
                        manifest = json.loads((target / "manifest.json").read_text())
                        require(manifest.get("export_id") == ident, "EXPORT_PUBLICATION", 'The unfinished directory has a different identity; its asset handles were removed, but the directory was preserved for inspection')
                    shutil.rmtree(path)
                except Exception as exc:
                    errors.append("Unpublished directory cleanup: " + system_error(exc))
            if registry_restored:
                errors.extend(self._discard_publication_journal(ident))
            return errors

    def _effective_decision_ids(self, state):
        'Use the same persistent event interpretation as the main workspace and batch inheritance.'
        return self.owner.store.effective_decision_ids(state)

    def _context(self, state):
        from .annotation_batch_review import blank, family_title
        report = completion_report(state)
        projected = item_projection(state)
        sources, assessment = self.owner.assessment.snapshot(state, report, projected)
        binding, _support, human_blocking = self.owner.assessment._context(state, report, projected)
        scope = copy.deepcopy(state.get("catalogue_scope") or {})
        scope.update(declared=bool(state.get("catalogue_scope")), scope_ids=list(report["scope_ids"]),
                     total_task_items=len(self.owner.dataset.item_ids))
        selected = set(scope["scope_ids"])
        families = {row["local_type_id"]: row for row in state["memberships"] if row["parent_type_id"] is None}
        leaves = {row["local_type_id"]: row for row in state["memberships"] if row["parent_type_id"] is not None}
        assignment = state.get("batch_review", {}).get("assignments", {})
        family_map = {item: key for key, row in families.items() for item in row["member_ids"]}
        leaf_map = {item: key for key, row in leaves.items() for item in row["member_ids"]}
        decisions = list(state["decisions"].values()) if isinstance(state["decisions"], dict) else state["decisions"]
        effective_decisions = self._effective_decision_ids(state)
        sources_by_id = {row["item_id"]: row for row in sources["mappings"]}
        items = []
        for media in self.owner.dataset.public_items():
            ident = media["item_id"]
            current = projected[ident]
            saved = assignment.get(ident, {})
            refinement = saved.get("legacy_refinement", {})
            candidates = [row for row in decisions if row["decision_id"] in effective_decisions and ident in row.get("affected_ids", [])
                          and (row.get("issue_id") in current.get("current_issue_ids", []) or row["decision_id"] == refinement.get("decision_id"))]
            decision = max(candidates, key=lambda row: (row.get("catalogue_revision_after", -1), row["decision_id"]), default={})
            decision_axes = next((row["axes"] for row in decision.get("item_axes", []) if row["item_id"] == ident), decision.get("axes"))
            # 只报告已有三轴；批量分配未判定末级时保持not_assessed，不凭名称推断主体/变体。
            decision_current = bool(decision and decision.get("issue_id") in current.get("current_issue_ids", []))
            if refinement.get("decision_id") == decision.get("decision_id"):
                decision_current = True
            if decision_axes:
                assigned = decision_axes.get("family_status") == "assigned"
                same_family = decision_axes.get("family_id") == family_map.get(ident) and (assigned == (ident in family_map))
                same_leaf = decision_axes.get("leaf_id") == leaf_map.get(ident)
                # 后续批量改归属不一定删除旧fine issue；旧三轴只保留在审计里，不能贴到新family/leaf上。
                decision_current = decision_current and same_family and same_leaf
            axes = copy.deepcopy(decision_axes) if decision_current and decision_axes else None
            disposition = current.get("batch_disposition")
            if current["processing_state"] == "unfinished" or current.get("closure_confirmation_required"):
                category = "unreviewed"
            elif disposition == "discard":
                category = "excluded"
            elif ident in family_map:
                category = "assigned"
            else:
                category = "unresolved"
            disposition_reason = (saved.get("reason", "") if saved.get("submission_contract_version") == "compact_v1"
                                  else saved.get("reason") or saved.get("note") or "")
            # 只投影实际保存的出处；旧记录缺字段时不能补写为新版“未提供”。
            reason_metadata = {key: copy.deepcopy(saved[key]) for key in
                               ("reason_code", "reason_source", "submission_contract_version") if key in saved}
            if saved.get("reason_code"):
                from .annotation_batch_review import COMPACT_REASON_CODES
                reason_metadata["reason_code_label"] = COMPACT_REASON_CODES.get(saved.get("disposition"), {}).get(saved["reason_code"])
            axes_reason = (decision.get("reason") or decision.get("rationale") or "") if decision_current else ""
            reason = axes_reason or disposition_reason
            items.append({**copy.deepcopy(media), "in_scope": ident in selected, "family_id": family_map.get(ident),
                "leaf_id": leaf_map.get(ident), "processing_state": current["processing_state"],
                "disposition": disposition, "category": category, "axes": axes,
                "form_status": (axes or {}).get("form_status", saved.get("form_status", "not_assessed")),
                "reason": reason, "disposition_reason": disposition_reason, "axes_reason": axes_reason,
                **reason_metadata,
                "source_record_id": saved.get("record_id"), "decision_id": decision.get("decision_id") if decision_current else None,
                "review_projection": copy.deepcopy(current), "saved_disposition": copy.deepcopy(saved),
                "raw_recording_id": sources_by_id[ident]["raw_recording_id"],
                "source_status": sources_by_id[ident]["status"], "source_evidence": sources_by_id[ident]["evidence"]})
        media_ids = {ident for card in state.get("type_cards", {}).values()
                     for field in ("representative_ids", "atypical_ids", "counterexample_ids") for ident in card.get(field, [])}
        counts = {"n_task": len(items), "n_scope": len(selected), "n_out_of_scope": len(items) - len(selected),
                  "families": sum(bool(selected & set(row["member_ids"])) for row in families.values()),
                  "leaves": sum(bool(selected & set(row["member_ids"])) for row in leaves.values()),
                  "catalogue_families": len(families), "catalogue_leaves": len(leaves), "media_examples": len(media_ids)}
        counts.update({name: sum(row["in_scope"] and row["category"] == name for row in items)
                       for name in ("assigned", "unresolved", "excluded", "unreviewed")})
        completion_id = state.get("current_completion_id")
        completion = copy.deepcopy(state.get("completion_records", {}).get(completion_id))
        options = []
        for job in assessment["jobs"]:
            eligible = job["status"] in {"completed", "not_estimable"} and bool(job.get("report")) and not job["stale_for_current"]
            reasons = [] if eligible else (['The selected assessment is stale'] if job["stale_for_current"] else ['The selected run has no complete internal assessment report'])
            options.append({"job_id": job["job_id"], "status": job["status"], "created_at": job["created_at"],
                "stale_for_current": job["stale_for_current"], "stale_reasons": job["stale_reasons"],
                "eligible_for_complete": eligible, "reasons": reasons})
        # 生命周期、显示与计时不影响导出适用性；完整人工依据与定义卡仍受版本保护。
        # The read-only batch projection supplies blank() before the first
        # organization. Use the same default without upgrading stored legacy data.
        protocol_version = state.get("batch_review", blank()).get("protocol_version", "0.4")
        context = {"assessment_binding": binding, "item_records": items, "type_cards": copy.deepcopy(state.get("type_cards", {})),
                   "protocol_version": protocol_version,
                   "export_code_identity": self.code_identity, "display": {"renderer": VERSION, "settings": dict(DEFAULTS)}}
        titles = {key: family_title(state, key) for key in families if key not in state.get("type_cards", {})}
        titles = {key: title for key, title in titles.items() if title != key}
        if titles:
            context["family_titles"] = titles
        return context, {"scope": scope, "counts": counts, "items": items, "source_registry": sources,
                         "completion": {"report": report, "record": completion}, "assessment_options": options,
                         "human_completion_current": not human_blocking, "completion_id": completion_id,
                         "human_blocking": human_blocking}

    def snapshot(self, state):
        context, preview = self._context(state)
        fingerprint = canonical_hash(context)
        data = export_state(state)
        jobs = []
        for original in _ordered_jobs(data):
            row = {key: copy.deepcopy(value) for key, value in original.items()
                   if key not in {"frozen_context", "document", "audit", "assessment_document", "asset_map"}}
            frozen = original["frozen_context"]
            changed = [key for key in dict.fromkeys([*frozen, *context]) if frozen.get(key) != context.get(key)]
            row.update(stale_for_current=bool(changed), stale_reasons=changed)
            if original["status"] in ACTIVE and original["job_id"] in self.runtime_progress:
                row["progress"] = copy.deepcopy(self.runtime_progress[original["job_id"]])
            jobs.append(row)
        active = [row for row in jobs if row["status"] in ACTIVE]
        blockers = copy.deepcopy(preview["human_blocking"])
        if not any(row["eligible_for_complete"] for row in preview["assessment_options"]):
            blockers.append({"code": "CURRENT_ASSESSMENT_REQUIRED", "message": 'Complete delivery requires a full internal assessment report for the current version; an explicit not-estimable report can be delivered with its limitations.'})
        if active:
            blockers.append({"code": "EXPORT_BUSY", "message": 'This task already has an active export.'})
        # 导出是读已有证据；已截止任务仍可保存草稿或已有有效完成结果。
        return {"implemented": True, "schema_version": SCHEMA, "can_export_draft": not active,
                "can_export_complete": not blockers, "blocking_reasons": blockers,
                "current_fingerprint": fingerprint, "jobs": jobs,
                "latest_job_id": jobs[-1]["job_id"] if jobs else None,
                "current_job_id": active[0]["job_id"] if active else None,
                **{key: copy.deepcopy(preview[key]) for key in ("scope", "counts", "assessment_options", "human_completion_current", "completion_id")}}

    def _document(self, state, preview, context, mode, job_id, reason, assessment_id):
        from .annotation_final_review import final_review_projection
        types = final_review_projection(dict(state, effective_decision_ids=sorted(self._effective_decision_ids(state))), preview["completion"]["report"])["types"]
        scope_ids = set(preview["scope"]["scope_ids"])
        memberships = {row["local_type_id"]: row for row in state["memberships"]}
        catalogue = []
        for row in types:
            members = memberships[row["local_type_id"]]["member_ids"]
            catalogue.append({**copy.deepcopy(row), "member_ids": list(members),
                              "scope_member_ids": sorted(scope_ids & set(members)),
                              "scope_member_count": len(scope_ids & set(members)), "task_member_count": len(members)})
        assessment = None
        full_report = None
        if assessment_id is not None:
            job = state.get("label_assessment", {}).get("jobs", {}).get(assessment_id)
            require(job is not None, "ASSESSMENT_NOT_FOUND", 'The selected assessment does not belong to this task')
            option = next(row for row in preview["assessment_options"] if row["job_id"] == assessment_id)
            assessment = {**copy.deepcopy(option), "report": copy.deepcopy(job.get("report")),
                          "snapshot_fingerprint": job["snapshot_fingerprint"], "error": copy.deepcopy(job.get("error"))}
            if job.get("report_url"):
                path = self.owner.media_path(job["report_url"].rsplit("/", 1)[1])
                require(_sha(path) == job["report_sha256"], "ASSESSMENT_CHANGED", 'The internal assessment report file has changed')
                full_report = json.loads(path.read_text())
                require(full_report["snapshot_fingerprint"] == job["snapshot_fingerprint"] and full_report["result"] == job["report"],
                        "ASSESSMENT_CHANGED", 'The internal assessment report differs from its frozen record')
                assessment["report_path"] = "assessment.json"
        document = {"schema_version": "annotation-catalogue-export-v1", "export_id": job_id, "created_at": utc(),
            "project_identity": self.owner.record_metadata(),
            "mode": mode, "requested_mode": mode, "reason": reason, "snapshot_fingerprint": canonical_hash(context),
            "scope": preview["scope"], "counts": preview["counts"], "catalogue": catalogue,
            "items": preview["items"], "source_registry": preview["source_registry"],
            "completion": preview["completion"], "assessment": assessment, "media": [],
            "versions": {"input_fingerprint": self.owner.dataset.input_fingerprint,
                "input_versions": copy.deepcopy(self.owner.dataset.input_versions), "input_artifacts": copy.deepcopy(self.owner.dataset._provenance),
                "catalogue_revision": state["catalogue_revision"], "source_version": preview["source_registry"]["version"],
                "protocol_version": context["protocol_version"], "export_code_identity": self.code_identity},
            **({"audio_preprocessing": copy.deepcopy(self.owner.audio_preprocessing)} if self.owner.audio_preprocessing else {}),
            "spectrogram_display": context["display"], "audit_path": "audit.json",
            "audit_scope": "task-wide audit; primary catalogue counts and completion concern declared scope only",
            "scientific_validity_certified": False, "labels_changed": False,
            "limitations": ['Complete delivery means the declared scope and listed versions have the required engineering records; it does not certify type truth or annotation accuracy.',
                            'Internal assessment does not demonstrate individual independence, external generalization or annotation workflow efficacy.',
                            'Only examples explicitly selected in human definition cards are included; parent recordings and all syllables are not included, so this is not a complete dataset backup.']}
        audit = {"schema_version": "annotation-catalogue-audit-v1", "scope": "task-wide saved records",
                 "state": {k: copy.deepcopy(v) for k, v in state.items() if k not in {"catalogue_exports", "label_assessment"}},
                 "events": _audit_events(self.owner.store.events(self.owner.run_id)),
                 "assessment_history": copy.deepcopy(state.get("label_assessment", {}).get("history", [])),
                 "export_history": copy.deepcopy(export_state(state)["history"]),
                 "computational_event_policy": "Assessment and catalogue snapshots are referenced by original event payload hashes and job/report fingerprints. All human and source events remain verbatim. The selected assessment report is assessment.json."}
        # Localize only explicit system fields in this new derived delivery.
        # The original assessment report and complete human audit stay verbatim.
        from .annotation_catalogue_report import localize_system_document
        return localize_system_document(document), audit, full_report

    def execute(self, action, payload, *, expected_revision, idempotency_key):
        require(isinstance(idempotency_key, str) and 0 < len(idempotency_key) <= 160, "IDEMPOTENCY_REQUIRED", 'Request ID is missing')
        owner = self.owner
        request_hash = canonical_hash({"action": action, "payload": payload, "expected_revision": expected_revision})
        with owner._lock:
            previous = owner.store.lookup_event(owner.run_id, idempotency_key)
            if previous:
                require(previous["payload"].get("service_request_hash") == request_hash, "IDEMPOTENCY_CONFLICT", 'The same request ID was used with different parameters')
                return {"receipt": previous["receipt"], "record": {key: value for key, value in previous["payload"]["record"].items()
                        if key not in {"frozen_context", "document", "audit", "assessment_document"}}, "replayed": True}
            owner.command_guard(action, payload)
            state = owner.store.snapshot(owner.run_id)
            valid = type(expected_revision) is int and (0 <= expected_revision <= state["revision"] if action == "catalogue_export_cancel" else expected_revision == state["revision"])
            require(valid, "REVISION_CONFLICT", 'The state has changed; refresh and review it before exporting')
            meta = {"recorded_at": utc(), "operator_id": state["contract"]["operator_id"], "source": owner.source, "experiment_enabled": False}
            meta.update(owner.record_metadata())
            # The project contract's mode and this export's draft/complete mode
            # are different fields. Worker metadata must not replace the latter.
            meta["project_mode"] = meta.pop("mode", None)
            if action == "catalogue_export_start":
                require(set(payload) == {"mode", "assessment_job_id", "reason"} and payload["mode"] in {"draft", "complete"}, "EXPORT_FIELDS", 'Select draft or complete delivery, select an assessment version, and state the purpose')
                require(isinstance(payload["reason"], str) and 0 < len(payload["reason"].strip()) <= 4000, "EXPORT_REASON", 'Enter the purpose and basis for this export')
                require(payload["assessment_job_id"] is None or isinstance(payload["assessment_job_id"], str), "ASSESSMENT_NOT_FOUND", 'Invalid assessment ID')
                context, preview = self._context(state)
                option = next((row for row in preview["assessment_options"] if row["job_id"] == payload["assessment_job_id"]), None)
                require(payload["assessment_job_id"] is None or option is not None, "ASSESSMENT_NOT_FOUND", 'The selected assessment does not belong to this task')
                if payload["mode"] == "complete":
                    require(preview["human_completion_current"], "EXPORT_HUMAN_INCOMPLETE", 'The current scope has not completed integrity and type-definition checks; a draft can be exported first')
                    require(option is not None and option["eligible_for_complete"], "EXPORT_ASSESSMENT_REQUIRED", 'Complete delivery requires a full assessment report for the current version; stale or failed results cannot substitute for it')
                require(not any(row["status"] in ACTIVE for row in export_state(state)["jobs"].values()), "EXPORT_BUSY", 'An active export already exists')
                ident = "catalogue-" + hashlib.sha256((owner.run_id + ":" + idempotency_key).encode()).hexdigest()[:20]
                document, audit, assessment_document = self._document(state, preview, context, payload["mode"], ident, payload["reason"], payload["assessment_job_id"])
                record = dict(meta, job_id=ident, mode=payload["mode"], assessment_job_id=payload["assessment_job_id"],
                    reason=payload["reason"], created_at=utc(), status="queued", cancel_requested=False,
                    frozen_context=context, snapshot_fingerprint=canonical_hash(context), document=document, audit=audit,
                    assessment_document=assessment_document,
                    progress={"stage": "queued", "completed_media": 0, "total_media": preview["counts"]["media_examples"]})
                subaction = "start"
            else:
                require(set(payload) == {"job_id", "reason"} and isinstance(payload["job_id"], str), "EXPORT_FIELDS", 'Cancellation requires an export ID and a reason')
                require(isinstance(payload["reason"], str) and payload["reason"].strip(), "EXPORT_REASON", 'Enter a reason for cancellation')
                record = dict(meta, job_id=payload["job_id"], reason=payload["reason"], requested_state_revision=expected_revision)
                subaction = "cancel"
            # 导出已有记录不是人工标注，可在deadline之后保存；事件仍完整审计。
            receipt = owner.store.transact(owner.run_id, expected_revision=state["revision"], idempotency_key=idempotency_key,
                operation="record_job", payload={"catalogue_export_event": True, "catalogue_export_action": subaction,
                                                 "record": record, "service_request_hash": request_hash})
            if subaction == "start":
                event = threading.Event(); self.cancel[record["job_id"]] = event
                try:
                    self.futures[record["job_id"]] = self.executor.submit(self._worker, copy.deepcopy(record), event)
                except Exception as exc:
                    self._update(record["job_id"], status="failed", ended_at=utc(), error={"code": "WORKER_START_FAILED", "message": system_error(exc)})
            elif record["job_id"] in self.cancel:
                self.cancel[record["job_id"]].set()
            return {"receipt": receipt, "record": {key: value for key, value in record.items() if key not in {"frozen_context", "document", "audit", "assessment_document"}}}

    def _update(self, ident, **fields):
        with self.owner._lock:
            state = self.owner.store.snapshot(self.owner.run_id)
            job = export_state(state)["jobs"][ident]
            if job["status"] not in ACTIVE:
                return
            metadata = self.owner.record_metadata()
            metadata["project_mode"] = metadata.pop("mode", None)
            record = {**metadata, "job_id": ident, "recorded_at": utc(), "status": job["status"], **fields}
            self.owner.store.transact(self.owner.run_id, expected_revision=state["revision"],
                idempotency_key="catalogue-worker:" + uuid.uuid4().hex, operation="record_job",
                payload={"catalogue_export_event": True, "catalogue_export_action": "update", "record": record})
            if record["status"] in TERMINAL:
                self.runtime_progress.pop(ident, None)

    @staticmethod
    def _check_cancel(event):
        require(not event.is_set(), "CANCELLED", 'This export was cancelled; no partial result was published')

    def _verify_inputs(self, event):
        for key, path in self.input_paths.items():
            self._check_cancel(event)
            require(path.is_file() and _sha(path) == self.owner.dataset._provenance[key], "EXPORT_INPUT_CHANGED", 'Input files have changed; old labels cannot be exported with new input')
        for parent in self.owner.dataset._parents.values():
            self._check_cancel(event)
            require(parent["path"].is_file() and _sha(parent["path"]) == parent["sha256"], "EXPORT_MEDIA_CHANGED", 'The original parent audio has changed; this export cannot continue')

    def _write_media(self, document, folder, event, ident):
        roles = {}
        for row in document["catalogue"]:
            for field, role in (("representative_ids", "representative"), ("atypical_ids", "atypical"), ("counterexample_ids", "counterexample")):
                for item in (row.get("card") or {}).get(field, []):
                    roles.setdefault(item, []).append({"type_id": row["local_type_id"], "role": role})
        media_dir = folder / "media"; media_dir.mkdir()
        all_rows = {row["item_id"]: row for row in document["items"]}
        media = []
        for index, item in enumerate(sorted(roles)):
            self._check_cancel(event)
            samples, rate, interval = self.owner.dataset.read_audio(item, context=False)
            name = "example-" + hashlib.sha256(item.encode()).hexdigest()[:24]
            wav, image = media_dir / (name + ".wav"), media_dir / (name + ".png")
            sf.write(wav, samples, rate, format="WAV", subtype="FLOAT")
            check, check_rate = sf.read(wav, dtype="float32", always_2d=False)
            require(check_rate == rate and np.array_equal(check, samples), "EXPORT_PCM", 'Export audio PCM verification failed')
            display = {**DEFAULTS, "time_max_s": max(DEFAULTS["time_max_s"], len(samples) / rate)}
            render_metadata = render(samples, rate, display, image)
            self.owner.dataset._verify_parent(self.owner.dataset._items[item])
            media.append({"item_id": item, "roles": roles[item], "in_scope": all_rows[item]["in_scope"],
                "context_only": not all_rows[item]["in_scope"], "wav_path": wav.relative_to(folder).as_posix(),
                "spectrogram_path": image.relative_to(folder).as_posix(), "wav_sha256": _sha(wav),
                "spectrogram_sha256": _sha(image), "source_clip_id": interval["source_clip_id"],
                "source_frame_start": interval["source_frame_start"], "source_frame_stop": interval["source_frame_stop"],
                "sample_rate_hz": rate, "parent_audio_sha256": self.owner.dataset._parents[interval["source_clip_id"]]["sha256"],
                "pcm_sha256": hashlib.sha256(np.asarray(samples, dtype="<f4").tobytes()).hexdigest(),
                "audio_processing": document.get("audio_preprocessing", "none; float32 PCM exact"), "display": render_metadata})
            with self.owner._lock:
                self.runtime_progress[ident] = {"stage": "media", "completed_media": index + 1, "total_media": len(roles)}
        document["media"] = media

    def _worker(self, job, event):
        from .annotation_catalogue_report import write_catalogue_report
        ident = job["job_id"]
        published, cleanup = False, None
        try:
            root, staging, target, _journal, _backup = self._publication_paths(ident)
            root.mkdir(exist_ok=True)
            self._check_cancel(event)
            self._update(ident, status="running", started_at=utc())
            staging.mkdir(exist_ok=False)
            self._verify_inputs(event)
            document = copy.deepcopy(job["document"])
            self._write_media(document, staging, event, ident)
            self._check_cancel(event)
            atomic_json(staging / "audit.json", job["audit"])
            if job["assessment_document"] is not None:
                atomic_json(staging / "assessment.json", job["assessment_document"])
            write_catalogue_report(staging, document)
            self._verify_inputs(event)
            self._check_cancel(event)
            files = []
            for path in sorted(staging.rglob("*")):
                if path.is_file():
                    require(not path.is_symlink(), "EXPORT_PATH", 'Symbolic links are not allowed in an export package')
                    files.append({"path": path.relative_to(staging).as_posix(), "size": path.stat().st_size, "sha256": _sha(path)})
            manifest = {"schema_version": "annotation-catalogue-package-manifest-v1", "export_id": ident,
                "project_identity": self.owner.record_metadata(),
                "mode": job["mode"], "snapshot_fingerprint": job["snapshot_fingerprint"], "files": files,
                "manifest_self_excluded": True, "counts": document["counts"], "media_count": len(document["media"]),
                "assessment_job_id": job["assessment_job_id"], "scientific_validity_certified": False}
            atomic_json(staging / "manifest.json", manifest)
            archive = staging / "catalogue.zip"
            with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED) as bundle:
                for row in files + [{"path": "manifest.json"}]:
                    self._check_cancel(event)
                    path = staging / row["path"]
                    bundle.write(path, row["path"])
            # 资产哈希和注册表准备在锁外完成；一次落盘，避免每份例证都重写整个资产表。
            registrations, asset_map = {}, {}
            for path in sorted(staging.rglob("*")):
                if not path.is_file():
                    continue
                relative = path.relative_to(staging).as_posix()
                stored = (target / relative).relative_to(self.owner.work_dir).as_posix()
                asset_id = "asset-" + hashlib.sha256(stored.encode()).hexdigest()[:24]
                registrations[asset_id] = {"path": stored, "sha256": _sha(path)}
                asset_map[relative] = "/api/media/" + asset_id
            manifest_hash, bundle_hash = _sha(staging / "manifest.json"), _sha(archive)
            bundle_bytes = archive.stat().st_size
            # 原子公开完整目录；取消或写入失败时没有可下载的部分成果。
            with self.owner._lock:
                self._check_cancel(event)
                require(not target.exists(), "EXPORT_IMMUTABLE", 'This export version already exists and cannot be overwritten')
                require(not set(registrations) & set(self.owner.assets), "EXPORT_IMMUTABLE", 'This export asset ID already exists and cannot be overwritten')
                self._begin_publication(ident)
                try:
                    os.replace(staging, target)
                    published = True
                    self.owner.assets.update(registrations)
                    atomic_json(self.owner.assets_path, self.owner.assets)
                    self._update(ident, status="completed", ended_at=utc(), asset_map=asset_map,
                        bundle_url=asset_map["catalogue.zip"], manifest_url=asset_map["manifest.json"],
                        report_url="/api/catalogue-report?job_id=" + ident,
                        manifest_sha256=manifest_hash, bundle_sha256=bundle_hash,
                        metadata={"file_count": len(files) + 1, "media_count": len(document["media"]),
                            "bundle_bytes": bundle_bytes, "counts": document["counts"], "mode": job["mode"],
                            "assessment_status": (document.get("assessment") or {}).get("status")},
                        progress={"stage": "completed", "completed_media": len(document["media"]), "total_media": len(document["media"])})
                except Exception:
                    current = export_state(self.owner.store.snapshot(self.owner.run_id))["jobs"][ident]
                    if current["status"] == "completed":
                        # 提交已经落盘但调用方未收到返回：保留该成功版本。
                        self._discard_publication_journal(ident)
                        return
                    cleanup = self._cleanup_unpublished(ident, remove_target=published)
                    raise
                self._discard_publication_journal(ident)
        except Exception as exc:
            cancelled = event.is_set() or getattr(exc, "code", None) == "CANCELLED"
            error = {"code": "CANCELLED" if cancelled else getattr(exc, "code", "EXPORT_FAILED"), "message": system_error(exc)}
            # 终态可被轮询立即观察；先清理未发布目录，避免终态早于资源收尾。
            if cleanup is None:
                cleanup = self._cleanup_unpublished(ident, remove_target=published)
            if cleanup:
                cancelled = False
                error = {"code": "EXPORT_CLEANUP_FAILED", "message": "; ".join(cleanup),
                         "original_error": error, "cleanup_required": True}
            self._update(ident, status="cancelled" if cancelled else "failed", ended_at=utc(),
                         error=error)

    def preview_html(self, job_id, media_url_builder):
        from .annotation_catalogue_report import render_html
        with self.owner._lock:
            state = self.owner.store.snapshot(self.owner.run_id)
            job = copy.deepcopy(export_state(state)["jobs"].get(job_id))
            require(job is not None and job["status"] == "completed", "EXPORT_NOT_READY", 'The export is not complete; its report cannot be opened')
            mapping = copy.deepcopy(job["asset_map"])
        # 已发布资产不可变；读取校验不占人工事务锁。
        manifest_path = self.owner.media_path(mapping["manifest.json"].rsplit("/", 1)[1])
        require(_sha(manifest_path) == job["manifest_sha256"], "EXPORT_CHANGED", 'The export manifest has changed')
        manifest = json.loads(manifest_path.read_text())
        for row in manifest["files"]:
            require(row["path"] in mapping, "EXPORT_CHANGED", 'The export file index is incomplete')
            path = self.owner.media_path(mapping[row["path"]].rsplit("/", 1)[1])
            require(_sha(path) == row["sha256"] and path.stat().st_size == row["size"], "EXPORT_CHANGED", 'An export file does not match the manifest')
        path = self.owner.media_path(mapping["catalogue.json"].rsplit("/", 1)[1])
        document = json.loads(path.read_text())
        require(document["snapshot_fingerprint"] == job["snapshot_fingerprint"], "EXPORT_CHANGED", 'Export snapshot identity mismatch')
        return render_html(document, {relative: media_url_builder(url) for relative, url in mapping.items()})
