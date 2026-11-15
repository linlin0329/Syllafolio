"""Read-only comparison of one directional confusion with its recorded type examples."""
from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import json

from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFormLayout,
                               QHBoxLayout, QLabel, QPushButton, QVBoxLayout)

from .annotation_desktop_comparison_audition import ComparisonAuditionWidget
from .annotation_english import system_error


def confusion_cases(document, actual, predicted):
    """Count distinct held-out syllables in the requested direction across repeats."""
    result = document["result"]
    if result["status"] != "completed" or actual == predicted:
        raise ValueError("Select a scored, off-diagonal confusion pair.")
    repeats = result["repeats"]
    expected = {row["item_id"]: row["source_group_id"] for row in document["frozen_snapshot"]["items"]
                if row["family_id"] == actual and row["in_scope"] and row["label_status"] == "confirmed"
                and row["source_group_id"] is not None}
    if actual not in result["eligible_family_ids"] or predicted not in result["eligible_family_ids"]:
        raise ValueError("This family pair was not scored in this report.")
    seen_repeats = set()
    hits = Counter()
    for repeat in repeats:
        if repeat["repeat"] in seen_repeats:
            raise ValueError("Repeated partition identifier in the selected report.")
        seen_repeats.add(repeat["repeat"])
        seen = set()
        for row in repeat["predictions"]:
            if row["true_family_id"] != actual:
                continue
            ident = row["item_id"]
            if ident in seen or ident not in expected or expected[ident] != row["source_group_id"]:
                raise ValueError("Fold predictions do not match the frozen family and recording sources.")
            seen.add(ident)
            if row["predicted_family_id"] == predicted:
                hits[ident] += 1
        if seen != set(expected):
            raise ValueError("This repeat is missing held-out members of the selected family.")
    if not hits:
        raise ValueError("This direction has no held-out examples to listen to.")
    return [{"item_id": ident, "source_group_id": expected[ident], "hits": hits[ident], "total": len(repeats)}
            for ident in sorted(hits, key=lambda i: (-hits[i], expected[i], i))]


def _load_examples(read, actual, predicted, cards):
    document = json.loads(read())
    frozen = document["frozen_snapshot"]
    members = {row["local_type_id"]: set(row["member_ids"]) for row in frozen["memberships"]}
    choices = {}
    for fid in (actual, predicted):
        card = cards[fid]
        definition = frozen["definitions"].get(fid)
        if not definition or card.get("card_id") != definition.get("card_id"):
            raise ValueError("The family definition changed. Open a current assessment for this comparison.")
        refs = list(card["representative_ids"])
        if len(refs) != len(set(refs)) or not set(refs) <= members.get(fid, set()):
            raise ValueError("The representatives do not match this report's family membership.")
        choices[fid] = refs
    return confusion_cases(document, actual, predicted), choices


class _ComparisonPlayer(ComparisonAuditionWidget):
    def __init__(self, project, parent, gap, before_play):
        self._selection = ()
        self._before_play = before_play
        super().__init__(project, parent, gap_seconds=gap)
        self.stop_button.hide()
        self.loop_button = QPushButton("Loop")
        self.loop_button.setObjectName("comparisonLoop")
        self.loop_button.setEnabled(False)
        self.loop_button.clicked.connect(lambda: self._toggle(True))
        self.controls.insertWidget(1, self.loop_button)
        fit = QPushButton("Fit")
        fit.setToolTip("Restore the current review time scale")
        fit.clicked.connect(self.canvas.fit_page)
        self.controls.insertWidget(2, fit)
        self.canvas.itemClicked.connect(self._clicked)
        self.player.playingChanged.connect(self._buttons)

    def clear(self):
        self._selection = ()
        super().clear()
        if hasattr(self, "loop_button"):
            self.loop_button.setEnabled(False)

    def _page_ready(self, page):
        super()._page_ready(page)
        self.loop_button.setEnabled(True)
        self.status.setText("Click a syllable to select it. Ctrl-click to compare a selection.")

    def _buttons(self, _playing):
        active = self.player.sink is not None and not self.player.paused
        self.play_button.setText("Pause" if active and not self.player.looping else "Play")
        self.loop_button.setText("Pause" if active and self.player.looping else "Loop")

    def _clicked(self, ident, ctrl):
        if not self.play_button.isEnabled():
            return
        selected = set(self._selection)
        if ctrl:
            selected.symmetric_difference_update([ident])
        else:
            selected = set() if self._selection == (ident,) else {ident}
        self._selection = tuple(row["item_id"] for row in self.canvas.slots if row["item_id"] in selected)
        self.stop()
        self.canvas.select_items(self._selection)

    def _play(self):
        self._toggle(False)

    def _toggle(self, loop):
        if not self.play_button.isEnabled() or not self._before_play():
            return
        if self.player.sink is not None and self.player.looping == loop:
            if self.player.paused:
                self.player.resume()
            else:
                self.player.pause()
            return
        ranges = [(s["start_frame"], s["stop_frame"]) for s in self.canvas.slots if s["item_id"] in self._selection]
        if ranges:
            self.player.play_ranges(ranges, gap_frames=round(self.gap_seconds * self.player.rate), loop=loop)
        else:
            self.player.play(loop=loop)


