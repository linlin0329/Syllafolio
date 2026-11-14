"""C完整池入口：首轮门、双来源及冻结范围；写入仅经共同事务服务。"""
from __future__ import annotations
from .annotation_english import system_fields, system_text, system_error

import copy
import json
import threading
import uuid

from .annotation_batch_review import item_version


ACTIVE = {"queued", "running", "cancel_requested"}


class PoolReviewError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(f"{code}: {message}")


def _require(condition, code, message):
    if not condition:
        raise PoolReviewError(code, message)


def _settled(project, review):
    future = getattr(project.service, "_futures", {}).get(review.get("review_id"))
    return bool(future.done()) if future is not None else review.get("status") in {"ready", "failed", "cancelled"}


def _item_versions(project, state, targets):
    # A steady Qt poll must not rescan every decision for every pool member.
    # Object versions change only with the authoritative business revision.
    binding = (id(project.service), state.get("run_id"), state["revision"], tuple(targets))
    cached = getattr(project, "_desktop_pool_versions", None)
    if cached is None or cached[0] != binding:
        cached = (binding, {ident: item_version(state, ident) for ident in targets})
        project._desktop_pool_versions = cached
    return dict(cached[1])


def project_view(project, snapshot=None, *, new_route="cluster"):
    """Use the one owner's supplied projection; stable-ID sources are not labels."""
    state = project.snapshot() if snapshot is None else snapshot
    batch = state.get("batch_review", {})
    organization = batch.get("organization")
    pool = batch.get("pool", {})
    dirty = set(batch.get("legacy_dirty_ids", []))
    groups = []
    for group in (organization or {}).get("groups", []):
        review = batch.get("reviews", {}).get(batch.get("active_reviews", {}).get(group["group_id"]), {})
        affected = sorted(dirty & set(group["item_ids"]))
        complete = bool(review.get("closed") and not review.get("read_only") and not affected)
        groups.append({"group_id": group["group_id"], "label": group.get("label", group["group_id"]),
            "item_count": len(group["item_ids"]), "review_id": review.get("review_id"),
            "status": review.get("status", "not_prepared"), "closed": bool(review.get("closed")),
            "complete": complete, "affected_item_ids": affected,
            "missing_decision_count": len(set(group["item_ids"]) - set(review.get("decisions", {})))})
    current_ids = sorted(set(pool.get("active_ids", [])))
    entries = pool.get("entries", {})
    history = pool.get("source_history", {})
    rows = []
    for ident in current_ids:
        entry = entries.get(ident, {})
        sources = copy.deepcopy(entry.get("sources", history.get(ident, [])))
        rows.append({"item_id": ident, "state": entry.get("state", "not_recorded"), "sources": sources,
            "source_kinds": sorted({row.get("kind", "not_recorded") for row in sources}),
            "pending_required_checks": bool(entry.get("pending_required_checks")),
            "latest_review_id": entry.get("latest_review_id"), "review_origin": entry.get("review_origin")})
    previous = batch.get("reviews", {}).get(pool.get("latest_review_id"), {})
    previous = copy.deepcopy(previous)
    current_review = bool(previous and not previous.get("read_only") and not previous.get("archived"))
    first_complete = bool(pool.get("first_pass_complete"))
    family_pending = list(pool.get("pending_family_review_ids", []))
    new_current_pool = new_route == "pool_cluster" and bool(current_ids)
    current_pool_round = previous.get("pool_mode") == "current_pool"
    continuing = bool(current_review and not previous.get("closed") and not previous.get("scope_stale")
        and previous.get("status") in ACTIVE | {"ready"} and (current_pool_round or first_complete and not family_pending))
    stage_complete = bool(pool.get("stage_complete"))
    can_go_final = first_complete and stage_complete and not family_pending
    completed_freeze = (new_current_pool and current_review and current_pool_round and previous.get("closed")
                        and not previous.get("pool_refresh_required"))
    targets = list(previous.get("item_ids", [])) if continuing or can_go_final else current_ids
    route = previous.get("route") if continuing or can_go_final else new_route
    exception = copy.deepcopy(previous.get("direct_exception", {"allowed": False, "code": "NO_CLUSTER_FAILURE",
        "message": "First run clustering for the current complete pool; only a matching actual numerical failure can authorize an exception."}))
    settled = _settled(project, previous) if previous else True
    needs_archive = bool(current_review and previous.get("scope_stale") and not previous.get("closed"))
    can_archive = needs_archive and previous.get("status") in {"ready", "failed", "cancelled"} and settled
    blockers = []
    if organization is None:
        blockers.append({"code": "BATCH_ORGANIZATION", "message": "Apply a candidate organisation explicitly before the first-pass and global-pool review."})
    needs_final_prerequisites = not new_current_pool and not (current_pool_round and continuing)
    if needs_final_prerequisites and not first_complete:
        blockers.append({"code": "BATCH_FIRST_PASS_PENDING", "message": "Complete every current first-pass group; a temporarily empty pool does not skip this gate."})
    if needs_final_prerequisites and family_pending:
        blockers.append({"code": "BATCH_FAMILY_PENDING", "message": "Finish the listed human-family rounds before freezing the whole pool.", "review_ids": family_pending})
    if needs_archive:
        blockers.append({"code": "POOL_SCOPE_STALE", "message": "Keep the old round as history; explicitly archive it with a reason after its worker stops, then inspect the new complete pool."})
    may_prepare = pool.get("can_prepare_current_pool") if new_current_pool else pool.get("can_prepare")
    if not may_prepare and not continuing and not blockers and not can_go_final:
        blockers.append({"code": "BATCH_BUSY", "message": "Wait for the active batch calculation or explicitly cancel its exact task."})
    state_counts = {}
    for row in rows:
        state_counts[row["state"]] = state_counts.get(row["state"], 0) + 1
    residual = [row["item_id"] for row in rows if row["state"] in {"pooled_reviewed_unresolved", "directed_reviewed_unresolved"}]
    source_counts = {kind: sum(kind in row["source_kinds"] for row in rows)
                     for kind in ("algorithmic_unclustered", "human_unresolved")}
    source_counts["both"] = sum({"algorithmic_unclustered", "human_unresolved"} <= set(row["source_kinds"]) for row in rows)
    scope = {"current_pool_ids": current_ids, "current_pool_count": len(current_ids), "rows": rows,
        "target_ids": targets, "preparation_item_ids": targets,
        "fit_ids": targets if route == "cluster" or (route == "pool_cluster" and len(targets) > 1) else [],
        "eligible_change_ids": previous.get("editable_item_ids", targets) if continuing else targets,
        "item_locks": previous.get("item_locks", {}) if continuing else {},
        "read_only_reference_ids": [], "route": route,
        "current_item_versions": _item_versions(project, state, targets),
        "frozen_expected_item_versions": copy.deepcopy(previous.get("expected_item_versions", {})),
        "frozen_target_ids": list(previous.get("item_ids", [])),
        "new_outside_frozen_ids": sorted(set(current_ids) - set(previous.get("item_ids", []))) if previous else [],
        "review": previous or None, "review_id": previous.get("review_id"),
        "can_continue": continuing, "waiting": continuing and previous.get("status") in ACTIVE,
        "can_prepare": not blockers and not continuing and not can_go_final and not completed_freeze,
        "can_refreeze": bool(current_pool_round and previous.get("pool_refresh_required") and
                             previous.get("status") == "ready" and settled and current_ids and may_prepare),
        "can_skip_empty": not current_ids and not blockers and not continuing and not can_go_final,
        "can_go_final": can_go_final, "stage_complete": stage_complete,
        "empty_pool_skip": bool(current_review and previous.get("empty_pool_skip") and previous.get("closed") and stage_complete),
        "requires_reopen_reason": bool(previous and (previous.get("closed") or previous.get("decisions"))),
        "needs_archive": needs_archive, "can_archive_stale": can_archive, "worker_settled": settled,
        "direct_exception": exception, "blockers": blockers,
        "state_counts": state_counts, "source_counts": source_counts, "source_history": copy.deepcopy(history),
        "residual_reviewed_unresolved_ids": residual, "residual_reviewed_unresolved_count": len(residual),
        "saved_outcome_count": len(previous.get("decisions", {})),
        "pool_is_not_required_to_be_empty": True, "noise_is_not_exclusion": True}
    first = {"complete": first_complete, "total": len(groups), "completed": sum(row["complete"] for row in groups),
             "closed": sum(row["closed"] for row in groups), "groups": groups,
             "missing_groups": [row for row in groups if not row["complete"]],
             "organization_applied": organization is not None, "pending_family_review_ids": family_pending,
             "legacy_dirty_ids": sorted(dirty), "zero_candidate_groups": organization is not None and not groups}
    return {"project_id": project.metadata["project_id"], "run_id": project.service.run_id,
            "revision": state["revision"], "input_fingerprint": state.get("input_fingerprint"),
            "organization_id": (organization or {}).get("organization_id"), "first_pass": first, "scope": scope}


