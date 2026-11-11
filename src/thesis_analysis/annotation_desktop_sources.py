"""D6原生来源声明：验证与明确登记分离，未知不推断，草稿不是权威记录。"""
from __future__ import annotations
from .annotation_english import system_error, system_fields, system_text

import copy
import csv
import hashlib
import io
import json
from pathlib import Path
import threading
import uuid

from .annotation_assistant_contracts import canonical_hash
from .annotation_desktop_review_media import _checked_path


FILENAME = "desktop_source_draft.json"
SCHEMA = "syllafolio-desktop-source-draft-v1"
LIMITATION = ("Original-recording groups are operator declarations. The application checks task scope and known same-source "
    "or exact-PCM constraints; it does not verify recording identity or individual independence. A WAV or parent clip is not automatically a recording.")
ERROR_TEXT = {
    "SOURCE_CONFLICT": "Known same-parent, identical-parent-PCM or exact-segment-PCM constraints conflict with the declared recording IDs.",
    "SOURCE_VERSION_CONFLICT": "The saved source version changed. Keep your input, refresh the current version and validate again.",
    "REVISION_CONFLICT": "The project changed. Keep your input, refresh and explicitly validate the current context again.",
    "SOURCE_SCOPE": "A mapping refers to an unknown item or parent ID in this project.",
    "SOURCE_ID": "Recording IDs must be explicit non-path identifiers. Use JSON null for an unknown recording.",
    "SOURCE_EVIDENCE": "Provide an actual registration reason and source evidence, each at most 4000 characters.",
    "SOURCE_FIELDS": "Each mapping must contain item_id OR source_clip_id, and raw_recording_id. Labels and external paths are not accepted.",
    "SOURCE_MAPPING": "Use a nonempty explicit mapping list within the task; the same target cannot receive conflicting IDs.",
    "PROJECT_DRAINING": "The project is closing. Save input as a local draft or return to the workspace before a new registration.",
}


class SourceRegistryError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(f"{code}: {message}")


def _require(condition, code, message):
    if not condition: raise SourceRegistryError(code, message)


def _read_json(text):
    def pairs(rows):
        result = {}
        for key, value in rows:
            _require(key not in result, "SOURCE_INPUT_JSON", "Duplicate JSON field: " + key)
            result[key] = value
        return result
    def invalid(value): raise SourceRegistryError("SOURCE_INPUT_JSON", "Nonfinite JSON number: " + value)
    try: return json.loads(text, object_pairs_hook=pairs, parse_constant=invalid)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise SourceRegistryError("SOURCE_INPUT_JSON", "Mapping input is not valid UTF-8 JSON.") from exc


def parse_mappings(value):
    """JSON list/{mappings:list}, or explicit item/parent + recording CSV/TSV.

    CSV/TSV blank recording cells explicitly mean unknown. JSON must use null;
    no filename, parent identifier, label or literal 'unknown' is reinterpreted.
    """
    if isinstance(value, str):
        text = value.lstrip("\ufeff").strip()
        _require(bool(text), "SOURCE_INPUT_EMPTY", "Paste or import explicit mappings first.")
        if text.startswith(("[", "{")):
            value = _read_json(text)
        else:
            text = value.lstrip("\ufeff").strip("\r\n")
            delimiter = "\t" if "\t" in text.splitlines()[0] else ","
            reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
            headers = reader.fieldnames
            _require(headers is not None and len(headers) == len(set(headers)) and set(headers) in (
                {"item_id", "raw_recording_id"}, {"source_clip_id", "raw_recording_id"},
                {"item_id", "source_clip_id", "raw_recording_id"}), "SOURCE_INPUT_HEADERS",
                "CSV/TSV needs explicit item_id or source_clip_id and raw_recording_id headers.")
            entries = []
            for ordinal, row in enumerate(reader, 2):
                _require(None not in row and all(cell is not None for cell in row.values()), "SOURCE_INPUT_COLUMNS", f"Incorrect column count on row {ordinal}.")
                fields = {key: row[key].strip() for key in ("item_id", "source_clip_id") if key in row and row[key].strip()}
                _require(len(fields) == 1, "SOURCE_INPUT_TARGET", f"Row {ordinal} must specify exactly one item or parent ID.")
                entries.append({**fields, "raw_recording_id": row["raw_recording_id"].strip() or None})
            value = entries
    if isinstance(value, dict):
        _require(set(value) == {"mappings"}, "SOURCE_INPUT_FIELDS", "A mapping JSON object may contain only the mappings list; source versions, labels and evidence belong to the current form.")
        value = value["mappings"]
    _require(isinstance(value, list) and bool(value), "SOURCE_MAPPING", "Use a nonempty explicit mapping list.")
    for ordinal, entry in enumerate(value, 1):
        _require(isinstance(entry, dict) and set(entry) in ({"item_id", "raw_recording_id"}, {"source_clip_id", "raw_recording_id"}),
                 "SOURCE_FIELDS", f"Entry {ordinal}: choose item_id OR source_clip_id, plus raw_recording_id.")
        field = "item_id" if "item_id" in entry else "source_clip_id"
        _require(isinstance(entry[field], str) and bool(entry[field]), "SOURCE_SCOPE", f"Entry {ordinal} needs an explicit stable target ID.")
        _require(entry["raw_recording_id"] is None or isinstance(entry["raw_recording_id"], str), "SOURCE_ID", f"Entry {ordinal} must use a recording string or null.")
    return copy.deepcopy(value)


