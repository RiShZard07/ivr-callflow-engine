"""Call-data metrics and the two-version comparison.

Definitions (``n_handled`` is the number of calls that ended as contained,
transferred or abandoned; error-outcome calls are counted separately):

- containment rate  = contained   / n_handled
- transfer rate     = transferred / n_handled
- abandonment rate  = abandoned   / n_handled
- error rate        = error       / n_total
- handle time       = simulated seconds from the first prompt to the end of
  the IVR part of the call; the average and the 95th percentile are reported
- invalid-input rate per menu = invalid attempts / inputs received at that menu
- retries per call  = retry events / n_total

``compare`` puts two versions side by side with a two-proportion z-test on
each rate (two-sided p-value from the normal CDF via ``math.erfc``) and a 95%
Wilson score interval for each rate.
"""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ivr.engine import CallResult

HANDLED_OUTCOMES = ("contained", "transferred", "abandoned")
Z_95 = 1.959963984540054


def percentile(values: Sequence[float], q: float) -> float:
    """Linear-interpolation percentile (``q`` in 0..100); 0.0 for no values."""
    if not values:
        return 0.0
    if not 0 <= q <= 100:
        raise ValueError("q must be between 0 and 100")
    ordered = sorted(values)
    position = (len(ordered) - 1) * q / 100.0
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def wilson_interval(successes: int, trials: int, z: float = Z_95) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion; (0, 0) when there are no trials."""
    if trials <= 0:
        return (0.0, 0.0)
    if not 0 <= successes <= trials:
        raise ValueError("successes must be between 0 and trials")
    p_hat = successes / trials
    z2 = z * z
    denominator = 1 + z2 / trials
    center = (p_hat + z2 / (2 * trials)) / denominator
    half = z * math.sqrt(p_hat * (1 - p_hat) / trials + z2 / (4 * trials * trials)) / denominator
    return (max(0.0, center - half), min(1.0, center + half))


def two_proportion_z_test(x1: int, n1: int, x2: int, n2: int) -> tuple[float, float]:
    """Pooled two-proportion z-test of ``x2/n2`` against ``x1/n1``.

    Returns ``(z, p_value)`` where ``z`` is positive when the second
    proportion is larger and ``p_value`` is two-sided. With no trials or a
    pooled proportion of exactly 0 or 1 the test is undefined and (0, 1) is
    returned.
    """
    if n1 <= 0 or n2 <= 0:
        return (0.0, 1.0)
    pooled = (x1 + x2) / (n1 + n2)
    standard_error = math.sqrt(pooled * (1 - pooled) * (1 / n1 + 1 / n2))
    if standard_error == 0:
        return (0.0, 1.0)
    z = (x2 / n2 - x1 / n1) / standard_error
    p_value = math.erfc(abs(z) / math.sqrt(2))
    return (z, p_value)


def _rate(count: int, total: int) -> float:
    return count / total if total else 0.0


def summarize(results: Sequence[CallResult], label: str = "") -> dict[str, Any]:
    """Compute every metric for one set of calls as a JSON-ready dictionary."""
    outcomes = Counter(result.outcome for result in results)
    n_total = len(results)
    n_handled = sum(outcomes[o] for o in HANDLED_OUTCOMES)
    handle_times = [result.handle_time_seconds for result in results]

    funnel: dict[str, dict[str, Any]] = {}
    menu_inputs: Counter[str] = Counter()
    menu_invalid: Counter[str] = Counter()
    retries = 0
    transfer_reasons: dict[str, Counter[str]] = defaultdict(Counter)
    action_calls: Counter[str] = Counter()
    action_successes: Counter[str] = Counter()
    identify_lookups = 0
    identify_hits = 0

    for result in results:
        for node_id in dict.fromkeys(result.nodes_visited):
            entry = funnel.setdefault(node_id, {"entries": 0, "exits_by_outcome": Counter()})
            entry["entries"] += 1
            entry["exits_by_outcome"][result.outcome] += 1
        node_types = {
            event.node_id: event.data.get("type")
            for event in result.events
            if event.kind == "node_entered"
        }
        for event in result.events:
            if event.kind == "input_received":
                if node_types.get(event.node_id) == "menu":
                    menu_inputs[event.node_id or "?"] += 1
            elif event.kind == "input_invalid":
                if node_types.get(event.node_id) == "menu":
                    menu_invalid[event.node_id or "?"] += 1
            elif event.kind == "retry":
                retries += 1
            elif event.kind == "transferred":
                transfer_reasons[event.data["queue"]][event.data["reason"]] += 1
            elif event.kind == "action_called":
                action_calls[event.data["name"]] += 1
                if event.data["success"]:
                    action_successes[event.data["name"]] += 1
            elif event.kind == "identify_result":
                identify_lookups += 1
                identify_hits += int(bool(event.data["found"]))

    final_nodes = Counter(result.final_node or "?" for result in results)
    return {
        "label": label,
        "calls": n_total,
        "handled": n_handled,
        "outcomes": {outcome: outcomes.get(outcome, 0) for outcome in (*HANDLED_OUTCOMES, "error")},
        "rates": {
            "containment": _rate(outcomes["contained"], n_handled),
            "transfer": _rate(outcomes["transferred"], n_handled),
            "abandonment": _rate(outcomes["abandoned"], n_handled),
            "error": _rate(outcomes["error"], n_total),
        },
        "handle_time_seconds": {
            "average": sum(handle_times) / n_total if n_total else 0.0,
            "p95": percentile(handle_times, 95),
        },
        "retries_per_call": _rate(retries, n_total),
        "menu_invalid_input_rate": {
            node_id: _rate(menu_invalid[node_id], menu_inputs[node_id])
            for node_id in sorted(menu_inputs)
        },
        "funnel": {
            node_id: {
                "entries": data["entries"],
                "exits_by_outcome": dict(sorted(data["exits_by_outcome"].items())),
            }
            for node_id, data in sorted(funnel.items(), key=lambda item: -item[1]["entries"])
        },
        "final_nodes": dict(final_nodes.most_common()),
        "transfer_reasons_by_queue": {
            queue: dict(reasons.most_common()) for queue, reasons in sorted(transfer_reasons.items())
        },
        "action_success_rates": {
            name: {
                "calls": action_calls[name],
                "successes": action_successes[name],
                "rate": _rate(action_successes[name], action_calls[name]),
            }
            for name in sorted(action_calls)
        },
        "identify_hit_rate": _rate(identify_hits, identify_lookups),
    }


def compare(
    a_results: Sequence[CallResult],
    b_results: Sequence[CallResult],
    label_a: str = "A",
    label_b: str = "B",
) -> dict[str, Any]:
    """Compare two flow versions: per-rate differences with z-tests and Wilson intervals."""
    summary_a = summarize(a_results, label_a)
    summary_b = summarize(b_results, label_b)
    rates: dict[str, Any] = {}
    for name in ("containment", "transfer", "abandonment"):
        outcome = {"containment": "contained", "transfer": "transferred", "abandonment": "abandoned"}[name]
        xa, na = summary_a["outcomes"][outcome], summary_a["handled"]
        xb, nb = summary_b["outcomes"][outcome], summary_b["handled"]
        z, p_value = two_proportion_z_test(xa, na, xb, nb)
        rates[name] = {
            "a": {"count": xa, "n": na, "rate": _rate(xa, na), "wilson_95": wilson_interval(xa, na)},
            "b": {"count": xb, "n": nb, "rate": _rate(xb, nb), "wilson_95": wilson_interval(xb, nb)},
            "difference": _rate(xb, nb) - _rate(xa, na),
            "z": z,
            "p_value": p_value,
        }
    return {
        "a": summary_a,
        "b": summary_b,
        "rates": rates,
        "handle_time_seconds": {
            "average_difference": (
                summary_b["handle_time_seconds"]["average"] - summary_a["handle_time_seconds"]["average"]
            ),
            "p95_difference": summary_b["handle_time_seconds"]["p95"] - summary_a["handle_time_seconds"]["p95"],
        },
    }


def format_summary(summary: dict[str, Any]) -> str:
    """Plain-text block for one flow version."""
    rates = summary["rates"]
    times = summary["handle_time_seconds"]
    lines = [
        f"{summary['label'] or 'flow'}: {summary['calls']} calls",
        f"  containment {rates['containment']:.1%}   transfer {rates['transfer']:.1%}   "
        f"abandonment {rates['abandonment']:.1%}   errors {rates['error']:.1%}",
        f"  handle time: average {times['average']:.1f}s, p95 {times['p95']:.1f}s, "
        f"retries per call {summary['retries_per_call']:.2f}",
    ]
    if summary["action_success_rates"]:
        parts = [
            f"{name} {data['successes']}/{data['calls']}"
            for name, data in summary["action_success_rates"].items()
        ]
        lines.append("  actions: " + ", ".join(parts))
    if summary["transfer_reasons_by_queue"]:
        parts = [
            f"{queue} {sum(reasons.values())}" for queue, reasons in summary["transfer_reasons_by_queue"].items()
        ]
        lines.append("  transfers by queue: " + ", ".join(parts))
    return "\n".join(lines)


def format_p_value(p_value: float) -> str:
    """``p = 0.0209`` for ordinary values, ``p < 0.0001`` for very small ones."""
    if p_value < 0.0001:
        return "p < 0.0001"
    return f"p = {p_value:.4f}"


def format_comparison(report: dict[str, Any]) -> str:
    """Plain-text comparison of two versions."""
    lines = [format_summary(report["a"]), format_summary(report["b"]), ""]
    label_a = report["a"]["label"] or "A"
    label_b = report["b"]["label"] or "B"
    lines.append(f"{label_b} versus {label_a}:")
    for name, data in report["rates"].items():
        lo_a, hi_a = data["a"]["wilson_95"]
        lo_b, hi_b = data["b"]["wilson_95"]
        lines.append(
            f"  {name:<12} {data['a']['rate']:.1%} [{lo_a:.1%}, {hi_a:.1%}]  ->  "
            f"{data['b']['rate']:.1%} [{lo_b:.1%}, {hi_b:.1%}]   "
            f"difference {data['difference']:+.1%}, z {data['z']:+.2f}, {format_p_value(data['p_value'])}"
        )
    times = report["handle_time_seconds"]
    lines.append(
        f"  handle time  average {times['average_difference']:+.1f}s, p95 {times['p95_difference']:+.1f}s"
    )
    return "\n".join(lines)


def write_json(path: str | Path, data: dict[str, Any]) -> None:
    """Write a report dictionary as indented JSON."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, indent=2, default=_json_default) + "\n", encoding="utf-8")


def _json_default(value: Any) -> Any:
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    raise TypeError(f"cannot serialize {type(value).__name__}")
