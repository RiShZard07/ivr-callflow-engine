"""Speech intent matching for menu options.

This is keyword matching over text, not speech recognition. An utterance is
normalized (lower case, punctuation removed, spaces collapsed, a small
synonym map applied, stop words dropped) and scored against each menu option
by the number of tokens it shares with that option's intents. The best option
wins when its score reaches ``MIN_SCORE`` and no other option ties with it;
a tie is reported as ambiguous so the engine re-prompts instead of guessing.

Spoken keys are also understood: "press two", "two" and "2" all select key 2.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from ivr.flow import MenuOption

MIN_SCORE = 1

PHRASE_SYNONYMS: tuple[tuple[str, str], ...] = (
    ("service is down", "outage"),
    ("service down", "outage"),
    ("not working", "outage"),
    ("no internet", "outage"),
    ("wi fi", "outage"),
    ("voice mail", "voicemail"),
    ("customer service", "agent"),
    ("real person", "agent"),
    ("talk to someone", "agent"),
    ("speak to someone", "agent"),
    ("speak with someone", "agent"),
    ("how much do i owe", "balance"),
    ("more time to pay", "payment arrangement"),
)

WORD_SYNONYMS: dict[str, str] = {
    "bill": "billing",
    "bills": "billing",
    "invoice": "billing",
    "statement": "billing",
    "charge": "billing",
    "charges": "billing",
    "pay": "payment",
    "paying": "payment",
    "payments": "payment",
    "paid": "payment",
    "internet": "outage",
    "wifi": "outage",
    "outages": "outage",
    "down": "outage",
    "connection": "outage",
    "representative": "agent",
    "rep": "agent",
    "operator": "agent",
    "human": "agent",
    "person": "agent",
    "someone": "agent",
    "somebody": "agent",
    "gigabytes": "data",
    "gigs": "data",
    "gb": "data",
    "owe": "balance",
    "mailbox": "voicemail",
    "password": "pin",
    "passcode": "pin",
}

STOPWORDS = frozenset(
    {
        "i", "me", "my", "a", "an", "the", "to", "is", "it", "its", "of", "on", "for",
        "and", "or", "please", "want", "need", "would", "like", "can", "could", "you",
        "help", "with", "about", "hi", "hello", "um", "uh", "yes", "yeah", "okay", "ok",
        "there", "this", "that", "do", "have", "am", "think", "something", "some", "just",
        "get", "know", "wanted", "im", "id", "be", "in", "at", "so",
    }
)

DIGIT_WORDS: dict[str, str] = {
    "zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "star": "*", "pound": "#", "hash": "#",
}
_KEY_FILLER = frozenset(
    {"press", "number", "option", "key", "the", "hit", "dial", "my", "is", "account",
     "it", "its", "zip", "code", "um", "uh", "please"}
)
_YES_WORDS = frozenset({"yes", "yeah", "yep", "correct", "right", "confirm", "ok", "okay"})
_NO_WORDS = frozenset({"no", "nope", "wrong", "incorrect", "again", "redo", "reenter"})


def normalize(utterance: str) -> list[str]:
    """Return the normalized, synonym-mapped, stop-word-free tokens of an utterance."""
    text = utterance.lower().replace("-", " ").replace("'", "")
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    text = " " + re.sub(r"\s+", " ", text).strip() + " "
    for phrase, replacement in PHRASE_SYNONYMS:
        text = text.replace(f" {phrase} ", f" {replacement} ")
    tokens = []
    for token in text.split():
        token = WORD_SYNONYMS.get(token, token)
        if token not in STOPWORDS:
            tokens.append(token)
    return tokens


def _key_tokens(utterance: str) -> list[str]:
    cleaned = re.sub(r"[^a-z0-9#* ]", " ", utterance.lower())
    return [tok for tok in cleaned.split() if tok not in _KEY_FILLER]


def spoken_dtmf(utterance: str) -> str | None:
    """Return the single DTMF key an utterance names ("press two" -> "2"), else None."""
    keys = []
    for token in _key_tokens(utterance):
        if token in DIGIT_WORDS:
            keys.append(DIGIT_WORDS[token])
        elif len(token) == 1 and token in "0123456789*#":
            keys.append(token)
        else:
            return None
    return keys[0] if len(keys) == 1 else None


def spoken_digits(utterance: str) -> str | None:
    """Return the digit string an utterance spells out ("four one five" -> "415").

    Digit words and digit characters are concatenated in order. Any other word
    besides a few fillers makes the utterance unusable and the result is None.
    """
    digits = []
    for token in _key_tokens(utterance):
        if token in DIGIT_WORDS:
            digits.append(DIGIT_WORDS[token])
        elif all(ch in "0123456789#*" for ch in token):
            digits.append(token)
        else:
            return None
    return "".join(digits) or None


def spoken_confirmation(utterance: str) -> str | None:
    """Map yes/no style answers at a confirmation step to key 1 or 2."""
    key = spoken_dtmf(utterance)
    if key in ("1", "2"):
        return key
    tokens = set(_key_tokens(utterance))
    if tokens & _YES_WORDS and not tokens & _NO_WORDS:
        return "1"
    if tokens & _NO_WORDS and not tokens & _YES_WORDS:
        return "2"
    return None


@dataclass(frozen=True)
class SpeechMatch:
    """Outcome of matching one utterance against a menu.

    ``reason`` is ``matched`` or ``key`` on success, and ``no_match`` or
    ``ambiguous`` when ``key`` is None.
    """

    key: str | None
    reason: str
    scores: dict[str, int] = field(default_factory=dict)


def score_options(utterance: str, options: Mapping[str, MenuOption]) -> dict[str, int]:
    """Count the normalized tokens each option's intents share with the utterance."""
    tokens = set(normalize(utterance))
    scores: dict[str, int] = {}
    for key, option in options.items():
        intent_tokens: set[str] = set()
        for intent in option.intents:
            intent_tokens.update(normalize(intent))
        scores[key] = len(tokens & intent_tokens)
    return scores


def match_option(utterance: str, options: Mapping[str, MenuOption]) -> SpeechMatch:
    """Pick the menu option an utterance selects, or explain why none was picked."""
    key = spoken_dtmf(utterance)
    if key is not None:
        if key in options:
            return SpeechMatch(key, "key")
        return SpeechMatch(None, "no_match")
    scores = score_options(utterance, options)
    best = max(scores.values(), default=0)
    if best < MIN_SCORE:
        return SpeechMatch(None, "no_match", scores)
    winners = [option_key for option_key, score in scores.items() if score == best]
    if len(winners) > 1:
        return SpeechMatch(None, "ambiguous", scores)
    return SpeechMatch(winners[0], "matched", scores)
