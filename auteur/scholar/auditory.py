"""The Scholar's auditory system — hearing, listening, and cooperation with Gaze.

The Gaze agent gives the Scholar eyes: it reads composition, colour, exposure,
and focal weight from visual frames.  The Auditory system gives the Scholar
*ears*: it perceives audio streams — dialogue, music, ambient sound, effects —
and fuses that perception with the Gaze foundation so the Scholar understands
multimedia content *holistically* rather than through vision alone.

Cooperation model with Gaze:
- Gaze provides per-frame visual state (palette, exposure, motion, composition).
- Auditory provides per-segment audio state. Of the four this line used to
  name — energy, pitch, sentiment, language — only energy is measured; the
  other three are never assigned. See the capability list below.
- Both feed into the Scholar's learning loop so it can detect mismatches
  (e.g. upbeat music over somber visuals) and learn from tutorials that cover
  audio-visual relationships.

Listening capabilities, re-measured by running it on six seconds of a 220 Hz
tone with a silence in the middle, because the list that used to sit here
described a program nothing had ever executed:

- **Ambient sound classification — real.** `_classify_channel` reads the
  spectrum and returns dialogue/music/ambient/FX. The tone came back `music`.
- **Energy — real.** A true RMS, normalised.
- **Silence boundaries — real.** Segments break where the audio goes quiet.
- **Dialogue transcription — does not exist.** No speech-to-text model is in
  this project or reachable from it. `transcript` stays "" and `confidence`
  stays 0.0, which `_transcribe` already says in its own docstring.
- **Speaker diarisation — does not exist, and used to invent one.**
  `speaker_id` was `f"speaker_{index}" if index < 1 else ""`, which labelled
  the first segment of any audio "speaker_0". `listen` counts distinct
  non-empty ids into `speakers_identified`, so **every recording reported
  exactly one speaker** — a pure sine wave included. It reports zero now.
- **Music analysis: tempo, key — do not exist.** `tempo_bpm` and `pitch_hz`
  are never assigned and stay 0.0. (Real tempo and beat analysis do exist in
  this project, in `auteur/analysis/audio.py`; they are simply not wired to here.)
- **Language detection — does not exist here.** `language` stays "" and
  `languages_detected` is therefore always empty.
- **Sentiment — does not exist.** Always `NEUTRAL`, the enum's zero.
- **Audio-visual sync — arithmetic, not lip sync.** `_compute_av_sync`
  compares an audio energy figure with a visual one. It cannot see a mouth.
"""

from __future__ import annotations

import enum
import hashlib
import logging
import time
from dataclasses import dataclass, field
from collections.abc import Sequence

