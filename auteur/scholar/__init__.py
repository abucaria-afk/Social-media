"""The Scholar — a self-directed learning agent that watches, learns, and teaches.

The Scholar is *separate* from the editing crew. It does not propose changes to
an edit in real time the way the Hook or Loop agents do. Instead it operates on
a longer cycle:

1. **Watch** — find and consume YouTube videos about craft disciplines it is
   studying: animation, VFX, colour grading, editing workflows in Premiere Pro,
   DaVinci Resolve, CapCut, iMovie, and Final Cut.

2. **Learn** — extract the main focus of each video, distil it into structured
   knowledge (techniques, principles, workflows), and store that knowledge in a
   persistent local corpus.

3. **Teach** — when the editing agents run, surface relevant learnings as
   context. When a workflow is consistently wrong, propose a workflow mutation
   that the agents will use going forward.

4. **Review** — watch the *final output* of the crew before it reaches the gate,
   applying everything it has learned to catch mistakes or missed opportunities
   that the narrow-objective agents cannot see.

The Scholar uses the Gaze agent's visual analysis as its perceptual layer —
it sees composition, exposure, palette, and focal weight the same way the Gaze
does — but wraps it in a learning loop rather than a single-pass optimiser.

**Autonomy model.** The Scholar may:
- Search YouTube and select videos to watch on its own initiative.
- Run when it detects new uploads from its most-watched creators.
- Accumulate knowledge without asking.

It may *not*:
- Apply its learnings directly. Teaching goes through the crew's scoring loop.
- Override the gate. Its review produces proposals, not decisions.
- Publish or schedule anything.
"""

from .scholar import Scholar
from .knowledge import KnowledgeStore, Learning, Discipline
from .youtube import YouTubeAccess, VideoMeta, Subscription
from .teach import TeachingBrief, WorkflowPatch
from .review import OutputReview, ReviewFinding

__all__ = [
    "Scholar",
    "KnowledgeStore",
    "Learning",
    "Discipline",
    "YouTubeAccess",
    "VideoMeta",
    "Subscription",
    "TeachingBrief",
    "WorkflowPatch",
    "OutputReview",
    "ReviewFinding",
]
