"""Performance data, and what an edit can learn from it.

    from auteur.insight import corpus, fit, predict

    signals = corpus(["exports/short_form_video.csv"])   # real rows + simulation
    model = fit(signals)
    prediction = predict(edl, model)

Three things live here: a schema that normalises the three content forms onto
one record, a loader that reads real exports and a simulator that invents
plausible ones, and a scorer that turns a planned timeline into a prediction
about hook, share and loop.

**On simulated data.** With no real export this package will happily fit a
model to numbers it made up, because rehearsing the whole machine is worth
doing before there is anything to rehearse it on. It will also say so, every
time, in `FitReport.provenance`. Do not quote a number from here as evidence
about a platform.
"""

from __future__ import annotations

from pathlib import Path
from collections.abc import Sequence

from .dataset import CURVE_POINTS, load, simulate, write_csv, write_json
from .schema import (
    FORMS,
    HOOK_STYLES,
    TARGET_LOOP_COUNT,
    TARGET_SHARE_TO_VIEW,
    TARGET_THREE_SECOND_WATCH,
    TIER_WORDS,
    VELOCITY_WORDS,
    Signal,
    detect_form,
)
from .score import FitReport, Objective, Prediction, fit, predict

__all__ = [
    "CURVE_POINTS",
    "FORMS",
    "FitReport",
    "HOOK_STYLES",
    "Objective",
    "Prediction",
    "Signal",
    "TARGET_LOOP_COUNT",
    "TARGET_SHARE_TO_VIEW",
    "TARGET_THREE_SECOND_WATCH",
    "TIER_WORDS",
    "VELOCITY_WORDS",
    "corpus",
    "detect_form",
    "fit",
    "load",
    "predict",
    "simulate",
    "write_csv",
    "write_json",
]


def corpus(
    exports: Sequence[str | Path] = (),
    *,
    simulate_rows: int = 2000,
    seed: int = 0xC4A7E,
) -> list[Signal]:
    """Everything measured, plus enough simulated rows to fit on.

    Real rows are loaded first and used to anchor the simulation, so a handful
    of measured examples pull the whole corpus toward the truth rather than
    being drowned by it. Pass `simulate_rows=0` once you have enough real data
    that inventing more would only add noise.
    """
    measured = load(exports) if exports else []
    if simulate_rows <= 0:
        return measured
    return measured + simulate(simulate_rows, seed=seed, seeded_by=measured)