def read_mapping_file(path):
    path = Path(path)
    _require(path.suffix.lower() in {".json", ".csv", ".tsv", ".txt"}, "SOURCE_INPUT_FILE", "Choose a JSON/CSV/TSV/text mapping file, not an audio file or database.")
    _require(path.stat().st_size <= 8 * 1024 * 1024, "SOURCE_INPUT_SIZE", "Mapping input exceeds the 8 MiB text limit.")
    try: return path.read_bytes().decode("utf-8-sig")
    except UnicodeError as exc: raise SourceRegistryError("SOURCE_INPUT_ENCODING", "Save the explicit mapping file as UTF-8 text.") from exc


def _identity(project):
    return {"project_id": project.metadata["project_id"], "run_id": project.service.run_id,
            "input_fingerprint": project.dataset.input_fingerprint}


class SourceDraftStore:
    def __init__(self, project):
        self.project, self.path, self.identity = project, project.project_dir / FILENAME, _identity(project)
        with project.service._lock:
            if not hasattr(project.service, "_desktop_source_draft_lock"):
                project.service._desktop_source_draft_lock = threading.RLock()
        self.lock = project.service._desktop_source_draft_lock
        self.warning, self._source_sha = None, None
        self.load()

    def load(self):
        with self.lock:
            _checked_path(self.project, self.path)
            if not self.path.exists(): self._source_sha = None; return None
            raw = self.path.read_bytes(); self._source_sha = hashlib.sha256(raw).hexdigest()
            try:
                value = _read_json(raw.decode("utf-8"))
                _require(isinstance(value, dict) and value.get("schema_version") == SCHEMA
                    and all(value.get(key) == expected for key, expected in self.identity.items())
                    and value.get("submission_state") == "unsubmitted" and value.get("register_confirmed") is False
                    and isinstance(value.get("form"), dict) and set(value["form"]) == {"mapping_text", "reason", "evidence"}
                    and all(isinstance(text, str) for text in value["form"].values()) and isinstance(value.get("binding"), dict)
                    and all(value["binding"].get(key) == expected for key, expected in self.identity.items()),
                    "SOURCE_DRAFT_IDENTITY", "Saved source input belongs to a different project/input or is not an unsubmitted form.")
                self.warning = None
                return value
            except (UnicodeError, ValueError, TypeError) as exc:
                self.warning = "Damaged or foreign source input is retained. A byte-identical recovery copy is required before saving replacement input: " + system_error(exc)
                return None

    def save(self, form, binding):
        _require(isinstance(form, dict) and set(form) == {"mapping_text", "reason", "evidence"}
                 and all(isinstance(value, str) for value in form.values()), "SOURCE_DRAFT_INPUT", "Source drafts contain only unsubmitted input text.")
        with self.lock:
            _checked_path(self.project, self.path)
            raw = self.path.read_bytes() if self.path.exists() else None
            original = hashlib.sha256(raw).hexdigest() if raw is not None else None
            _require(original == self._source_sha, "SOURCE_DRAFT_CHANGED", "Saved draft bytes changed outside this form. Original bytes and current typed input are retained.")
            if raw is not None and self.warning:
                recovery = self.path.with_name("desktop_source_draft.recovery-" + original[:16] + ".json")
                _checked_path(self.project, recovery)
                if recovery.exists(): _require(recovery.read_bytes() == raw, "SOURCE_DRAFT_BACKUP", "Recovery filename contains different bytes.")
                else:
                    with recovery.open("xb") as stream: stream.write(raw)
            value = {"schema_version": SCHEMA, **self.identity, "submission_state": "unsubmitted", "register_confirmed": False,
                     "form": copy.deepcopy(form), "binding": {**copy.deepcopy(binding), **self.identity}}
            temporary = self.path.with_name(".source-draft-" + uuid.uuid4().hex + ".tmp")
            try:
                with temporary.open("x", encoding="utf-8") as stream: stream.write(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n")
                current = hashlib.sha256(self.path.read_bytes()).hexdigest() if self.path.exists() else None
                _require(current == original, "SOURCE_DRAFT_CHANGED", "Source draft changed during saving; no original was overwritten.")
                temporary.replace(self.path); self._source_sha = hashlib.sha256(self.path.read_bytes()).hexdigest(); self.warning = None
            finally: temporary.unlink(missing_ok=True)
            return copy.deepcopy(value)


class SourceRegistryController:
    def __init__(self):
        self._lock = threading.RLock(); self.base = None; self.validation = None; self.invalid_reason = None
        self._requests, self._stores = {}, {}

    @staticmethod
    def view(project, snapshot=None):
        state = project.snapshot() if snapshot is None else snapshot
        registry = state["source_registry"]
        rows = registry["mappings"].values() if isinstance(registry["mappings"], dict) else registry["mappings"]
        rows = copy.deepcopy(list(rows)); known = [row for row in rows if row.get("raw_recording_id")]
        index = project.service.assessment.index
        constraints = []
        indexed = {row["item_id"]: row for row in index["items"]}
        for number, ids in enumerate(index["components"], 1):
            if len(ids) < 2: continue
            kinds = []
            for field, label in (("source_clip_id", "same_parent"), ("parent_pcm_identity", "identical_parent_pcm"), ("segment_pcm_identity", "exact_segment_pcm")):
                values = [indexed[item][field] for item in ids]
                if len(set(values)) < len(values): kinds.append(label)
            constraints.append({"component_id": "constraint-" + str(number), "item_ids": list(ids), "relation_kinds": kinds})
        return {**_identity(project), "revision": state["revision"], "source_version": registry["version"], "mappings": rows,
            "counts": {"total": len(rows), "declared": len(known), "unknown": len(rows) - len(known), "raw_recording_groups": len({row["raw_recording_id"] for row in known})},
            "unknown_fraction": (len(rows) - len(known)) / len(rows) if rows else None,
            "constraints": constraints, "index_fingerprint": index["fingerprint"], "history": copy.deepcopy(registry.get("history", [])),
            "limitation": LIMITATION, "declaration_only": True}

    @staticmethod
    def _binding(view, generation):
        return {key: copy.deepcopy(view[key]) for key in ("project_id", "run_id", "input_fingerprint", "revision", "source_version", "index_fingerprint")} | {"generation": generation}

    def rebase(self, project, *, generation=0, snapshot=None):
        view = self.view(project, snapshot)
        with self._lock:
            self.base = self._binding(view, generation); self.validation = None; self.invalid_reason = None
        return view

    select = rebase

    def snapshot(self):
        with self._lock: return {"binding": copy.deepcopy(self.base), "validation": copy.deepcopy(self.validation), "invalid_reason": self.invalid_reason}

    def invalidate_validation(self, reason=None):
        with self._lock: self.validation = None; self.invalid_reason = reason

    def observe_context(self, view, *, generation):
        with self._lock:
            changed = self.base is not None and self._binding(view, generation) != self.base
            if changed:
                self.validation = None; self.invalid_reason = "The saved source/project version changed. Your input is retained; refresh and validate the current context again."
            return changed

    def _owner(self, project, generation):
        _require(self.base is not None and all(self.base.get(key) == value for key, value in _identity(project).items())
                 and self.base["generation"] == generation, "SOURCE_PROJECT_CHANGED", "Reopen this source form for its project owner.")

    @staticmethod
    def _intent(mappings, reason, evidence):
        _require(isinstance(reason, str) and isinstance(evidence, str), "SOURCE_EVIDENCE", ERROR_TEXT["SOURCE_EVIDENCE"])
        _require(reason.strip() and evidence.strip() and len(reason) <= 4000 and len(evidence) <= 4000, "SOURCE_EVIDENCE", ERROR_TEXT["SOURCE_EVIDENCE"])
        return {"mappings": parse_mappings(mappings), "reason": reason, "evidence": evidence}

    def validate(self, project, mappings, reason, evidence, *, generation=0, idempotency_key=None):
        intent = self._intent(mappings, reason, evidence)
        with project.service._lock:
            with self._lock:
                self._owner(project, generation); view = self.view(project)
                _require(self._binding(view, generation) == self.base, "SOURCE_CONTEXT_CHANGED", "The project/source version changed. Keep the input, refresh and validate again.")
                payload = {"expected_source_version": self.base["source_version"], **intent}
                result = project.execute("source_validate", payload, expected_revision=self.base["revision"],
                    idempotency_key=idempotency_key or "source-preview-" + uuid.uuid4().hex)
                self.validation = {"binding": copy.deepcopy(self.base), "intent_hash": canonical_hash(intent), "payload": payload,
                                   "preview": copy.deepcopy(result)}
                self.invalid_reason = None
                return self.snapshot()["validation"]

    def register(self, project, mappings, reason, evidence, *, generation=0, idempotency_key, confirmed=False):
        intent = self._intent(mappings, reason, evidence); signature = canonical_hash(intent)
        _require(confirmed is True, "SOURCE_CONFIRMATION", "Explicitly confirm this exact validated source declaration.")
        _require(isinstance(idempotency_key, str) and 0 < len(idempotency_key) <= 160, "IDEMPOTENCY_REQUIRED", "Use an explicit source-registration request key.")
        with project.service._lock:
            with self._lock:
                self._owner(project, generation)
                prior = self._requests.get(idempotency_key)
                if prior is not None:
                    _require(prior["intent_hash"] == signature and prior["identity"] == _identity(project) and prior["generation"] == generation,
                             "IDEMPOTENCY_CONFLICT", "The request key belongs to a different original source declaration.")
                    return project.execute("source_register", copy.deepcopy(prior["payload"]), expected_revision=prior["revision"], idempotency_key=idempotency_key)
                _require(self.validation is not None and self.validation["intent_hash"] == signature, "SOURCE_VALIDATION_REQUIRED", "Validate these exact mappings, reason and evidence before registration.")
                _require(self.validation["preview"].get("valid") is True, "SOURCE_CONFLICT", ERROR_TEXT["SOURCE_CONFLICT"])
                view = self.view(project)
                _require(self._binding(view, generation) == self.validation["binding"] == self.base, "SOURCE_CONTEXT_CHANGED", "The validated source/project version changed; input is retained for explicit revalidation.")
                pending = {"identity": _identity(project), "generation": generation, "intent_hash": signature,
                           "payload": copy.deepcopy(self.validation["payload"]), "revision": self.base["revision"]}
                self._requests[idempotency_key] = pending
                return project.execute("source_register", copy.deepcopy(pending["payload"]), expected_revision=pending["revision"], idempotency_key=idempotency_key)

    def _store(self, project):
        key = (id(project.service), project.service.run_id)
        with self._lock: current = self._stores.get(key)
        if current is not None: return current
        candidate = SourceDraftStore(project)
        with self._lock: return self._stores.setdefault(key, candidate)

    def save_draft(self, project, form, *, generation=0):
        with self._lock:
            self._owner(project, generation)
            binding = copy.deepcopy(self.base)
        return self._store(project).save(form, binding)

    def restore_draft(self, project, *, generation=0):
        with self._lock: self._owner(project, generation)
        value = self._store(project).load()
        _require(value is not None, "SOURCE_DRAFT_MISSING", "No matching unsubmitted input draft is available.")
        with self._lock: self.validation = None
        return {**copy.deepcopy(value), "register_confirmed": False, "requires_revalidation": True}


from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QGridLayout, QLabel, QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy,
    QTableWidget, QTableWidgetItem, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)


