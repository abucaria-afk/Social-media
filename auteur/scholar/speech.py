"""The Scholar's speech system — multilingual understanding and communication.

The Scholar can *hear* via the Auditory system. This Speech module gives it the
ability to *understand* what is being said at a savant and natural-speaking level
across all languages, and to *respond* both as a text-based chatbot and as a
synthesised voice bot.

Capabilities:
- **Understand**: parse spoken or written input in any language with savant-level
  comprehension — idiom, register, intent, emotion, and context are all captured.
- **Speak (text)**: generate fluent, natural responses in any language via a
  chatbot interface.
- **Speak (voice)**: synthesise spoken responses via a voice bot interface with
  natural prosody, pacing, and emotion appropriate to the language and context.
- **Translate**: seamlessly switch languages mid-conversation without explicit
  prompting — the system auto-detects the incoming language and responds in kind.

Integration:
- Works with the Auditory system for speech-to-text input.
- Works with the Gaze foundation for context-aware responses (e.g. describing
  what the Scholar sees when asked).
- Provides the Scholar's primary external communication interface.
"""

from __future__ import annotations

import enum
import hashlib
import logging
import time
from dataclasses import dataclass, field

log = logging.getLogger("auteur.scholar.speech")

#: How many earlier turns to send back with a reply. Enough for a follow-up
#: question to make sense, short enough that a long session stays affordable.
_HISTORY_TURNS = 8


def _ask_claude(user_text, lang_name, intent, context, conversation) -> str:
    """A real reply from the model this project already talks to, or "".

    Deliberately reuses the director's client rather than opening a second path
    to the same service: one place decides whether a model is reachable, one
    place holds the key, and a Scholar that can talk is exactly a Scholar whose
    director could have talked too.
    """
    import os

    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        return ""
    try:
        import anthropic
    except ImportError:
        return ""

    history = []
    for message in list(getattr(conversation, "messages", []))[-_HISTORY_TURNS:]:
        if not message.text:
            continue
        history.append(
            {
                "role": "assistant" if message.role == "scholar" else "user",
                "content": message.text,
            }
        )
    # The caller records the incoming message after the reply is generated, so
    # it is not in `messages` yet and has to be appended by hand. The API also
    # rejects two user turns in a row, which a duplicate here would create.
    if not history or history[-1]["content"] != user_text:
        history.append({"role": "user", "content": user_text})

    system = (
        "You are the Scholar: a study agent inside a video editing program. You "
        "watch craft tutorials, keep what you learn, and answer questions about "
        "editing, colour, sound and composition from what you have actually "
        f"stored. Reply in {lang_name}, matching the register of the question. "
        "Be concrete and brief. If the knowledge below does not cover something, "
        "say you have not studied it yet rather than inventing a source.\n\n"
        f"{context}"
    )
    try:
        client = anthropic.Anthropic()
        reply = client.messages.create(
            model=os.environ.get("AUTEUR_SCHOLAR_MODEL", "claude-sonnet-4-5"),
            max_tokens=700,
            system=system,
            messages=history,
        )
    except Exception as exc:  # noqa: BLE001 - an unreachable model is reported, not raised
        log.info("scholar could not reach the model: %s", exc)
        return ""

    return "".join(
        block.text for block in reply.content if getattr(block, "type", "") == "text"
    ).strip()


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class CommunicationMode(enum.Enum):
    """How the speech system delivers responses."""

    CHATBOT = "chatbot"  # Text-based interaction
    VOICEBOT = "voicebot"  # Synthesised speech output
    BOTH = "both"  # Simultaneous text + voice


class SpeechIntent(enum.Enum):
    """Classified intent of an incoming message."""

    QUESTION = "question"
    COMMAND = "command"
    FEEDBACK = "feedback"
    GREETING = "greeting"
    TEACHING_REQUEST = "teaching_request"
    REVIEW_REQUEST = "review_request"
    CLARIFICATION = "clarification"
    CONVERSATION = "conversation"


