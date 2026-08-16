"""Public-domain art, searched and sifted.

Three collections give their public-domain work away with no key: the
Metropolitan Museum of Art, the Art Institute of Chicago and the Cleveland
Museum of Art. Between them that is hundreds of thousands of images, most of
which are catalogue photographs of objects rather than pictures.

`Curator` searches them and keeps the ones that are pictures. See
`auteur.gallery.curator` for why that could not be done with the craft score
this project already had.
"""

from .curator import (
    ALL_OVER,
    Candidate,
    Curation,
    Curator,
    FLAT_CONTRAST,
    FLAT_HUE,
    Judgement,
    looks_like_a_record_shot,
    paperwork_clears,
)
from .sources import COLLECTIONS, Transport, Unavailable, Web, search_all

__all__ = [
    "ALL_OVER",
    "COLLECTIONS",
    "Candidate",
    "Curation",
    "Curator",
    "FLAT_CONTRAST",
    "FLAT_HUE",
    "Judgement",
    "Transport",
    "Unavailable",
    "Web",
    "looks_like_a_record_shot",
    "paperwork_clears",
    "search_all",
]