class PoolReviewController:
    project_view = staticmethod(project_view)

    def __init__(self, *, cluster_for_audition=False):
        self.new_route = "pool_cluster" if cluster_for_audition else "cluster"
        self._lock = threading.RLock()
        self._selected = None
        self._requests = {}
        self._invalid_reason = None

    @staticmethod
    def _binding(view, generation):
        return {"project_id": view["project_id"], "run_id": view["run_id"], "generation": generation,
            "revision": view["revision"], "input_fingerprint": view["input_fingerprint"],
            "organization_id": view["organization_id"], "current_pool_ids": view["scope"]["current_pool_ids"],
            "item_versions": view["scope"]["current_item_versions"], "review_id": view["scope"]["review_id"]}

    def select(self, project, *, snapshot=None, generation=0):
        view = project_view(project, snapshot, new_route=self.new_route)
        with self._lock:
            self._selected = {**view, "binding": self._binding(view, generation)}
            self._invalid_reason = None
            return self.snapshot()

    def snapshot(self):
        with self._lock:
            result = copy.deepcopy(self._selected) if self._selected is not None else {"scope": None, "binding": None, "first_pass": None}
            result["invalid_reason"] = self._invalid_reason
            return result

    def observe_context(self, view, *, generation):
        with self._lock:
            changed = self._selected is not None and self._binding(view, generation) != self._selected["binding"]
            if changed:
                self._invalid_reason = "The pool or state version changed. Explicitly refresh the whole scope before creating another round."
            return changed

    def _check_owner(self, project, generation):
        _require(self._selected is not None, "POOL_SCOPE_REQUIRED", "Inspect the complete current pool first.")
        binding = self._selected["binding"]
        _require(binding["project_id"] == project.metadata["project_id"] and binding["run_id"] == project.service.run_id
                 and binding["generation"] == generation, "POOL_PROJECT_CHANGED", "Reopen the pool for this project owner.")
        return binding

    def _replay(self, project, action, intent, generation, key):
        _require(isinstance(key, str) and 0 < len(key) <= 160, "IDEMPOTENCY_REQUIRED", "Use an explicit request key.")
        prior = self._requests.get(key)
        if prior is None:
            return None
        _require(prior["action"] == action and prior["intent"] == intent and prior["project_id"] == project.metadata["project_id"]
                 and prior["run_id"] == project.service.run_id and prior["generation"] == generation,
                 "IDEMPOTENCY_CONFLICT", "This key belongs to a different original request.")
        response = project.execute(action, copy.deepcopy(prior["payload"]), expected_revision=prior["revision"], idempotency_key=key)
        return response

    def _execute(self, project, action, payload, intent, generation, key, revision):
        self._requests[key] = {"action": action, "intent": copy.deepcopy(intent), "payload": copy.deepcopy(payload),
            "project_id": project.metadata["project_id"], "run_id": project.service.run_id, "generation": generation, "revision": revision}
        return project.execute(action, payload, expected_revision=revision, idempotency_key=key)

    @staticmethod
    def _result(response, view, *, continued=False):
        review = response.get("record", {}).get("review") if response else None
        review = review or view["scope"].get("review") or {}
        scope = copy.deepcopy(view["scope"])
        if response and response.get("record", {}).get("review"):
            same_round = review.get("review_id") == scope.get("review_id")
            scope.update(target_ids=list(review["item_ids"]), preparation_item_ids=list(review["item_ids"]),
                fit_ids=list(review["item_ids"]) if review.get("route") == "cluster" or
                    (review.get("route") == "pool_cluster" and len(review["item_ids"]) > 1) else [],
                eligible_change_ids=(scope["eligible_change_ids"] if same_round else
                                     [] if review.get("pool_mode") == "current_pool" else list(review["item_ids"])),
                item_locks=scope["item_locks"] if same_round else {}, frozen_target_ids=list(review["item_ids"]),
                review_id=review["review_id"], route=review.get("route"))
        return {**(response or {}), "review_id": review.get("review_id"), "continued": continued,
                "waiting": review.get("status") in ACTIVE, "empty_pool_skip": bool(review.get("empty_pool_skip")),
                "scope": scope}

    def prepare(self, project, *, generation, idempotency_key, reopen_reason="", use_direct_exception=False,
                exception_reason="", gap_seconds=.4, normalization="per_item", empty_only=False, refreeze=False, page_layout=None):
        _require(isinstance(reopen_reason, str) and isinstance(exception_reason, str), "POOL_REASON", "Reasons must be explicit text.")
        _require(all(type(flag) is bool for flag in (use_direct_exception, empty_only, refreeze)),
                 "POOL_SELECTION", "Select the route explicitly.")
        intent = {"reopen_reason": reopen_reason.strip(), "use_direct_exception": use_direct_exception,
                  "exception_reason": exception_reason.strip(), "gap_seconds": gap_seconds, "normalization": normalization,
                  "empty_only": empty_only, "route": self.new_route, "refreeze": refreeze, "page_layout": page_layout}
        with project.service._lock:
            with self._lock:
                binding = self._check_owner(project, generation)
                replay = self._replay(project, "batch_prepare_pool", intent, generation, idempotency_key)
                view = project_view(project, new_route=self.new_route)
                if replay is not None:
                    return self._result(replay, view)
                scope = view["scope"]
                _require(view["first_pass"]["organization_applied"], "BATCH_ORGANIZATION", "Apply a candidate organisation explicitly before reviewing the global pool.")
                current_mode = (self.new_route == "pool_cluster" and bool(scope["current_pool_ids"]) and
                                (not use_direct_exception or (scope["review"] or {}).get("pool_mode") == "current_pool"))
                continuing_current = scope["can_continue"] and (scope["review"] or {}).get("pool_mode") == "current_pool"
                if empty_only or not (current_mode or continuing_current):
                    _require(view["first_pass"]["complete"], "BATCH_FIRST_PASS_PENDING", "Finish the listed first-pass groups; a temporarily empty pool does not skip this gate.")
                    _require(not view["first_pass"]["pending_family_review_ids"], "BATCH_FAMILY_PENDING", "Finish the listed human-family rounds first.")
                _require(not empty_only or not scope["current_pool_ids"], "POOL_NOT_EMPTY", "The current pool is nonempty and must use its complete clustering route.")
                if use_direct_exception:
                    _require(scope["direct_exception"].get("allowed") is True, "POOL_EXCEPTION_NOT_ALLOWED",
                             scope["direct_exception"].get("message", "No current bound numerical failure exists."))
                    _require(bool(intent["exception_reason"]), "POOL_EXCEPTION_REASON", "Give a separate actual reason for this exception.")
                if (scope["can_continue"] or scope["can_go_final"]) and not use_direct_exception and not refreeze:
                    return self._result(None, view, continued=True)
                _require(self._binding(view, generation) == binding, "POOL_SCOPE_CHANGED", "The reviewed pool or version changed; refresh and inspect the complete scope.")
                _require(not scope["needs_archive"], "POOL_SCOPE_STALE", "Explicitly archive this stopped stale round before freezing a new complete pool.")
                _require(scope["can_refreeze"] if refreeze else scope["can_prepare"], "BATCH_BUSY",
                         "Check the current pool and wait for its active calculation before preparing it.")
                _require(not (refreeze or scope["requires_reopen_reason"]) or bool(intent["reopen_reason"]),
                         "POOL_REOPEN_REASON", "Give a reason for preparing a new complete round after earlier reviewed material.")
                _require(scope["current_pool_ids"] or empty_only, "POOL_EMPTY_CONFIRMATION",
                         "Confirm the empty-pool skip before recording it.")
                payload = {"route": "direct_exception" if use_direct_exception else self.new_route,
                           "gap_seconds": gap_seconds, "normalization": normalization}
                if page_layout is not None:
                    payload["page_layout"] = page_layout
                if current_mode and not empty_only:
                    payload["pool_mode"] = "current_pool"
                if intent["reopen_reason"]:
                    payload["reopen_reason"] = intent["reopen_reason"]
                if use_direct_exception:
                    payload.update(exception_reason=intent["exception_reason"], exception_source_review_id=scope["direct_exception"]["source_review_id"])
                response = self._execute(project, "batch_prepare_pool", payload, intent, generation, idempotency_key, view["revision"])
                if current_mode:
                    view = project_view(project, new_route=self.new_route)
                result = self._result(response, view)
                if use_direct_exception:
                    result["scope"].update(route="direct_exception", fit_ids=[])
                return result

    def cancel(self, project, *, generation, idempotency_key, review_id=None):
        with project.service._lock:
            with self._lock:
                self._check_owner(project, generation)
                intent = {"review_id": review_id}
                replay = self._replay(project, "batch_cancel", intent, generation, idempotency_key)
                if replay is not None:
                    return replay
                view = project_view(project); review = view["scope"].get("review") or {}
                _require(review and (review_id is None or review_id == review["review_id"]), "POOL_JOB_SCOPE", "Select this project's current pool task.")
                _require(review.get("status") in {"queued", "running"}, "BATCH_JOB_STATE", "This preparation has ended or is already cancelling.")
                return self._execute(project, "batch_cancel", {"review_id": review["review_id"]}, intent, generation, idempotency_key, view["revision"])

    def archive_stale(self, project, *, generation, idempotency_key, reason):
        _require(isinstance(reason, str) and bool(reason.strip()), "POOL_ARCHIVE_REASON", "Give the actual reason for retaining the stale round as history.")
        intent = {"reason": reason.strip()}
        with project.service._lock:
            with self._lock:
                binding = self._check_owner(project, generation)
                replay = self._replay(project, "batch_archive_stale", intent, generation, idempotency_key)
                if replay is not None:
                    return replay
                view = project_view(project); scope = view["scope"]
                _require(self._binding(view, generation) == binding, "POOL_SCOPE_CHANGED", "Refresh the changed pool before archiving its old scope.")
                _require(scope["can_archive_stale"], "POOL_ARCHIVE_STATE", "Only a stopped, unclosed and genuinely stale current pool round can be archived.")
                return self._execute(project, "batch_archive_stale", {"review_id": scope["review_id"], "reason": reason.strip()},
                                     intent, generation, idempotency_key, view["revision"])

    def final_status(self, project, *, generation):
        with project.service._lock:
            with self._lock:
                self._check_owner(project, generation)
                view = project_view(project)
                _require(view["scope"]["can_go_final"], "POOL_REVIEW_PENDING", "Complete the current first pass and pooled review, or explicitly record an empty-pool skip, before final review.")
                return view


