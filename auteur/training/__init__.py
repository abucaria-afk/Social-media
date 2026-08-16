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
and a performance envelope — watch time, shares, saves, click-through.

**What this data is, exactly.** It is invented. The distributions are shaped to
look like a platform export — long right tail on views, a left tail of
underperformance, rates in plausible ranges — but no row corresponds to
anything that was ever posted, and the numbers are not calibrated against any
measured corpus. Fit a model on this alone and you have modelled this file.
`auteur insight fit` says so in its provenance line every time.

**What it is for.** Each creative lever — the primary concept, the palette, the
cognitive bias, the audio anchor — carries a fixed, hidden effect on the
outcome. That makes the ground truth *knowable*, which is the whole point: you
can check whether the crew recovers the levers that actually matter, and
whether it correctly ignores the ones that do not. A dataset where the choices
were drawn independently of the results could not test that, because the only
lesson available in it is that nothing you choose matters.
"""
