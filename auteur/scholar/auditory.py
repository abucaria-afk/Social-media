"""The Scholar's auditory system — hearing, listening, and cooperation with Gaze.

The Gaze agent gives the Scholar eyes: it reads composition, colour, exposure,
and focal weight from visual frames.  The Auditory system gives the Scholar
*ears*: it perceives audio streams — dialogue, music, ambient sound, effects —
and fuses that perception with the Gaze foundation so the Scholar understands
multimedia content *holistically* rather than through vision alone.

Cooperation model with Gaze:
- Gaze provides per-frame visual state (palette, exposure, motion, composition).
- Auditory provides per-segment audio state (energy, pitch, sentiment, language).
- Both feed into the Scholar's learning loop so it can detect mismatches
  (e.g. upbeat music over somber visuals) and learn from tutorials that cover
  audio-visual relationships.

Listening capabilities:
- Dialogue transcription and speaker diarisation.
- Music analysis: tempo, key, energy curve.
- Ambient sound classification.
- Audio-visual sync detection (lip sync, sound effects alignment).
"""

from __future__ import annotations

import enum
import hashlib
import logging
import time
from dataclasses import dataclass, field
from collections.abc import Sequence

log = logging.getLogger("auteur.scholar.auditory")


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class AudioChannel(enum.Enum):
    """Primary audio channels the Auditory system separates."""

    DIALOGUE = "dialogue"
    MUSIC = "music"
    AMBIENT = "ambient"
    EFFECTS = "effects"


class ListeningMode(enum.Enum):
    """How the Auditory system processes incoming audio."""

    PASSIVE = "passive"  # Background monitoring
    ACTIVE = "active"  # Full transcription + analysis
    FOCUSED = "focused"  # Single-speaker tracking


class AudioSentiment(enum.Enum):
    """Broad emotional tone detected in audio."""

    POSITIVE = "positive"
    NEGATIVE = "negative"
    NEUTRAL = "neutral"
    INTENSE = "intense"
    CALM = "calm"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class AudioSegment:
    """A discrete time-slice of processed audio."""

    start_sec: float
    end_sec: float
    channel: AudioChannel
    transcript: str = ""
    language: str = ""
    speaker_id: str = ""
    sentiment: AudioSentiment = AudioSentiment.NEUTRAL
    energy: float = 0.0  # 0–1 normalised loudness
    tempo_bpm: float = 0.0
    pitch_hz: float = 0.0
    confidence: float = 0.0  # 0–1 transcription confidence

    @property
    def duration_sec(self) -> float:
        return self.end_sec - self.start_sec

    def to_json(self) -> dict:
        return {
            "start_sec": round(self.start_sec, 3),
            "end_sec": round(self.end_sec, 3),
            "channel": self.channel.value,
            "transcript": self.transcript,
            "language": self.language,
            "speaker_id": self.speaker_id,
            "sentiment": self.sentiment.value,
            "energy": round(self.energy, 3),
            "tempo_bpm": round(self.tempo_bpm, 1),
            "pitch_hz": round(self.pitch_hz, 1),
            "confidence": round(self.confidence, 3),
        }


@dataclass
class AudioVisualState:
    """Fused state from Gaze (visual) and Auditory (audio) at a moment in time.

    This is the joint perception the Scholar uses to understand content.
    """

    timestamp_sec: float
    # Visual state (from Gaze)
    visual_energy: float = 0.0
    palette_warmth: float = 0.5
    focal_strength: float = 0.0
    motion_intensity: float = 0.0
    # Audio state (from Auditory)
    audio_energy: float = 0.0
    audio_sentiment: AudioSentiment = AudioSentiment.NEUTRAL
    dialogue_active: bool = False
    music_tempo_bpm: float = 0.0
    # Computed
    av_sync_score: float = 1.0  # 1 = well matched, 0 = mismatch

    @property
    def is_mismatch(self) -> bool:
        """Detect audio-visual energy mismatch."""
        return self.av_sync_score < 0.4

    def to_json(self) -> dict:
        return {
            "timestamp_sec": round(self.timestamp_sec, 3),
            "visual_energy": round(self.visual_energy, 3),
            "audio_energy": round(self.audio_energy, 3),
            "audio_sentiment": self.audio_sentiment.value,
            "dialogue_active": self.dialogue_active,
            "music_tempo_bpm": round(self.music_tempo_bpm, 1),
            "av_sync_score": round(self.av_sync_score, 3),
            "is_mismatch": self.is_mismatch,
        }