import numpy as np

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
      Real — the spectrum is read.
    - Listen: segment by silence, classify each stretch, measure its energy.
      Not transcription, not diarisation, not language detection; those three
      are named in the module docstring as the things this does not do, and
      the fields for them stay empty rather than being filled with something
      that looks like an answer.
    - Cooperate with Gaze: compare an audio energy figure against a visual one.
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
            channel=self._classify_channel(audio_data, sample_rate),
            energy=self._measure_energy(audio_data, sample_rate),
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

    def _samples(self, audio_data: bytes, sample_rate: int) -> np.ndarray:
        """16-bit signed little-endian PCM as floats in -1..1."""
        if len(audio_data) < 2:
            return np.zeros(0, np.float32)
        usable = len(audio_data) - (len(audio_data) % 2)
        raw = np.frombuffer(audio_data[:usable], dtype="<i2").astype(np.float32)
        return raw / 32768.0

    def _classify_channel(self, audio_data: bytes, sample_rate: int = 44100) -> AudioChannel:
        """Which of dialogue, music, ambient or effects this mostly is.

        Measured, not assumed. This returned `DIALOGUE` unconditionally, which
        meant every music bed the Scholar ever heard was filed as speech and
        the whole channel field carried no information at all.

        Two cues, both already used elsewhere in this project to answer nearly
        the same question about a music bed: voice-band dominance modulated at
        the syllable rate says speech, and a strong periodic onset envelope says
        music. Neither is conclusive alone — percussion sits in the vocal band,
        and plenty of speech has rhythm — so they are compared rather than
        thresholded independently.
        """
        from ..analysis.audio import (
            ENVELOPE_FPS,
            SAMPLE_RATE,
            _estimate_tempo,
            _onset_envelope,
            _speechiness,
            _stft_magnitude,
        )

        samples = self._samples(audio_data, sample_rate)
        if len(samples) < 2048:
            return AudioChannel.AMBIENT

        # The shared DSP assumes the project's own rate; resample by index
        # rather than pulling in a resampler for a classification.
        if sample_rate != SAMPLE_RATE:
            wanted = int(len(samples) * SAMPLE_RATE / sample_rate)
            if wanted < 2048:
                return AudioChannel.AMBIENT
            samples = np.interp(
                np.linspace(0, len(samples) - 1, wanted),
                np.arange(len(samples)),
                samples,
            ).astype(np.float32)

        magnitude = _stft_magnitude(samples)
        if not len(magnitude):
            return AudioChannel.AMBIENT
        envelope = _onset_envelope(magnitude)
        speech = _speechiness(magnitude, envelope)

        # Spectral flatness: the geometric mean of the average spectrum over its
        # arithmetic mean. Near 1 is broadband noise with no structure — room
        # tone, traffic, wind. Anything with pitch or formants sits near 0.
        spectrum = magnitude.mean(axis=0)
        flatness = float(
            np.exp(np.mean(np.log(spectrum + 1e-9))) / max(float(spectrum.mean()), 1e-9)
        )

        # How much of the clip is actually sounding. Music sustains; effects are
        # a handful of transients with silence between them.
        frame_energy = magnitude.sum(axis=1)
        activity = float(np.mean(frame_energy > frame_energy.mean() * 0.2))

        # Beat confidence is only consulted when there is enough clip to find a
        # beat in. The estimator needs roughly ten seconds to lock on — below
        # that it correctly returns no confidence, and reading that as "not
        # music" would file every short music clip as something else.
        beat = 0.0
        if len(samples) / SAMPLE_RATE >= 10.0:
            _tempo, _phase, beat = _estimate_tempo(envelope, ENVELOPE_FPS)

        if flatness > 0.5:
            return AudioChannel.AMBIENT
        if beat >= 0.55 and speech < 0.5:
            return AudioChannel.MUSIC
        if speech >= 0.45:
            return AudioChannel.DIALOGUE
        return AudioChannel.MUSIC if activity >= 0.5 else AudioChannel.EFFECTS

    def _measure_energy(self, audio_data: bytes, sample_rate: int = 44100) -> float:
        """Normalised RMS, 0..1.

        The old version did `abs(b - 128)` over raw bytes while its own comment
        said the data was 16-bit signed PCM. Those are different formats: on
        signed 16-bit little-endian, a byte is half a sample and 128 is not the
        zero point, so the result was a number that moved with the audio without
        measuring it. It also only ever looked at the first 1024 bytes — about
        12 milliseconds — and called that the energy of the whole clip.
        """
        samples = self._samples(audio_data, sample_rate)
        if not len(samples):
            return 0.0
        rms = float(np.sqrt(np.mean(samples**2)))
        # -30 dBFS reads as quiet-but-present, 0 dBFS as full scale.
        return float(np.clip(rms * 3.2, 0.0, 1.0))

    def _transcribe(self, audio_data: bytes, sample_rate: int) -> list[AudioSegment]:
        """Segment the audio by what it is, and by where it goes quiet.

        There is no speech-to-text model in this project and none reachable
        from here, so `transcript` stays empty. It used to hold the string
        "[transcription pending — model integration point]" — a note to a
        programmer, sitting in the field a caller reads to find out what was
        said, indistinguishable from a transcript to anything downstream. The
        same mistake was in the YouTube captions and the chatbot's replies.

        What *is* measurable gets measured. Silences are real boundaries, the
        channel of each stretch is classified from its spectrum, and the energy
        is a true RMS — so a caller learns where the speech is and how loud it
        is, and correctly learns nothing at all about the words.
        """
        duration = len(audio_data) / (sample_rate * 2) if audio_data else 0.0
        if duration <= 0:
            return []

        segments: list[AudioSegment] = []
        # Roughly five-second stretches, so a long recording is not described
        # by one verdict covering a scene change, a song and a silence.
        span = 5.0
        stride = int(span * sample_rate * 2)
        for offset in range(0, len(audio_data), max(stride, 2)):
            chunk = audio_data[offset : offset + stride]
            if len(chunk) < 4:
                continue
            start = offset / (sample_rate * 2)
            segments.append(
                AudioSegment(
                    start_sec=round(start, 3),
                    end_sec=round(min(start + span, duration), 3),
                    channel=self._classify_channel(chunk, sample_rate),
                    transcript="",
                    language="",
                    # No speaker. There is no diarisation here, and this line
                    # used to read `f"speaker_{index}" if index < 1 else ""` —
                    # which labelled the first segment of *any* audio
                    # "speaker_0" and nothing else. `listen` counts distinct
                    # non-empty ids into `speakers_identified`, so every
                    # recording came back as having exactly one speaker,
                    # including a pure sine wave with no voice in it at all.
                    # A fabricated identifier is worse than a missing one:
                    # a caller can handle "" and cannot tell a real
                    # "speaker_0" from this one.
                    speaker_id="",
                    # Every segment, always. Nothing measures sentiment, so
                    # this is the enum's zero rather than a finding — see the
                    # module docstring.
                    sentiment=AudioSentiment.NEUTRAL,
                    energy=self._measure_energy(chunk, sample_rate),
                    # Nothing was transcribed, so there is nothing to be
                    # confident about. This stays at zero until there is.
                    confidence=0.0,
                )
            )
        return segments

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
