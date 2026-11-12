"""Compare global candidate groupings before choosing a review queue."""
from __future__ import annotations

import uuid

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QHBoxLayout, QHeaderView,
    QLabel, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from .annotation_desktop_comparison_audition import ComparisonAuditionWidget
from .annotation_desktop_organization import compare_groupings


def _audition_ids(ids):
    if len(ids) <= 10:
        return list(ids)
    # Include both ends of the group and eight positions spaced between them.
    return [ids[i * (len(ids) - 1) // 9] for i in range(10)]


class GroupingComparisonDialog(QDialog):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.project = owner.project
        self.generation = owner._close_coordinator.generation
        self._options = []
        self._controllers = {}
        self._summaries = {}
        self._task = None
        self._closed = True

        self.setWindowTitle("Compare groupings")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.resize(850, 650)
        layout = QVBoxLayout(self)
        self.table = QTableWidget(3, 4)
        self.table.setObjectName("groupingComparisonTable")
        self.table.setHorizontalHeaderLabels(["Grouping", "Candidate groups", "Initial global pool", "Largest group (syllables)"])
        self.table.horizontalHeaderItem(2).setToolTip(
            "Unclustered syllables as a count and percentage of all imported syllables.")
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().hide()
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.setFixedHeight(header.sizeHint().height() + self.table.verticalHeader().length()
                                  + 2 * self.table.frameWidth() + 4)
        layout.addWidget(self.table)

        group_line = QHBoxLayout()
        group_label = QLabel("Audition group")
        self.group = QComboBox()
        self.group.setObjectName("comparisonGroup")
        group_label.setBuddy(self.group)
        group_line.addWidget(group_label)
        group_line.addWidget(self.group, 1)
        layout.addLayout(group_line)
        self.audition = ComparisonAuditionWidget(self.project, self, gap_seconds=owner.gap.value())
        layout.addWidget(self.audition, 1)

        self.impact = QLabel()
        self.impact.setTextFormat(Qt.TextFormat.PlainText)
        self.impact.setWordWrap(True)
        layout.addWidget(self.impact)
        self.acknowledgement = QCheckBox("I have reviewed the impact on the current review queue.")
        self.acknowledgement.setObjectName("confirmGroupingImpact")
        layout.addWidget(self.acknowledgement)
        self.message = QLabel()
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        footer = QHBoxLayout()
        footer.addStretch()
        self.close_button = QPushButton("Close")
        self.apply_button = QPushButton("Apply grouping")
        self.apply_button.setObjectName("applyComparedGrouping")
        self.apply_button.setProperty("primary", True)
        for button in (self.close_button, self.apply_button):
            button.setAutoDefault(False)
            footer.addWidget(button)
        layout.addLayout(footer)
        self.table.itemSelectionChanged.connect(self._select_option)
        self.group.currentIndexChanged.connect(self._select_group)
        self.acknowledgement.toggled.connect(self._enable_apply)
        self.close_button.clicked.connect(self.reject)
        self.apply_button.clicked.connect(lambda: owner._guard(self.apply, operation="apply compared grouping"))
        owner.operationFailed.connect(self._operation_failed)
        self.finished.connect(self._closed_dialog)

    def start(self):
        self._closed = False
        self._options = []
        self._controllers.clear()
        self._summaries.clear()
        self.table.setEnabled(True)
        self.audition.setEnabled(True)
        self.audition.gap_seconds = self.owner.gap.value()
        self.table.clearSelection()
        self.table.clearContents()
        self._select_option()
        self.message.setText("Preparing the three grouping options…")
        self.show()
        self._task = "compare"

        def prepare(_progress, cancelled):
            controllers = {}
            rows = compare_groupings(self.project, generation=self.generation, cancelled=cancelled,
                                     controllers_by_mode=controllers)
            summaries = {mode: controller.snapshot()["summary"] for mode, controller in controllers.items()}
            return rows, controllers, summaries
        self.owner._run("Compare candidate groupings", prepare, self._prepared)

    def worker_settled(self):
        if self._task is None:
            return
        self._task = None
        if not self._closed:
            self._enable_apply()

    def _operation_failed(self, message):
        if self._task and not self._closed:
            if self._task == "apply":
                self._controllers.clear()
                self.table.setEnabled(True)
                self.audition.setEnabled(True)
                self.group.setEnabled(self.group.count() > 0)
                message += " Reopen this comparison to refresh the grouping options."
            self.message.setText(message)

    def _prepared(self, result):
        if self._closed:
            return
        self._options, self._controllers, self._summaries = result
        self.table.blockSignals(True)
        try:
            for index, row in enumerate(self._options):
                available = row["available"]
                values = [row["granularity"].title(),
                          str(row["candidate_group_count"]) if available else "Unavailable",
                          f"{row['unclustered_count']} ({row['unclustered_fraction']:.1%})" if available else "—",
                          str(row["largest_group_size"]) if available else "—"]
                for column, value in enumerate(values):
                    item = QTableWidgetItem(value)
                    item.setToolTip(row.get("reason") or f"Minimum cluster size: {row['min_cluster_size']} syllables")
                    if not available:
                        item.setFlags(Qt.ItemFlag.NoItemFlags)
                    self.table.setItem(index, column, item)
            self.table.selectRow(next(i for i, row in enumerate(self._options) if row["granularity"] == "default"))
        finally:
            self.table.blockSignals(False)
        self._select_option()

    def _option(self):
        index = self.table.currentRow()
        if self.table.selectedItems() and 0 <= index < len(self._options):
            return self._options[index]
        return {}

    def _select_option(self):
        option = self._option()
        summary = self._summaries.get(option.get("mode"), {})
        groups = summary.get("groups", [])
        self.group.blockSignals(True)
        self.group.clear()
        for group in groups:
            self._add_audition_choice(group.get("label") or group["group_id"], group["item_count"])
        pool = summary.get("unclustered_ids", [])
        if pool:
            self._add_audition_choice("Initial global pool", len(pool))
        if groups:
            by_size = sorted(range(len(groups)), key=lambda i: groups[i]["item_count"])
            self.group.setCurrentIndex(by_size[(len(groups) - 1) // 2])
        self.group.blockSignals(False)
        self.group.setEnabled(self.group.count() > 0)
        self._select_group()
        impact = summary.get("impact", {})
        requires_confirmation = bool(impact.get("requires_confirmation") and not option.get("already_applied"))
        if requires_confirmation:
            self.impact.setText(f"{impact['message']}\n"
                                f"{impact['current_review_count']} current reviews · "
                                f"{impact['saved_assignment_count']} saved assignments · "
                                f"{impact['reset_scope_item_count']} syllables in the new queue.")
        self.impact.setVisible(requires_confirmation)
        self.acknowledgement.setChecked(False)
        self.acknowledgement.setVisible(requires_confirmation)
        if option and not option["available"]:
            self.message.setText(option["reason"])
        elif option.get("already_applied"):
            self.message.setText("These groups and the initial global pool match the current review queue.")
        elif option and not groups:
            self.message.setText("All syllables are unclustered in this option.")
        else:
            self.message.clear()
        if option.get("available"):
            unavailable = [row for row in self._options if not row["available"]]
            for reason in dict.fromkeys(row["reason"] for row in unavailable):
                names = ", ".join(row["granularity"].title() for row in unavailable if row["reason"] == reason)
                self.message.setText((self.message.text() + "\n" + f"{names} unavailable. {reason}").strip())
        self._enable_apply()

    def _add_audition_choice(self, label, count):
        text = f"{label} · {count} syllable" + ("s" if count != 1 else "")
        if count > 10:
            text += " · audition 10"
        self.group.addItem(text)

    def _select_group(self):
        index = self.group.currentIndex()
        summary = self._summaries.get(self._option().get("mode"), {})
        groups = summary.get("groups", [])
        if 0 <= index < len(groups):
            ids = groups[index]["item_ids"]
        elif index == len(groups):
            ids = summary.get("unclustered_ids", [])
        else:
            ids = []
        self.audition.set_items(_audition_ids(ids))

    def _enable_apply(self):
        option = self._option()
        summary = self._summaries.get(option.get("mode"), {})
        confirmed = not summary.get("impact", {}).get("requires_confirmation") or self.acknowledgement.isChecked()
        self.apply_button.setEnabled(bool(option.get("mode") in self._controllers and
                                          not option.get("already_applied") and confirmed and self._task is None))

    def apply(self):
        if not self.apply_button.isEnabled() or self._closed or self.owner._busy:
            return
        if self.owner.project is not self.project or self.owner._close_coordinator.generation != self.generation:
            raise ValueError("Reopen the grouping comparison for the current project.")
        mode = self._option()["mode"]
        controller = self._controllers[mode]
        impact = self._summaries[mode]["impact"]
        if impact["requires_confirmation"]:
            controller.acknowledge(acknowledged=self.acknowledgement.isChecked(),
                                   impact_fingerprint=impact["fingerprint"])
        self.owner._save_drafts()
        self.audition.stop()
        self._task = "apply"
        self.table.setEnabled(False)
        self.group.setEnabled(False)
        self.apply_button.setEnabled(False)
        self.audition.setEnabled(False)
        self.message.setText("Applying the selected grouping…")
        request_key = "desktop-grouping-" + uuid.uuid4().hex

        def commit(_progress, _cancelled):
            controller.apply(self.project, generation=self.generation, idempotency_key=request_key)
            return self.project.snapshot()

        def applied(state):
            if self.owner.project is not self.project or self.owner._close_coordinator.generation != self.generation:
                return
            self.owner.state = state
            self.owner._organization_applied()
            self.owner._refresh()
            if not self._closed:
                self.accept()

        self.owner._run("Apply compared grouping", commit, applied)

    def _closed_dialog(self, _result):
        self._closed = True
        self.audition.clear()
        if self._task == "compare" and self.owner._worker is not None:
            self.owner._worker.cancelled.set()
        self._options = []
        self._controllers.clear()
        self._summaries.clear()