from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QDoubleSpinBox, QFormLayout, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton,
    QScrollArea, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)


def _rows(table, values):
    table.setRowCount(len(values))
    for index, columns in enumerate(values):
        for column, value in enumerate(columns):
            item = QTableWidgetItem("Not recorded" if value is None else str(value))
            item.setToolTip(item.text()); table.setItem(index, column, item)
    table.resizeColumnsToContents()


def _table(headers):
    table = QTableWidget(0, len(headers)); table.setHorizontalHeaderLabels(headers)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    return table


class PoolReviewDialog(QDialog):
    """Read-only global-pool overview and explicit commands on the same project owner."""
    def __init__(self, owner, on_review_selected, on_final_requested, on_group_requested=None):
        super().__init__(owner)
        self.owner, self.project = owner, owner.project
        self.generation = owner._close_coordinator.generation
        self.on_review_selected, self.on_final_requested, self.on_group_requested = on_review_selected, on_final_requested, on_group_requested
        self.controller = PoolReviewController(cluster_for_audition=True)
        self._pending_review_id, self._waiting, self._dialog_active = None, False, False
        self._invalid_reason, self._draft = None, {}
        self.setWindowTitle("Complete global-pool review")
        available = self.screen().availableGeometry()
        self.resize(min(1080, available.width() - 24), min(820, available.height() - 24))
        outer = QVBoxLayout(self)
        scroll = QScrollArea(); scroll.setWidgetResizable(True); body = QWidget(); layout = QVBoxLayout(body)
        scroll.setWidget(body); outer.addWidget(scroll, 1)
        self.first_pass_label = QLabel(); self.first_pass_label.setWordWrap(True); layout.addWidget(self.first_pass_label)
        self.missing_groups = _table(["Incomplete first-pass group", "Targets", "Current state", "Missing decisions", "Changed IDs"])
        self.missing_groups.setObjectName("poolMissingFirstGroups"); layout.addWidget(self.missing_groups)
        self.group_button = QPushButton("Open selected incomplete first-pass group")
        self.group_button.clicked.connect(lambda: self._guard(self.open_group)); layout.addWidget(self.group_button)
        self.pool_summary = QLabel(); self.pool_summary.setWordWrap(True); layout.addWidget(self.pool_summary)
        self.pool_items = _table(["Stable ID", "Current pool state", "Source kinds (may overlap)", "Necessary checks", "Latest round"])
        self.pool_items.setObjectName("poolStableItems"); layout.addWidget(self.pool_items, 1)
        self.details = self.scope = QPlainTextEdit(); self.details.setReadOnly(True); layout.addWidget(self.details, 1)
        self.refresh_button = QPushButton("Refresh and inspect the complete current pool")
        self.refresh_button.clicked.connect(lambda: self._guard(self.select_scope)); layout.addWidget(self.refresh_button)
        form = QFormLayout()
        self.reopen_reason = QPlainTextEdit(); self.reopen_reason.setMaximumHeight(80)
        self.exception = self.direct_exception = QCheckBox("Use only the displayed current backend-authorized numerical exception")
        self.exception_reason = QPlainTextEdit(); self.exception_reason.setMaximumHeight(80)
        self.archive_reason = QPlainTextEdit(); self.archive_reason.setMaximumHeight(80)
        self.gap = QDoubleSpinBox(); self.gap.setRange(.05, 2); self.gap.setDecimals(2); self.gap.setValue(.4)
        self.normalization = QComboBox(); self.normalization.addItem("Per syllable", "per_item"); self.normalization.addItem("Whole page", "whole_page")
        for label, widget in (("New-round reason", self.reopen_reason), ("Direct-audition exception", self.exception),
            ("Separate exception reason", self.exception_reason), ("Stopped stale-round archive reason", self.archive_reason),
            ("Silence gap (s)", self.gap), ("Display normalization", self.normalization)):
            form.addRow(label, widget)
        layout.addLayout(form)
        self.message = QLabel("Prepare the complete current pool to compare syllables within acoustic groups.")
        self.message.setWordWrap(True); outer.addWidget(self.message)
        actions = QHBoxLayout()
        self.prepare_button = QPushButton("Prepare or continue the complete pool")
        self.prepare_button.setObjectName("prepareCompletePool")
        self.empty_button = QPushButton("Record empty-pool skip")
        self.empty_button.setObjectName("recordEmptyPoolSkip")
        self.final_button = QPushButton("Open final review")
        self.final_button.setObjectName("poolToFinalReview")
        self.prepare_button.clicked.connect(lambda: self._guard(self.prepare))
        self.empty_button.clicked.connect(lambda: self._guard(lambda: self.prepare(empty_only=True)))
        self.final_button.clicked.connect(lambda: self._guard(self.go_final))
        for button in (self.prepare_button, self.empty_button, self.final_button): actions.addWidget(button)
        outer.addLayout(actions)
        controls = QHBoxLayout()
        self.cancel_button = QPushButton("Cancel this pool calculation")
        self.cancel_button.clicked.connect(lambda: self._guard(self.cancel))
        self.archive_button = QPushButton("Archive stopped stale pool (not completion)")
        self.archive_button.clicked.connect(lambda: self._guard(self.archive))
        controls.addWidget(self.cancel_button); controls.addWidget(self.archive_button); outer.addLayout(controls)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject); outer.addWidget(close)
        self.timer = QTimer(self); self.timer.setInterval(350); self.timer.timeout.connect(lambda: self._guard(self.refresh))
        self.finished.connect(self._finished)
        owner.operationFailed.connect(self._operation_failed)

    def _current(self):
        return self.owner.project is self.project and self.owner._close_coordinator.generation == self.generation and self.owner._close_coordinator.state == "open" and self._invalid_reason is None

    def _guard(self, operation):
        try:
            return operation()
        except Exception as exc:
            self.owner._error(exc, operation="complete pool review")
            self.message.setText(f"{getattr(exc, 'code', type(exc).__name__)}: {system_error(exc)}")

    def _operation_failed(self, message):
        if self._waiting:
            self._waiting = False
            self.message.setText(message)

    def select_scope(self):
        _require(self._current(), "POOL_PROJECT_CHANGED", "Reopen the pool for the current project.")
        state = self.project.snapshot()
        self.owner.state = state
        self.controller.select(self.project, snapshot=state, generation=self.generation)
        self.refresh()

    def refresh(self):
        if not self._current():
            for button in (self.prepare_button, self.empty_button, self.final_button, self.cancel_button, self.archive_button, self.group_button): button.setEnabled(False)
            return
        state = self.owner.state or self.project.snapshot()
        view = project_view(self.project, state, new_route=self.controller.new_route); first, scope = view["first_pass"], view["scope"]
        self.controller.observe_context(view, generation=self.generation)
        selected = self.controller.snapshot()
        if self._waiting and not self.owner._busy: self._waiting = False
        available = not self.owner._busy and not self._waiting
        self.first_pass_label.setText(f"First pass: {first['completed']} / {first['total']} current groups complete. " +
            ("No candidate groups: unclustered input proceeds to the global pool after the candidate organisation is applied." if first["zero_candidate_groups"] else "") +
            (" Candidate reviews remain open. Independent pool items can still be assigned." if not first["complete"]
             and scope["current_pool_ids"] else " First pass is incomplete; locate the missing group below."
             if not first["complete"] else " First-pass gate is satisfied.") +
            (" Pending human-family rounds: " + ", ".join(first["pending_family_review_ids"]) if first["pending_family_review_ids"] else ""))
        old_group = self.missing_groups.currentRow()
        _rows(self.missing_groups, [[row["group_id"], row["item_count"], row["status"], row["missing_decision_count"], ", ".join(row["affected_item_ids"])] for row in first["missing_groups"]])
        if first["missing_groups"]: self.missing_groups.selectRow(min(max(old_group, 0), len(first["missing_groups"]) - 1))
        _rows(self.pool_items, [[row["item_id"], row["state"], ", ".join(row["source_kinds"]), row["pending_required_checks"], row["latest_review_id"]] for row in scope["rows"]])
        self.pool_summary.setText(f"Current pool: {scope['current_pool_count']} unique stable IDs · Source counts {scope['source_counts']} · "
            f"Frozen round: {len(scope['frozen_target_ids'])} targets / {scope['saved_outcome_count']} saved outcomes. " +
            (f"Review complete with {scope['residual_reviewed_unresolved_count']} reviewed unresolved residual members; clearing them is not required." if scope["can_go_final"] else
             "Saving outcomes is distinct from explicitly finishing all frozen pages and necessary checks.") +
            (" New IDs outside the existing frozen round remain pending: " + ", ".join(scope["new_outside_frozen_ids"]) if scope["new_outside_frozen_ids"] else ""))
        text = json.dumps(system_fields({"first_pass": first, "scope": scope}), ensure_ascii=False, indent=2, allow_nan=False)
        if self.details.toPlainText() != text: self.details.setPlainText(text)
        allowed = scope["direct_exception"].get("allowed") is True
        self.exception.setEnabled(available and allowed)
        if not allowed: self.exception.setChecked(False)
        valid_selection = selected.get("scope") is not None and (not selected.get("invalid_reason") or scope["can_continue"] or scope["can_go_final"])
        self.prepare_button.setEnabled(available and valid_selection and (scope["can_prepare"] or scope["can_continue"] or scope["can_go_final"]) and bool(scope["target_ids"] or scope["can_continue"] or scope["can_go_final"]))
        self.empty_button.setEnabled(available and valid_selection and scope["can_skip_empty"])
        self.final_button.setEnabled(available and scope["can_go_final"])
        self.cancel_button.setEnabled(available and (scope.get("review") or {}).get("status") in {"queued", "running"})
        self.archive_button.setEnabled(available and valid_selection and scope["can_archive_stale"])
        self.group_button.setEnabled(available and bool(first["missing_groups"]) and self.on_group_requested is not None)
        if selected.get("invalid_reason") and not scope["can_continue"] and not scope["can_go_final"]:
            self.message.setText(selected["invalid_reason"])
        elif scope["blockers"]:
            self.message.setText("\n".join(row["code"] + ": " + row["message"] for row in scope["blockers"]))
        elif scope["empty_pool_skip"]:
            self.message.setText("No pooled review is needed. The explicit empty-pool skip is saved; choose Open final review when ready.")
        elif (scope.get("review") or {}).get("status") in {"failed", "cancelled"}:
            self.message.setText(str(system_fields(scope["review"].get("error"))) + "\n" + system_text(scope["direct_exception"].get("message", "")))
        pending = state.get("batch_review", {}).get("reviews", {}).get(self._pending_review_id, {})
        open_allowed = pending.get("pool_mode") == "current_pool" or first["complete"] and not first["pending_family_review_ids"]
        if pending.get("status") == "ready" and available and self._dialog_active and open_allowed and not pending.get("scope_stale") and not pending.get("read_only"):
            ident, self._pending_review_id = self._pending_review_id, None
            self.on_review_selected(ident)
            if not pending.get("empty_pool_skip"): self.accept()
        elif pending.get("status") in {"failed", "cancelled"}:
            self._pending_review_id = None

    def prepare(self, *, empty_only=False):
        _require(self._current(), "POOL_PROJECT_CHANGED", "Reopen this pool for the current project.")
        self._waiting = True
        arguments = {"generation": self.generation, "idempotency_key": "desktop-pool-" + uuid.uuid4().hex,
            "reopen_reason": self.reopen_reason.toPlainText(), "use_direct_exception": self.exception.isChecked(),
            "exception_reason": self.exception_reason.toPlainText(), "gap_seconds": self.gap.value(),
            "normalization": self.normalization.currentData(), "empty_only": empty_only,
            "page_layout": self.owner.review_page_layout()}
        def done(result):
            self._pending_review_id = result["review_id"]
            self.owner.state = self.project.snapshot(); self.refresh()
        try:
            self.owner._run("Prepare or continue complete pool", lambda _progress, _cancelled: self.controller.prepare(self.project, **arguments), done)
        except Exception:
            self._waiting = False
            raise

    def cancel(self):
        _require(self._current() and not self.owner._busy, "POOL_PROJECT_CHANGED", "Wait for this current project owner before cancellation.")
        def done(_result): self.owner.state = self.project.snapshot(); self.refresh()
        self.owner._run("Cancel complete-pool calculation", lambda _progress, _cancelled: self.controller.cancel(self.project,
            generation=self.generation, idempotency_key="desktop-pool-cancel-" + uuid.uuid4().hex), done)

    def archive(self):
        _require(self._current() and not self.owner._busy, "POOL_PROJECT_CHANGED", "Reopen the current project scope before archiving.")
        reason = self.archive_reason.toPlainText()
        def done(_result):
            self.owner.state = self.project.snapshot(); self.select_scope()
            self.message.setText("Stopped stale round retained as history, not completion. Inspect and explicitly prepare the new complete pool.")
        self.owner._run("Archive stopped stale pool", lambda _progress, _cancelled: self.controller.archive_stale(self.project,
            generation=self.generation, idempotency_key="desktop-pool-archive-" + uuid.uuid4().hex, reason=reason), done)

    def open_group(self):
        _require(self._current() and not self.owner._busy, "POOL_PROJECT_CHANGED", "Wait for the current owner operation.")
        row = self.missing_groups.currentRow()
        _require(row >= 0 and self.on_group_requested is not None, "POOL_GROUP_REQUIRED", "Select an incomplete first-pass group.")
        self.on_group_requested(self.missing_groups.item(row, 0).text()); self.accept()

    def go_final(self):
        _require(self._current() and not self.owner._busy, "POOL_PROJECT_CHANGED", "Wait for the current owner operation.")
        self.controller.final_status(self.project, generation=self.generation)
        self.on_final_requested(); self.accept()

    def save_draft(self):
        self._draft = {"project_id": self.project.metadata["project_id"], "run_id": self.project.service.run_id,
            "reopen_reason": self.reopen_reason.toPlainText(), "exception_reason": self.exception_reason.toPlainText(),
            "archive_reason": self.archive_reason.toPlainText(), "binding": self.controller.snapshot().get("binding"),
            "submission_state": "unsubmitted_scope_intent"}
        return True

    def stop(self): self.timer.stop()

    def invalidate(self, reason):
        self.save_draft(); self._invalid_reason = reason; self.stop()
        self.message.setText(reason); self.refresh()

    def _finished(self, _result):
        self.save_draft(); self._dialog_active = False; self.stop()

    def showEvent(self, event):
        super().showEvent(event)
        self._dialog_active = True
        if self._current(): self.select_scope(); self.timer.start()

    def closeEvent(self, event):
        self.save_draft(); self.stop(); super().closeEvent(event)
