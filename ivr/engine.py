"""Deterministic call-flow interpreter.

A ``Call`` walks a ``Flow`` one node at a time. Nodes that need nothing from
the caller (play, identify, action, branch, transfer, hangup) run as soon as
they are entered. Menu and collect nodes stop the walk and wait for a
``CallerInput``: ``Dtmf``, ``Speech``, ``Timeout`` or ``Hangup``.

Time is simulated so handle time can be measured without audio: a prompt
takes ``len(text) / 15`` seconds, each DTMF press half a second, a speech
utterance two seconds, and a timeout the node's ``timeout_seconds``. Every
step appends to an ordered event log that the analytics read later.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from ivr import speech
from ivr.backend import Backend
from ivr.conditions import evaluate, parse_condition
from ivr.digits import is_digit_string, luhn_valid, normalize_ani
from ivr.directory import Directory, default_directory
from ivr.flow import (
    ActionNode,
    BranchNode,
    CollectNode,
    Flow,
    HangupNode,
    IdentifyNode,
    MenuNode,
    Node,
    PlayNode,
    TransferNode,
)

PROMPT_CHARS_PER_SECOND = 15.0
DTMF_PRESS_SECONDS = 0.5
SPEECH_SECONDS = 2.0
MAX_NODE_ENTRIES = 200
ACCOUNT_DIGITS = 10

OUTCOMES = ("contained", "transferred", "abandoned", "error")
EVENT_KINDS = frozenset(
    {
        "node_entered", "prompt_played", "prompt_variable_missing", "input_received",
        "input_invalid", "retry", "max_retries_reached", "variable_set", "identify_result",
        "action_called", "transferred", "hangup", "abandoned", "error",
    }
)

VARIABLE_PATTERN = re.compile(r"\{([A-Za-z_][A-Za-z0-9_.]*)\}")


@dataclass(frozen=True)
class Dtmf:
    """One or more keypad presses, for example ``Dtmf("4155550124#")``."""

    digits: str


@dataclass(frozen=True)
class Speech:
    """A spoken utterance as text."""

    utterance: str


@dataclass(frozen=True)
class Timeout:
    """The caller said and pressed nothing before the node's timeout."""


@dataclass(frozen=True)
class Hangup:
    """The caller hung up."""


CallerInput = Dtmf | Speech | Timeout | Hangup


@dataclass(frozen=True)
class Event:
    """One entry of the call's event log; ``time`` is the simulated clock in seconds."""

    kind: str
    time: float
    node_id: str | None
    data: dict[str, Any] = field(default_factory=dict)


class SimClock:
    """A simulated clock measured in seconds."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def advance(self, seconds: float) -> None:
        """Move the clock forward; negative durations are a programming error."""
        if seconds < 0:
            raise ValueError("cannot advance the clock backwards")
        self.now += seconds


class CallEndedError(RuntimeError):
    """Raised when input is fed to a call that has already ended."""


@dataclass
class CallResult:
    """Everything known about a finished call."""

    outcome: str
    events: list[Event]
    handle_time_seconds: float
    final_node: str | None
    variables: dict[str, Any]
    transfer_queue: str | None
    nodes_visited: list[str]
    ani: str = ""
    flow_name: str = ""
    flow_version: str = ""

    def prompts(self) -> list[str]:
        """Texts of every prompt played, in order."""
        return [event.data["text"] for event in self.events if event.kind == "prompt_played"]

    def count(self, kind: str) -> int:
        """Number of events of one kind."""
        return sum(1 for event in self.events if event.kind == kind)

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready representation."""
        data = asdict(self)
        data["events"] = [asdict(event) for event in self.events]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CallResult:
        """Inverse of ``to_dict``."""
        events = [Event(**event) for event in data["events"]]
        fields = {key: value for key, value in data.items() if key != "events"}
        return cls(events=events, **fields)


def render_prompt(text: str, variables: dict[str, Any]) -> tuple[str, list[str]]:
    """Substitute ``{variable}`` references; missing ones render as ``?``.

    Booleans render as yes/no. Floats render with two decimals (87.50) unless
    they are whole numbers (1000), so prompts read naturally when spoken.
    """
    missing: list[str] = []

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in variables or variables[name] is None:
            missing.append(name)
            return "?"
        value = variables[name]
        if isinstance(value, bool):
            return "yes" if value else "no"
        if isinstance(value, float):
            return str(int(value)) if value.is_integer() else f"{value:.2f}"
        return str(value)

    return VARIABLE_PATTERN.sub(replace, text), missing


