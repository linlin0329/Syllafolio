'Read-only initial-candidate recipe and wall-clock timing; does not choose features or clusters.'
from __future__ import annotations

import copy
from datetime import datetime, timezone
import math
import time

from .annotation_english import system_text

from .ei_b_assistance import FEATURE_NAMES, EXCLUDED_INDICES, PARAMETERS

PHASES = (
    ("queue", 'Waiting in queue'),
    ("prepare", 'Input checks and freezing'),
    ("features", 'Feature extraction and saving'),
    ("cluster", 'Scaling, dimension reduction and clustering'),
    ("organize", 'Candidate organization and saving'),
    ("validate", 'Result validation and completion recording'),
)
PHASE_MAP = {"starting": "prepare", "freeze": "prepare", "features": "features",
             "global_cluster": "cluster", "organize": "organize", "validate": "validate", "ready": "validate"}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def feature_scheme():
    groups = [
        ('Amplitude envelope', 0, 5, 'Maximum, mean, standard deviation, peak-to-trough range and envelope change rate'),
        ("MFCC", 5, 45, 'Mean and standard deviation of 20 coefficients'),
        ('MFCC first-order change', 45, 85, 'Mean and standard deviation of 20 change coefficients'),
        ('Spectral statistics', 85, 93, 'Mean and standard deviation of centroid, bandwidth, roll-off and flatness'),
        ('Spectral contrast', 93, 100, 'Contrast statistics for seven frequency bands'),
        ('Duration', 100, 101, 'Syllable duration'),
        ('Zero-crossing rate', 101, 103, 'Mean and standard deviation'),
        ('RMS energy', 103, 105, 'Mean and standard deviation'),
    ]
    return {"id": "stage-a-existing-105d-umap10-hdbscan-leaf-v1", "fixed": True,
            "dimensions": len(FEATURE_NAMES),
            "groups": [{"name": name, "dimensions": stop - start, "description": description,
                        "feature_names": FEATURE_NAMES[start:stop]} for name, start, stop, description in groups],
            "pipeline": '105 dimensions → RobustScaler → 10-dimensional UMAP → fine-grained HDBSCAN candidates',
            "comparison_dimensions": len(FEATURE_NAMES) - len(EXCLUDED_INDICES),
            "comparison_note": 'Neighbor and cross-cluster prototype comparisons use a 94-dimensional representation excluding 11 energy-related dimensions, with Ward relationships; this is not the initial clustering input.',
            "logmel_note": 'A log-mel representation is also extracted for later local tools and is not concatenated into initial 105-dimensional clustering.',
            "parameters": copy.deepcopy(PARAMETERS)}


class RunTimer:
    'Time each attempt independently; saved snapshots cannot reconstruct unknown downtime.'

    def __init__(self, *, clock=None, wall_clock=None):
        self.clock = clock or time.monotonic
        self.wall_clock = wall_clock or utc_now
        self.start = self.phase_start = self.clock()
        self.end = None
        self.phase = "queue"
        self.state = "queued"
        self.rows = {key: {"id": key, "label": label, "seconds": 0.0,
                           "status": "not_started"} for key, label in PHASES}
        self.rows["queue"].update(status="running", started_at=self.wall_clock())

    def _elapsed(self, now, start):
        elapsed = now - start
        if not math.isfinite(elapsed) or elapsed < 0:
            raise ValueError('Invalid monotonic clock for run timing')
        return elapsed

    def transition(self, stage):
        phase = PHASE_MAP[stage]
        if phase == self.phase:
            return
        if self.end is not None:
            raise ValueError('A finished timer cannot start a new phase')
        stamp, wall = self.clock(), self.wall_clock()
        self.rows[self.phase].update(seconds=self._elapsed(stamp, self.phase_start),
                                     status="completed", ended_at=wall)
        self.rows[phase].update(status="running", started_at=wall)
        self.phase, self.phase_start, self.state = phase, stamp, "running"

    def finish(self, state):
        if self.end is None:
            self.end = self.clock()
            self.rows[self.phase].update(seconds=self._elapsed(self.end, self.phase_start),
                                         status="completed" if state == "succeeded" else state,
                                         ended_at=self.wall_clock())
        self.state = state

    def snapshot(self):
        stamp = self.clock() if self.end is None else self.end
        rows = copy.deepcopy(list(self.rows.values()))
        if self.end is None:
            next(row for row in rows if row["id"] == self.phase)["seconds"] = self._elapsed(stamp, self.phase_start)
        return {"schema_version": "annotation-run-timing-v1", "source": "monotonic_wall_clock",
                "state": self.state, "total_seconds": self._elapsed(stamp, self.start),
                "phases": rows, "current_phase": self.phase if self.end is None else None,
                "measurement_complete": self.end is not None, "captured_at": self.wall_clock()}


def mark_timing_interrupted(attempt):
    'Retain only the recorded lower bound; detecting a stopped process is not its computation end time.'
    timing = attempt.get("timing")
    if timing is not None:
        timing.update(state="interrupted", measurement_complete=False)
        for phase in timing["phases"]:
            if phase["status"] == "running":
                phase["status"] = "interrupted"
        timing["current_phase"] = None


def attempt_view(attempt, timer=None):
    result = {key: attempt.get(key) for key in ("attempt", "status", "started_at", "run_started_at", "ended_at")}
    if timer is not None:
        result["timing"] = timer.snapshot()
    elif attempt.get("timing"):
        result["timing"] = copy.deepcopy(attempt["timing"])
        for phase in result["timing"].get("phases", []):
            if "label" in phase:
                phase["label"] = system_text(phase["label"])
    else:
        seconds = None
        try:
            if attempt.get("status") != "interrupted":
                start = datetime.fromisoformat(attempt["started_at"])
                end = datetime.fromisoformat(attempt["ended_at"])
                delta = (end - start).total_seconds()
                if math.isfinite(delta) and delta >= 0:
                    seconds = delta
        except (KeyError, TypeError, ValueError, OverflowError):
            pass
        result["timing"] = {"source": "legacy_timestamps", "state": attempt.get("status"),
                            "total_seconds": seconds, "measurement_complete": seconds is not None,
                            "current_phase": None,
                            "phases": [{"id": key, "label": label, "seconds": None, "status": "unrecorded"}
                                       for key, label in PHASES]}
    return result
