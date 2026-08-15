"""The Scholar agent — watches, learns, teaches, reviews.

This is the orchestrator that ties the YouTube access layer, knowledge store,
teaching layer, and output review into a single autonomous learning loop.

The Scholar is *separate* from the editing crew. It runs on its own schedule:
- Periodically, to fill knowledge gaps across its disciplines.
- When a most-watched creator uploads a new video.
- Before publishing, to review the final output.

It uses the Gaze agent's perceptual analysis as its foundation for understanding
visual content — composition, colour, focal weight, exposure — and extends it
with structured learning from YouTube tutorials and reference material.

The disciplines it studies:

**Theory:** color theory, music theory, art history, art basics, art theory,
photography, cinematography, human behavior, human condition, psychology,
philosophy, psychological philosophy, pattern recognition, content creation,
movie making, directing.

**Tools:** animation, computer SFX generation, Adobe Premiere Pro, CapCut,
iMovie, DaVinci Resolve, After Effects, Final Cut Pro.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Sequence

from ..edl import EditDecisionList
from ..insight import FitReport, Prediction, predict
from ..agents.base import Proposal, Risk
from ..agents.gaze import GazeAgent
from .knowledge import (
    Confidence,
    Discipline,
    KnowledgeStore,
    Learning,
    THEORY_DISCIPLINES,
    TOOL_DISCIPLINES,
)
from .youtube import YouTubeAccess, SearchStrategy, VideoMeta, Subscription
from .teach import Teacher, TeachingBrief, WorkflowPatch
from .review import OutputReview, ReviewFinding
from .auditory import AuditorySystem, AudioSegment, AudioVisualState, ListeningSession
from .speech import SpeechSystem, SpeechResponse, CommunicationMode, VoiceStyle

log = logging.getLogger("auteur.scholar")

#: Search queries the Scholar uses for each discipline when filling gaps.
_CURRICULUM: dict[Discipline, list[str]] = {
    Discipline.COLOR_THEORY: [
        "color theory for filmmakers",
        "color grading psychology",
        "colour palette design cinematic",
    ],
    Discipline.MUSIC_THEORY: [
        "music theory for video editors",
        "sound design short form video",
        "audio pacing retention",
    ],
    Discipline.ART_HISTORY: [
        "art history composition techniques film",
        "renaissance composition in cinematography",
        "baroque lighting for video",
    ],
    Discipline.ART_BASICS: [
        "visual design fundamentals video",
        "composition rules for content creators",
        "negative space in video editing",
    ],
    Discipline.ART_THEORY: [
        "contemporary art theory digital media",
        "semiotics in visual content",
        "affect theory and engagement",
    ],
    Discipline.PHOTOGRAPHY: [
        "photography composition for video",
        "lighting techniques content creation",
        "lens choice storytelling",
    ],
    Discipline.CINEMATOGRAPHY: [
        "cinematography techniques short form",
        "camera movement emotion",
        "framing and blocking for engagement",
    ],
    Discipline.HUMAN_BEHAVIOR: [
        "audience psychology content creation",
        "behavioral triggers viral content",
        "social proof in video marketing",
    ],
    Discipline.HUMAN_CONDITION: [
        "storytelling universal themes",
        "emotional resonance in short content",
        "vulnerability in content creation",
    ],
    Discipline.PSYCHOLOGY: [
        "psychology of attention retention",
        "cognitive biases content creation",
        "peak end rule video editing",
    ],
    Discipline.PHILOSOPHY: [
        "philosophy of storytelling",
        "philosophical content that goes viral",
        "existentialism in modern media",
    ],
    Discipline.PSYCHOLOGICAL_PHILOSOPHY: [
        "meaning making in content",
        "Jungian archetypes in branding",
        "authenticity psychology content creation",
    ],
    Discipline.PATTERN_RECOGNITION: [
        "pattern recognition in editing",
        "visual rhythm attention science",
        "gestalt principles video editing",
    ],
    Discipline.CONTENT_CREATION: [
        "content creation masterclass retention",
        "viral video structure breakdown",
        "hook techniques top creators",
    ],
    Discipline.MOVIE_MAKING: [
        "filmmaking techniques for short form",
        "story structure in 60 seconds",
        "sound design micro films",
    ],
    Discipline.DIRECTING: [
        "directing techniques solo creator",
        "blocking and staging short form",
        "performance direction non-actors",
    ],
    Discipline.ANIMATION: [
        "animation principles for video editors",
        "motion graphics tutorial 2024",
        "keyframe animation fundamentals",
        "2D animation for content creators",
    ],
    Discipline.COMPUTER_SFX: [
        "visual effects for content creators",
        "compositing tutorial beginners",
        "VFX breakdown short form video",
        "particle effects tutorial",
    ],
    Discipline.PREMIERE_PRO: [
        "premiere pro workflow efficiency",
        "premiere pro color grading tutorial",
        "premiere pro effects masterclass",
        "adobe premiere pro tips 2024",
    ],
    Discipline.DAVINCI_RESOLVE: [
        "davinci resolve color grading masterclass",
        "davinci resolve editing workflow",
        "davinci resolve fusion tutorial",
        "davinci resolve free features tutorial",
    ],
    Discipline.CAPCUT: [
        "capcut editing tutorial advanced",
        "capcut effects and transitions",
        "capcut workflow professional",
        "capcut keyframe animation",
    ],
    Discipline.IMOVIE: [
        "imovie professional results tutorial",
        "imovie editing tips advanced",
        "imovie color correction",
        "imovie workflow for creators",
    ],
    Discipline.AFTER_EFFECTS: [
        "after effects motion graphics tutorial",
        "after effects compositing",
        "after effects expressions tutorial",
        "after effects workflow premiere pro",
    ],
    Discipline.FINAL_CUT: [
        "final cut pro editing workflow",
        "final cut pro color grading",
        "final cut pro effects tutorial",
        "final cut pro multicam editing",
    ],
}


@dataclass
class StudySession:
    """Record of one Scholar study session."""

    started_at: float = field(default_factory=time.time)
    strategy: SearchStrategy = SearchStrategy.CURRICULUM
    discipline: Discipline | None = None
    videos_watched: int = 0
    learnings_extracted: int = 0
    duration_sec: float = 0.0

    def to_json(self) -> dict:
        return {
            "started_at": self.started_at,
            "strategy": self.strategy.value,
            "discipline": self.discipline.value if self.discipline else None,
            "videos_watched": self.videos_watched,
            "learnings_extracted": self.learnings_extracted,
            "duration_sec": round(self.duration_sec, 1),
        }


class Scholar:
    """The autonomous learning agent.

    Separate from the editing crew. Watches YouTube, extracts knowledge,
    teaches other agents, and reviews final output.

    Autonomy rules:
    - MAY search YouTube and watch videos without asking.
    - MAY accumulate knowledge without permission.
    - MAY NOT apply changes directly — teaching goes through the crew.
    - MAY NOT publish or schedule anything.
    - MAY NOT override the gate.
    """

    name = "scholar"
    objective = "knowledge_accumulation_and_application"

    def __init__(
        self,
        *,
        store: KnowledgeStore | None = None,
        youtube: YouTubeAccess | None = None,
        base_dir: Path | None = None,
    ):
        self._base_dir = base_dir or Path.home() / ".auteur" / "scholar"
        self._base_dir.mkdir(parents=True, exist_ok=True)

        self._store = store or KnowledgeStore(self._base_dir / "knowledge.jsonl")
        self._youtube = youtube or YouTubeAccess(
            subscriptions_path=self._base_dir / "subscriptions.json",
            cache_dir=self._base_dir / "yt_cache",
        )
        self._teacher = Teacher(self._store)
        self._reviewer = OutputReview(self._store)
        self._gaze = GazeAgent()
        self._auditory = AuditorySystem()
        self._speech = SpeechSystem()
        self._sessions: list[StudySession] = []

    @property
    def knowledge(self) -> KnowledgeStore:
        return self._store

    @property
    def youtube(self) -> YouTubeAccess:
        return self._youtube

    @property
    def teacher(self) -> Teacher:
        return self._teacher

    @property
    def auditory(self) -> AuditorySystem:
        return self._auditory

    @property
    def speech(self) -> SpeechSystem:
        return self._speech

    # ------------------------------------------------------------------
    # Autonomous learning loop
    # ------------------------------------------------------------------

    def should_study(self) -> tuple[bool, str]:
        """Decide whether the Scholar should run a study session now.

        Returns (should_run, reason). Triggers:
        1. Knowledge gaps exist in any discipline.
        2. A most-watched creator has new uploads.
        3. It has been more than 24 hours since the last session.
        """
        gaps = self._store.gaps()
        if gaps:
            return True, f"knowledge gaps in {len(gaps)} disciplines: {[g.value for g in gaps[:3]]}"

        new_uploads = self._youtube.check_new_uploads()
        if new_uploads:
            channels = [sub.channel_name for sub, _ in new_uploads]
            return True, f"new uploads from: {', '.join(channels)}"

        if self._sessions:
            last = self._sessions[-1]
            hours_since = (time.time() - last.started_at) / 3600
            if hours_since > 24:
                return True, f"last session was {hours_since:.0f} hours ago"

        if not self._sessions:
            return True, "no study sessions yet — initial learning run"

        return False, "no trigger active"

    def study(
        self,
        *,
        strategy: SearchStrategy | None = None,
        discipline: Discipline | None = None,
        max_videos: int = 5,
    ) -> StudySession:
        """Run a study session — search, watch, and learn.

        If no strategy is given, the Scholar decides based on its current state:
        - Gaps → CURRICULUM
        - New uploads → SUBSCRIPTION_CHECK
        - Otherwise → GAP_FILL
        """
        session = StudySession(
            strategy=strategy or SearchStrategy.CURRICULUM, discipline=discipline
        )

        if strategy is None:
            strategy, session.strategy = self._choose_strategy()

        if discipline is None and strategy == SearchStrategy.CURRICULUM:
            gaps = self._store.gaps()
            discipline = gaps[0] if gaps else Discipline.CONTENT_CREATION
            session.discipline = discipline

        # Get videos to watch
        videos = self._find_videos(strategy, discipline, max_videos)

        # Watch and extract learnings
        for video in videos:
            if self._store.already_watched(video.video_id):
                continue

            learnings = self._extract_learnings(video, discipline)
            for learning in learnings:
                self._store.add(learning)
                session.learnings_extracted += 1

            session.videos_watched += 1
            if session.videos_watched >= max_videos:
                break

        session.duration_sec = time.time() - session.started_at
        self._sessions.append(session)
        log.info(
            "study session complete: %d videos, %d learnings (%s)",
            session.videos_watched,
            session.learnings_extracted,
            session.strategy.value,
        )
        return session

    def _choose_strategy(self) -> tuple[SearchStrategy, SearchStrategy]:
        """Pick the best strategy based on current state."""
        new_uploads = self._youtube.check_new_uploads()
        if new_uploads:
            return SearchStrategy.SUBSCRIPTION_CHECK, SearchStrategy.SUBSCRIPTION_CHECK

        gaps = self._store.gaps()
        if gaps:
            return SearchStrategy.CURRICULUM, SearchStrategy.CURRICULUM

        return SearchStrategy.GAP_FILL, SearchStrategy.GAP_FILL

    def _find_videos(
        self,
        strategy: SearchStrategy,
        discipline: Discipline | None,
        max_videos: int,
    ) -> list[VideoMeta]:
        """Find videos to watch based on the strategy."""
        if strategy == SearchStrategy.SUBSCRIPTION_CHECK:
            uploads = self._youtube.check_new_uploads()
            return [video for _, video in uploads[:max_videos]]

        if strategy == SearchStrategy.CURRICULUM and discipline:
            queries = _CURRICULUM.get(discipline, [])
            if not queries:
                return []
            # Rotate through queries based on how many sessions we've had
            query = queries[len(self._sessions) % len(queries)]
            return self._youtube.search(query, max_results=max_videos)

        if strategy == SearchStrategy.GAP_FILL:
            gaps = self._store.gaps()
            if not gaps:
                return []
            gap = gaps[0]
            queries = _CURRICULUM.get(gap, [f"{gap.value} tutorial"])
            return self._youtube.search(queries[0], max_results=max_videos)

        return []

    def _extract_learnings(self, video: VideoMeta, discipline: Discipline | None) -> list[Learning]:
        """Extract structured learnings from a video's metadata and transcript.

        This is where the Scholar does its actual learning. It reads the video's
        title, description, chapters, and transcript to identify:
        1. The main focus/topic of the video.
        2. Key techniques or principles being taught.
        3. How those techniques apply to content creation.

        The Gaze foundation means it understands visual principles — when a video
        discusses composition, colour, or movement, the Scholar can map those to
        the same analysis the Gaze agent uses on the timeline.
        """
        learnings: list[Learning] = []

        # Determine disciplines from video content
        disciplines = [discipline] if discipline else self._infer_disciplines(video)

        # Determine if this is a tool-specific tutorial
        tool = self._detect_tool(video)

        # Extract the main focus from chapters (most structured source)
        if video.chapters:
            for chapter in video.chapters:
                chapter_title = chapter.get("title", "")
                if not chapter_title:
                    continue

                learning_id = hashlib.sha256(
                    f"{video.video_id}:{chapter_title}".encode()
                ).hexdigest()[:16]

                learnings.append(
                    Learning(
                        learning_id=learning_id,
                        disciplines=[d for d in disciplines if d is not None],
                        insight=f"From '{video.title}': {chapter_title}",
                        technique=chapter_title,
                        application=self._infer_application(chapter_title, disciplines),
                        source_video_id=video.video_id,
                        source_channel=video.channel,
                        source_title=video.title,
                        source_start_sec=chapter.get("start_time", 0),
                        source_end_sec=chapter.get("end_time", 0),
                        tool=tool,
                        confidence=Confidence.TENTATIVE,
                    )
                )

        # If no chapters, extract from the video as a whole
        if not learnings:
            learning_id = hashlib.sha256(video.video_id.encode()).hexdigest()[:16]
            learnings.append(
                Learning(
                    learning_id=learning_id,
                    disciplines=[d for d in disciplines if d is not None],
                    insight=f"Main focus of '{video.title}': {self._summarise_focus(video)}",
                    technique=self._extract_technique(video),
                    application=self._infer_application(video.title, disciplines),
                    source_video_id=video.video_id,
                    source_channel=video.channel,
                    source_title=video.title,
                    tool=tool,
                    confidence=Confidence.TENTATIVE,
                )
            )

        return learnings

    def _infer_disciplines(self, video: VideoMeta) -> list[Discipline]:
        """Guess which disciplines a video covers from its metadata."""
        text = f"{video.title} {video.description} {' '.join(video.tags)}".lower()

        matches: list[Discipline] = []
        keyword_map: dict[str, Discipline] = {
            "color": Discipline.COLOR_THEORY,
            "colour": Discipline.COLOR_THEORY,
            "palette": Discipline.COLOR_THEORY,
            "grading": Discipline.COLOR_THEORY,
            "music": Discipline.MUSIC_THEORY,
            "audio": Discipline.MUSIC_THEORY,
            "sound design": Discipline.MUSIC_THEORY,
            "tempo": Discipline.MUSIC_THEORY,
            "cinematography": Discipline.CINEMATOGRAPHY,
            "camera": Discipline.CINEMATOGRAPHY,
            "lighting": Discipline.CINEMATOGRAPHY,
            "composition": Discipline.ART_BASICS,
            "design": Discipline.ART_BASICS,
            "psychology": Discipline.PSYCHOLOGY,
            "attention": Discipline.PSYCHOLOGY,
            "retention": Discipline.PSYCHOLOGY,
            "philosophy": Discipline.PHILOSOPHY,
            "directing": Discipline.DIRECTING,
            "premiere": Discipline.PREMIERE_PRO,
            "davinci": Discipline.DAVINCI_RESOLVE,
            "resolve": Discipline.DAVINCI_RESOLVE,
            "capcut": Discipline.CAPCUT,
            "imovie": Discipline.IMOVIE,
            "after effects": Discipline.AFTER_EFFECTS,
            "final cut": Discipline.FINAL_CUT,
            "animation": Discipline.ANIMATION,
            "motion graphics": Discipline.ANIMATION,
            "vfx": Discipline.COMPUTER_SFX,
            "visual effects": Discipline.COMPUTER_SFX,
            "sfx": Discipline.COMPUTER_SFX,
        }

        for keyword, discipline in keyword_map.items():
            if keyword in text and discipline not in matches:
                matches.append(discipline)

        return matches or [Discipline.CONTENT_CREATION]

    def _detect_tool(self, video: VideoMeta) -> str:
        """Detect if a video is about a specific NLE tool."""
        text = f"{video.title} {video.description}".lower()
        tool_keywords = {
            "premiere pro": "premiere_pro",
            "davinci resolve": "davinci_resolve",
            "capcut": "capcut",
            "imovie": "imovie",
            "after effects": "after_effects",
            "final cut": "final_cut",
        }
        for keyword, tool in tool_keywords.items():
            if keyword in text:
                return tool
        return ""

    def _summarise_focus(self, video: VideoMeta) -> str:
        """Identify the main focus of a video from its metadata."""
        # Use title as primary signal, description as secondary
        title = video.title
        # Strip common YouTube title patterns
        for noise in ("| Tutorial", "- Tutorial", "(Full Guide)", "[2024]", "[2025]"):
            title = title.replace(noise, "")
        return title.strip()

    def _extract_technique(self, video: VideoMeta) -> str:
        """Extract the core technique being taught."""
        # From tags if available
        if video.tags:
            # Prefer longer, more specific tags
            sorted_tags = sorted(video.tags, key=len, reverse=True)
            return sorted_tags[0] if sorted_tags else video.title
        return video.title

    def _infer_application(self, title: str, disciplines: list[Discipline | None]) -> str:
        """Infer how a technique applies to content creation."""
        # Context-dependent application inference
        clean_disciplines = [d for d in disciplines if d is not None]
        if any(d in TOOL_DISCIPLINES for d in clean_disciplines):
            return f"Apply in editing workflow: {title}"
        if Discipline.PSYCHOLOGY in clean_disciplines:
            return f"Apply to audience engagement: {title}"
        if Discipline.CINEMATOGRAPHY in clean_disciplines:
            return f"Apply to shot design and camera work: {title}"
        if Discipline.COLOR_THEORY in clean_disciplines:
            return f"Apply to colour grading and palette design: {title}"
        return f"Apply to content creation: {title}"

    # ------------------------------------------------------------------
    # Teaching interface
    # ------------------------------------------------------------------

    def teach(self, agent_name: str) -> TeachingBrief:
        """Generate a teaching brief for a specific agent."""
        return self._teacher.brief_for_agent(agent_name)

    def teach_all(self) -> TeachingBrief:
        """Generate a teaching brief for the entire crew."""
        return self._teacher.brief_for_all()

    def propose_workflow_changes(self) -> list[WorkflowPatch]:
        """Propose workflow changes backed by validated learnings."""
        return self._teacher.propose_patches()

    # ------------------------------------------------------------------
    # Auditory interface — hearing and listening
    # ------------------------------------------------------------------

    def hear(self, audio_data: bytes, sample_rate: int = 44100) -> list[AudioSegment]:
        """Hear audio passively — classify channels and energy."""
        return self._auditory.hear(audio_data, sample_rate)

    def listen(
        self, audio_data: bytes, *, source_id: str = "", sample_rate: int = 44100
    ) -> ListeningSession:
        """Actively listen to audio — full transcription and analysis."""
        return self._auditory.listen(audio_data, source_id=source_id, sample_rate=sample_rate)

    def perceive(
        self,
        audio_segments: Sequence[AudioSegment],
        *,
        visual_energy: float = 0.0,
        palette_warmth: float = 0.5,
        focal_strength: float = 0.0,
        motion_intensity: float = 0.0,
        timestamp_sec: float = 0.0,
    ) -> AudioVisualState:
        """Fuse audio perception with Gaze visual state for holistic understanding."""
        return self._auditory.fuse_with_gaze(
            audio_segments,
            visual_energy=visual_energy,
            palette_warmth=palette_warmth,
            focal_strength=focal_strength,
            motion_intensity=motion_intensity,
            timestamp_sec=timestamp_sec,
        )

    # ------------------------------------------------------------------
    # Speech interface — chatbot and voicebot communication
    # ------------------------------------------------------------------

    def chat(self, user_text: str, *, conversation_id: str = "") -> SpeechResponse:
        """Respond to a text message via chatbot."""
        context = f"Scholar status: {self.describe()}"
        return self._speech.respond_text(
            user_text, conversation_id=conversation_id, context=context
        )

    def speak(self, user_text: str, *, conversation_id: str = "") -> SpeechResponse:
        """Respond with synthesised voice via voicebot."""
        context = f"Scholar status: {self.describe()}"
        return self._speech.respond_voice(
            user_text, conversation_id=conversation_id, context=context
        )

    def converse(self, user_text: str, *, conversation_id: str = "") -> SpeechResponse:
        """Respond using the current communication mode (chatbot, voicebot, or both)."""
        context = f"Scholar status: {self.describe()}"
        return self._speech.respond(user_text, conversation_id=conversation_id, context=context)

    # ------------------------------------------------------------------
    # Output review interface
    # ------------------------------------------------------------------

    def review_output(
        self,
        edl: EditDecisionList,
        prediction: Prediction,
        model: FitReport,
    ) -> list[ReviewFinding]:
        """Review a final cut before it goes to the gate."""
        return self._reviewer.review(edl, prediction, model)

    def review_as_proposals(
        self,
        edl: EditDecisionList,
        prediction: Prediction,
        model: FitReport,
    ) -> list[Proposal]:
        """Review and return proposals compatible with the crew system."""
        return self._reviewer.review_as_proposals(edl, prediction, model)

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def status(self) -> dict:
        """Current state of the Scholar."""
        return {
            "total_learnings": self._store.total_learnings,
            "knowledge_gaps": [d.value for d in self._store.gaps()],
            "subscriptions": len(self._youtube.subscriptions),
            "most_watched_creators": [s.channel_name for s in self._youtube.most_watched_creators],
            "sessions_completed": len(self._sessions),
            "disciplines_studied": len(Discipline) - len(self._store.gaps()),
        }

    def describe(self) -> str:
        """Human-readable summary."""
        status = self.status()
        lines = [
            f"Scholar: {status['total_learnings']} learnings across "
            f"{status['disciplines_studied']}/{len(Discipline)} disciplines",
            f"  Gaps: {', '.join(status['knowledge_gaps'][:5]) or 'none'}",
            f"  Subscriptions: {status['subscriptions']} channels "
            f"({len(status['most_watched_creators'])} most-watched)",
            f"  Sessions: {status['sessions_completed']} completed",
        ]
        should, reason = self.should_study()
        if should:
            lines.append(f"  ⚡ Wants to study: {reason}")
        return "\n".join(lines)
