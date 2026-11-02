"""Static validation of call flows.

``validate(flow)`` returns a list of ``Issue`` records without running a
single call. Errors are problems that will break or strand a call (a target
that does not exist, a play loop with no way out, a transfer to an unknown
queue). Warnings are design smells that a reviewer should look at (an
unreachable node, a menu option speech callers cannot choose).

Two shape problems are caught earlier, when the JSON is loaded: a node whose
fields have the wrong type, and two nodes sharing one id (a JSON object
cannot hold both, so ``ivr.flow`` rejects duplicate keys at parse time).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from ivr.backend import ACTION_OUTPUTS
from ivr.conditions import ConditionError, parse_condition
from ivr.directory import IDENTIFY_VARIABLES
from ivr.engine import VARIABLE_PATTERN
from ivr.flow import (
    INPUT_NODE_TYPES,
    TERMINAL_NODE_TYPES,
    ActionNode,
    BranchNode,
    CollectNode,
    Flow,
    IdentifyNode,
    MenuNode,
    Node,
    TransferNode,
)

ERROR = "error"
WARNING = "warning"
MIN_MAX_RETRIES = 1
MAX_MAX_RETRIES = 10
ALWAYS_SET_VARIABLES = frozenset({"ani"})


@dataclass(frozen=True)
class Issue:
    """One validation finding."""

    level: str
    node_id: str | None
    message: str
    code: str

    def __str__(self) -> str:
        where = self.node_id if self.node_id is not None else "flow"
        return f"{self.level}: [{where}] {self.message} ({self.code})"


def validate(flow: Flow) -> list[Issue]:
    """Run every static check and return the findings, errors first."""
    issues: list[Issue] = []
    issues += _check_ids(flow)
    issues += _check_start(flow)
    issues += _check_targets(flow)
    issues += _check_reachability(flow)
    issues += _check_input_handling(flow)
    issues += _check_retry_ranges(flow)
    issues += _check_queues(flow)
    issues += _check_conditions(flow)
    issues += _check_input_free_cycles(flow)
    issues += _check_option_intents(flow)
    issues += _check_prompt_variables(flow)
    issues += _check_terminal(flow)
    issues.sort(key=lambda issue: (issue.level != ERROR, issue.node_id or "", issue.code))
    return issues


def errors(issues: list[Issue]) -> list[Issue]:
    """Only the error-level findings."""
    return [issue for issue in issues if issue.level == ERROR]


def has_errors(issues: list[Issue]) -> bool:
    """True when at least one finding is an error."""
    return any(issue.level == ERROR for issue in issues)


def format_issues(issues: list[Issue]) -> str:
    """Human-readable report, one finding per line."""
    if not issues:
        return "no issues found"
    return "\n".join(str(issue) for issue in issues)


def _check_ids(flow: Flow) -> list[Issue]:
    return [
        Issue(ERROR, node_id, "node id is empty", "empty_node_id")
        for node_id in flow.nodes
        if not node_id.strip()
    ]


def _check_start(flow: Flow) -> list[Issue]:
    if flow.start in flow.nodes:
        return []
    return [Issue(ERROR, None, f"start node {flow.start!r} does not exist", "unknown_start")]


def _check_targets(flow: Flow) -> list[Issue]:
    issues = []
    for node_id, node in flow.nodes.items():
        for target in node.targets():
            if target not in flow.nodes:
                issues.append(
                    Issue(ERROR, node_id, f"points at unknown node {target!r}", "dangling_target")
                )
    return issues


def _reachable(flow: Flow) -> set[str]:
    if flow.start not in flow.nodes:
        return set()
    seen = {flow.start}
    queue = deque([flow.start])
    while queue:
        node_id = queue.popleft()
        for target in flow.nodes[node_id].targets():
            if target in flow.nodes and target not in seen:
                seen.add(target)
                queue.append(target)
    return seen


def _check_reachability(flow: Flow) -> list[Issue]:
    reachable = _reachable(flow)
    return [
        Issue(WARNING, node_id, "node cannot be reached from the start node", "unreachable_node")
        for node_id in flow.nodes
        if node_id not in reachable
    ]


def _check_input_handling(flow: Flow) -> list[Issue]:
    issues = []
    for node_id, node in flow.nodes.items():
        if isinstance(node, (MenuNode, CollectNode)):
            if node.on_max_retries is None:
                issues.append(
                    Issue(
                        ERROR,
                        node_id,
                        "no on_max_retries target: callers who keep failing have nowhere to go",
                        "missing_max_retries_target",
                    )
                )
            if node.invalid_prompt is None:
                issues.append(
                    Issue(
                        WARNING,
                        node_id,
                        "no invalid_prompt: callers get no explanation before the re-prompt",
                        "missing_invalid_prompt",
                    )
                )
            if node.timeout_prompt is None:
                issues.append(
                    Issue(
                        WARNING,
                        node_id,
                        "no timeout_prompt: silent callers get no nudge before the re-prompt",
                        "missing_timeout_prompt",
                    )
                )
    return issues


def _check_retry_ranges(flow: Flow) -> list[Issue]:
    issues = []
    for node_id, node in flow.nodes.items():
        if isinstance(node, (MenuNode, CollectNode)) and not (
            MIN_MAX_RETRIES <= node.max_retries <= MAX_MAX_RETRIES
        ):
            issues.append(
                Issue(
                    ERROR,
                    node_id,
                    f"max_retries must be between {MIN_MAX_RETRIES} and {MAX_MAX_RETRIES}, "
                    f"got {node.max_retries}",
                    "max_retries_out_of_range",
                )
            )
    return issues


def _check_queues(flow: Flow) -> list[Issue]:
    return [
        Issue(ERROR, node_id, f"transfer to unknown queue {node.queue!r}", "unknown_queue")
        for node_id, node in flow.nodes.items()
        if isinstance(node, TransferNode) and node.queue not in flow.queues
    ]


def _check_conditions(flow: Flow) -> list[Issue]:
    issues = []
    for node_id, node in flow.nodes.items():
        if isinstance(node, BranchNode):
            for rule in node.conditions:
                try:
                    parse_condition(rule.when)
                except ConditionError as exc:
                    issues.append(Issue(ERROR, node_id, str(exc), "invalid_condition"))
    return issues


def _check_input_free_cycles(flow: Flow) -> list[Issue]:
    """Find cycles made only of nodes that never wait for the caller.

    Such a cycle (play -> play, or play -> branch -> play) would spin until
    the engine's loop guard ends the call with an error, so it is an error.
    """
    automatic = {
        node_id
        for node_id, node in flow.nodes.items()
        if node.type not in INPUT_NODE_TYPES and node.type not in TERMINAL_NODE_TYPES
    }
    edges = {
        node_id: [t for t in flow.nodes[node_id].targets() if t in automatic]
        for node_id in automatic
    }
    issues = []
    reported: set[frozenset[str]] = set()
    state: dict[str, int] = {}  # 1 = on the current path, 2 = finished
    path: list[str] = []

    def visit(node_id: str) -> None:
        state[node_id] = 1
        path.append(node_id)
        for target in edges[node_id]:
            if state.get(target) == 1:
                cycle = path[path.index(target):]
                key = frozenset(cycle)
                if key not in reported:
                    reported.add(key)
                    issues.append(
                        Issue(
                            ERROR,
                            cycle[0],
                            "cycle with no input node: " + " -> ".join(cycle + [target]),
                            "input_free_cycle",
                        )
                    )
            elif target not in state:
                visit(target)
        path.pop()
        state[node_id] = 2

    # Start the search at the flow's start node so a cycle is reported from
    # the node a caller would reach first.
    order = ([flow.start] if flow.start in automatic else []) + sorted(automatic)
    for node_id in order:
        if node_id not in state:
            visit(node_id)
    return issues


def _check_option_intents(flow: Flow) -> list[Issue]:
    return [
        Issue(
            WARNING,
            node_id,
            f"option {option.key} ({option.label}) has no intents, so speech callers cannot choose it",
            "option_without_intents",
        )
        for node_id, node in flow.nodes.items()
        if isinstance(node, MenuNode)
        for option in node.options.values()
        if not option.intents
    ]


def _variables_set_by(node: Node) -> set[str]:
    if isinstance(node, CollectNode):
        return {node.variable}
    if isinstance(node, IdentifyNode):
        return set(IDENTIFY_VARIABLES)
    if isinstance(node, ActionNode):
        return set(ACTION_OUTPUTS.get(node.name, ()))
    return set()


def _check_prompt_variables(flow: Flow) -> list[Issue]:
    """Warn when a prompt reads a variable that no node on any path to it sets.

    The check is reachability based: a variable counts as available if some
    ancestor of the node can set it, even if not every path does. The engine
    renders a missing variable as ``?`` at run time, so this is a warning.
    """
    predecessors: dict[str, set[str]] = {node_id: set() for node_id in flow.nodes}
    for node_id, node in flow.nodes.items():
        for target in node.targets():
            if target in predecessors:
                predecessors[target].add(node_id)
    issues = []
    for node_id, node in flow.nodes.items():
        used = {name for prompt in node.prompts() for name in VARIABLE_PATTERN.findall(prompt)}
        if not used:
            continue
        available = set(ALWAYS_SET_VARIABLES)
        seen: set[str] = set()
        queue = deque(predecessors[node_id])
        while queue:
            ancestor = queue.popleft()
            if ancestor in seen:
                continue
            seen.add(ancestor)
            available |= _variables_set_by(flow.nodes[ancestor])
            queue.extend(predecessors[ancestor])
        for name in sorted(used - available):
            issues.append(
                Issue(
                    WARNING,
                    node_id,
                    f"prompt uses {{{name}}} but no upstream node sets it",
                    "unset_prompt_variable",
                )
            )
    return issues


def _check_terminal(flow: Flow) -> list[Issue]:
    if any(node.type in TERMINAL_NODE_TYPES for node in flow.nodes.values()):
        return []
    return [Issue(ERROR, None, "flow has no transfer or hangup node", "no_terminal_node")]