class PairAuditionDialog(QDialog):
    """One selectable held-out example per page beside both families' representatives."""
    def __init__(self, parent, controller, job_id, actual_family_id, predicted_family_id):
        super().__init__(parent)
        self.assessment = parent
        self.owner = parent.owner
        self.project = controller.project
        self.job_id = job_id
        self.actual, self.predicted = actual_family_id, predicted_family_id
        state = self.project.snapshot()
        job = controller.job(job_id, state)
        if not parent.is_current() or job["status"] != "completed" or job["stale_for_current"]:
            raise ValueError("Open a current scored report to compare its syllables.")
        cards = {fid: state.get("type_cards", {}).get(fid, {}) for fid in (self.actual, self.predicted)}
        self.names = {fid: card.get("title") or fid for fid, card in cards.items()}
        if self.names[self.actual] == self.names[self.predicted]:
            self.names = {fid: f"{name} ({fid})" for fid, name in self.names.items()}
        read = controller.report_reader(job_id, state)
        self._bound_revision = state["revision"]
        self._digest = job["report_sha256"]
        self._closed = False
        self.rows = []
        self._loading = True
        self.setWindowTitle("Listen to confusion examples")
        space = parent.screen().availableGeometry()
        self.resize(min(980, space.width() - 24), min(695, space.height() - 55))
        layout = QVBoxLayout(self)
        self.pair_title = f"Human family: {self.names[self.actual]}   →   Predicted: {self.names[self.predicted]}"
        self.status = self._label(self.pair_title)
        layout.addWidget(self.status)
        navigation = QHBoxLayout()
        self.previous_button = QPushButton("Previous")
        self.next_button = QPushButton("Next")
        self.previous_button.clicked.connect(lambda: self.examples.setCurrentIndex(self.examples.currentIndex() - 1))
        self.next_button.clicked.connect(lambda: self.examples.setCurrentIndex(self.examples.currentIndex() + 1))
        self.examples = self._combo("confusionExamples", "Confused syllable")
        navigation.addWidget(QLabel("Example"))
        navigation.addWidget(self.examples, 1)
        navigation.addWidget(self.previous_button)
        navigation.addWidget(self.next_button)
        layout.addLayout(navigation)
        references = QFormLayout()
        self.true_rep = self._combo("trueFamilyRepresentative", "Human family representative")
        self.predicted_rep = self._combo("predictedFamilyRepresentative", "Predicted family representative")
        for fid, combo in ((self.actual, self.true_rep), (self.predicted, self.predicted_rep)):
            references.addRow(self._label("Representative · " + self.names[fid]), combo)
        layout.addLayout(references)
        self.legend = self._label("Reading the selected assessment…")
        layout.addWidget(self.legend)
        self.audition = _ComparisonPlayer(self.project, self, self.owner.gap.value(), self._before_play)
        layout.addWidget(self.audition, 1)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)
        layout.addWidget(close)
        self.previous_button.setEnabled(False)
        self.next_button.setEnabled(False)
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="assessment-examples")
        self._future = executor.submit(_load_examples, read, self.actual, self.predicted, cards)
        executor.shutdown(wait=False)
        self._watch = QTimer(self)
        self._watch.setInterval(100)
        self._watch.timeout.connect(self._tick)
        self._watch.start()
        self.finished.connect(self._retire)

    def _combo(self, name, accessible_name):
        combo = QComboBox()
        combo.setObjectName(name)
        combo.setAccessibleName(accessible_name)
        combo.setMinimumWidth(80)
        combo.currentIndexChanged.connect(self._page_changed)
        return combo

    @staticmethod
    def _label(text):
        label = QLabel(text)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        return label

    def _current(self):
        if self._closed or not self.assessment.is_current() or self.assessment._selected_job != self.job_id:
            return False
        state = self.owner.state
        if not state:
            return False
        if state["revision"] != self._bound_revision:
            jobs = state.get("final_review", {}).get("assessment", {}).get("jobs", [])
            matching = next((j for j in jobs if j["job_id"] == self.job_id), None)
            if not matching or matching["stale_for_current"] or matching.get("report_sha256") != self._digest:
                return False
            self._bound_revision = state["revision"]
        return True

    def _tick(self):
        if not self._current():
            self.reject()
            return
        if self._future is None or not self._future.done():
            return
        future, self._future = self._future, None
        try:
            self.rows, choices = future.result()
            source_count = len({row["source_group_id"] for row in self.rows})
            self.status.setText(f"{self.pair_title}\nSyllables: {len(self.rows)}   ·   Original recordings: {source_count}")
            for i, row in enumerate(self.rows, 1):
                self.examples.addItem(f"{i}/{len(self.rows)}   {row['item_id']} · {row['source_group_id']} · "
                                      f"{row['hits']}/{row['total']} repeats", row["item_id"])
            for fid, combo in ((self.actual, self.true_rep), (self.predicted, self.predicted_rep)):
                for ident in choices[fid]:
                    combo.addItem(ident, ident)
                if not choices[fid]:
                    combo.addItem("No representative recorded", None)
                    combo.setEnabled(False)
            self._loading = False
            self._page_changed()
        except Exception as exc:
            self.audition.clear()
            self.legend.setText(system_error(exc))

    def _page_changed(self, *_args):
        if self._loading or not self._current():
            return
        index = self.examples.currentIndex()
        if index < 0:
            return
        self.previous_button.setEnabled(index > 0)
        self.next_button.setEnabled(index + 1 < len(self.rows))
        roles = {}
        for ident, role in ((self.examples.currentData(), "Example"),
                            (self.true_rep.currentData(), self.names[self.actual] + " representative"),
                            (self.predicted_rep.currentData(), self.names[self.predicted] + " representative")):
            if ident is not None:
                roles.setdefault(ident, []).append(role)
        self.legend.setText("    ".join(f"{n} · {ident} ({' / '.join(role)})" for n, (ident, role) in enumerate(roles.items(), 1)))
        # All three comparison roles share one time axis, even when routine review
        # is set to one item per page. Keep the user's milliseconds per logical pixel.
        view = self.owner.display_controller
        scale = dict(self.owner.review_page_layout(), mode="fixed", count=3)
        preferences = view.snapshot()
        self.audition.canvas.set_waveform_range(view.waveform_range)
        self.audition.set_items(list(roles), page_layout=scale, settings=preferences["settings"],
                                normalization=preferences["normalization"])

    def _before_play(self):
        if not self._current():
            self.reject()
            return False
        self.owner._stop_review_audio()
        return True

    def changeEvent(self, event):
        if event.type() == QEvent.Type.ActivationChange and hasattr(self, "audition") and not self.isActiveWindow():
            self.audition.stop()
        super().changeEvent(event)

    def _retire(self, *_args):
        self._closed = True
        self._watch.stop()
        if self._future:
            self._future.cancel()
            self._future = None
        self.audition.clear()
