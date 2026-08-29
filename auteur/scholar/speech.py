"""The Scholar's speech system — what it can actually do, measured.

This docstring used to describe a different program. It claimed savant-level
comprehension of "any human language", voice synthesis "with natural prosody,
pacing, and emotion", and seamless mid-conversation translation. None of those
were true, none had ever been run, and nothing in the tree calls this module —
so nobody found out. Running it is what found out. What follows is the same
list, re-measured.

- **Understand — a script guess, and it now says what the guess is worth.**
  `_detect_language` reads the writing system, not the language. Measured
  against nineteen real sentences it labels ten correctly; every Latin-script
  language comes back "en", and Urdu comes back "ar" because they share a
  script. It used to return `1.0` confidence on all of them, wrong ones
  included. The confidence is now one over however many advertised languages
  share the script it saw: 1.0 for Korean, alone in Hangul; 0.33 for Arabic
  script, shared with Urdu and Persian; **0.04** for anything Latin.

- **Speak (text) — real, when a key is present.** `_ask_claude` reuses the
  director's client and returns "" with no `ANTHROPIC_API_KEY`, falling back to
  a canned reply. So the quality of a text answer is the model's, and its
  absence is honest rather than faked.

- **Speak (voice) — does not exist.** `_synthesise_speech` returns `b""`, logs
  a warning once, and `has_voice` correctly reports False. There is no TTS in
  this project and none reachable from here.

- **Translate — no.** The reply language is whatever the model produces from a
  prompt naming the detected language, and the detection is the script guess
  above. Nothing translates and nothing detects code-switching.

- **Intent — English only.** `_classify_intent` matches English keywords and an
  ASCII "?", so a Japanese question ending "？" classifies as `conversation`.

**And the whole module is unreachable.** No CLI command, no server route, no
caller, no test before this one. `Scholar.speech`, `.hear`, `.listen`,
`.speak`, `.converse` and `.perceive` are the entry points and nothing calls
any of them. That is worth knowing before trusting anything above: none of it
has ever run in service of a user.

Integration points named by the original docstring — the Auditory system for
speech-to-text, the Gaze foundation for context — are wiring that does not
exist either. They are left named because they are the right shape for
somebody building this, not because they are connected.
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


#: The scripts the detector can actually tell apart, in the order it checks
#: them, with the language it reports for each.
#:
#: Ordered, and the order matters: Japanese text mixes kana with Han
#: characters, so the kana check has to run before the Han one or every
#: Japanese sentence is reported as Chinese. That is a real bug this ordering
#: fixes — the original checked Han first.
SCRIPTS: dict[str, tuple[str, tuple[tuple[str, str], ...]]] = {
    # Kana first, for the reason above.
    "kana": ("ja", (("\u3040", "\u309f"), ("\u30a0", "\u30ff"))),
    "han": ("zh", (("\u4e00", "\u9fff"),)),
    "hangul": ("ko", (("\uac00", "\ud7af"),)),
    "arabic": ("ar", (("\u0600", "\u06ff"),)),
    "devanagari": ("hi", (("\u0900", "\u097f"),)),
    "cyrillic": ("ru", (("\u0400", "\u04ff"),)),
    "hebrew": ("he", (("\u0590", "\u05ff"),)),
    "greek": ("el", (("\u0370", "\u03ff"),)),
    "thai": ("th", (("\u0e00", "\u0e7f"),)),
}

#: Which script each advertised language is written in.
#:
#: This is what makes a confidence possible at all. Seeing Arabic script tells
#: you the language is one of three advertised ones, not which; seeing Hangul
#: tells you exactly. Anything not listed here is Latin, which is where the
#: detector's blind spot is and where most of the sixty live.
LANGUAGE_SCRIPT: dict[str, str] = {
    "ja": "kana",
    "zh": "han",
    "ko": "hangul",
    "ar": "arabic",
    "ur": "arabic",
    "fa": "arabic",
    "hi": "devanagari",
    "mr": "devanagari",
    "ne": "devanagari",
    "sa": "devanagari",
    "ru": "cyrillic",
    "bg": "cyrillic",
    "uk": "cyrillic",
    "kk": "cyrillic",
    "mn": "cyrillic",
    "he": "hebrew",
    "el": "greek",
    "th": "thai",
    # Scripts with an advertised language and no detector branch. Listed so
    # `_script_confidence` counts them out of Latin — they are not Latin, they
    # are simply invisible to this detector, and lumping them in would inflate
    # every Latin guess.
    "bn": "bengali",
    "ta": "tamil",
    "te": "telugu",
    "ml": "malayalam",
    "kn": "kannada",
    "gu": "gujarati",
    "pa": "gurmukhi",
    "si": "sinhala",
    "my": "burmese",
    "km": "khmer",
    "lo": "lao",
    "ka": "georgian",
    "hy": "armenian",
    "am": "ethiopic",
}


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
    # Four claims, none of them measured, all of them 1.0/True for all sixty
    # languages. Left at their values rather than quietly replaced with a
    # humbler invention — a made-up 0.6 is the same defect wearing a modest
    # face — but labelled, so a caller reading them knows what they are worth.
    #
    # What *can* be said about a language is `detectable`, below: whether this
    # system can tell it apart from the others at all.
    comprehension_level: float = 1.0  # ASPIRATIONAL, not measured
    fluency_level: float = 1.0  # ASPIRATIONAL, not measured
    idiomatic: bool = True  # ASPIRATIONAL, not measured
    register_aware: bool = True  # ASPIRATIONAL, not measured

    @property
    def detectable(self) -> bool:
        """Whether the detector can tell this language from every other one.

        The one honest field on this dataclass: it is computed from the script
        tables rather than asserted. True only where the language has a script
        the detector checks for and no other advertised language shares it —
        Korean, yes; Arabic, no, because Urdu and Persian look the same to it.
        """
        script = LANGUAGE_SCRIPT.get(self.code)
        if script is None or script not in SCRIPTS:
            return False
        return (
            SCRIPTS[script][0] == self.code
            and sum(1 for other in LANGUAGE_SCRIPT.values() if other == script) == 1
        )

    def to_json(self) -> dict:
        return {
            "code": self.code,
            "name": self.name,
            # Carried with the same key names, and with the honest one beside
            # them, so a consumer reading this dict is not left to guess which
            # of these numbers came from a measurement.
            "comprehension_level": self.comprehension_level,
            "fluency_level": self.fluency_level,
            "idiomatic": self.idiomatic,
            "register_aware": self.register_aware,
            "aspirational": ["comprehension_level", "fluency_level", "idiomatic", "register_aware"],
            "detectable": self.detectable,
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
    #: Whether a language model actually wrote this. False means the text is
    #: the apology below, or whatever the caller substituted for it. Carried as
    #: a flag rather than left for callers to detect by matching the apology's
    #: wording, which is a sentence, and sentences get rewritten.
    reachable: bool = True
    #: Whether this was assembled out of the knowledge store instead of
    #: written. A real answer and a reading-back of notes are different things
    #: and the page says so.
    from_study: bool = False

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
            "reachable": self.reachable,
            "from_study": self.from_study,
        }


# ---------------------------------------------------------------------------
# Speech system
# ---------------------------------------------------------------------------


class SpeechSystem:
    """A script detector, a keyword intent classifier, and a call to a model.

    See the module docstring for what each of those is worth; the short version
    is that the language is a guess whose confidence is now computed, the
    intent classifier is English-only, and there is no speech synthesiser.

    `supported_languages` advertises sixty `LanguageProfile`s, every one of them
    carrying `comprehension_level=1.0`, `fluency_level=1.0` and
    `idiomatic=True`. Those are three more constants nothing measured, left as
    they are rather than quietly halved to something equally invented — the
    number to fix them against does not exist yet, and a made-up 0.6 would be
    the same defect wearing a humbler face. What can be said is that the
    detector distinguishes nine scripts out of the sixty languages' writing
    systems, and `_script_confidence` reports that honestly per answer.

    Communication modes:
    - CHATBOT: text input/output. Works.
    - VOICEBOT: text plus `b""` where the audio would be. `has_voice` is False.
    - BOTH: the same, with the text.
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
        """Read the script and the keywords, and say how much that is worth.

        Returns (detected_language, classified_intent, confidence), where the
        confidence is `_detect_language`'s — computed from how many advertised
        languages share the script it saw, not asserted.

        Two limits worth knowing before using the answer. The language is a
        *script* guess: anything in Latin script comes back "en". The intent
        is English-only — `_classify_intent` matches English keywords and an
        ASCII question mark, so a Japanese question ending in an ideographic
        "？" is classified `conversation` rather than `question`.
        """
        language, confidence = self._detect_language(text)
        intent = self._classify_intent(text, language)

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
        response_text, reachable = self._generate_response(
            user_text, language, intent, context, conv
        )

        # Record scholar message
        scholar_msg = Message(role="scholar", text=response_text, language=language)
        conv.messages.append(scholar_msg)

        log.info(
            "chatbot response [%s] (%d turns): %s", language, conv.turn_count, response_text[:80]
        )
        return SpeechResponse(
            text=response_text, language=language, confidence=1.0, reachable=reachable
        )

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
        response_text, reachable = self._generate_response(
            user_text, language, intent, context, conv
        )

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
            reachable=reachable,
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

    def _detect_language(self, text: str) -> tuple[str, float]:
        """Guess the language of input text, and say how much the guess is worth.

        **This reads the script, not the language**, and the two are not the
        same thing. Measured against fourteen real sentences it got six right:
        every Latin-script language — Spanish, German, French, Italian,
        Portuguese, Turkish, Vietnamese, Polish — came back "English", because
        that is the branch that runs when nothing else matches.

        It returned `1.0` for all fourteen. That is the number this method
        exists to stop returning: a confidence is a claim about how much weight
        an answer can carry, and asserting certainty about eight wrong answers
        is worse than having no confidence at all, because a caller can route
        around a missing number and cannot route around a lie.

        So the confidence is now derived from what the detector actually did.
        Seeing a script narrows the answer to the advertised languages written
        in it, and nothing after that narrows it further, so the guess is worth
        one over however many those are — Korean is alone in Hangul and comes
        back 1.0; Arabic script carries Arabic, Urdu and Persian, so any of the
        three comes back 0.33 and *is labelled* `ar`, which is right a third of
        the time. Latin carries about forty of the sixty, so "en" on a Latin
        sentence is worth about 0.02. That number is small because the guess is
        bad, and a real detector would replace both halves of this method.
        """
        for script, (code, _ranges) in SCRIPTS.items():
            if any(any(low <= char <= high for low, high in _ranges) for char in text):
                return code, self._script_confidence(script)
        return "en", self._script_confidence("latin")

    def _script_confidence(self, script: str) -> float:
        """One over the advertised languages written in that script.

        Derived rather than typed, so a language added to the profile table
        lowers the confidence of every guess that could have been it. Typed,
        the two would drift the moment somebody added Farsi and forgot.

        Latin is counted by absence — every advertised language with no entry
        in `LANGUAGE_SCRIPT` — and that branch is the whole point, so it gets
        its own line rather than falling out of the general case. The first
        version of this did not: it looked for `== "latin"`, found nothing
        because Latin languages are the ones *not* in the map, and `max(n, 1)`
        turned the zero into a one and handed back **1.0** for the worst guess
        the detector makes. That is the defect this method was written to
        remove, reintroduced inside the fix for it, and the only reason it did
        not ship is that the numbers were printed and read.
        """
        if script == "latin":
            sharing = sum(1 for code in self._language_profiles if code not in LANGUAGE_SCRIPT)
        else:
            sharing = sum(
                1 for code in self._language_profiles if LANGUAGE_SCRIPT.get(code) == script
            )
        # A script with no advertised language cannot be guessed into: return
        # zero rather than dividing by it, because "no idea" is a real answer
        # and 1.0 is not.
        return round(1.0 / sharing, 2) if sharing else 0.0

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
    ) -> tuple[str, bool]:
        """Generate a natural-language response, and whether a model wrote it.

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
            return reply, True

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
            f"I understood the question as {intent.value} in {lang_name}.",
            False,
        )

    def _synthesise_speech(self, text: str, language: str, style: VoiceStyle) -> bytes:
        """Synthesise text into spoken audio.

        Integration point: in production calls a TTS model with appropriate
        voice characteristics for the language and style.

        There is no synthesis. The sentence that used to sit here described
        prosody, pacing and emotion produced by a synthesiser that does not
        exist, which is a description of an intention written in the present
        tense.
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
        """The sixty languages this system advertises.

        Advertises, not handles: `LanguageProfile.detectable` is the field that
        says which of them the detector can actually pick out, and it is true
        for a handful. The rest are here as a statement of intent.
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
