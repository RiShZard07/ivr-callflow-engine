"""Scripted call-flow test cases.

A case is a JSON object that names a flow, a caller number, the inputs the
caller gives, and what the finished call must look like::

    {
      "name": "known caller hears balance",
      "flow": "../care_v2.json",
      "ani": "5550123456",
      "inputs": ["dtmf:1", "dtmf:3"],
      "expect": {
        "outcome": "contained",
        "prompts_contain": ["Welcome back, Avery", "Your current balance is 87.50"],
        "nodes_visited_includes": ["identify", "balance"],
        "max_handle_time_seconds": 60
      }
    }

Input strings are ``dtmf:<keys>``, ``speech:<utterance>``, ``timeout`` and
``hangup``. A file holds one case or ``{"cases": [...]}``. Flow paths are
resolved relative to the file that names them. These cases are the release
regression suite: CI runs every file under ``flows/tests``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ivr.engine import OUTCOMES, CallerInput, CallResult, Dtmf, Hangup, Speech, Timeout, run_script
from ivr.flow import Flow, FlowError, load_flow


class CaseError(ValueError):
    """Raised when a test case file has the wrong shape."""


@dataclass(frozen=True)
class Expectation:
    """What a finished call must satisfy."""

    outcome: str
    transfer_queue: str | None = None
    final_node: str | None = None
    prompts_contain: tuple[str, ...] = ()
    nodes_visited_includes: tuple[str, ...] = ()
    max_handle_time_seconds: float | None = None


@dataclass(frozen=True)
class ScriptedCase:
    """One scripted call."""

    name: str
    flow_path: Path
    ani: str
    inputs: tuple[CallerInput, ...]
    expect: Expectation
    source: str = ""


@dataclass
class CaseResult:
    """Pass/fail verdict for one case with the mismatches spelled out."""

    case: ScriptedCase
    passed: bool
    failures: list[str] = field(default_factory=list)
    result: CallResult | None = None


def parse_input(text: str) -> CallerInput:
    """Turn ``dtmf:1``, ``speech:pay my bill``, ``timeout`` or ``hangup`` into an input."""
    kind, _, value = text.partition(":")
    kind = kind.strip().lower()
    if kind == "dtmf":
        if not value:
            raise CaseError("dtmf input needs at least one key, for example 'dtmf:1'")
        return Dtmf(value.strip())
    if kind == "speech":
        if not value.strip():
            raise CaseError("speech input needs an utterance, for example 'speech:billing'")
        return Speech(value.strip())
    if kind == "timeout" and not value:
        return Timeout()
    if kind == "hangup" and not value:
        return Hangup()
    raise CaseError(f"cannot parse input {text!r}")


def format_input(caller_input: CallerInput) -> str:
    """Inverse of ``parse_input`` for readable reports."""
    if isinstance(caller_input, Dtmf):
        return f"dtmf:{caller_input.digits}"
    if isinstance(caller_input, Speech):
        return f"speech:{caller_input.utterance}"
    if isinstance(caller_input, Timeout):
        return "timeout"
    return "hangup"


def _expectation_from_dict(raw: Any, where: str) -> Expectation:
    if not isinstance(raw, Mapping):
        raise CaseError(f"{where}: 'expect' must be an object")
    allowed = {
        "outcome", "transfer_queue", "final_node", "prompts_contain",
        "nodes_visited_includes", "max_handle_time_seconds",
    }
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise CaseError(f"{where}: unknown expect field(s) {', '.join(unknown)}")
    outcome = raw.get("outcome")
    if outcome not in OUTCOMES:
        raise CaseError(f"{where}: expect.outcome must be one of {OUTCOMES}")
    for key in ("prompts_contain", "nodes_visited_includes"):
        values = raw.get(key, [])
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            raise CaseError(f"{where}: expect.{key} must be a list of strings")
    max_time = raw.get("max_handle_time_seconds")
    if max_time is not None and (isinstance(max_time, bool) or not isinstance(max_time, (int, float))):
        raise CaseError(f"{where}: expect.max_handle_time_seconds must be a number")
    return Expectation(
        outcome=outcome,
        transfer_queue=raw.get("transfer_queue"),
        final_node=raw.get("final_node"),
        prompts_contain=tuple(raw.get("prompts_contain", [])),
        nodes_visited_includes=tuple(raw.get("nodes_visited_includes", [])),
        max_handle_time_seconds=None if max_time is None else float(max_time),
    )


def case_from_dict(raw: Any, base_dir: Path, source: str = "") -> ScriptedCase:
    """Build a case from parsed JSON; ``base_dir`` anchors the flow path."""
    if not isinstance(raw, Mapping):
        raise CaseError(f"{source or 'case'}: expected an object")
    name = raw.get("name")
    where = f"case {name!r}" if name else (source or "case")
    for key in ("name", "flow", "ani", "inputs", "expect"):
        if key not in raw:
            raise CaseError(f"{where}: missing required field '{key}'")
    if not isinstance(name, str) or not name.strip():
        raise CaseError(f"{where}: 'name' must be a non-empty string")
    if not isinstance(raw["flow"], str) or not isinstance(raw["ani"], str):
        raise CaseError(f"{where}: 'flow' and 'ani' must be strings")
    if not isinstance(raw["inputs"], list) or not all(isinstance(v, str) for v in raw["inputs"]):
        raise CaseError(f"{where}: 'inputs' must be a list of strings")
    try:
        inputs = tuple(parse_input(text) for text in raw["inputs"])
    except CaseError as exc:
        raise CaseError(f"{where}: {exc}") from exc
    return ScriptedCase(
        name=name,
        flow_path=(base_dir / raw["flow"]).resolve(),
        ani=raw["ani"],
        inputs=inputs,
        expect=_expectation_from_dict(raw["expect"], where),
        source=source,
    )


def load_cases(path: str | Path) -> list[ScriptedCase]:
    """Load every case from a JSON file, or from all ``*.json`` files in a directory."""
    target = Path(path)
    if target.is_dir():
        cases: list[ScriptedCase] = []
        for file in sorted(target.glob("*.json")):
            cases.extend(load_cases(file))
        return cases
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CaseError(f"{target}: invalid JSON: {exc}") from exc
    if isinstance(data, Mapping) and "cases" in data:
        raw_cases = data["cases"]
        if not isinstance(raw_cases, list):
            raise CaseError(f"{target}: 'cases' must be a list")
    else:
        raw_cases = [data]
    return [case_from_dict(raw, target.parent, source=str(target)) for raw in raw_cases]


def check_expectation(expect: Expectation, result: CallResult) -> list[str]:
    """Compare a finished call with an expectation and describe every mismatch."""
    failures: list[str] = []
    if result.outcome != expect.outcome:
        failures.append(
            f"outcome: expected {expect.outcome!r}, saw {result.outcome!r} "
            f"(final node {result.final_node!r})"
        )
    if expect.transfer_queue is not None and result.transfer_queue != expect.transfer_queue:
        failures.append(
            f"transfer_queue: expected {expect.transfer_queue!r}, saw {result.transfer_queue!r}"
        )
    if expect.final_node is not None and result.final_node != expect.final_node:
        failures.append(f"final_node: expected {expect.final_node!r}, saw {result.final_node!r}")
    prompts = result.prompts()
    position = 0
    for needle in expect.prompts_contain:
        index = next((i for i in range(position, len(prompts)) if needle in prompts[i]), None)
        if index is None:
            failures.append(
                f"prompts_contain: {needle!r} not found in any prompt from #{position + 1} on; "
                f"prompts played: {prompts}"
            )
            break
        position = index
    missing_nodes = [n for n in expect.nodes_visited_includes if n not in result.nodes_visited]
    if missing_nodes:
        failures.append(
            f"nodes_visited_includes: {missing_nodes} not visited; path was {result.nodes_visited}"
        )
    if (
        expect.max_handle_time_seconds is not None
        and result.handle_time_seconds > expect.max_handle_time_seconds
    ):
        failures.append(
            f"handle time: expected at most {expect.max_handle_time_seconds:.1f}s, "
            f"saw {result.handle_time_seconds:.1f}s"
        )
    return failures


def run_case(case: ScriptedCase, flows: dict[Path, Flow] | None = None) -> CaseResult:
    """Run one case; ``flows`` caches loaded flows across cases."""
    cache = flows if flows is not None else {}
    try:
        if case.flow_path not in cache:
            cache[case.flow_path] = load_flow(case.flow_path)
    except (OSError, FlowError) as exc:
        return CaseResult(case, False, [f"could not load flow {case.flow_path}: {exc}"])
    result = run_script(cache[case.flow_path], list(case.inputs), ani=case.ani)
    failures = check_expectation(case.expect, result)
    return CaseResult(case, not failures, failures, result)


def run_cases(cases: list[ScriptedCase]) -> list[CaseResult]:
    """Run many cases, sharing one flow cache."""
    flows: dict[Path, Flow] = {}
    return [run_case(case, flows) for case in cases]


def format_results(results: list[CaseResult]) -> str:
    """A pytest-like report: one line per case and details for each failure."""
    lines = []
    for item in results:
        mark = "PASS" if item.passed else "FAIL"
        lines.append(f"{mark}  {item.case.name}")
        if not item.passed:
            lines.append(f"      inputs: {[format_input(i) for i in item.case.inputs]}")
            for failure in item.failures:
                lines.append(f"      - {failure}")
    passed = sum(1 for item in results if item.passed)
    lines.append(f"{passed} of {len(results)} scripted cases passed")
    return "\n".join(lines)
