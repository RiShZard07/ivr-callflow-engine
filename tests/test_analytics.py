"""Call-data metrics against hand-computed values."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ivr.analytics import (
    compare,
    format_comparison,
    format_p_value,
    format_summary,
    percentile,
    summarize,
    two_proportion_z_test,
    wilson_interval,
    write_json,
)
from ivr.engine import CallResult, Event


def fake_call(outcome: str, seconds: float, nodes: list[str], events: list[Event] | None = None) -> CallResult:
    base = [Event("node_entered", 0.0, node, {"type": "menu" if node == "menu" else "play"}) for node in nodes]
    return CallResult(
        outcome=outcome,
        events=base + (events or []),
        handle_time_seconds=seconds,
        final_node=nodes[-1],
        variables={},
        transfer_queue="billing" if outcome == "transferred" else None,
        nodes_visited=nodes,
    )


def sample_results() -> list[CallResult]:
    return [
        fake_call("contained", 30.0, ["start", "menu", "done"], [
            Event("input_received", 1.0, "menu", {"kind": "dtmf", "value": "9", "matched": None}),
            Event("input_invalid", 1.0, "menu", {"reason": "no_match", "attempt": 1}),
            Event("retry", 1.0, "menu", {"attempt": 1, "retries_left": 1}),
            Event("input_received", 2.0, "menu", {"kind": "dtmf", "value": "1", "matched": "1"}),
            Event("action_called", 3.0, "done", {"name": "balance_inquiry", "success": True, "message": ""}),
        ]),
        fake_call("contained", 50.0, ["start", "menu", "done"], [
            Event("input_received", 2.0, "menu", {"kind": "speech", "value": "x", "matched": "1"}),
            Event("action_called", 3.0, "done", {"name": "balance_inquiry", "success": False, "message": ""}),
            Event("identify_result", 0.5, "start", {"found": True}),
        ]),
        fake_call("transferred", 20.0, ["start", "menu", "xfer"], [
            Event("input_received", 2.0, "menu", {"kind": "timeout"}),
            Event("input_invalid", 2.0, "menu", {"reason": "timeout", "attempt": 1}),
            Event("retry", 2.0, "menu", {"attempt": 1, "retries_left": 1}),
            Event("input_received", 3.0, "menu", {"kind": "dtmf", "value": "2", "matched": "2"}),
            Event("transferred", 4.0, "xfer", {"queue": "billing", "reason": "billing_request", "priority": 3}),
            Event("identify_result", 0.5, "start", {"found": False}),
        ]),
        fake_call("abandoned", 10.0, ["start", "menu"]),
        fake_call("error", 5.0, ["start"]),
    ]


def test_rates_use_handled_calls_and_errors_use_all_calls():
    summary = summarize(sample_results(), "sample")
    assert summary["calls"] == 5 and summary["handled"] == 4
    assert summary["outcomes"] == {"contained": 2, "transferred": 1, "abandoned": 1, "error": 1}
    assert summary["rates"]["containment"] == pytest.approx(2 / 4)
    assert summary["rates"]["transfer"] == pytest.approx(1 / 4)
    assert summary["rates"]["abandonment"] == pytest.approx(1 / 4)
    assert summary["rates"]["error"] == pytest.approx(1 / 5)
    assert summary["handle_time_seconds"]["average"] == pytest.approx(115 / 5)
    assert summary["handle_time_seconds"]["p95"] == pytest.approx(46.0)  # interpolated between 30 and 50


def test_funnel_menu_rates_retries_transfers_actions_and_identify():
    summary = summarize(sample_results())
    assert summary["funnel"]["menu"] == {
        "entries": 4,
        "exits_by_outcome": {"abandoned": 1, "contained": 2, "transferred": 1},
    }
    assert summary["funnel"]["start"]["entries"] == 5
    assert list(summary["funnel"])[0] == "start"  # sorted by entries, largest first
    assert summary["menu_invalid_input_rate"] == {"menu": pytest.approx(2 / 5)}
    assert summary["retries_per_call"] == pytest.approx(2 / 5)
    assert summary["transfer_reasons_by_queue"] == {"billing": {"billing_request": 1}}
    assert summary["action_success_rates"] == {"balance_inquiry": {"calls": 2, "successes": 1, "rate": 0.5}}
    assert summary["identify_hit_rate"] == pytest.approx(0.5)
    assert summary["final_nodes"] == {"done": 2, "xfer": 1, "menu": 1, "start": 1}


def test_empty_input_does_not_divide_by_zero():
    summary = summarize([])
    assert summary["rates"] == {"containment": 0.0, "transfer": 0.0, "abandonment": 0.0, "error": 0.0}
    assert summary["handle_time_seconds"] == {"average": 0.0, "p95": 0.0}


def test_two_proportion_z_test_matches_a_hand_computed_value():
    z, p_value = two_proportion_z_test(600, 1000, 650, 1000)
    assert z == pytest.approx(2.3094, abs=1e-4)
    assert p_value == pytest.approx(0.02092, abs=1e-5)
    z_back, p_back = two_proportion_z_test(650, 1000, 600, 1000)
    assert z_back == pytest.approx(-z) and p_back == pytest.approx(p_value)
    assert two_proportion_z_test(0, 0, 5, 10) == (0.0, 1.0)
    assert two_proportion_z_test(10, 10, 10, 10) == (0.0, 1.0)
    assert two_proportion_z_test(500, 1000, 500, 1000) == (0.0, 1.0)


def test_wilson_interval_matches_a_hand_computed_value():
    low, high = wilson_interval(600, 1000)
    assert low == pytest.approx(0.5693, abs=1e-4)
    assert high == pytest.approx(0.6299, abs=1e-4)
    assert wilson_interval(0, 0) == (0.0, 0.0)
    zero_low, zero_high = wilson_interval(0, 20)
    assert zero_low == pytest.approx(0.0, abs=1e-12) and 0.0 < zero_high < 0.2
    full_low, full_high = wilson_interval(20, 20)
    assert 0.8 < full_low < 1.0 and full_high == pytest.approx(1.0, abs=1e-12)
    with pytest.raises(ValueError):
        wilson_interval(5, 4)


def test_percentile_interpolates_linearly():
    values = [10.0, 20.0, 30.0, 40.0]
    assert percentile(values, 0) == 10.0
    assert percentile(values, 100) == 40.0
    assert percentile(values, 50) == 25.0
    assert percentile(values, 95) == pytest.approx(38.5)
    assert percentile([7.0], 95) == 7.0
    assert percentile([], 95) == 0.0
    with pytest.raises(ValueError):
        percentile(values, 101)


def test_compare_reports_differences_in_the_b_minus_a_direction():
    a = [fake_call("contained", 30.0, ["s"])] * 6 + [fake_call("transferred", 30.0, ["s"])] * 4
    b = [fake_call("contained", 20.0, ["s"])] * 8 + [fake_call("abandoned", 20.0, ["s"])] * 2
    report = compare(a, b, "v1", "v2")
    containment = report["rates"]["containment"]
    assert containment["a"]["count"] == 6 and containment["b"]["count"] == 8
    assert containment["difference"] == pytest.approx(0.2)
    assert containment["z"] > 0 and 0 < containment["p_value"] < 1
    assert containment["a"]["wilson_95"][0] < 0.6 < containment["a"]["wilson_95"][1]
    assert report["rates"]["transfer"]["difference"] == pytest.approx(-0.4)
    assert report["rates"]["abandonment"]["difference"] == pytest.approx(0.2)
    assert report["handle_time_seconds"]["average_difference"] == pytest.approx(-10.0)
    assert report["a"]["label"] == "v1" and report["b"]["label"] == "v2"
    text = format_comparison(report)
    assert "v2 versus v1:" in text and "containment" in text and "handle time  average -10.0s" in text


def test_text_formatting_helpers():
    assert format_p_value(0.020921) == "p = 0.0209"
    assert format_p_value(0.0) == "p < 0.0001"
    assert format_p_value(0.00009) == "p < 0.0001"
    text = format_summary(summarize(sample_results(), "sample"))
    assert text.splitlines()[0] == "sample: 5 calls"
    assert "containment 50.0%" in text and "errors 20.0%" in text
    assert "actions: balance_inquiry 1/2" in text and "transfers by queue: billing 1" in text


def test_write_json_creates_parent_directories_and_serializes_tuples(tmp_path: Path):
    target = tmp_path / "nested" / "report.json"
    write_json(target, {"interval": (0.1, 0.2), "items": frozenset({"a"})})
    data = json.loads(target.read_text())
    assert data == {"interval": [0.1, 0.2], "items": ["a"]}
    with pytest.raises(TypeError):
        write_json(tmp_path / "bad.json", {"when": object()})