class SourceRegistryDialog(QDialog):
    changed = Signal(object)
    locateItem = Signal(str)

    def __init__(self, owner):
        super().__init__(owner)
        self.owner, self.project, self.generation = owner, owner.project, owner._close_coordinator.generation
        self.controller = SourceRegistryController(); self.controller.rebase(self.project, generation=self.generation)
        self._dirty, self._loading, self._waiting, self._invalid = False, False, False, None
        self._form_ticket, self._registry_version, self._pending = 0, None, None
        self._callback_generation = 0
        self.setWindowTitle("Original-recording source declarations")
        area = self.screen().availableGeometry(); self.resize(min(1100, int(area.width() * .94)), min(820, int(area.height() * .9)))
        outer = QVBoxLayout(self); self.status = QLabel(); self.status.setWordWrap(True); outer.addWidget(self.status)
        scroll = QScrollArea(); scroll.setWidgetResizable(True); self.body = QWidget(); layout = QVBoxLayout(self.body)
        scroll.setWidget(self.body); outer.addWidget(scroll, 1)
        note = QLabel(LIMITATION); note.setWordWrap(True); layout.addWidget(note)
        self.registry = QTableWidget(0, 6); self.registry.setHorizontalHeaderLabels(["Stable item", "Parent clip (not a recording)", "Declared recording", "Status", "Evidence", "Origin"])
        self.registry.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers); self.registry.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.registry.setMinimumHeight(170); self.registry.itemDoubleClicked.connect(lambda item: self.locateItem.emit(self.registry.item(item.row(), 0).text()))
        layout.addWidget(self.registry)
        instructions = QLabel('Paste JSON mappings [{"item_id":"stable-id","raw_recording_id":null}], or {"mappings":[...]}. '
            'An explicit source_clip_id request can cover that parent, but never invents a recording ID. CSV/TSV needs named ID and raw_recording_id headers; a blank recording cell explicitly means unknown. Import and Save draft do not register anything.')
        instructions.setWordWrap(True); layout.addWidget(instructions)
        form = QFormLayout(); self.mappings = self.mapping_input = QPlainTextEdit(); self.mappings.setObjectName("sourceMappings")
        self.mappings.setMinimumHeight(140); self.reason = QPlainTextEdit(); self.evidence = QPlainTextEdit()
        self.reason.setMaximumHeight(100); self.evidence.setMaximumHeight(120)
        for label, field in (("Explicit mapping input", self.mappings), ("Registration reason", self.reason), ("Actual source evidence", self.evidence)):
            field.setMinimumWidth(0); field.textChanged.connect(self._input_changed); form.addRow(label, field)
        layout.addLayout(form)
        self.conflicts = QTreeWidget(); self.conflicts.setHeaderLabels(["Consistency conflict / exact item", "Explanation / declared IDs"])
        self.conflicts.itemDoubleClicked.connect(lambda item, _column: self._locate(item)); self.conflicts.setMinimumHeight(120); layout.addWidget(self.conflicts)
        self.preview = QPlainTextEdit(); self.preview.setReadOnly(True); self.preview.setMinimumHeight(130); layout.addWidget(self.preview)
        self.confirmation = QCheckBox("Confirm this validated declaration.")
        self.confirmation.toggled.connect(lambda _checked: self._buttons()); outer.addWidget(self.confirmation)
        self.message = QLabel("Unknown recordings remain unknown. Validate explicit input, inspect every reported conflict, then register only after confirming the exact declaration.")
        self.message.setWordWrap(True); layout.addWidget(self.message)
        grid = QGridLayout(); outer.addLayout(grid)
        self.save_button, self.restore_button = QPushButton("Save draft"), QPushButton("Restore draft")
        self.import_button, self.refresh_button = QPushButton("Import file"), QPushButton("Refresh version")
        self.validate_button, self.register_button = QPushButton("Validate"), QPushButton("Register")
        for index, button in enumerate((self.save_button, self.restore_button, self.import_button, self.refresh_button, self.validate_button, self.register_button)):
            button.setMinimumWidth(0); button.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed); grid.addWidget(button, index // 2, index % 2)
        self.save_button.clicked.connect(lambda: self._guard(self.save_draft)); self.restore_button.clicked.connect(lambda: self._guard(self.restore_draft))
        self.import_button.clicked.connect(lambda: self._guard(self.import_file)); self.refresh_button.clicked.connect(lambda: self._guard(self.reload_context))
        self.validate_button.clicked.connect(lambda: self._guard(self.validate_input)); self.register_button.clicked.connect(lambda: self._guard(self.register_sources))
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close); close.rejected.connect(self.reject); outer.addWidget(close)
        self.timer = QTimer(self); self.timer.setInterval(350); self.timer.timeout.connect(lambda: self._guard(self.refresh))
        self.finished.connect(lambda _result: self.timer.stop()); owner.operationFailed.connect(self._operation_failed)
        store = self.controller._store(self.project)
        if store.warning: self.message.setText(store.warning)
        self.refresh(force=True)

    def is_current(self):
        return self.owner.project is self.project and self.owner._close_coordinator.generation == self.generation and self.owner._close_coordinator.state == "open" and self._invalid is None

    def _guard(self, call):
        try: return call()
        except Exception as exc:
            self.owner._error(exc, operation="Source declaration")
            text = ERROR_TEXT.get(getattr(exc, "code", ""), system_error(exc))
            self.message.setText(f"{getattr(exc, 'code', type(exc).__name__)}: {text}")

    def _operation_failed(self, message):
        if self._waiting and self.is_current():
            self._waiting = False
            code = str(message).split(":", 1)[0]
            text = ERROR_TEXT.get(code, system_text(str(message)))
            self.message.setText(text); self._buttons()

    def _form(self): return {"mapping_text": self.mappings.toPlainText(), "reason": self.reason.toPlainText(), "evidence": self.evidence.toPlainText()}

    def _signature(self):
        try: return canonical_hash(self.controller._intent(self.mappings.toPlainText(), self.reason.toPlainText(), self.evidence.toPlainText()))
        except ValueError: return None

    def _input_changed(self):
        if self._loading: return
        self._dirty = True; self._form_ticket += 1; self._pending = None
        self.controller.invalidate_validation(); self.confirmation.setChecked(False); self.preview.clear(); self.conflicts.clear(); self._buttons()

    def _buttons(self):
        available = self.is_current() and not self.owner._busy and not self._waiting
        self.body.setEnabled(available)
        selection = self.controller.snapshot(); validation = selection["validation"]
        original = self._pending is not None and self._pending["signature"] == self._signature()
        self.confirmation.setEnabled(available and (bool(validation and validation["preview"].get("valid")) or original))
        self.register_button.setEnabled(available and self.confirmation.isChecked() and (bool(validation and validation["preview"].get("valid")) or original))
        self.validate_button.setEnabled(available and selection["invalid_reason"] is None)
        for button in (self.import_button, self.refresh_button, self.save_button, self.restore_button): button.setEnabled(available)

    def refresh(self, force=False):
        if not self.is_current(): self.invalidate("This project is closing or changed. Input remains an unsubmitted draft; reopen the source workspace."); return
        if not self.owner._busy: self._waiting = False
        state = self.owner.state or self.project.snapshot(); view = self.controller.view(self.project, state)
        changed = self.controller.observe_context(view, generation=self.generation)
        if changed:
            self.confirmation.setChecked(False)
            self.message.setText(self.controller.invalid_reason)
        unknown = view["unknown_fraction"]
        ratio = f"{unknown:.1%}" if unknown is not None else "not recorded"
        self.status.setText(f"Saved source version {view['source_version']} · {view['counts']['declared']} declared / {view['counts']['unknown']} unknown "
            f"({ratio}) · {view['counts']['raw_recording_groups']} declared recording groups. These are not verified individuals.")
        if force or self._registry_version != view["source_version"]:
            self._registry_version = view["source_version"]; self.registry.setRowCount(len(view["mappings"]))
            for index, row in enumerate(view["mappings"]):
                for column, value in enumerate((row["item_id"], row["source_clip_id"], row.get("raw_recording_id") or "Unknown", row["status"], row.get("evidence", ""), row.get("origin", "unknown"))):
                    item = QTableWidgetItem(str(value)); item.setToolTip(str(value)); self.registry.setItem(index, column, item)
            self.registry.resizeColumnsToContents()
        self._buttons()

    def reload_context(self):
        _require(self.is_current() and not self.owner._busy, "SOURCE_PROJECT_CHANGED", "Wait for the current open project.")
        self.save_draft(); self.owner.state = self.project.snapshot(); self.controller.rebase(self.project, generation=self.generation, snapshot=self.owner.state)
        self._pending = None; self.confirmation.setChecked(False); self.preview.clear(); self.conflicts.clear(); self.refresh(force=True)
        self.message.setText("Current saved version loaded. Typed input is retained, unsubmitted; validate it explicitly against this version.")

    def validate_input(self):
        _require(self.is_current() and not self.owner._busy, "SOURCE_PROJECT_CHANGED", "Wait for the current open project.")
        text, reason, evidence = self.mappings.toPlainText(), self.reason.toPlainText(), self.evidence.toPlainText()
        ticket, callback_generation = self._form_ticket, self._callback_generation
        self._pending = None; self.confirmation.setChecked(False); self._waiting = True; self._buttons()
        def done(result):
            if not self.is_current() or callback_generation != self._callback_generation:
                self.controller.invalidate_validation(); return
            self._waiting = False
            if ticket != self._form_ticket:
                self.controller.invalidate_validation("Input changed while validation was running; validate the exact current fields again."); self._buttons(); return
            preview = result["preview"]; displayed = copy.deepcopy(preview)
            self.conflicts.clear()
            for conflict in displayed.get("conflicts", []):
                conflict["message"] = ERROR_TEXT.get(conflict["code"], "Inspect the reported consistency conflict.")
                parent = QTreeWidgetItem([conflict["code"], conflict["message"] + " IDs: " + ", ".join(conflict.get("raw_recording_ids", []))]); self.conflicts.addTopLevelItem(parent)
                for ident in conflict.get("item_ids", []):
                    child = QTreeWidgetItem([ident, "Open this exact stable item"]); child.setData(0, Qt.ItemDataRole.UserRole, {"item_id": ident}); parent.addChild(child)
            self.conflicts.expandAll(); self.preview.setPlainText(json.dumps(system_fields(displayed), ensure_ascii=False, indent=2, allow_nan=False))
            self.message.setText("Validation is read-only. Inspect the proposed counts/changed items and explicitly confirm to register." if preview.get("valid") else "Conflicting declarations are not registered. Correct the explicit input; all unknown items remain unchanged.")
            self._buttons()
        try: self.owner._run("Validate source declaration", lambda _progress, _cancelled: self.controller.validate(self.project, text, reason, evidence, generation=self.generation), done)
        except Exception: self._waiting = False; self._buttons(); raise

    def register_sources(self):
        _require(self.is_current() and not self.owner._busy, "SOURCE_PROJECT_CHANGED", "Wait for the current open project.")
        text, reason, evidence = self.mappings.toPlainText(), self.reason.toPlainText(), self.evidence.toPlainText()
        signature = self._signature(); ticket = self._form_ticket; confirmed = self.confirmation.isChecked()
        callback_generation = self._callback_generation
        if self._pending is None or self._pending["signature"] != signature:
            self._pending = {"signature": signature, "key": "desktop-source-" + uuid.uuid4().hex}
        key = self._pending["key"]; self.save_draft()
        hook = getattr(self.owner, "before_catalogue_change", None)
        if hook is not None: hook()
        self._waiting = True; self._buttons()
        def done(result):
            if not self.is_current() or callback_generation != self._callback_generation: return
            self._waiting = False; self.owner.state = self.project.snapshot()
            self.controller.rebase(self.project, generation=self.generation, snapshot=self.owner.state)
            self.confirmation.setChecked(False); self._pending = None
            if ticket == self._form_ticket: self._dirty = False
            self.refresh(force=True); self.changed.emit(result)
            self.message.setText("Original registration receipt replayed; no duplicate source version was written." if result.get("replayed") else
                "Explicit source version registered. Labels are unchanged; existing assessment applicability follows the shared backend and no assessment was started.")
        try: self.owner._run("Register validated source declaration", lambda _progress, _cancelled: self.controller.register(self.project, text, reason, evidence,
            generation=self.generation, idempotency_key=key, confirmed=confirmed), done)
        except Exception: self._waiting = False; self._buttons(); raise

    def save_draft(self):
        if self._dirty:
            self.controller.save_draft(self.project, self._form(), generation=self.generation); self._dirty = False
        return True

    def restore_draft(self):
        _require(self.is_current() and not self.owner._busy, "SOURCE_PROJECT_CHANGED", "Restore input only in its current project.")
        value = self.controller.restore_draft(self.project, generation=self.generation); self._loading = True
        for key, field in (("mapping_text", self.mappings), ("reason", self.reason), ("evidence", self.evidence)): field.setPlainText(value["form"][key])
        self._loading = False; self._form_ticket += 1; self._dirty = False; self._pending = None
        self.confirmation.setChecked(False); self.preview.clear(); self.conflicts.clear(); self._buttons()
        self.message.setText("Input restored as an unsubmitted draft. Validation and registration confirmation are cleared; no source version was restored or registered.")

    def import_file(self, path=None):
        _require(self.is_current() and not self.owner._busy, "SOURCE_PROJECT_CHANGED", "Wait before importing mapping input.")
        if path is None:
            path, _filter = QFileDialog.getOpenFileName(self, "Import explicit UTF-8 source mappings", "", "Mappings (*.json *.csv *.tsv *.txt)")
        if not path: return
        ticket, callback_generation = self._form_ticket, self._callback_generation
        self._waiting = True; self._buttons()
        def done(text):
            if not self.is_current() or callback_generation != self._callback_generation: return
            self._waiting = False
            if ticket != self._form_ticket:
                self.message.setText("Input changed while the file was being read. Current typed input is retained; import again explicitly if needed.")
                self._buttons(); return
            self.mappings.setPlainText(text); self._buttons()
            self.message.setText("File text loaded as input only. Add reason/evidence, validate, then explicitly register.")
        try: self.owner._run("Read source mapping file", lambda _progress, _cancelled: read_mapping_file(path), done)
        except Exception: self._waiting = False; self._buttons(); raise

    def _locate(self, item):
        data = item.data(0, Qt.ItemDataRole.UserRole) or {}
        if self.is_current() and data.get("item_id") in self.project.dataset.item_ids: self.locateItem.emit(data["item_id"])

    def invalidate(self, reason):
        self._invalid = reason; self._callback_generation += 1
        self.controller.invalidate_validation(reason); self.confirmation.setChecked(False); self.stop(); self._buttons(); self.message.setText(reason)

    def stop(self): self.timer.stop()

    def reject(self):
        try: self.save_draft()
        except Exception as exc: self._guard(lambda: (_ for _ in ()).throw(exc)); return
        self._callback_generation += 1; self._waiting = False; self.controller.invalidate_validation()
        self.confirmation.setChecked(False); self.stop(); super().reject()

    def closeEvent(self, event):
        try: self.save_draft()
        except Exception as exc: event.ignore(); self._guard(lambda: (_ for _ in ()).throw(exc)); return
        self._callback_generation += 1; self._waiting = False; self.controller.invalidate_validation()
        self.confirmation.setChecked(False); self.stop(); super().closeEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        if self.is_current():
            self._waiting = False; self.refresh(); self.timer.start()
