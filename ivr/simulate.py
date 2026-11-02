"""Synthetic caller simulator.

Each call is made by a ``Persona`` drawn from a seeded random generator: what
the caller wants, whether they are in the directory and calling from their
registered phone, whether they know their account number, how often they
mis-key, whether they prefer to speak or press keys, and how much patience
they have. The persona then plays the flow with a simple policy:

- at a menu, pick the option whose label and intents match the intent (by
  key, or by speaking a phrase for that intent); fall back to the agent
  option when nothing matches; press the "finished" option once the goal
  action has succeeded
- at a collect node, key the account number or zip code (with mis-keys), or
  stay silent when the number is unknown
- at a confirmation, accept the read-back only when it matches
- hang up once patience runs out, in seconds or in failed attempts

The same seed always yields the same personas and the same results.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from random import Random

from ivr import speech
from ivr.backend import Backend
from ivr.directory import ZIP_CODES, Directory, default_directory
from ivr.engine import Call, CallerInput, CallResult, Dtmf, Hangup, Speech, Timeout
from ivr.flow import DTMF_KEYS, CollectNode, Flow, MenuNode

INTENTS = ("billing", "outage", "data", "payment", "voicemail", "agent")
INTENT_WEIGHTS = (30, 20, 15, 15, 5, 15)
INTENT_KEYWORDS: dict[str, frozenset[str]] = {
    "billing": frozenset({"billing", "balance", "statement"}),
    "outage": frozenset({"outage", "service", "tv"}),
    "data": frozenset({"data", "usage", "plan"}),
    "payment": frozenset({"payment", "arrangement", "extension"}),
    "voicemail": frozenset({"voicemail", "pin"}),
    "agent": frozenset({"agent", "representative", "operator"}),
}
INTENT_ACTION: dict[str, str | None] = {
    "billing": "balance_inquiry",
    "outage": "outage_check",
    "data": "data_usage",
    "payment": "payment_arrangement",
    "voicemail": "reset_voicemail_pin",
    "agent": None,
}
INTENT_PHRASES: dict[str, tuple[str, ...]] = {
    "billing": ("what is my balance", "I have a question about my bill", "billing", "how much do I owe"),
    "outage": ("my internet is down", "is there an outage", "my service is not working", "no internet"),
    "data": ("how much data have I used", "data usage", "check my data"),
    "payment": ("I need more time to pay", "set up a payment arrangement", "payment arrangement"),
    "voicemail": ("reset my voicemail pin", "voicemail", "I forgot my voicemail password"),
    "agent": ("agent", "I want to talk to a person", "representative please", "customer service"),
}
DONE_WORDS = frozenset({"done", "finished", "goodbye", "else", "nothing"})
MUMBLES = ("um", "hello", "what", "can you repeat that", "hmm")
UNKNOWN_ANI_PREFIX = "55502"


@dataclass(frozen=True)
class Persona:
    """One synthetic caller."""

    intent: str
    ani: str
    account: str | None
    zip_code: str
    knows_account: bool
    miskey_probability: float
    prefers_speech: bool
    patience_seconds: float
    patience_attempts: int
    in_directory: bool
    seed: int


def draw_persona(rng: Random, directory: Directory) -> Persona:
    """Draw one persona from the population mix."""
    intent = rng.choices(INTENTS, weights=INTENT_WEIGHTS)[0]
    is_customer = rng.random() < 0.90
    in_directory = False
    if is_customer:
        customer = rng.choice(directory.customers)
        in_directory = rng.random() < 0.75  # calling from the registered phone
        ani = customer.ani if in_directory else _unknown_ani(rng)
        account: str | None = customer.account
        zip_code = customer.zip_code
        knows_account = rng.random() < 0.70
    else:
        # A prospect or a wrong number: no account anywhere in the directory.
        ani = _unknown_ani(rng)
        account = None
        zip_code = rng.choice(ZIP_CODES)
        knows_account = False
    return Persona(
        intent=intent,
        ani=ani,
        account=account,
        zip_code=zip_code,
        knows_account=knows_account,
        miskey_probability=rng.uniform(0.0, 0.15),
        prefers_speech=rng.random() < 0.40,
        patience_seconds=rng.uniform(60.0, 240.0),
        patience_attempts=rng.choice((2, 3, 4)),
        in_directory=in_directory,
        seed=rng.getrandbits(32),
    )


def _unknown_ani(rng: Random) -> str:
    return UNKNOWN_ANI_PREFIX + str(rng.randint(10000, 99999))


def draw_personas(n: int, seed: int, directory: Directory | None = None) -> list[Persona]:
    """Draw ``n`` personas deterministically from ``seed``."""
    directory = directory if directory is not None else default_directory()
    rng = Random(seed)
    return [draw_persona(rng, directory) for _ in range(n)]


def _option_scores(node: MenuNode, keywords: frozenset[str]) -> dict[str, int]:
    scores = {}
    for key, option in node.options.items():
        tokens = set(speech.normalize(option.label))
        for intent in option.intents:
            tokens.update(speech.normalize(intent))
        scores[key] = len(tokens & keywords)
    return scores


def _best_option(node: MenuNode, keywords: frozenset[str]) -> str | None:
    scores = _option_scores(node, keywords)
    best = max(scores.values(), default=0)
    if best == 0:
        return None
    winners = [key for key, score in scores.items() if score == best]
    return winners[0] if len(winners) == 1 else None


def _corrupt(digits: str, rng: Random) -> str:
    """Replace one digit with a different one, the way a mis-key would."""
    index = rng.randrange(len(digits))
    wrong = rng.choice([d for d in "0123456789" if d != digits[index]])
    return digits[:index] + wrong + digits[index + 1:]


def _invalid_attempts(call: Call, since_node_entry: bool) -> int:
    count = 0
    for event in reversed(call.events):
        if since_node_entry and event.kind == "node_entered":
            break
        if event.kind == "input_invalid":
            count += 1
    return count


def _goal_reached(call: Call, persona: Persona) -> bool:
    action = INTENT_ACTION[persona.intent]
    if action is None:
        return False
    return any(
        event.kind == "action_called" and event.data["name"] == action and event.data["success"]
        for event in call.events
    )


def _menu_choice(call: Call, node: MenuNode, persona: Persona, rng: Random) -> CallerInput:
    if _goal_reached(call, persona):
        done = _best_option(node, DONE_WORDS)
        return Dtmf(done) if done is not None else Hangup()
    spoken_intent = persona.intent
    key = _best_option(node, INTENT_KEYWORDS[persona.intent])
    if key is None:
        key = _best_option(node, INTENT_KEYWORDS["agent"])
        spoken_intent = "agent"
    if key is None:
        return Timeout()
    first_try = _invalid_attempts(call, since_node_entry=True) == 0
    if persona.prefers_speech and first_try:
        if rng.random() < persona.miskey_probability:
            return Speech(rng.choice(MUMBLES))
        return Speech(rng.choice(INTENT_PHRASES[spoken_intent]))
    if rng.random() < persona.miskey_probability:
        return Dtmf(rng.choice(sorted(DTMF_KEYS - {key})))
    return Dtmf(key)


def _intended_digits(node: CollectNode, persona: Persona) -> str | None:
    if node.variable == "account":
        return persona.account if persona.knows_account else None
    if node.variable == "zip":
        return persona.zip_code
    return None


def _collect_entry(node: CollectNode, persona: Persona, rng: Random) -> CallerInput:
    digits = _intended_digits(node, persona)
    if digits is None:
        return Timeout()
    if rng.random() < persona.miskey_probability:
        digits = _corrupt(digits, rng)
    return Dtmf(digits + node.terminator)


def next_input(call: Call, persona: Persona, rng: Random) -> CallerInput:
    """Decide the persona's next input for a call that is waiting."""
    if call.clock.now >= persona.patience_seconds:
        return Hangup()
    if _invalid_attempts(call, since_node_entry=False) >= persona.patience_attempts:
        return Hangup()
    node = call.current_node
    if call.phase == "menu" and isinstance(node, MenuNode):
        return _menu_choice(call, node, persona, rng)
    if call.phase == "collect" and isinstance(node, CollectNode):
        return _collect_entry(node, persona, rng)
    if call.phase == "confirm" and isinstance(node, CollectNode):
        return Dtmf("1") if call.pending_digits == _intended_digits(node, persona) else Dtmf("2")
    return Hangup()


def simulate_persona(
    flow: Flow, persona: Persona, directory: Directory, backend: Backend
) -> CallResult:
    """Play one persona through the flow."""
    call = Call(flow, persona.ani, directory=directory, backend=backend)
    rng = Random(persona.seed)
    call.step(None)
    while not call.ended:
        call.step(next_input(call, persona, rng))
    return call.result()


def simulate(
    flow: Flow,
    n_calls: int,
    seed: int,
    directory: Directory | None = None,
    backend: Backend | None = None,
) -> list[CallResult]:
    """Simulate ``n_calls`` calls through ``flow``; deterministic for a seed."""
    directory = directory if directory is not None else default_directory()
    backend = backend if backend is not None else Backend(directory)
    personas = draw_personas(n_calls, seed, directory)
    return simulate_personas(flow, personas, directory, backend)


def simulate_personas(
    flow: Flow, personas: Iterable[Persona], directory: Directory, backend: Backend
) -> list[CallResult]:
    """Play a fixed list of personas, for example the same ones against two flows."""
    return [simulate_persona(flow, persona, directory, backend) for persona in personas]
