"""Project preferences and a read-only sample for preparing audition pages."""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout,
    QLabel, QPushButton, QSpinBox, QVBoxLayout,
)

from .annotation_page_layout import MAX_PAGE_ITEMS, page_slices
from .annotation_desktop_comparison_audition import ComparisonAuditionWidget
from .annotation_acoustics import AcousticError
from .annotation_family_acoustics import family_audition_route, family_similarity_order


class ReviewPageSettings(QGroupBox):
    def __init__(self, owner, parent):
        super().__init__("Review pages", parent)
        self.owner = owner
        self.project = owner.project
        self.controller = owner.display_controller
        self._last_context = None
        self._ordered_family_members = ()
        self._family_order = []
        self._loading = True

        box = QVBoxLayout(self)
        form = QFormLayout()
        box.addLayout(form)
        self.scale_mode = QComboBox()
        self.scale_mode.setObjectName("timeScaleMode")
        self.scale_mode.addItem("Recommended", "recommended")
        self.scale_mode.addItem("Custom", "manual")
        self.scale = QDoubleSpinBox()
        self.scale.setObjectName("timeScaleMsPerPixel")
        self.scale.setRange(.1, 100.)
        self.scale.setDecimals(2)
        self.scale.setSingleStep(.25)
        self.scale.setSuffix(" ms/pixel")
        self.scale.setToolTip("Milliseconds per logical pixel. Choose Custom and use a smaller value to show more detail.")
        scale_row = QHBoxLayout()
        scale_row.addWidget(self.scale_mode)
        scale_row.addWidget(self.scale)
        scale_row.addStretch()
        form.addRow("Time scale", scale_row)

        self.mode = QComboBox()
        self.mode.setObjectName("reviewPageMode")
        self.mode.addItem("Automatic", "auto")
        self.mode.addItem("Fixed count", "fixed")
        self.count = QSpinBox()
        self.count.setObjectName("reviewPageCount")
        self.count.setRange(1, MAX_PAGE_ITEMS)
        self.count.setSuffix(" per page")
        page_row = QHBoxLayout()
        page_row.addWidget(self.mode)
        page_row.addWidget(self.count)
        page_row.addStretch()
        form.addRow("Pagination", page_row)
        self.estimate = QLabel()
        self.estimate.setObjectName("reviewPageEstimate")
        self.estimate.setWordWrap(True)
        box.addWidget(self.estimate)
        self.status = QLabel("Changes take effect when you prepare a new review round.")
        self.status.setWordWrap(True)
        box.addWidget(self.status)

        footer = QHBoxLayout()
        self.preview = QPushButton("Preview first page…")
        self.preview.setObjectName("previewReviewPageLayout")
        self.preview.clicked.connect(lambda: self.owner._guard(self.show_preview, operation="preview review pages"))
        self.apply = QPushButton("Apply to new rounds")
        self.apply.setObjectName("applyReviewPageLayout")
        self.apply.clicked.connect(lambda: self.owner._guard(self.save, operation="review page preferences"))
        footer.addWidget(self.preview)
        footer.addStretch()
        footer.addWidget(self.apply)
        box.addLayout(footer)

        self.sync()
        self.scale_mode.currentIndexChanged.connect(self._scale_mode_changed)
        self.mode.currentIndexChanged.connect(self._changed)
        self.scale.valueChanged.connect(self._changed)
        self.count.valueChanged.connect(self._changed)
        # A modeless settings window can stay open while the user changes group,
        # gap or available space in the main window.
        self.timer = QTimer(self)
        self.timer.setInterval(350)
        self.timer.timeout.connect(self._refresh_context)
        parent.finished.connect(lambda _: self.timer.stop())

    def sync(self):
        self._loading = True
        pages = self.controller.review_pages
        self.mode.setCurrentIndex(self.mode.findData(pages["mode"]))
        self.count.setValue(pages["count"])
        self.scale_mode.setCurrentIndex(0 if pages["ms_per_pixel"] is None else 1)
        self.scale.setValue(pages["ms_per_pixel"] or self.controller.recommended_time_scale)
        self._loading = False
        self._last_context = None
        self._changed()

    def showEvent(self, event):
        super().showEvent(event)
        self.timer.start()
        self._refresh_context()

    def _scale_mode_changed(self, *_):
        if self._loading:
            return
        if self.scale_mode.currentData() == "recommended":
            self.scale.setValue(self.controller.recommended_time_scale)
        self._changed()

    def _preferences(self):
        return {"mode": self.mode.currentData(), "count": self.count.value(),
                "ms_per_pixel": self.scale.value() if self.scale_mode.currentData() == "manual" else None}

    def _scope(self):
        scope = self.owner.selected_group or ""
        batch = (self.owner.state or {}).get("batch_review", {})
        if scope == "pool":
            return batch.get("pool", {}).get("active_ids", [])
        if scope.startswith("family:"):
            family = next((row for row in batch.get("families", []) if row["family_id"] == scope[7:]), {})
            return family.get("member_ids", [])
        group = next((row for row in (batch.get("organization") or {}).get("groups", [])
                      if row["group_id"] == scope), {})
        return group.get("item_ids", [])

    def _refresh_context(self):
        if self.owner.project is not self.project:
            return
        context = (self.owner.selected_group, (self.owner.state or {}).get("revision"),
                   round(self.owner.canvas.drawable_width_px()), self.owner.gap.value())
        if context != self._last_context:
            self._last_context = context
            self._changed()

    def _changed(self, *_):
        if self._loading:
            return
        self.scale.setEnabled(self.scale_mode.currentData() == "manual")
        self.count.setEnabled(self.mode.currentData() == "fixed")
        self.apply.setEnabled(self._preferences() != self.controller.review_pages)
        self.status.setText("Changes take effect when you prepare a new review round.")
        dataset = self.project.dataset
        ids = sorted(self._scope())
        self.layout_snapshot = self.owner.review_page_layout(self._preferences())
        self.preview.setEnabled(bool(ids))
        self.first_ids = []
        if not ids:
            self.estimate.setText("Select a review group to preview its pages.")
            return
        route = None
        if self.owner.selected_group.startswith("family:") and not self.owner._directed_editor():
            route = family_audition_route(len(ids))
            if route == "family_order":
                members = tuple(ids)
                if members != self._ordered_family_members:
                    try:
                        self._family_order = family_similarity_order(dataset, ids)
                    except AcousticError as exc:
                        self.preview.setEnabled(False)
                        self.estimate.setText(str(exc))
                        return
                    self._ordered_family_members = members
                ids = self._family_order
        clustered = route == "family_cluster" or self.owner.selected_group == "pool" or (route is None and len(ids) > 30)
        self.preview.setText("Preview example page…" if clustered else "Preview first page…")
        frame_counts = [dataset._items[i]["source_frame_stop"] - dataset._items[i]["source_frame_start"] for i in ids]
        rate = dataset.sample_rate_hz
        pages = list(page_slices(frame_counts, rate, round(self.owner.gap.value() * rate), self.layout_snapshot))
        self.first_ids = ids[pages[0]]
        sizes = [part.stop - part.start for part in pages]
        label = str(sizes[0]) if min(sizes) == max(sizes) else f"{min(sizes)}–{max(sizes)}"
        suffix = " before cluster boundaries. The prepared first page may differ." if clustered else "."
        self.estimate.setText(f"Estimated for {len(ids)} syllables: {len(pages)} page{'s' if len(pages) != 1 else ''}, "
                              f"{label} per page{suffix}")

    def save(self):
        if self.owner.project is not self.project:
            raise ValueError("Open display settings for the current project.")
        self.controller.save_page_preferences(self.project, self._preferences())
        self.apply.setEnabled(False)
        self.status.setText("Saved for the next review round.")

    def show_preview(self):
        self._changed()
        if not self.first_ids:
            return
        self.owner._stop_review_audio()
        dialog = QDialog(self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.setWindowTitle(self.preview.text().rstrip("…"))
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        box = QVBoxLayout(dialog)
        audition = ComparisonAuditionWidget(self.project, dialog, gap_seconds=self.owner.gap.value())
        box.addWidget(audition)
        close = QPushButton("Close")
        close.clicked.connect(dialog.accept)
        box.addWidget(close, 0, Qt.AlignmentFlag.AlignRight)
        dialog.finished.connect(lambda _: audition.clear())
        dialog.resize(min(self.owner.canvas.width() + 24, self.owner.screen().availableGeometry().width() - 24),
                      max(330, min(self.owner.canvas.height() + 130, self.owner.screen().availableGeometry().height() - 60)))
        dialog.show()
        audition.set_items(self.first_ids, page_layout=self.layout_snapshot)