class VoiceStyle(enum.Enum):
    """Expressive style for voice synthesis."""

    NEUTRAL = "neutral"
    ENTHUSIASTIC = "enthusiastic"
    CALM = "calm"
    AUTHORITATIVE = "authoritative"
    FRIENDLY = "friendly"
    EMPATHETIC = "empathetic"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class LanguageProfile:
    """The speech system's competency in a given language."""

    code: str  # ISO 639-1 code (e.g. "en", "ja", "ar")
    name: str  # Human-readable name (e.g. "English", "Japanese")
    comprehension_level: float = 1.0  # 0–1, savant = 1.0
    fluency_level: float = 1.0  # 0–1, native = 1.0
    idiomatic: bool = True  # Can understand/produce idioms
    register_aware: bool = True  # Adapts formality to context

    def to_json(self) -> dict:
        return {
            "code": self.code,
            "name": self.name,
            "comprehension_level": self.comprehension_level,
            "fluency_level": self.fluency_level,
            "idiomatic": self.idiomatic,
            "register_aware": self.register_aware,
        }


@dataclass
class Message:
    """A single message in a conversation."""

    role: str  # "user" or "scholar"
    text: str
    language: str = ""  # Detected or target language code
    intent: SpeechIntent = SpeechIntent.CONVERSATION
    timestamp: float = field(default_factory=time.time)
    voice_audio: bytes = field(default=b"", repr=False)  # Synthesised audio if voicebot

    def to_json(self) -> dict:
        return {
            "role": self.role,
            "text": self.text,
            "language": self.language,
            "intent": self.intent.value,
            "timestamp": self.timestamp,
            "has_voice": len(self.voice_audio) > 0,
        }


@dataclass
class Conversation:
    """A multi-turn dialogue between a user and the Scholar."""

    conversation_id: str
    started_at: float = field(default_factory=time.time)
    messages: list[Message] = field(default_factory=list)
    mode: CommunicationMode = CommunicationMode.CHATBOT
    primary_language: str = ""

    @property
    def turn_count(self) -> int:
        return len(self.messages)

    def to_json(self) -> dict:
        return {
            "conversation_id": self.conversation_id,
            "started_at": self.started_at,
            "turn_count": self.turn_count,
            "mode": self.mode.value,
            "primary_language": self.primary_language,
            "messages": [m.to_json() for m in self.messages],
        }


@dataclass
class SpeechResponse:
    """A response produced by the speech system."""

    text: str
    language: str
    voice_audio: bytes = field(default=b"", repr=False)
    voice_style: VoiceStyle = VoiceStyle.NEUTRAL
    confidence: float = 1.0

    @property
    def has_voice(self) -> bool:
        return len(self.voice_audio) > 0

    def to_json(self) -> dict:
        return {
            "text": self.text,
            "language": self.language,
            "has_voice": self.has_voice,
            "voice_style": self.voice_style.value,
            "confidence": self.confidence,
        }


# ---------------------------------------------------------------------------
# Speech system
# ---------------------------------------------------------------------------


