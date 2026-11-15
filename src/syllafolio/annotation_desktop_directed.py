"""Question, scope and audition entry for one complete directed family review."""
from __future__ import annotations

import copy
import uuid

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QPushButton, QScrollArea, QStackedWidget,
                               QVBoxLayout, QWidget)

from .annotation_desktop_review_media import ReferenceStore
from .annotation_english import system_fields

BASIS_LABELS = {"new_evidence": "New evidence", "criteria_change": "Changed type criteria",
                "reinterpretation": "Reinterpretation", "input_error": "Input error",
                "systematic_error": "Systematic error"}


def _label(text=""):
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


class DirectedWorkflow:
    def _build_directed(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        layout = QVBoxLayout(body)
        scroll.setWidget(body)
        self.tabs.addTab(scroll, "Directed review")
        self._wizard_tab = scroll
        self.tabs.currentChanged.connect(self._tab_changed)
        self._wizard_view = {}
        self._wizard_names = {}
        self._wizard_preview = None
        self._wizard_loading = False
        self._wizard_family = None
        self._wizard_draft = None
        self.wizard_heading = _label()
        font = self.wizard_heading.font()
        font.setBold(True)
        self.wizard_heading.setFont(font)
        layout.addWidget(self.wizard_heading)
        self.wizard_pages = QStackedWidget()
        layout.addWidget(self.wizard_pages)
        question = QWidget()
        qbox = QVBoxLayout(question)
        self.wizard_pages.addWidget(question)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        qbox.addLayout(form)
        self.directed_texts = {}
        for key, caption in (("question", "Review question"), ("reason", "Evidence or reason")):
            field = self.directed_texts[key] = QLineEdit()
            field.setObjectName("directed" + key.title())
            field.textChanged.connect(self._directed_changed)
            form.addRow(caption, field)
        self.basis = QComboBox()
        self.basis.setObjectName("directedBasis")
        self.basis.addItem("Choose a basis…", None)
        for key, title in BASIS_LABELS.items():
            self.basis.addItem(title, key)
        self.basis.currentIndexChanged.connect(self._directed_changed)
        form.addRow("Why reopen?", self.basis)

        options = QPushButton("Related types and supporting records…")
        options.setCheckable(True)
        qbox.addWidget(options, 0, Qt.AlignmentFlag.AlignLeft)
        self._wizard_options = options
        extra = QWidget()
        extras = QFormLayout(extra)
        self.affected = QListWidget()
        self.affected.setAccessibleName("Affected types")
        self.affected.setMaximumHeight(120)
        self.affected.itemChanged.connect(self._directed_changed)
        extras.addRow("Affected types", self.affected)
        for key, caption in (("observation_ids", "Linked observation IDs"), ("parent_issue_id", "Parent issue ID"),
                             ("purpose", "Review purpose")):
            edit = self.directed_texts[key] = QLineEdit()
            edit.textChanged.connect(self._directed_changed)
            extras.addRow(caption, edit)
        qbox.addWidget(extra)
        extra.hide()
        options.toggled.connect(extra.setVisible)

        qbuttons = QHBoxLayout()
        self.restore_directed_button = QPushButton("Restore question draft")
        self.restore_directed_button.clicked.connect(lambda: self._guard(self._restore_directed))
        qbuttons.addWidget(self.restore_directed_button)
        qbuttons.addStretch()
        self.inspect_scope_button = QPushButton("Review scope…")
        self.inspect_scope_button.setObjectName("directedInspect")
        self.inspect_scope_button.clicked.connect(lambda: self._guard(self._inspect_directed))
        qbuttons.addWidget(self.inspect_scope_button)
        qbox.addLayout(qbuttons)

        confirm = QWidget()
        cbox = QVBoxLayout(confirm)
        self.wizard_pages.addWidget(confirm)
        self.directed_scope = _label()
        self.directed_scope.setObjectName("directedScopeSummary")
        self.directed_scope.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        cbox.addWidget(self.directed_scope)
        self._scope_details = QPushButton("Show members and references")
        self._scope_details.setCheckable(True)
        cbox.addWidget(self._scope_details, 0, Qt.AlignmentFlag.AlignLeft)
        self.scope_member_list = QListWidget()
        self.scope_member_list.setAccessibleName("Review targets and references")
        self.scope_member_list.setMaximumHeight(150)
        cbox.addWidget(self.scope_member_list)
        self.scope_member_list.hide()
        self._scope_details.toggled.connect(self.scope_member_list.setVisible)
        cform = QFormLayout()
        self.directed_texts["reopen_reason"] = QLineEdit()
        self.directed_texts["reopen_reason"].setObjectName("directedReopenReason")
        cform.addRow("Reason for a new round", self.directed_texts["reopen_reason"])
        self.direct_exception = QCheckBox("Use the available direct-audition exception")
        cform.addRow(self.direct_exception)
        self.directed_texts["exception_reason"] = QLineEdit()
        cform.addRow("Exception reason", self.directed_texts["exception_reason"])
        cbox.addLayout(cform)
        self._confirmation_form = cform
        for key in ("reopen_reason", "exception_reason"):
            self.directed_texts[key].textChanged.connect(self._confirmation_edited)
        self.direct_exception.toggled.connect(self._exception_toggled)
        cbuttons = QHBoxLayout()
        self.edit_question = QPushButton("Edit question")
        self.edit_question.clicked.connect(self._edit_question)
        cbuttons.addWidget(self.edit_question)
        cbuttons.addStretch()
        self.prepare_button = QPushButton("Start review")
        self.prepare_button.setObjectName("directedStart")
        self.prepare_button.clicked.connect(lambda: self._guard(self._prepare))
        self.continue_button = QPushButton("Continue review")
        self.continue_button.setObjectName("directedContinue")
        self.continue_button.clicked.connect(lambda: self._guard(self._continue))
        cbuttons.addWidget(self.prepare_button)
        cbuttons.addWidget(self.continue_button)
        cbox.addLayout(cbuttons)
        self._history_toggle = QPushButton("Previous directed rounds")
        self._history_toggle.setCheckable(True)
        self._history_list = QListWidget()
        self._history_list.setAccessibleName("Previous directed rounds")
        self._history_list.setMaximumHeight(110)
        layout.addWidget(self._history_toggle, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self._history_list)
        self._history_list.hide()
        self._history_toggle.toggled.connect(self._history_list.setVisible)
        layout.addStretch()
        back = QPushButton("Back to definition")
        back.setObjectName("directedBack")
        back.clicked.connect(lambda: self._guard(lambda: self.show_card(self.type_id)))
        layout.addWidget(back, 0, Qt.AlignmentFlag.AlignLeft)

    def _tab_changed(self):
        if self.tabs.currentWidget() is not self._wizard_tab:
            self._pending_review_id = None

    def _active_directed(self, family_id, state=None):
        state = state if state is not None else self.owner.state or {}
        review = self.controller._active(state, family_id)
        if (review and review.get("directed_issue_id") and not any(review.get(k) for k in ("closed", "archived", "scope_stale", "read_only"))
                and review["status"] in {"queued", "running", "ready"}):
            return review
        return None

    def _render_directed(self, view):
        self._wizard_view = view
        self._wizard_names = {r["local_type_id"]: (r.get("card") or {}).get("title") or r.get("title") or r["local_type_id"]
                              for r in view["types"]}
        self._update_directed_history()
        if (self._wizard_preview and not self._pending_review_id and
                self._wizard_preview["binding"]["revision"] != view["revision"]):
            self._edit_question()
            if self.tabs.currentWidget() is self._wizard_tab:
                self.message.setText("Project data changed. Check the question and inspect the current scope again.")

    def _update_directed_history(self):
        rows = [r for r in self._wizard_view.get("directed_reviews", []) if r["family_id"] == self._wizard_family]
        self._history_list.clear()
        for row in rows:
            status = "Closed" if row["closed"] else "Historical" if row["historical_only"] else "Open"
            self._history_list.addItem(f"{status} · {len(row['item_ids'])} targets · {row['question']}")
            self._history_list.item(self._history_list.count()-1).setToolTip(row["review_id"])
        self._history_toggle.setVisible(bool(rows))
        self._history_list.setVisible(bool(rows) and self._history_toggle.isChecked())

    def _set_question(self, family, payload, *, dirty):
        self._wizard_loading = True
        try:
            self._wizard_family = self._directed_family_id = family
            self._wizard_preview = None
            self.controller._directed.clear()
            ctx = payload.get("context", {})
            for key, field in self.directed_texts.items():
                val = ctx.get(key, "") if key in ("question", "reason", "parent_issue_id") else payload.get(key, "")
                field.setText(", ".join(ctx.get("observation_ids", [])) if key == "observation_ids" else val or "")
            self.basis.setCurrentIndex(max(0, self.basis.findData(ctx.get("basis"))))
            self.direct_exception.setChecked(False)
            self.affected.clear()
            checked = set(ctx.get("affected_type_ids", [family]))
            checked.add(family)
            for ident, name in self._wizard_names.items():
                item = QListWidgetItem(name)
                item.setData(Qt.ItemDataRole.UserRole, ident)
                item.setToolTip(ident)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Checked if ident in checked else Qt.CheckState.Unchecked)
                if ident == family:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
                self.affected.addItem(item)
            self.wizard_heading.setText("Directed review · " + self._wizard_names.get(family, family))
            self._wizard_options.setChecked(False)
            self.wizard_pages.setCurrentIndex(0)
        finally:
            self._wizard_loading = False
        self._directed_dirty = dirty
        self._set_enabled()

    def show_directed(self, family_id=None):
        from .annotation_desktop_catalogue import CatalogueDraftStore
        family = family_id or (self.controller.snapshot().get("selection") or {}).get("family_id")
        self._save_directed_draft()
        state = self.project.snapshot()
        types = {row["local_type_id"]: row for row in state["memberships"]}
        if family not in types:
            raise ValueError("Choose a human family or a fine form before opening a directed review.")
        family = types[family]["parent_type_id"] or family
        self._render_directed(self.controller.view(self.project, state))
        self._wizard_draft = CatalogueDraftStore(self.project).get("directed:" + family)
        self._directed_draft_binding = self.controller._binding(self.project, state, family, self.generation)
        self.tabs.setCurrentWidget(self._wizard_tab)
        self._history_toggle.setChecked(False)
        active = self._active_directed(family, state)
        if active:
            ctx = active["directed_context"]
            fields = {k: copy.deepcopy(ctx.get(k)) for k in (
                "question", "basis", "reason", "affected_type_ids", "parent_issue_id", "observation_ids")}
            self._set_question(family, {"context": fields}, dirty=False)
            self._inspect_directed()
        else:
            affected = [family] if self.type_id in (None, family) else [family, self.type_id]
            self._set_question(family, {"context": {"affected_type_ids": affected}}, dirty=False)
            self.message.clear()
            self.directed_texts["question"].setFocus()
        self._update_directed_history()

    def prefill_directed_question(self, value):
        state = self.project.snapshot()
        types = {row["local_type_id"]: row for row in state["memberships"]}
        family, affected, question = value.get("family_id"), value.get("affected_type_ids"), value.get("question")
        if (family not in types or types[family]["parent_type_id"] is not None or not isinstance(affected, list)
                or family not in affected or not set(affected) <= set(types) or not isinstance(question, str) or not question.strip()):
            raise ValueError("Choose a current family, its affected types and a review question.")
        self.show_card(family)
        self.show_directed(family)
        self._set_question(family, {"context": {"question": question.strip(), "reason": value.get("reason", ""),
                                                 "affected_type_ids": affected}}, dirty=True)
        self.message.setText("Assessment question copied. Choose the actual basis and review the scope.")
        return self._directed_payload()

    def _directed_payload(self):
        text = {k: edit.text().strip() for k, edit in self.directed_texts.items()}
        checked = [self.affected.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.affected.count())
                   if self.affected.item(i).checkState() == Qt.CheckState.Checked]
        ctx = dict(question=text["question"], reason=text["reason"], basis=self.basis.currentData(),
                   affected_type_ids=checked, parent_issue_id=text["parent_issue_id"] or None,
                   observation_ids=[s.strip() for s in text["observation_ids"].split(",") if s.strip()])
        return dict(family_id=self._wizard_family, context=ctx, purpose=text["purpose"],
                    reopen_reason=text["reopen_reason"], use_direct_exception=self.direct_exception.isChecked(),
                    exception_reason=text["exception_reason"])

    def _save_directed_draft(self):
        if self._directed_dirty and self._wizard_family:
            self.controller.save_draft(self.project, self._directed_payload(), kind="directed", type_id=self._wizard_family,
                                       generation=self.generation, binding=self._directed_draft_binding)
            self._directed_dirty = False

    def _restore_directed(self):
        draft = self.controller.restore_draft(self.project, self._wizard_family, kind="directed", generation=self.generation)
        self._set_question(self._wizard_family, draft["payload"], dirty=True)
        self.message.setText("Question draft restored. Check the current members before starting.")

    def _directed_changed(self, *_):
        if not self._wizard_loading:
            self._directed_dirty = True
            self._edit_question()

    def _edit_question(self):
        self._wizard_preview = None
        self.controller._directed.clear()
        self.direct_exception.setChecked(False)
        self.wizard_pages.setCurrentIndex(0)
        if self._wizard_family:
            self.wizard_heading.setText("Directed review · " + self._wizard_names.get(self._wizard_family, self._wizard_family))
        self._set_enabled()

    def _confirmation_edited(self, *_):
        if not self._wizard_loading:
            self._directed_dirty = True
            self._set_enabled()

    def _exception_toggled(self, checked):
        self._confirmation_form.setRowVisible(self.directed_texts["exception_reason"], checked)
        self._confirmation_edited()

    def _inspect_directed(self):
        data = self._directed_payload()
        result = self.controller.preview_directed(self.project, data["family_id"], data["context"], generation=self.generation)
        self._wizard_preview = result
        self._directed_draft_binding = result["binding"]
        s = result["scope"]
        d = s["derived_context"]
        targets = s["target_ids"]
        pinned = set(ReferenceStore(self.project).ids())
        external = sorted(pinned.difference(targets))
        self.scope_member_list.clear()
        self.scope_member_list.addItems(["Target · " + i for i in targets] + ["Reference only · " + i for i in external])
        self._scope_details.setChecked(False)
        families = s["coverage"]["families"]
        names = self._wizard_names
        other = [names.get(r["family_id"],r["family_id"]) for r in families if r["family_id"] != data["family_id"]]
        version = ("version " + str(s["card_revision"])) if s["card_revision"] is not None else "none"
        what = "Frozen round" if s["continuing"] else "Complete family"
        lines = [data["context"]["question"],
                 f"{what}: {names.get(data['family_id'], data['family_id'])} · {len(targets)} syllables",
                 f"Saved definition: {version} · Verification level: {d['verification_level']}",
                 f"Pinned references outside this round: {len(external)} (read-only)"]
        if other:
            lines.append("Separate family rounds: " + ", ".join(other))
        if self._dirty or self._prior_draft or (self.type_id and self._store.get("card:" + self.type_id)):
            lines.append("The open definition has unsubmitted edits. Return to its card to submit them before review if needed.")
        if d["fine_required_ids"]:
            lines.append(f"Fine review required for {len(d['fine_required_ids'])} targets.")
        if s["routine_in_progress"]:
            lines.append("Finish the open routine family review before starting this directed round.")
        self.wizard_heading.setText("Review scope · " + names.get(data["family_id"], data["family_id"]))
        self.directed_scope.setText("\n\n".join(lines))
        self.wizard_pages.setCurrentIndex(1)
        self.edit_question.setVisible(not s["continuing"])
        self.prepare_button.setVisible(not s["continuing"])
        self.continue_button.setVisible(s["continuing"])
        self._confirmation_form.setRowVisible(self.directed_texts["reopen_reason"], not s["continuing"] and s["requires_reopen_reason"])
        self._confirmation_form.setRowVisible(self.direct_exception, not s["continuing"] and s["direct_exception"].get("allowed", False))
        self._confirmation_form.setRowVisible(self.directed_texts["exception_reason"], self.direct_exception.isChecked())
        self.message.clear()
        self._set_enabled()
        return result

    def _directed_controls(self, available):
        if not hasattr(self, "inspect_scope_button"):
            return
        self.wizard_pages.setEnabled(available and self._pending_review_id is None)
        self.inspect_scope_button.setEnabled(bool(self._wizard_family and self.basis.currentData()
            and self.directed_texts["question"].text().strip() and self.directed_texts["reason"].text().strip()))
        self.restore_directed_button.setVisible(bool(self._wizard_draft))
        if self._wizard_preview:
            s = self._wizard_preview["scope"]
            self.prepare_button.setEnabled(not s["routine_in_progress"] and not s["continuing"] and
                (not s["requires_reopen_reason"] or bool(self.directed_texts["reopen_reason"].text().strip())) and
                (not self.direct_exception.isChecked() or bool(self.directed_texts["exception_reason"].text().strip())))
            self.continue_button.setEnabled(s["continuing"])

    def _prepare(self):
        if self._wizard_preview is None or not self._current() or self.owner._busy or not self.prepare_button.isEnabled():
            raise ValueError("Check the current family and question before starting the review.")
        self._before_change()
        data = self._directed_payload()
        data.update(page_layout=self.owner.review_page_layout(), gap_seconds=self.owner.gap.value(),
                    normalization=self.owner.display_controller.normalization)
        self._waiting = True
        self._set_enabled()

        def done(result):
            self._waiting = False
            self._pending_review_id = result["review_id"]
            self.owner.state = self.project.snapshot()
            self.refresh()

        self.owner._run("Prepare directed family review", lambda _progress, _cancelled:
            self.controller.prepare_directed(self.project, generation=self.generation, idempotency_key="desktop-directed-" + uuid.uuid4().hex,
                                             **data), done)

    def _continue(self):
        if not self._current() or self.owner._busy or not self._wizard_preview:
            raise ValueError("Inspect the current round before continuing.")
        s = self._wizard_preview["scope"]
        if not s["continuing"]:
            raise ValueError("Choose an open directed round.")
        result = self.controller.continue_directed(self.project, self._wizard_family, generation=self.generation,
                                                  expected_review_id=s["active_review_id"])
        self._pending_review_id = result["review_id"]
        self.refresh()

    def refresh_pending_directed(self, state):
        if self._pending_review_id is None or self.owner._busy:
            return
        review = state.get("batch_review", {}).get("reviews", {}).get(self._pending_review_id, {})
        if review.get("status") in {"queued", "running"}:
            self.message.setText("Preparing the review audio and spectrogram…")
            return
        rid, self._pending_review_id = self._pending_review_id, None
        if review.get("status") == "ready" and not any(review.get(k) for k in ("scope_stale", "read_only", "closed", "archived")):
            self.reviewPrepared.emit(rid)
            if self.on_review_selected:
                self.on_review_selected(rid)
        else:
            self._edit_question()
            error = review.get("error")
            detail = (error.get("message") if isinstance(error, dict) else error) or review.get("status", "unavailable")
            self.message.setText("Review preparation: " + str(system_fields(detail)) + ". Check the current scope.")
