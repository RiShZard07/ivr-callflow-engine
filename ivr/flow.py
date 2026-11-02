"""Call-flow definition model.

A flow is a directed graph of typed nodes stored as JSON. This module turns
the JSON into frozen dataclasses and checks the shape of every field: the
right keys, the right types, known enumerations and ranges that would make a
node meaningless (a transfer priority of 9, a collect that wants more minimum
digits than maximum digits). Graph-level checks such as dangling targets,
unreachable nodes and infinite play loops live in ``ivr.validate``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar


class FlowError(ValueError):
    """Raised when a flow definition has the wrong shape."""


NODE_TYPES = frozenset(
    {"play", "menu", "collect", "identify", "action", "branch", "transfer", "hangup"}
)
INPUT_NODE_TYPES = frozenset({"menu", "collect"})
TERMINAL_NODE_TYPES = frozenset({"transfer", "hangup"})
COLLECT_VALIDATIONS = frozenset({"digits", "luhn", "account"})
ACTION_NAMES = frozenset(
    {"balance_inquiry", "data_usage", "outage_check", "payment_arrangement", "reset_voicemail_pin"}
)
DTMF_KEYS = frozenset("0123456789*#")
DEFAULT_TIMEOUT_SECONDS = 5.0
MIN_TRANSFER_PRIORITY = 1
MAX_TRANSFER_PRIORITY = 5


@dataclass(frozen=True)
class MenuOption:
    """One DTMF key of a menu, with the speech phrases that select it."""

    key: str
    label: str
    next: str
    intents: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlayNode:
    """Play a prompt and continue."""

    type: ClassVar[str] = "play"
    id: str
    prompt: str
    next: str

    def targets(self) -> tuple[str, ...]:
        """Outgoing edges."""
        return (self.next,)

    def prompts(self) -> tuple[str, ...]:
        """Prompt texts this node can play."""
        return (self.prompt,)


@dataclass(frozen=True)
class MenuNode:
    """Play a prompt and wait for a DTMF key or a speech utterance.

    ``max_retries`` is the number of re-prompts after a failed attempt, so a
    caller gets ``max_retries + 1`` attempts before ``on_max_retries``.
    """

    type: ClassVar[str] = "menu"
    id: str
    prompt: str
    options: dict[str, MenuOption]
    max_retries: int
    on_max_retries: str | None
    invalid_prompt: str | None = None
    timeout_prompt: str | None = None
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    def targets(self) -> tuple[str, ...]:
        """Outgoing edges: every option plus the max-retry fallback."""
        edges = tuple(option.next for option in self.options.values())
        if self.on_max_retries is not None:
            edges += (self.on_max_retries,)
        return edges

    def prompts(self) -> tuple[str, ...]:
        """Prompt texts this node can play."""
        return tuple(p for p in (self.prompt, self.invalid_prompt, self.timeout_prompt) if p)


@dataclass(frozen=True)
class CollectNode:
    """Collect a digit string into a variable.

    ``validation`` is ``digits`` (any digits within the length range),
    ``luhn`` (digits that pass the Luhn check) or ``account`` (exactly ten
    digits). With ``confirm`` the digits are read back and the caller presses
    1 to accept or 2 to enter them again; each re-entry uses one retry so the
    loop is bounded.
    """

    type: ClassVar[str] = "collect"
    id: str
    prompt: str
    variable: str
    next: str
    max_retries: int
    on_max_retries: str | None
    min_digits: int = 1
    max_digits: int = 20
    terminator: str = "#"
    validation: str = "digits"
    confirm: bool = False
    invalid_prompt: str | None = None
    timeout_prompt: str | None = None
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    def targets(self) -> tuple[str, ...]:
        """Outgoing edges: the success path plus the max-retry fallback."""
        edges: tuple[str, ...] = (self.next,)
        if self.on_max_retries is not None:
            edges += (self.on_max_retries,)
        return edges

    def prompts(self) -> tuple[str, ...]:
        """Prompt texts this node can play."""
        return tuple(p for p in (self.prompt, self.invalid_prompt, self.timeout_prompt) if p)


@dataclass(frozen=True)
class IdentifyNode:
    """Look the caller up by ANI and set the ``customer.*`` variables."""

    type: ClassVar[str] = "identify"
    id: str
    on_found: str
    on_not_found: str

    def targets(self) -> tuple[str, ...]:
        """Outgoing edges."""
        return (self.on_found, self.on_not_found)

    def prompts(self) -> tuple[str, ...]:
        """Identify plays nothing."""
        return ()


@dataclass(frozen=True)
class ActionNode:
    """Call a self-service backend action and store its results as variables."""

    type: ClassVar[str] = "action"
    id: str
    name: str
    on_success: str
    on_failure: str

    def targets(self) -> tuple[str, ...]:
        """Outgoing edges."""
        return (self.on_success, self.on_failure)

    def prompts(self) -> tuple[str, ...]:
        """Actions play nothing."""
        return ()


@dataclass(frozen=True)
class BranchRule:
    """One ``when`` condition of a branch and where it leads."""

    when: str
    next: str


@dataclass(frozen=True)
class BranchNode:
    """Route on variables: the first true condition wins, else ``default``."""

    type: ClassVar[str] = "branch"
    id: str
    conditions: tuple[BranchRule, ...]
    default: str

    def targets(self) -> tuple[str, ...]:
        """Outgoing edges."""
        return tuple(rule.next for rule in self.conditions) + (self.default,)

    def prompts(self) -> tuple[str, ...]:
        """Branches play nothing."""
        return ()


@dataclass(frozen=True)
class TransferNode:
    """Hand the call to an agent queue; the call outcome is ``transferred``."""

    type: ClassVar[str] = "transfer"
    id: str
    queue: str
    priority: int
    reason: str

    def targets(self) -> tuple[str, ...]:
        """Transfers end the call."""
        return ()

    def prompts(self) -> tuple[str, ...]:
        """Transfers play nothing."""
        return ()


@dataclass(frozen=True)
class HangupNode:
    """End the call from the IVR side; the call outcome is ``contained``."""

    type: ClassVar[str] = "hangup"
    id: str
    prompt: str | None = None

    def targets(self) -> tuple[str, ...]:
        """Hangups end the call."""
        return ()

    def prompts(self) -> tuple[str, ...]:
        """Prompt texts this node can play."""
        return (self.prompt,) if self.prompt else ()


Node = (
    PlayNode
    | MenuNode
    | CollectNode
    | IdentifyNode
    | ActionNode
    | BranchNode
    | TransferNode
    | HangupNode
)


@dataclass(frozen=True)
class Flow:
    """A complete call flow: metadata, the queue list and the node graph."""

    name: str
    version: str
    start: str
    queues: tuple[str, ...]
    nodes: dict[str, Node]
    description: str = ""

    @property
    def label(self) -> str:
        """Short identifier used in reports, for example ``care 2.0``."""
        return f"{self.name} {self.version}"


_MISSING = object()


class _Fields:
    """Typed access to one JSON object with precise error messages.

    A key that is absent or set to JSON ``null`` counts as missing.
    """

    def __init__(self, raw: Any, where: str) -> None:
        if not isinstance(raw, Mapping):
            raise FlowError(f"{where}: expected an object, got {type(raw).__name__}")
        self.raw = raw
        self.where = where
        self.seen: set[str] = set()

    def _get(self, key: str, required: bool) -> Any:
        self.seen.add(key)
        if key in self.raw and self.raw[key] is not None:
            return self.raw[key]
        if required:
            raise FlowError(f"{self.where}: missing required field '{key}'")
        return _MISSING

    def string(self, key: str, required: bool = True, default: str | None = None) -> Any:
        """Read a non-empty string field."""
        value = self._get(key, required)
        if value is _MISSING:
            return default
        if not isinstance(value, str) or not value.strip():
            raise FlowError(f"{self.where}: '{key}' must be a non-empty string")
        return value

    def integer(self, key: str, required: bool = True, default: int | None = None) -> Any:
        """Read an integer field (booleans are rejected)."""
        value = self._get(key, required)
        if value is _MISSING:
            return default
        if isinstance(value, bool) or not isinstance(value, int):
            raise FlowError(f"{self.where}: '{key}' must be an integer")
        return value

    def number(self, key: str, required: bool = True, default: float | None = None) -> Any:
        """Read a float field (integers are accepted, booleans are not)."""
        value = self._get(key, required)
        if value is _MISSING:
            return default
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise FlowError(f"{self.where}: '{key}' must be a number")
        return float(value)

    def boolean(self, key: str, required: bool = True, default: bool | None = None) -> Any:
        """Read a boolean field."""
        value = self._get(key, required)
        if value is _MISSING:
            return default
        if not isinstance(value, bool):
            raise FlowError(f"{self.where}: '{key}' must be true or false")
        return value

    def string_list(self, key: str, required: bool = True) -> tuple[str, ...]:
        """Read a list of strings; an optional missing list is empty."""
        value = self._get(key, required)
        if value is _MISSING:
            return ()
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise FlowError(f"{self.where}: '{key}' must be a list of strings")
        return tuple(value)

    def mapping(self, key: str) -> Mapping[str, Any]:
        """Read a required object field."""
        value = self._get(key, True)
        if not isinstance(value, Mapping):
            raise FlowError(f"{self.where}: '{key}' must be an object")
        return value

    def sequence(self, key: str) -> list[Any]:
        """Read a required list field."""
        value = self._get(key, True)
        if not isinstance(value, list):
            raise FlowError(f"{self.where}: '{key}' must be a list")
        return value

    def finish(self) -> None:
        """Reject keys that were never read; this catches misspelled fields."""
        unknown = sorted(set(self.raw) - self.seen)
        if unknown:
            raise FlowError(f"{self.where}: unknown field(s) {', '.join(unknown)}")


def _retry_fields(fields: _Fields) -> tuple[int, str | None]:
    max_retries = fields.integer("max_retries")
    on_max_retries = fields.string("on_max_retries", required=False)
    return max_retries, on_max_retries


def _parse_play(node_id: str, fields: _Fields) -> PlayNode:
    return PlayNode(id=node_id, prompt=fields.string("prompt"), next=fields.string("next"))


def _parse_option(node_id: str, key: str, raw: Any) -> MenuOption:
    if key not in DTMF_KEYS:
        raise FlowError(f"node '{node_id}': option key {key!r} is not a DTMF key (0-9, * or #)")
    fields = _Fields(raw, f"node '{node_id}' option '{key}'")
    option = MenuOption(
        key=key,
        label=fields.string("label"),
        next=fields.string("next"),
        intents=fields.string_list("intents", required=False),
    )
    fields.finish()
    return option


def _parse_menu(node_id: str, fields: _Fields) -> MenuNode:
    prompt = fields.string("prompt")
    raw_options = fields.mapping("options")
    if not raw_options:
        raise FlowError(f"node '{node_id}': a menu needs at least one option")
    options = {key: _parse_option(node_id, key, raw) for key, raw in raw_options.items()}
    max_retries, on_max_retries = _retry_fields(fields)
    return MenuNode(
        id=node_id,
        prompt=prompt,
        options=options,
        max_retries=max_retries,
        on_max_retries=on_max_retries,
        invalid_prompt=fields.string("invalid_prompt", required=False),
        timeout_prompt=fields.string("timeout_prompt", required=False),
        timeout_seconds=_positive(fields, "timeout_seconds", DEFAULT_TIMEOUT_SECONDS),
    )


def _positive(fields: _Fields, key: str, default: float) -> float:
    value = fields.number(key, required=False, default=default)
    if value <= 0:
        raise FlowError(f"{fields.where}: '{key}' must be greater than zero")
    return value


def _parse_collect(node_id: str, fields: _Fields) -> CollectNode:
    prompt = fields.string("prompt")
    variable = fields.string("variable")
    nxt = fields.string("next")
    max_retries, on_max_retries = _retry_fields(fields)
    min_digits = fields.integer("min_digits", required=False, default=1)
    max_digits = fields.integer("max_digits", required=False, default=20)
    if min_digits < 1 or max_digits < min_digits:
        raise FlowError(
            f"node '{node_id}': need 1 <= min_digits <= max_digits, got {min_digits} and {max_digits}"
        )
    terminator = fields.string("terminator", required=False, default="#")
    if terminator not in ("#", "*"):
        raise FlowError(f"node '{node_id}': terminator must be '#' or '*'")
    validation = fields.string("validation", required=False, default="digits")
    if validation not in COLLECT_VALIDATIONS:
        raise FlowError(
            f"node '{node_id}': validation must be one of {sorted(COLLECT_VALIDATIONS)}, got {validation!r}"
        )
    return CollectNode(
        id=node_id,
        prompt=prompt,
        variable=variable,
        next=nxt,
        max_retries=max_retries,
        on_max_retries=on_max_retries,
        min_digits=min_digits,
        max_digits=max_digits,
        terminator=terminator,
        validation=validation,
        confirm=fields.boolean("confirm", required=False, default=False),
        invalid_prompt=fields.string("invalid_prompt", required=False),
        timeout_prompt=fields.string("timeout_prompt", required=False),
        timeout_seconds=_positive(fields, "timeout_seconds", DEFAULT_TIMEOUT_SECONDS),
    )


def _parse_identify(node_id: str, fields: _Fields) -> IdentifyNode:
    return IdentifyNode(
        id=node_id, on_found=fields.string("on_found"), on_not_found=fields.string("on_not_found")
    )


def _parse_action(node_id: str, fields: _Fields) -> ActionNode:
    name = fields.string("name")
    if name not in ACTION_NAMES:
        raise FlowError(f"node '{node_id}': unknown action {name!r}; known: {sorted(ACTION_NAMES)}")
    return ActionNode(
        id=node_id,
        name=name,
        on_success=fields.string("on_success"),
        on_failure=fields.string("on_failure"),
    )


def _parse_branch(node_id: str, fields: _Fields) -> BranchNode:
    rules = []
    for index, raw in enumerate(fields.sequence("conditions")):
        rule_fields = _Fields(raw, f"node '{node_id}' condition {index}")
        rules.append(BranchRule(when=rule_fields.string("when"), next=rule_fields.string("next")))
        rule_fields.finish()
    return BranchNode(id=node_id, conditions=tuple(rules), default=fields.string("default"))


def _parse_transfer(node_id: str, fields: _Fields) -> TransferNode:
    priority = fields.integer("priority")
    if not MIN_TRANSFER_PRIORITY <= priority <= MAX_TRANSFER_PRIORITY:
        raise FlowError(
            f"node '{node_id}': priority must be between {MIN_TRANSFER_PRIORITY} and "
            f"{MAX_TRANSFER_PRIORITY}, got {priority}"
        )
    return TransferNode(
        id=node_id, queue=fields.string("queue"), priority=priority, reason=fields.string("reason")
    )


def _parse_hangup(node_id: str, fields: _Fields) -> HangupNode:
    return HangupNode(id=node_id, prompt=fields.string("prompt", required=False))


_PARSERS = {
    "play": _parse_play,
    "menu": _parse_menu,
    "collect": _parse_collect,
    "identify": _parse_identify,
    "action": _parse_action,
    "branch": _parse_branch,
    "transfer": _parse_transfer,
    "hangup": _parse_hangup,
}


def parse_node(node_id: str, raw: Any) -> Node:
    """Build one node from its JSON object, raising ``FlowError`` on bad shape."""
    fields = _Fields(raw, f"node '{node_id}'")
    node_type = fields.string("type")
    if node_type not in NODE_TYPES:
        raise FlowError(f"node '{node_id}': unknown type {node_type!r}; known: {sorted(NODE_TYPES)}")
    node = _PARSERS[node_type](node_id, fields)
    fields.finish()
    return node


def flow_from_dict(data: Any) -> Flow:
    """Build a ``Flow`` from already-parsed JSON data."""
    fields = _Fields(data, "flow")
    name = fields.string("name")
    version = fields.string("version")
    start = fields.string("start")
    description = fields.string("description", required=False, default="")
    queues = fields.string_list("queues", required=False)
    raw_nodes = fields.mapping("nodes")
    if not raw_nodes:
        raise FlowError("flow: 'nodes' must contain at least one node")
    nodes = {node_id: parse_node(node_id, raw) for node_id, raw in raw_nodes.items()}
    fields.finish()
    return Flow(
        name=name,
        version=version,
        start=start,
        queues=queues,
        nodes=nodes,
        description=description,
    )


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise FlowError(f"duplicate key {key!r} in flow JSON")
        result[key] = value
    return result


def loads_flow(text: str) -> Flow:
    """Parse flow JSON text. Duplicate keys (two nodes with one id) are an error."""
    try:
        data = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise FlowError(f"invalid JSON: {exc}") from exc
    return flow_from_dict(data)


def load_flow(path: str | Path) -> Flow:
    """Load a flow from a JSON file."""
    return loads_flow(Path(path).read_text(encoding="utf-8"))