def _collect_problem(node: CollectNode, digits: str) -> str | None:
    """Return why ``digits`` fail the node's rules, or None when they pass."""
    if not is_digit_string(digits):
        return "not digits"
    if node.validation == "account":
        if len(digits) != ACCOUNT_DIGITS:
            return f"account numbers have {ACCOUNT_DIGITS} digits"
        return None
    if not node.min_digits <= len(digits) <= node.max_digits:
        return f"expected {node.min_digits} to {node.max_digits} digits"
    if node.validation == "luhn" and not luhn_valid(digits):
        return "failed the Luhn check"
    return None


class Call:
    """One call through a flow. Create it, then ``step`` it with caller inputs.

    ``phase`` is ``idle`` before the first step, ``menu``, ``collect`` or
    ``confirm`` while waiting for the caller, and ``ended`` afterwards.
    """

    def __init__(
        self,
        flow: Flow,
        ani: str,
        directory: Directory | None = None,
        backend: Backend | None = None,
        clock: SimClock | None = None,
    ) -> None:
        self.flow = flow
        self.ani = normalize_ani(ani)
        self.directory = directory if directory is not None else default_directory()
        self.backend = backend if backend is not None else Backend(self.directory)
        self.clock = clock if clock is not None else SimClock()
        self.variables: dict[str, Any] = {"ani": self.ani}
        self.events: list[Event] = []
        self.nodes_visited: list[str] = []
        self.outcome: str | None = None
        self.transfer_queue: str | None = None
        self._phase = "idle"
        self._current: str | None = None
        self._attempts = 0
        self._pending_digits: str | None = None
        self._entries = 0

    # Read-only state -----------------------------------------------------

    @property
    def phase(self) -> str:
        """Where the call is: idle, menu, collect, confirm or ended."""
        return self._phase

    @property
    def ended(self) -> bool:
        """True once the call has an outcome."""
        return self._phase == "ended"

    @property
    def waiting_for_input(self) -> bool:
        """True while a menu or collect node is waiting on the caller."""
        return self._phase in ("menu", "collect", "confirm")

    @property
    def current_node(self) -> Node | None:
        """The node the call is at, or None before the first step."""
        return self.flow.nodes.get(self._current)

    @property
    def pending_digits(self) -> str | None:
        """Digits awaiting confirmation at a collect node, else None."""
        return self._pending_digits

    def result(self) -> CallResult:
        """Snapshot of the call; ``outcome`` is ``in_progress`` until it ends."""
        return CallResult(
            outcome=self.outcome or "in_progress",
            events=list(self.events),
            handle_time_seconds=self.clock.now,
            final_node=self._current,
            variables=dict(self.variables),
            transfer_queue=self.transfer_queue,
            nodes_visited=list(self.nodes_visited),
            ani=self.ani,
            flow_name=self.flow.name,
            flow_version=self.flow.version,
        )

    # Driving the call ----------------------------------------------------

    def step(self, caller_input: CallerInput | None = None) -> None:
        """Advance the call.

        With ``None`` the flow starts (or keeps waiting if it already waits
        for input). With an input, the input is applied to the waiting node
        and the flow runs until it needs the caller again or ends.
        """
        if self.ended:
            raise CallEndedError(f"call already ended with outcome {self.outcome}")
        if self._phase == "idle":
            self._enter(self.flow.start)
            self._run()
        if caller_input is None or self.ended:
            return
        self._apply_input(caller_input)
        self._run()

    # Internals -----------------------------------------------------------

    def _emit(self, kind: str, data: dict[str, Any] | None = None) -> None:
        if kind not in EVENT_KINDS:
            raise ValueError(f"unknown event kind {kind!r}")
        self.events.append(Event(kind, self.clock.now, self._current, dict(data or {})))

    def _end(self, outcome: str) -> None:
        self.outcome = outcome
        self._phase = "ended"

    def _error(self, message: str) -> None:
        self._emit("error", {"message": message})
        self._end("error")

    def _store(self, name: str, value: Any) -> None:
        self.variables[name] = value
        self._emit("variable_set", {"name": name, "value": value})

    def _play(self, text: str) -> str:
        rendered, missing = render_prompt(text, self.variables)
        for name in missing:
            self._emit("prompt_variable_missing", {"variable": name})
        seconds = len(rendered) / PROMPT_CHARS_PER_SECOND
        self._emit("prompt_played", {"text": rendered, "seconds": seconds})
        self.clock.advance(seconds)
        return rendered

    def _enter(self, node_id: str) -> None:
        self._entries += 1
        if self._entries > MAX_NODE_ENTRIES:
            self._error(f"loop guard: more than {MAX_NODE_ENTRIES} node entries")
            return
        if node_id not in self.flow.nodes:
            self._error(f"flow points at unknown node {node_id!r}")
            return
        self._current = node_id
        self._phase = "running"
        self._attempts = 0
        self._pending_digits = None
        self.nodes_visited.append(node_id)
        self._emit("node_entered", {"type": self.flow.nodes[node_id].type})

    def _run(self) -> None:
        while self._phase == "running":
            node = self.flow.nodes[self._current]  # type: ignore[index]
            if isinstance(node, PlayNode):
                self._play(node.prompt)
                self._enter(node.next)
            elif isinstance(node, MenuNode):
                self._play(node.prompt)
                self._phase = "menu"
            elif isinstance(node, CollectNode):
                self._play(node.prompt)
                self._phase = "collect"
            elif isinstance(node, IdentifyNode):
                self._identify(node)
            elif isinstance(node, ActionNode):
                self._action(node)
            elif isinstance(node, BranchNode):
                self._branch(node)
            elif isinstance(node, TransferNode):
                self._emit(
                    "transferred",
                    {"queue": node.queue, "reason": node.reason, "priority": node.priority},
                )
                self.transfer_queue = node.queue
                self._end("transferred")
            elif isinstance(node, HangupNode):
                if node.prompt:
                    self._play(node.prompt)
                self._emit("hangup", {})
                self._end("contained")

    def _identify(self, node: IdentifyNode) -> None:
        customer = self.directory.by_ani(self.ani)
        self._emit("identify_result", {"found": customer is not None})
        if customer is None:
            self._enter(node.on_not_found)
            return
        for name, value in customer.as_variables().items():
            self._store(name, value)
        self._enter(node.on_found)

    def _action(self, node: ActionNode) -> None:
        result = self.backend.call(node.name, self.variables)
        self._emit(
            "action_called",
            {"name": node.name, "success": result.success, "message": result.message},
        )
        for name, value in result.variables.items():
            self._store(name, value)
        self._enter(node.on_success if result.success else node.on_failure)

    def _branch(self, node: BranchNode) -> None:
        for rule in node.conditions:
            try:
                condition = parse_condition(rule.when)
            except ValueError as exc:
                self._error(f"bad condition in branch {node.id!r}: {exc}")
                return
            if evaluate(condition, self.variables):
                self._enter(rule.next)
                return
        self._enter(node.default)

    def _apply_input(self, caller_input: CallerInput) -> None:
        if isinstance(caller_input, Hangup):
            self._emit("abandoned", {"phase": self._phase})
            self._end("abandoned")
            return
        node = self.flow.nodes[self._current]  # type: ignore[index]
        if self._phase == "menu" and isinstance(node, MenuNode):
            self._menu_input(node, caller_input)
        elif self._phase == "collect" and isinstance(node, CollectNode):
            self._collect_input(node, caller_input)
        elif self._phase == "confirm" and isinstance(node, CollectNode):
            self._confirm_input(node, caller_input)

    def _charge_for(self, node: MenuNode | CollectNode, caller_input: CallerInput) -> None:
        """Advance the clock by the time the input took."""
        if isinstance(caller_input, Dtmf):
            self.clock.advance(DTMF_PRESS_SECONDS * len(caller_input.digits))
        elif isinstance(caller_input, Speech):
            self.clock.advance(SPEECH_SECONDS)
        elif isinstance(caller_input, Timeout):
            self.clock.advance(node.timeout_seconds)

    def _menu_input(self, node: MenuNode, caller_input: CallerInput) -> None:
        self._charge_for(node, caller_input)
        if isinstance(caller_input, Timeout):
            self._emit("input_received", {"kind": "timeout"})
            self._fail(node, "timeout")
            return
        if isinstance(caller_input, Dtmf):
            key = caller_input.digits
            matched = key if key in node.options else None
            self._emit("input_received", {"kind": "dtmf", "value": key, "matched": matched})
            reason = "no_match"
        else:
            match = speech.match_option(caller_input.utterance, node.options)
            matched = match.key
            self._emit(
                "input_received",
                {"kind": "speech", "value": caller_input.utterance, "matched": matched},
            )
            reason = match.reason
        if matched is None:
            self._fail(node, reason)
            return
        self._enter(node.options[matched].next)

    def _collect_input(self, node: CollectNode, caller_input: CallerInput) -> None:
        self._charge_for(node, caller_input)
        if isinstance(caller_input, Timeout):
            self._emit("input_received", {"kind": "timeout"})
            self._fail(node, "timeout")
            return
        if isinstance(caller_input, Dtmf):
            digits: str | None = caller_input.digits
            kind, value = "dtmf", caller_input.digits
        else:
            digits = speech.spoken_digits(caller_input.utterance)
            kind, value = "speech", caller_input.utterance
        if digits is not None and digits.endswith(node.terminator):
            digits = digits[: -len(node.terminator)]
        self._emit("input_received", {"kind": kind, "value": value, "digits": digits})
        problem = "no digits heard" if digits is None else _collect_problem(node, digits)
        if problem is not None:
            self._fail(node, "validation_failed", problem)
            return
        assert digits is not None
        if node.confirm:
            self._pending_digits = digits
            self._phase = "confirm"
            self._play(self._confirm_prompt(digits))
            return
        self._accept(node, digits)

    def _confirm_input(self, node: CollectNode, caller_input: CallerInput) -> None:
        self._charge_for(node, caller_input)
        if isinstance(caller_input, Timeout):
            self._emit("input_received", {"kind": "timeout"})
            self._fail(node, "timeout")
            return
        if isinstance(caller_input, Dtmf):
            answer: str | None = caller_input.digits
            kind, value = "dtmf", caller_input.digits
        else:
            answer = speech.spoken_confirmation(caller_input.utterance)
            kind, value = "speech", caller_input.utterance
        self._emit("input_received", {"kind": kind, "value": value, "matched": answer})
        if answer == "1":
            assert self._pending_digits is not None
            self._accept(node, self._pending_digits)
        elif answer == "2":
            self._reenter(node)
        else:
            self._fail(node, "no_match")

    def _accept(self, node: CollectNode, digits: str) -> None:
        self._pending_digits = None
        self._store(node.variable, digits)
        self._enter(node.next)

    @staticmethod
    def _confirm_prompt(digits: str) -> str:
        spaced = " ".join(digits)
        return f"You entered {spaced}. Press 1 if that is correct, or press 2 to enter it again."

    def _exhausted(self, node: MenuNode | CollectNode) -> bool:
        """Count one failed attempt; route to the fallback when retries run out."""
        self._attempts += 1
        if self._attempts <= node.max_retries:
            return False
        self._emit("max_retries_reached", {"attempts": self._attempts})
        if node.on_max_retries is None:
            self._error(f"node {node.id!r} ran out of retries and has no on_max_retries target")
        else:
            self._enter(node.on_max_retries)
        return True

    def _fail(self, node: MenuNode | CollectNode, reason: str, detail: str | None = None) -> None:
        data: dict[str, Any] = {"reason": reason, "attempt": self._attempts + 1}
        if detail:
            data["detail"] = detail
        self._emit("input_invalid", data)
        if self._exhausted(node):
            return
        self._emit("retry", {"attempt": self._attempts, "retries_left": node.max_retries - self._attempts})
        # Silence gets the timeout prompt, anything else the invalid prompt; a
        # node without the matching prompt simply replays its main prompt.
        hint = node.timeout_prompt if reason == "timeout" else node.invalid_prompt
        if hint:
            self._play(hint)
        if self._phase == "confirm":
            assert self._pending_digits is not None
            self._play(self._confirm_prompt(self._pending_digits))
        else:
            self._play(node.prompt)

    def _reenter(self, node: CollectNode) -> None:
        """The caller rejected the read-back and wants to enter the digits again."""
        if self._exhausted(node):
            return
        self._emit("retry", {"attempt": self._attempts, "retries_left": node.max_retries - self._attempts,
                             "reason": "reentered"})
        self._pending_digits = None
        self._phase = "collect"
        self._play(node.prompt)


def run_script(
    flow: Flow,
    inputs: list[CallerInput],
    ani: str = "5550100999",
    directory: Directory | None = None,
    backend: Backend | None = None,
) -> CallResult:
    """Run a whole call from a list of inputs.

    If the script runs out while the flow still waits for the caller, the
    caller is treated as having hung up, so the outcome is ``abandoned``.
    """
    call = Call(flow, ani, directory=directory, backend=backend)
    call.step(None)
    for caller_input in inputs:
        if call.ended:
            break
        call.step(caller_input)
    if not call.ended:
        call.step(Hangup())
    return call.result()
