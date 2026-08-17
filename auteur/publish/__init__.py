"""Getting a finished film to a platform — by hand, or through an API.

Kept apart from the crew on purpose. An agent may restructure a cut without
being asked; nothing in this package happens without a person asking for it.
"""

from .connections import ABOUT, PLATFORMS, Connection, Connections, Handoff, configured

__all__ = [
    "ABOUT",
    "PLATFORMS",
    "Connection",
    "Connections",
    "Handoff",
    "configured",
]
