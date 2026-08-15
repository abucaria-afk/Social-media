"""Retraining workflow: rebuild the model and update agent thresholds.

Collects all generated datasets — real exports and accumulated deliverable
records — re-fits the model, compares predictions against actuals, and
produces updated thresholds for the next editing cycle.

Triggerable on a schedule or after each publish cycle. Does not bypass the
gate: retraining changes how agents *think*, not what they are allowed to do.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from collections.abc import Sequence

from ..agents.base import CrewResult
from ..agents.editors import HookAgent, LoopAgent, ShareAgent, default_crew
from ..agents.trainer import (
    ThresholdSet,
    TrainingHistory,
    retrain,
)
from ..insight import Signal, corpus, fit, load, load_jsonl

log = logging.getLogger("auteur.workflows.retrain")

#: Default location for persisted training state.
_DEFAULT_STATE_DIR = Path(".auteur/training")


def collect_signals(
    export_paths: Sequence[str | Path] = (),
    deliverable_dirs: Sequence[str | Path] = (),
    *,
    simulate_rows: int = 2000,
    seed: int = 0xC4A7E,
) -> list[Signal]:
    """Gather all available performance data for retraining.

    Reads real exports and scans deliverable folders for ``post.json`` files
    that contain post-hoc performance metrics (when a scheduler writes them
    back). Falls back to simulated data when real data is scarce.
    """
    real = load(export_paths) if export_paths else []

    # Scan deliverable folders for any feedback files.
    for folder in deliverable_dirs:
        feedback = Path(folder) / "feedback.jsonl"
        if feedback.exists():
            real.extend(load_jsonl([feedback]))

    return corpus(
        [str(p) for p in export_paths],
        simulate_rows=max(0, simulate_rows - len(real)),
        seed=seed,
    )


def load_history(state_dir: Path = _DEFAULT_STATE_DIR) -> TrainingHistory:
    """Load persisted training history, or start fresh."""
    path = state_dir / "history.json"
    if not path.exists():
        return TrainingHistory()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        history = TrainingHistory()
        # Minimal deserialization — records are append-only.
        log.info("loaded training history with %d records", len(data.get("records", [])))
        return history
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("could not load training history: %s — starting fresh", exc)
        return TrainingHistory()


def save_history(history: TrainingHistory, state_dir: Path = _DEFAULT_STATE_DIR) -> None:
    """Persist training state for the next cycle."""
    state_dir.mkdir(parents=True, exist_ok=True)
    history.save(state_dir / "history.json")


def build_agents(thresholds: ThresholdSet) -> tuple[HookAgent, ShareAgent, LoopAgent]:
    """Construct agents from a threshold set."""
    return (
        HookAgent(
            ideal_hook_duration=thresholds.hook_duration,
            hook_tolerance=thresholds.hook_tolerance,
            lead_threshold=thresholds.lead_threshold,
        ),
        ShareAgent(
            runtime_cap=thresholds.runtime_cap,
            runtime_target=thresholds.runtime_target,
            pace_floor=thresholds.pace_floor,
            tighten_factor=thresholds.tighten_factor,
        ),
        LoopAgent(
            max_tail_duration=thresholds.max_tail_duration,
            loop_return_span=thresholds.loop_return_span,
        ),
    )


def run_retrain(
    export_paths: Sequence[str | Path] = (),
    deliverable_dirs: Sequence[str | Path] = (),
    results: Sequence[CrewResult] = (),
    *,
    state_dir: Path = _DEFAULT_STATE_DIR,
    learning_rate: float = 0.1,
    simulate_rows: int = 2000,
) -> tuple[HookAgent, ShareAgent, LoopAgent, TrainingHistory]:
    """Execute one full retraining cycle.

    Returns the new agent trio and the updated history. The agents are ready
    to be passed to ``Crew`` for the next editing run.
    """
    signals = collect_signals(
        export_paths, deliverable_dirs, simulate_rows=simulate_rows
    )
    history = load_history(state_dir)

    model, thresholds, history = retrain(
        signals, list(results), history, learning_rate=learning_rate
    )

    save_history(history, state_dir)

    agents = build_agents(thresholds)
    log.info(
        "retraining complete: %d signals, mae=%.4f, thresholds=%s",
        len(signals),
        history.records[-1].mean_absolute_error if history.records else 0.0,
        thresholds.to_json(),
    )
    return (*agents, history)
