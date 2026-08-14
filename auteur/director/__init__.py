"""The director: reads the brief, watches the dailies, writes the EDL."""

from .brief import Brief, parse_brief
from .heuristic import cut as heuristic_cut

__all__ = ["Brief", "parse_brief", "heuristic_cut", "direct"]


def direct(*args, **kwargs):  # pragma: no cover - thin dispatch helper
    """Direct an edit, preferring the model when one is reachable."""
    from .plan import direct as _direct

    return _direct(*args, **kwargs)
