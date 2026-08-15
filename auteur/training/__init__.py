"""Training metadata generator for the auteur agent crew.

Produces natural metadata datasets across sixteen creative and intellectual
disciplines — each one structured as a `metadata_domain` export that the
existing `auteur.insight.dataset.load` function can read without modification.

The disciplines are chosen to cover every lever an agent needs when deciding
how a piece of content should be built, presented, and refined:

  - **Visual perception:** color theory, art basics, art theory, art history,
    photography, cinematography
  - **Auditory perception:** music theory
  - **Narrative structure:** content creation, movie making, directing
  - **Human understanding:** human behavior, human condition, psychology,
    philosophy, psychological philosophy, pattern recognition

Each domain CSV contains rows that vary a `Primary_<domain>` lever (the main
concept being applied), a `Secondary_<domain>` lever (the supporting framework),
and a performance envelope — watch time, shares, saves, click-through — drawn
from distributions calibrated to the Tier-1 labelled corpus described in
`docs/agent-briefs.md`.

The distributions are *natural* in the sense that they exhibit realistic
variance, realistic correlation structure, and realistic failure rates. A
generated row can underperform; the dataset is not all winners. This is what
makes it useful for training agents that need to distinguish a good decision
from a plausible-sounding bad one.
"""
