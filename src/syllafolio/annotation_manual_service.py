"""纯本地人工标注服务：显式结束、限定用途交付、重开及人工类型定义。"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import uuid
from pathlib import Path

from .annotation_assistant_contracts import canonical_hash, require
from .annotation_manual_protocol import (
    ManualAnnotationStateStore, REVIEW_CHECKS, COMPLETION_CHECKS, completion_report, item_projection,
    issue_projection,
)
from .annotation_service import AnnotationService, utc, atomic_json
from .annotation_spectrogram import DEFAULTS, VERSION, validate_display, render
from .annotation_material_origin import classify_material
from .annotation_batch_review import BATCH_ACTIONS, BatchReviewService
from .annotation_assessment_service import ACTIONS as ASSESSMENT_ACTIONS, LabelAssessmentService
from .annotation_catalogue_export import ACTIONS as CATALOGUE_EXPORT_ACTIONS, CatalogueExportService

MANUAL_ACTIONS = {"review_status", "reopen_issue", "type_card", "catalogue_scope",
                  "scoped_complete", "inspect_completion", "final_item_review"}
LOCAL_ACTIONS = {"neighbors", "comparison", "observe", "issue", "local_job", "cancel_job",
                 "decision", "timer", "visibility", "checkpoint", "restore", "export"}


class ManualAnnotationService(AnnotationService):
    """无模型运行时或提案入口；历史纯手动合同可接续，助手合同不原地转换。"""

    def __setattr__(self, name, value):
        if name == "assistant_gateway":
            require(value is None, "MANUAL_ONLY", 'This application supports local manual review only.')
        super().__setattr__(name, value)

    def __init__(self, dataset_dir, work_dir, **kwargs):
        require("assistant_contract" not in kwargs, "MANUAL_ONLY", 'This application does not accept model or assistant contracts.')
        store = kwargs.pop("store", None)
        require(store is None or isinstance(store, ManualAnnotationStateStore),
                "MANUAL_STORE", 'The manual review service requires a transactional store with explicit closure.')
        owned = store is None
        store = store or ManualAnnotationStateStore(Path(work_dir) / "state.sqlite3")
        try:
            super().__init__(dataset_dir, work_dir, store=store, **kwargs)
            self.audio_preprocessing = json.loads((self.dataset_dir / "manifest.json").read_bytes()).get("audio_preprocessing")
            self.batch = BatchReviewService(self)
            self.batch.recover()
            self.material_origin = classify_material(
                row.get("original_audio", {}).get("parent_sha256") or self.dataset._parents[row["source_clip_id"]]["sha256"]
                for row in self.dataset._items.values())
            self.assessment = LabelAssessmentService(self)
            self.assessment.recover()
            self.catalogue_export = CatalogueExportService(self, kwargs.get("preset_path"))
            self.catalogue_export.recover()
        except Exception:
            if owned:
                store.close()
            raise

    def close(self):
        if hasattr(self, "catalogue_export"):
            self.catalogue_export.close()
        if hasattr(self, "assessment"):
            self.assessment.close()
        super().close()

    def snapshot(self):
        state = super().snapshot()
        state["effective_decision_ids"] = sorted(self.store.effective_decision_ids(state))
        projected = item_projection(state)
        for item in state["items"]:
            item.update(projected[item["item_id"]])
        superseded_index = {}
        for issue in state["issues"].values():
            parent = issue.get("parent_issue_id")
            if parent:
                superseded_index.setdefault(parent, set()).update(issue.get("target_ids", []))
        for issue in state["issues"].values():
            issue.setdefault("closure_confirmed", False)
            issue.update(issue_projection(state, issue, superseded_index))
        state.update(manual_only=True,
                     batch_review=self.batch.snapshot(state),
                     material_origin=copy.deepcopy(self.material_origin),
                     spectrogram_display={"renderer": VERSION, "defaults": dict(DEFAULTS),
                                          "audio_processing": copy.deepcopy(self.audio_preprocessing) or "none"},
                     input_fingerprint=self.dataset.input_fingerprint,
                     type_cards=state.get("type_cards", {}), catalogue_scope=state.get("catalogue_scope"),
                     current_completion_id=state.get("current_completion_id"),
                     completion=completion_report(state))
        from .annotation_final_review import final_review_projection
        state["final_review"] = final_review_projection(state, state["completion"])
        state["final_review"]["publication_report"] = self.catalogue_export.snapshot(state)
        state["source_registry"], state["final_review"]["assessment"] = self.assessment.snapshot(state, state["completion"], projected)
        state.setdefault("capabilities", {}).update(source_registry=True, label_assessment=True, catalogue_export=True)
        # 轮询只传投影，完整冻结快照和审计留在SQLite/导出包，避免历史导出递归膨胀响应。
        state.pop("label_assessment", None)
        state.pop("catalogue_exports", None)
        return state

    def display_image(self, item_id, settings, *, context=False):
        """仅生成显示资产；不改变音频、拟合预设、输入指纹或人工状态。"""
        settings = validate_display(settings)
        require(type(context) is bool, "INVALID_CONTEXT", 'The context field must be true or false.')
        with self._lock:
            samples, rate, interval = self.dataset.read_audio(item_id, context=context)
            ident = canonical_hash({"version": VERSION, "input": self.dataset.input_fingerprint,
                                    "item_id": item_id, "settings": settings, "context": context})
            folder = self.work_dir / "display_spectrograms"
            folder.mkdir(exist_ok=True)
            image, receipt = folder / (ident + ".png"), folder / (ident + ".json")
            if image.is_file() and receipt.is_file():
                metadata = json.loads(receipt.read_text())
                require(hashlib.sha256(image.read_bytes()).hexdigest() == metadata["image_sha256"],
                        "DISPLAY_CHANGED", 'The cached spectrogram has changed.')
                return image
            temporary = folder / (ident + "." + uuid.uuid4().hex + ".png")
            try:
                target = [interval["target_start_frame_in_context"] / rate,
                          interval["target_stop_frame_in_context"] / rate] if context else None
                metadata = render(samples, rate, settings, temporary, target_interval=target)
                metadata.update(item_id=item_id, context=context, created_at=utc(),
                                input_fingerprint=self.dataset.input_fingerprint,
                                image_sha256=hashlib.sha256(temporary.read_bytes()).hexdigest())
                os.replace(temporary, image)
                atomic_json(receipt, metadata)
            finally:
                temporary.unlink(missing_ok=True)
            return image

    def _decision(self, payload, *args):
        require("proposal_id" not in payload, "MANUAL_ONLY", 'Manual decisions cannot reference model proposals.')
        return super()._decision(payload, *args)

    @staticmethod
    def _fields(payload, allowed):
        require(set(payload) <= set(allowed), "REQUEST_FIELDS", 'The request includes fields this manual action does not support.')

    @staticmethod
    def _texts(payload, fields):
        result = {}
        for field in fields:
            value = payload.get(field, "")
            require(isinstance(value, str), "INVALID_INPUT", field + ' must be text.')
            result[field] = value
        return result

    @staticmethod
    def _checks(payload, expected):
        checks = payload.get("checks")
        require(isinstance(checks, dict) and set(checks) == expected
                and all(type(value) is bool for value in checks.values()),
                "INVALID_CHECKS", 'Confirm each item explicitly or leave it unchecked.')
        return copy.deepcopy(checks)

    @staticmethod
    def _type_ids(value):
        require(isinstance(value, list) and all(isinstance(i, str) and i for i in value)
                and len(value) == len(set(value)), "INVALID_TYPE_IDS", 'Type IDs must form a list with no duplicates.')
        return list(value)

    def execute(self, action, payload, *, expected_revision, idempotency_key):
        require(action in LOCAL_ACTIONS | MANUAL_ACTIONS | BATCH_ACTIONS | ASSESSMENT_ACTIONS | CATALOGUE_EXPORT_ACTIONS | {"batch_display"},
                "MANUAL_ONLY", 'Only local manual actions are available in this application.')
        require(isinstance(payload, dict), "INVALID_INPUT", 'The payload must be an object.')
        require("proposal_id" not in payload, "MANUAL_ONLY", 'This application does not accept model proposal references.')
        if action in CATALOGUE_EXPORT_ACTIONS:
            return self.catalogue_export.execute(action, payload, expected_revision=expected_revision,
                                                 idempotency_key=idempotency_key)
        if action in ASSESSMENT_ACTIONS:
            return self.assessment.execute(action, payload, expected_revision=expected_revision,
                                           idempotency_key=idempotency_key)
        if action == "batch_display":
            return self.batch_display(payload, expected_revision=expected_revision)
        if action in BATCH_ACTIONS:
            return self.batch.execute(action, payload, expected_revision=expected_revision,
                                      idempotency_key=idempotency_key)
        if action not in MANUAL_ACTIONS:
            result = super().execute(action, payload, expected_revision=expected_revision,
                                     idempotency_key=idempotency_key)
            if action == "export":
                with self._lock:
                    source_file = self.media_path(result["download_url"].rsplit("/", 1)[1])
                    dest = source_file.with_name(source_file.stem + "_display.json")
                    if not dest.exists():
                        data = json.loads(source_file.read_text())
                        data["material_origin"] = self.material_origin
                        data["spectrogram_display"] = {
                            "renderer": VERSION, "defaults": DEFAULTS,
                            "scope": "requested_renderings_not_evidence_of_viewing",
                            "renderings": [json.loads(p.read_text()) for p in
                                           sorted((self.work_dir / "display_spectrograms").glob("*.json"))]}
                        atomic_json(dest, data)
                    result = dict(result, download_url=self._asset(dest))
            return result
        require(isinstance(idempotency_key, str) and 0 < len(idempotency_key) <= 160,
                "IDEMPOTENCY_REQUIRED", 'The request ID is missing.')
        request_hash = canonical_hash({"action": action, "payload": payload,
                                       "expected_revision": expected_revision})
        with self._lock:
            prior = self.store.lookup_event(self.run_id, idempotency_key)
            if prior:
                require(prior["payload"].get("service_request_hash") == request_hash,
                        "IDEMPOTENCY_CONFLICT", 'This request ID has already been used with different parameters.')
                return {"receipt": prior["receipt"], "record": prior["payload"].get("record"), "replayed": True}
            self.command_guard(action, payload)
            state = self.store.snapshot(self.run_id)
            require(type(expected_revision) is int and expected_revision == state["revision"],
                    "REVISION_CONFLICT", 'The state has changed. Refresh before submitting.')
            if action == "inspect_completion":
                self._fields(payload, set())
                return completion_report(state)
            ident = hashlib.sha256((self.run_id + ":" + idempotency_key).encode()).hexdigest()[:20]
            meta = {"source": self.source, "experiment_enabled": False,
                    "operator_id": state["contract"]["operator_id"], "recorded_at": utc(),
                    "input_versions": copy.deepcopy(self.dataset.input_versions),
                    "catalogue_revision_at_request": state["catalogue_revision"]}
            meta.update(self.record_metadata())
            if action == "final_item_review":
                from .annotation_final_review import build_final_item_record
                record = build_final_item_record(self, state, payload, meta, ident)
                for name in ("observation", "issue", "decision", "closure", "leaf_card"):
                    if record.get(name) is not None:
                        record[name].update(self.record_metadata())
            elif action == "review_status":
                text_fields = {"answer", "comparison_note", "affected_relations_note", "pending_steps",
                               "unresolved_level", "missing_information"}
                self._fields(payload, text_fields | {"issue_id", "status", "checks"})
                issue_id = payload.get("issue_id")
                require(isinstance(issue_id, str) and issue_id in state["issues"], "ISSUE_NOT_FOUND", 'Issue not found.')
                record = dict(meta, **self._texts(payload, text_fields),
                              review_status_id="review-status-" + ident, issue_id=issue_id,
                              expected_issue_revision=state["issues"][issue_id]["revision"],
                              status=payload.get("status"), checks=self._checks(payload, REVIEW_CHECKS))
            elif action == "reopen_issue":
                text_fields = {"basis", "reason", "question", "scope_reason", "downstream_recheck"}
                item_fields = {"target_ids", "fit_ids", "comparison_ids", "eligible_change_ids"}
                self._fields(payload, text_fields | item_fields | {"parent_issue_id", "action", "observation_ids", "affected_type_ids"})
                parent_id = payload.get("parent_issue_id")
                require(isinstance(parent_id, str) and parent_id in state["issues"], "ISSUE_NOT_FOUND", 'Parent issue not found.')
                parent = state["issues"][parent_id]
                record = dict(meta, **self._texts(payload, text_fields), issue_id="issue-" + ident,
                              parent_issue_id=parent_id, parent_issue_revision=parent["revision"],
                              parent_catalogue_revision=parent["catalogue_revision_at_request"],
                              parent_status=parent["status"], parent_scope={field: copy.deepcopy(parent[field])
                                  for field in ("target_ids", "fit_ids", "comparison_ids", "eligible_change_ids")},
                              continuation_kind="review_reopen" if parent["status"] in {"decided", "reviewed_unresolved"}
                                  else "catalogue_refresh",
                              observation_ids=copy.deepcopy(payload.get("observation_ids", [])),
                              action=payload.get("action"), status="open", revision=0,
                              affected_type_ids=self._type_ids(payload.get("affected_type_ids", [])))
                for field in item_fields:
                    record[field] = self._ids(payload.get(field, []), nonempty=field == "target_ids")
                record["unchanged_item_ids"] = [i for i in self.dataset.item_ids if i not in record["eligible_change_ids"]]
                task, context = self._validation_context(state)
                self.rules.review_issue(self._validation_issue(record), task, context)
            elif action == "type_card":
                text_fields = {"title", "diagnostic_body", "main_variant_difference", "uncertainties", "reason", "definition_status"}
                item_fields = {"representative_ids", "atypical_ids", "counterexample_ids"}
                self._fields(payload, text_fields | item_fields | {"local_type_id", "competing_type_ids"})
                require(isinstance(payload.get("local_type_id"), str), "TYPE_NOT_FOUND", 'The type ID must be text.')
                record = dict(meta, **self._texts(payload, text_fields), card_id="card-" + ident,
                              local_type_id=payload.get("local_type_id"),
                              competing_type_ids=self._type_ids(payload.get("competing_type_ids", [])))
                for field in item_fields:
                    record[field] = self._ids(payload.get(field, []))
            elif action == "catalogue_scope":
                text_fields = {"purpose", "target_level", "coverage_plan", "quality_requirements", "unresolved_policy", "exclusions_policy"}
                self._fields(payload, text_fields | {"scope_ids"})
                record = dict(meta, **self._texts(payload, text_fields), scope_id="scope-" + ident,
                              scope_ids=self._ids(payload.get("scope_ids", []), nonempty=True))
            else:
                text_fields = {"quality_evidence", "unresolved_summary", "exclusions_summary", "limitations"}
                self._fields(payload, text_fields | {"scope_id", "checks"})
                record = dict(meta, **self._texts(payload, text_fields), completion_id="completion-" + ident,
                              scope_id=payload.get("scope_id"), checks=self._checks(payload, COMPLETION_CHECKS))
            receipt = self.store.transact(self.run_id, expected_revision=expected_revision,
                idempotency_key=idempotency_key, operation="manual_" + action,
                payload={"record": record, "service_request_hash": request_hash})
            committed = self.store.lookup_event(self.run_id, idempotency_key)
            return {"receipt": receipt, "record": committed["payload"]["record"]}

    def batch_display(self, payload, *, expected_revision):
        """只重新绘制冻结拼接页；不改PCM、页槽位、聚类或人工处置。"""
        from .annotation_batch_acoustics import render_page_display, validate_batch_display, NORMALIZATIONS
        self._fields(payload, {"review_id", "page_id", "settings", "normalization"})
        require(isinstance(payload.get("review_id"), str) and isinstance(payload.get("page_id"), str),
                "BATCH_PAGE_REQUIRED", 'Select a saved audition page.')
        require(isinstance(payload.get("settings"), dict), "DISPLAY_SETTINGS", 'Display settings must be an object.')
        settings = validate_batch_display(payload["settings"])
        with self._lock:
            state = self.store.snapshot(self.run_id)
            require(type(expected_revision) is int and state["revision"] == expected_revision,
                    "REVISION_CONFLICT", 'The state has changed. Refresh before adjusting the display.')
            review = self.batch.snapshot(state)["reviews"].get(payload["review_id"])
            require(review is not None, "BATCH_REVIEW_NOT_FOUND", 'Review round not found.')
            normalization = payload.get("normalization", review["normalization"])
            require(isinstance(normalization, str) and normalization in NORMALIZATIONS,
                    "DISPLAY_NORMALIZATION", 'Select per-syllable or whole-page display normalization.')
            page = next((p for p in review.get("pages", []) if p["page_id"] == payload["page_id"]), None)
            require(page is not None, "BATCH_PAGE_NOT_FOUND", 'This audition page does not belong to the current round.')
            audio = self.media_path(page["audio_url"].rsplit("/", 1)[1])
            ident = canonical_hash({"input": self.dataset.input_fingerprint,
                                    "audio": hashlib.sha256(audio.read_bytes()).hexdigest(),
                                    "slots": page["slots"], "settings": settings,
                                    "normalization": normalization})
            folder = self.work_dir / "batch_display" / ident
            receipt = folder / "display.json"
            if receipt.is_file():
                result = json.loads(receipt.read_text())
            else:
                # 部分绘图失败也保留原文件；重试使用独立子目录，不覆盖已生成媒体。
                attempt = uuid.uuid4().hex
                result = render_page_display(audio, page["slots"], folder / attempt,
                                             settings=settings, normalization=normalization)
                result["artifacts"] = {attempt + "/" + name: dict(row, path=attempt + "/" + row["path"])
                                       for name, row in result["artifacts"].items()}
                for field in ("waveform_path", "spectrogram_path"):
                    result[field] = attempt + "/" + result[field]
                atomic_json(receipt, result)
            public = copy.deepcopy(result)
            for field, name in (("waveform_path", "waveform_url"), ("spectrogram_path", "spectrogram_url")):
                path = public.pop(field)
                public[name] = self._asset(folder / path, result["artifacts"][path]["sha256"])
            public.pop("artifacts", None)
            return public
