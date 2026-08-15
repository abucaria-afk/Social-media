"""Continuous training: calibrate agent thresholds against measured outcomes.

The training loop compares what the agents *predicted* they would achieve
(stored in ``CrewResult``) against what *actually* happened (measured as
``Signal`` rows in the next export). The gap between the two — calibration
error — tells us which thresholds to move and by how much.

Nothing here changes agent *structure* — only the numbers they use to decide
when to propose. The agents still own one objective each, the crew still
hill-climbs, and the gate still holds the line on publishing.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Sequence

from ..insight import FitReport, Signal, fit
from ..insight.schema import (
    TARGET_LOOP_COUNT,
    TARGET_SHARE_TO_VIEW,
    TARGET_THREE_SECOND_WATCH,
)
from .base import CrewResult

log = logging.getLogger("auteur.agents.trainer")


@dataclass
class CalibrationRecord:
    """One comparison between what we predicted and what happened."""

    predicted_hook: float
    actual_hook: float
    predicted_share: float
    actual_share: float
    predicted_loop: float
    actual_loop: float
    timestamp: float = field(default_factory=time.time)

    @property
    def hook_error(self) -> float:
        return self.predicted_hook - self.actual_hook

    @property
    def share_error(self) -> float:
        return self.predicted_share - self.actual_share

    @property
    def loop_error(self) -> float:
        return self.predicted_loop - self.actual_loop

    @property
    def mean_absolute_error(self) -> float:
        return (abs(self.hook_error) + abs(self.share_error) + abs(self.loop_error)) / 3

    def to_json(self) -> dict:
        return {
            "predicted_hook": round(self.predicted_hook, 4),
            "actual_hook": round(self.actual_hook, 4),
            "predicted_share": round(self.predicted_share, 4),
            "actual_share": round(self.actual_share, 4),
            "predicted_loop": round(self.predicted_loop, 4),
            "actual_loop": round(self.actual_loop, 4),
            "hook_error": round(self.hook_error, 4),
            "share_error": round(self.share_error, 4),
            "loop_error": round(self.loop_error, 4),
            "mae": round(self.mean_absolute_error, 4),
            "timestamp": self.timestamp,
        }


@dataclass
class ThresholdSet:
    """The tuneable numbers that govern agent behaviour."""

    hook_duration: float = 1.6
    hook_tolerance: float = 0.35
    lead_threshold: float = 1.6
    runtime_cap: float = 22.0
    runtime_target: float = 18.0
    pace_floor: float = 0.8
    tighten_factor: float = 0.78
    max_tail_duration: float = 2.0
    loop_return_span: float = 0.9

    def to_json(self) -> dict:
        return {
            "hook_duration": self.hook_duration,
            "hook_tolerance": self.hook_tolerance,
            "lead_threshold": self.lead_threshold,
            "runtime_cap": self.runtime_cap,
            "runtime_target": self.runtime_target,
            "pace_floor": self.pace_floor,
            "tighten_factor": self.tighten_factor,
            "max_tail_duration": self.max_tail_duration,
            "loop_return_span": self.loop_return_span,
        }

    @classmethod
    def from_model(cls, model: FitReport) -> "ThresholdSet":
        """Derive thresholds from a fitted model."""
        thresholds = cls()
        if model.best_hook_duration:
            thresholds.hook_duration = model.best_hook_duration
        return thresholds


@dataclass
class TrainingHistory:
    """Accumulated training state across retraining cycles."""

    records: list[CalibrationRecord] = field(default_factory=list)
    threshold_log: list[tuple[float, ThresholdSet]] = field(default_factory=list)

    @property
    def latest_thresholds(self) -> ThresholdSet | None:
        return self.threshold_log[-1][1] if self.threshold_log else None

    @property
    def calibration_improving(self) -> bool:
        """Whether the last cycle improved on the one before it."""
        if len(self.records) < 2:
            return True
        return self.records[-1].mean_absolute_error <= self.records[-2].mean_absolute_error

    def to_json(self) -> dict:
        return {
            "records": [r.to_json() for r in self.records[-20:]],
            "threshold_snapshots": len(self.threshold_log),
            "latest_mae": round(self.records[-1].mean_absolute_error, 4) if self.records else None,
            "improving": self.calibration_improving,
        }

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_json(), indent=2), encoding="utf-8")


def calibrate(
    results: Sequence[CrewResult],
    actuals: Sequence[Signal],
) -> list[CalibrationRecord]:
    """Compare predicted outcomes against measured performance.

    Each ``CrewResult`` carries the prediction the crew made at editing time.
    Each ``Signal`` carries the real performance observed after publishing.
    We match them by position (oldest result to oldest signal).
    """
    records: list[CalibrationRecord] = []
    for result, signal in zip(results, actuals, strict=False):
        records.append(
            CalibrationRecord(
                predicted_hook=result.final.hook.predicted,
                actual_hook=signal.three_second_watch_rate,
                predicted_share=result.final.share.predicted,
                actual_share=signal.share_to_view_ratio,
                predicted_loop=result.final.loop.predicted,
                actual_loop=signal.loop_count,
            )
        )
    return records


def adjust_thresholds(
    current: ThresholdSet,
    records: Sequence[CalibrationRecord],
    *,
    learning_rate: float = 0.1,
) -> ThresholdSet:
    """Nudge thresholds toward better calibration.

    Conservative by design: large moves risk over-correcting on a handful of
    posts, and a threshold that oscillates is worse than one that converges
    slowly. The learning rate caps any single adjustment.
    """
    if not records:
        return current

    mean_hook_err = sum(r.hook_error for r in records) / len(records)
    mean_share_err = sum(r.share_error for r in records) / len(records)

    adjusted = ThresholdSet(
        hook_duration=current.hook_duration,
        hook_tolerance=current.hook_tolerance,
        lead_threshold=current.lead_threshold,
        runtime_cap=current.runtime_cap,
        runtime_target=current.runtime_target,
        pace_floor=current.pace_floor,
        tighten_factor=current.tighten_factor,
        max_tail_duration=current.max_tail_duration,
        loop_return_span=current.loop_return_span,
    )

    # If we consistently over-predict the hook, the ideal duration is too
    # aggressive — loosen it slightly.
    if mean_hook_err > 0.02:
        adjusted.hook_duration = current.hook_duration + learning_rate * 0.3
        adjusted.hook_tolerance = min(0.6, current.hook_tolerance + learning_rate * 0.05)
    elif mean_hook_err < -0.02:
        adjusted.hook_duration = max(0.8, current.hook_duration - learning_rate * 0.3)
        adjusted.hook_tolerance = max(0.15, current.hook_tolerance - learning_rate * 0.05)

    # If we over-predict shares, the runtime cap may be too lenient.
    if mean_share_err > 0.02:
        adjusted.runtime_cap = max(15.0, current.runtime_cap - learning_rate * 2.0)
        adjusted.pace_floor = min(1.5, current.pace_floor + learning_rate * 0.1)
    elif mean_share_err < -0.02:
        adjusted.runtime_cap = min(30.0, current.runtime_cap + learning_rate * 2.0)
        adjusted.pace_floor = max(0.4, current.pace_floor - learning_rate * 0.1)

    return adjusted


def retrain(
    signals: Sequence[Signal],
    results: Sequence[CrewResult],
    history: TrainingHistory | None = None,
    *,
    learning_rate: float = 0.1,
) -> tuple[FitReport, ThresholdSet, TrainingHistory]:
    """Full retraining cycle: re-fit the model, calibrate, adjust thresholds.

    Returns the new model, the updated thresholds, and the extended history.
    """
    history = history or TrainingHistory()
    model = fit(signals)

    # Start from model-derived thresholds, then adjust from calibration.
    base = ThresholdSet.from_model(model)
    if history.latest_thresholds is not None:
        base = history.latest_thresholds

    # Calibrate against any results that have corresponding actuals.
    new_records = calibrate(results, signals[-len(results) :] if results else [])
    history.records.extend(new_records)

    # Use the full record history (capped) for adjustment.
    recent = history.records[-50:]
    thresholds = adjust_thresholds(base, recent, learning_rate=learning_rate)
    history.threshold_log.append((time.time(), thresholds))

    log.info(
        "retrained: mae=%.4f, hook_dur=%.2f, runtime_cap=%.1f, pace=%.2f",
        recent[-1].mean_absolute_error if recent else 0.0,
        thresholds.hook_duration,
        thresholds.runtime_cap,
        thresholds.pace_floor,
    )
    return model, thresholds, history