@dataclass
class ListeningSession:
    """Record of a single listening pass over an audio stream."""

    source_id: str
    started_at: float = field(default_factory=time.time)
    mode: ListeningMode = ListeningMode.ACTIVE
    segments: list[AudioSegment] = field(default_factory=list)
    duration_sec: float = 0.0
    languages_detected: list[str] = field(default_factory=list)
    speakers_identified: int = 0

    def to_json(self) -> dict:
        return {
            "source_id": self.source_id,
            "started_at": self.started_at,
            "mode": self.mode.value,
            "segment_count": len(self.segments),
            "duration_sec": round(self.duration_sec, 3),
            "languages_detected": self.languages_detected,
            "speakers_identified": self.speakers_identified,
        }


# ---------------------------------------------------------------------------
# Auditory system
# ---------------------------------------------------------------------------


class AuditorySystem:
    """The Scholar's ears — perceives, transcribes, and analyses audio.

    Cooperates with the Gaze agent's visual output to produce fused
    audio-visual state for the Scholar's learning loop.

    Capabilities:
    - Hear: detect and classify audio channels (dialogue, music, ambient, FX).
    - Listen: full transcription with speaker diarisation and language detection.
    - Cooperate with Gaze: fuse visual state with audio state to detect
      audio-visual alignment and mismatches.
    """

    def __init__(self) -> None:
        self._sessions: list[ListeningSession] = []
        self._mode: ListeningMode = ListeningMode.PASSIVE
        self._supported_languages: list[str] = []  # Populated on first listen

    @property
    def mode(self) -> ListeningMode:
        return self._mode

    @property
    def sessions(self) -> list[ListeningSession]:
        return list(self._sessions)

    # ------------------------------------------------------------------
    # Hearing — classification without full transcription
    # ------------------------------------------------------------------

    def hear(self, audio_data: bytes, sample_rate: int = 44100) -> list[AudioSegment]:
        """Perform rapid audio classification — detect channels and energy.

        This is the *passive* path. It identifies what's in the audio stream
        (dialogue vs music vs ambient) and measures energy without fully
        transcribing every word. Use for background monitoring.
        """
        segments: list[AudioSegment] = []

        # Determine dominant channel from energy distribution
        segment = AudioSegment(
            start_sec=0.0,
            end_sec=len(audio_data) / (sample_rate * 2),  # 16-bit mono
            channel=self._classify_channel(audio_data),
            energy=self._measure_energy(audio_data),
            confidence=0.7,
        )
        segments.append(segment)

        log.debug(
            "heard %d bytes → %s (energy=%.2f)",
            len(audio_data),
            segment.channel.value,
            segment.energy,
        )
        return segments

    # ------------------------------------------------------------------
    # Listening — full transcription and analysis
    # ------------------------------------------------------------------

    def listen(
        self,
        audio_data: bytes,
        *,
        source_id: str = "",
        sample_rate: int = 44100,
        mode: ListeningMode = ListeningMode.ACTIVE,
    ) -> ListeningSession:
        """Full listening pass — transcribe, diarise, and analyse.

        This is the *active* path used when the Scholar is studying a video
        tutorial or reviewing final output audio.

        Returns a ListeningSession with all extracted segments.
        """
        self._mode = mode
        session = ListeningSession(
            source_id=source_id or hashlib.sha256(audio_data[:256]).hexdigest()[:12],
            mode=mode,
        )

        # Transcription pipeline (abstracted — actual model integration pluggable)
        segments = self._transcribe(audio_data, sample_rate)
        session.segments = segments
        session.duration_sec = segments[-1].end_sec if segments else 0.0

        # Identify languages and speakers
        languages = set()
        speakers = set()
        for seg in segments:
            if seg.language:
                languages.add(seg.language)
            if seg.speaker_id:
                speakers.add(seg.speaker_id)

        session.languages_detected = sorted(languages)
        session.speakers_identified = len(speakers)

        self._sessions.append(session)
        log.info(
            "listening session complete: %d segments, %d languages, %d speakers (%.1fs)",
            len(segments),
            len(languages),
            len(speakers),
            session.duration_sec,
        )
        return session

    # ------------------------------------------------------------------
    # Gaze cooperation — fuse visual and audio perception
    # ------------------------------------------------------------------

    def fuse_with_gaze(
        self,
        audio_segments: Sequence[AudioSegment],
        *,
        visual_energy: float = 0.0,
        palette_warmth: float = 0.5,
        focal_strength: float = 0.0,
        motion_intensity: float = 0.0,
        timestamp_sec: float = 0.0,
    ) -> AudioVisualState:
        """Combine audio perception with Gaze visual state.

        The Scholar uses this fused state to understand content holistically —
        detecting when audio and visuals align (high energy music + fast motion)
        or mismatch (calm narration + chaotic visuals).
        """
        # Find audio segment closest to the timestamp
        audio_energy = 0.0
        audio_sentiment = AudioSentiment.NEUTRAL
        dialogue_active = False
        music_tempo = 0.0

        for seg in audio_segments:
            if seg.start_sec <= timestamp_sec <= seg.end_sec:
                audio_energy = seg.energy
                audio_sentiment = seg.sentiment
                dialogue_active = seg.channel == AudioChannel.DIALOGUE
                music_tempo = seg.tempo_bpm
                break

        # Compute audio-visual sync score
        av_sync = self._compute_av_sync(
            visual_energy=visual_energy,
            motion_intensity=motion_intensity,
            audio_energy=audio_energy,
            music_tempo=music_tempo,
        )

        return AudioVisualState(
            timestamp_sec=timestamp_sec,
            visual_energy=visual_energy,
            palette_warmth=palette_warmth,
            focal_strength=focal_strength,
            motion_intensity=motion_intensity,
            audio_energy=audio_energy,
            audio_sentiment=audio_sentiment,
            dialogue_active=dialogue_active,
            music_tempo_bpm=music_tempo,
            av_sync_score=av_sync,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _classify_channel(self, audio_data: bytes) -> AudioChannel:
        """Classify audio data into a primary channel type."""
        # Placeholder: real implementation would use an audio classifier model
        # For now, assume dialogue-dominant
        return AudioChannel.DIALOGUE

    def _measure_energy(self, audio_data: bytes) -> float:
        """Compute normalised energy (RMS) of audio data."""
        if not audio_data:
            return 0.0
        # Simplified: treat as 16-bit signed PCM mono
        sample_count = len(audio_data) // 2
        if sample_count == 0:
            return 0.0
        # Quick RMS approximation from byte magnitudes
        total = sum(abs(b - 128) for b in audio_data[:1024]) / min(len(audio_data), 1024)
        return min(total / 128.0, 1.0)

    def _transcribe(self, audio_data: bytes, sample_rate: int) -> list[AudioSegment]:
        """Transcribe audio into segments.

        Integration point: in production this calls into a speech-to-text model
        (e.g. Whisper) with speaker diarisation. The implementation here provides
        the structural contract.
        """
        duration = len(audio_data) / (sample_rate * 2) if audio_data else 0.0

        # Produce a single segment placeholder; real model would produce many
        if duration <= 0:
            return []

        return [
            AudioSegment(
                start_sec=0.0,
                end_sec=duration,
                channel=AudioChannel.DIALOGUE,
                transcript="[transcription pending — model integration point]",
                language="en",
                speaker_id="speaker_0",
                sentiment=AudioSentiment.NEUTRAL,
                energy=self._measure_energy(audio_data),
                confidence=0.0,
            )
        ]

    def _compute_av_sync(
        self,
        *,
        visual_energy: float,
        motion_intensity: float,
        audio_energy: float,
        music_tempo: float,
    ) -> float:
        """Score how well audio and visual energy match (0–1).

        High sync means audio energy matches visual energy/motion. Low sync
        means mismatch — which could be intentional (tension) or an error.
        """
        # Weighted average of energy difference and motion-tempo alignment
        energy_diff = abs(visual_energy - audio_energy)
        # Normalise tempo to 0–1 range (60–180 bpm mapped to 0–1)
        norm_tempo = max(0.0, min((music_tempo - 60) / 120, 1.0)) if music_tempo > 0 else 0.5
        tempo_motion_diff = abs(motion_intensity - norm_tempo)

        raw = 1.0 - (0.6 * energy_diff + 0.4 * tempo_motion_diff)
        return max(0.0, min(raw, 1.0))

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def status(self) -> dict:
        """Current state of the auditory system."""
        return {
            "mode": self._mode.value,
            "sessions_completed": len(self._sessions),
            "total_segments_processed": sum(len(s.segments) for s in self._sessions),
            "languages_encountered": sorted(
                {lang for s in self._sessions for lang in s.languages_detected}
            ),
        }