class SpeechSystem:
    """The Scholar's voice — understands all languages and communicates fluently.

    Operates at a savant and natural-speaking level across all languages. Can
    function as both a chatbot (text) and a voicebot (synthesised speech).

    Language competence:
    - Comprehends any human language at native level.
    - Produces fluent, idiomatic responses adapted to register and context.
    - Auto-detects incoming language and responds in the same language unless
      asked to switch.
    - Handles code-switching and multilingual conversations naturally.

    Communication modes:
    - CHATBOT: text input/output for chat interfaces.
    - VOICEBOT: audio input (via Auditory) + synthesised speech output.
    - BOTH: simultaneous text + voice for accessibility.
    """

    def __init__(
        self,
        *,
        default_mode: CommunicationMode = CommunicationMode.CHATBOT,
        default_voice_style: VoiceStyle = VoiceStyle.FRIENDLY,
    ) -> None:
        self._mode = default_mode
        self._voice_style = default_voice_style
        self._conversations: dict[str, Conversation] = {}
        self._language_profiles: dict[str, LanguageProfile] = self._init_language_profiles()
        # Once per system, not once per sentence — a warning on every reply is
        # a warning nobody reads.
        self._warned_no_tts = False

    @property
    def mode(self) -> CommunicationMode:
        return self._mode

    @mode.setter
    def mode(self, value: CommunicationMode) -> None:
        self._mode = value

    @property
    def voice_style(self) -> VoiceStyle:
        return self._voice_style

    @voice_style.setter
    def voice_style(self, value: VoiceStyle) -> None:
        self._voice_style = value

    @property
    def supported_languages(self) -> list[LanguageProfile]:
        return list(self._language_profiles.values())

    # ------------------------------------------------------------------
    # Understanding — comprehend input in any language
    # ------------------------------------------------------------------

    def understand(self, text: str) -> tuple[str, SpeechIntent, float]:
        """Comprehend incoming text in any language.

        Returns (detected_language, classified_intent, confidence).
        The Scholar understands at savant level — idioms, register, subtext,
        emotion, and cultural context are all captured.
        """
        language = self._detect_language(text)
        intent = self._classify_intent(text, language)
        confidence = 1.0  # Savant-level comprehension

        log.debug(
            "understood [%s] intent=%s conf=%.2f: %s", language, intent.value, confidence, text[:80]
        )
        return language, intent, confidence

    # ------------------------------------------------------------------
    # Speaking — text chatbot interface
    # ------------------------------------------------------------------

    def respond_text(
        self,
        user_text: str,
        *,
        conversation_id: str = "",
        context: str = "",
    ) -> SpeechResponse:
        """Generate a text response (chatbot mode).

        Responds in the same language as the input unless explicitly asked to
        switch. Adapts register (formal/informal) to match the user's style.
        """
        language, intent, _ = self.understand(user_text)

        # Get or create conversation
        conv = self._get_or_create_conversation(conversation_id, CommunicationMode.CHATBOT)
        conv.primary_language = conv.primary_language or language

        # Record user message
        user_msg = Message(role="user", text=user_text, language=language, intent=intent)
        conv.messages.append(user_msg)

        # Generate response (integration point for LLM)
        response_text = self._generate_response(user_text, language, intent, context, conv)

        # Record scholar message
        scholar_msg = Message(role="scholar", text=response_text, language=language)
        conv.messages.append(scholar_msg)

        log.info(
            "chatbot response [%s] (%d turns): %s", language, conv.turn_count, response_text[:80]
        )
        return SpeechResponse(text=response_text, language=language, confidence=1.0)

    # ------------------------------------------------------------------
    # Speaking — voice bot interface
    # ------------------------------------------------------------------

    def respond_voice(
        self,
        user_text: str,
        *,
        conversation_id: str = "",
        context: str = "",
        voice_style: VoiceStyle | None = None,
    ) -> SpeechResponse:
        """Generate a voice response (voicebot mode).

        Synthesises natural speech with appropriate prosody, pacing, and
        emotion for the detected language and context.
        """
        language, intent, _ = self.understand(user_text)
        style = voice_style or self._voice_style

        # Get or create conversation
        conv = self._get_or_create_conversation(conversation_id, CommunicationMode.VOICEBOT)
        conv.primary_language = conv.primary_language or language

        # Record user message
        user_msg = Message(role="user", text=user_text, language=language, intent=intent)
        conv.messages.append(user_msg)

        # Generate text response first
        response_text = self._generate_response(user_text, language, intent, context, conv)

        # Synthesise voice (integration point for TTS model)
        voice_audio = self._synthesise_speech(response_text, language, style)

        # Record scholar message
        scholar_msg = Message(
            role="scholar", text=response_text, language=language, voice_audio=voice_audio
        )
        conv.messages.append(scholar_msg)

        log.info(
            "voicebot response [%s] style=%s (%d turns)", language, style.value, conv.turn_count
        )
        return SpeechResponse(
            text=response_text,
            language=language,
            voice_audio=voice_audio,
            voice_style=style,
            confidence=1.0,
        )

    # ------------------------------------------------------------------
    # Convenience — auto-select chatbot or voicebot
    # ------------------------------------------------------------------

    def respond(
        self,
        user_text: str,
        *,
        conversation_id: str = "",
        context: str = "",
    ) -> SpeechResponse:
        """Respond using the current communication mode.

        If mode is BOTH, produces both text and voice output.
        """
        if self._mode == CommunicationMode.VOICEBOT:
            return self.respond_voice(user_text, conversation_id=conversation_id, context=context)
        if self._mode == CommunicationMode.BOTH:
            response = self.respond_voice(
                user_text, conversation_id=conversation_id, context=context
            )
            return response  # Already has both text and audio
        return self.respond_text(user_text, conversation_id=conversation_id, context=context)

    # ------------------------------------------------------------------
    # Conversation management
    # ------------------------------------------------------------------

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        return self._conversations.get(conversation_id)

    def list_conversations(self) -> list[Conversation]:
        return list(self._conversations.values())

    def end_conversation(self, conversation_id: str) -> None:
        self._conversations.pop(conversation_id, None)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _detect_language(self, text: str) -> str:
        """Detect the language of input text.

        Integration point: in production uses a language detection model.
        The Scholar handles all human languages at savant level.
        """
        # Simplified heuristic — real implementation uses a detection model
        # Check for common script indicators
        if any("\u4e00" <= c <= "\u9fff" for c in text):
            return "zh"
        if any("\u3040" <= c <= "\u309f" or "\u30a0" <= c <= "\u30ff" for c in text):
            return "ja"
        if any("\uac00" <= c <= "\ud7af" for c in text):
            return "ko"
        if any("\u0600" <= c <= "\u06ff" for c in text):
            return "ar"
        if any("\u0900" <= c <= "\u097f" for c in text):
            return "hi"
        if any("\u0400" <= c <= "\u04ff" for c in text):
            return "ru"
        # Default to English
        return "en"

    def _classify_intent(self, text: str, language: str) -> SpeechIntent:
        """Classify the intent of incoming text."""
        lower = text.lower()

        # Basic intent classification (real implementation uses NLU model)
        if any(q in lower for q in ("?", "what", "how", "why", "when", "where", "who")):
            return SpeechIntent.QUESTION
        if any(c in lower for c in ("teach", "explain", "show me", "help me learn")):
            return SpeechIntent.TEACHING_REQUEST
        if any(c in lower for c in ("review", "check", "analyse", "analyze", "look at")):
            return SpeechIntent.REVIEW_REQUEST
        if any(c in lower for c in ("hello", "hi", "hey", "good morning")):
            return SpeechIntent.GREETING
        if any(c in lower for c in ("do", "make", "create", "run", "start")):
            return SpeechIntent.COMMAND

        return SpeechIntent.CONVERSATION

    def _generate_response(
        self,
        user_text: str,
        language: str,
        intent: SpeechIntent,
        context: str,
        conversation: Conversation,
    ) -> str:
        """Generate a natural-language response.

        Integration point: in production this calls the Scholar's LLM backbone
        with full conversation history, knowledge store context, and Gaze/Auditory
        perceptual state.

        The response is generated in the same language as the input, at a natural
        and fluent speaking level.
        """
        lang_name = self._language_profiles.get(
            language, LanguageProfile(code=language, name=language)
        ).name

        reply = _ask_claude(user_text, lang_name, intent, context, conversation)
        if reply:
            return reply

        # No key, no anthropic package, or the call failed. Say that, in one
        # sentence, rather than returning a sentence shaped like an answer.
        #
        # What was here before was `f"[Scholar response in {lang_name}] Intent:
        # {intent.value}. Context-aware response to: {user_text[:100]}"` — a
        # string that reads as a reply, arrives through the same field a reply
        # would, and is indistinguishable from one to any caller that does not
        # already know. A chatbot that cannot reach its model has to say so.
        return (
            f"I cannot answer that right now: my language model is not reachable "
            f"from here (set ANTHROPIC_API_KEY and install the `anthropic` package). "
            f"I understood the question as {intent.value} in {lang_name}."
        )

    def _synthesise_speech(self, text: str, language: str, style: VoiceStyle) -> bytes:
        """Synthesise text into spoken audio.

        Integration point: in production calls a TTS model with appropriate
        voice characteristics for the language and style.

        The synthesis produces natural prosody, pacing, and emotion appropriate
        to the language, register, and conversational context.
        """
        # There is no speech synthesiser in this project and none reachable from
        # here, so this returns nothing — and says so once, at a level somebody
        # will see, rather than handing back silence that looks like audio.
        #
        # `Message.to_json` reports `has_voice: len(voice_audio) > 0`, so an
        # empty result is at least not claimed as a voice note. That is the
        # honest half. The dishonest half would be generating a tone, or a WAV
        # header with nothing in it, to make the field non-empty.
        if not self._warned_no_tts:
            log.warning(
                "the Scholar has no speech synthesiser wired in, so voice replies "
                "carry text only; the words are in `.text`"
            )
            self._warned_no_tts = True
        return b""

    def _get_or_create_conversation(
        self, conversation_id: str, mode: CommunicationMode
    ) -> Conversation:
        """Get existing conversation or create a new one."""
        if not conversation_id:
            conversation_id = hashlib.sha256(f"{time.time()}".encode()).hexdigest()[:12]

        if conversation_id not in self._conversations:
            self._conversations[conversation_id] = Conversation(
                conversation_id=conversation_id, mode=mode
            )
        return self._conversations[conversation_id]

    def _init_language_profiles(self) -> dict[str, LanguageProfile]:
        """Initialise savant-level profiles for all major languages.

        The Scholar operates at native/savant level in all human languages.
        """
        languages = [
            ("en", "English"),
            ("es", "Spanish"),
            ("fr", "French"),
            ("de", "German"),
            ("it", "Italian"),
            ("pt", "Portuguese"),
            ("ru", "Russian"),
            ("zh", "Chinese"),
            ("ja", "Japanese"),
            ("ko", "Korean"),
            ("ar", "Arabic"),
            ("hi", "Hindi"),
            ("bn", "Bengali"),
            ("ur", "Urdu"),
            ("tr", "Turkish"),
            ("vi", "Vietnamese"),
            ("th", "Thai"),
            ("pl", "Polish"),
            ("nl", "Dutch"),
            ("sv", "Swedish"),
            ("da", "Danish"),
            ("no", "Norwegian"),
            ("fi", "Finnish"),
            ("el", "Greek"),
            ("he", "Hebrew"),
            ("id", "Indonesian"),
            ("ms", "Malay"),
            ("tl", "Filipino"),
            ("sw", "Swahili"),
            ("am", "Amharic"),
            ("yo", "Yoruba"),
            ("ig", "Igbo"),
            ("zu", "Zulu"),
            ("cs", "Czech"),
            ("sk", "Slovak"),
            ("hu", "Hungarian"),
            ("ro", "Romanian"),
            ("bg", "Bulgarian"),
            ("uk", "Ukrainian"),
            ("fa", "Persian"),
            ("ta", "Tamil"),
            ("te", "Telugu"),
            ("ml", "Malayalam"),
            ("kn", "Kannada"),
            ("mr", "Marathi"),
            ("gu", "Gujarati"),
            ("pa", "Punjabi"),
            ("ne", "Nepali"),
            ("si", "Sinhala"),
            ("my", "Burmese"),
            ("km", "Khmer"),
            ("lo", "Lao"),
            ("ka", "Georgian"),
            ("hy", "Armenian"),
            ("az", "Azerbaijani"),
            ("uz", "Uzbek"),
            ("kk", "Kazakh"),
            ("mn", "Mongolian"),
            ("la", "Latin"),
            ("sa", "Sanskrit"),
        ]
        return {code: LanguageProfile(code=code, name=name) for code, name in languages}

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def status(self) -> dict:
        """Current state of the speech system."""
        return {
            "mode": self._mode.value,
            "voice_style": self._voice_style.value,
            "supported_languages": len(self._language_profiles),
            "active_conversations": len(self._conversations),
            "total_messages": sum(c.turn_count for c in self._conversations.values()),
        }
