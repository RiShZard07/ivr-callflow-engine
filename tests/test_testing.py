"""The scripted call-flow test runner."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import AVERY_ANI, FLOWS_DIR, UNKNOWN_ANI

from ivr.engine import Dtmf, Hangup, Speech, Timeout, run_script
from ivr.testing import (
    CaseError,
    Expectation,
    check_expectation,
    format_input,
    format_results,
    load_cases,
    parse_input,
    run_case,
    run_cases,
)


def test_parse_and_format_inputs_round_trip():
    texts = ["dtmf:4155550124#", "speech:pay my bill", "timeout", "hangup"]
    parsed = [parse_input(t) for t in texts]
    assert parsed == [Dtmf("4155550124#"), Speech("pay my bill"), Timeout(), Hangup()]
    assert [format_input(p) for p in parsed] == texts
    assert parse_input("DTMF:1") == Dtmf("1")


@pytest.mark.parametrize("text", ["dtmf:", "speech:", "timeout:5", "hangup:now", "shout:hi", ""])
def test_parse_input_rejects_malformed_strings(text):
    with pytest.raises(CaseError):
        parse_input(text)


def test_shipped_cases_load_cover_both_flows_and_all_pass():
    cases = load_cases(FLOWS_DIR / "tests")
    assert len(cases) >= 12
    assert {case.flow_path.name for case in cases} == {"care_v1.json", "care_v2.json"}
    assert all(case.flow_path.exists() for case in cases)
    results = run_cases(cases)
    assert all(item.passed for item in results), format_results(results)
    assert format_results(results).splitlines()[-1] == f"{len(cases)} of {len(cases)} scripted cases passed"


def test_failing_case_reports_each_mismatch_readably(care_v2):
    result = run_script(care_v2, [Dtmf("1"), Dtmf("3")], ani=AVERY_ANI)  # contained, balance path
    expect = Expectation(
        outcome="transferred",
        transfer_queue="billing",
        final_node="transfer_billing",
        prompts_contain=["Welcome back, Avery", "no such prompt"],
        nodes_visited_includes=["identify", "collect_account_billing"],
        max_handle_time_seconds=10,
    )
    failures = check_expectation(expect, result)
    assert len(failures) == 6
    assert failures[0] == "outcome: expected 'transferred', saw 'contained' (final node 'goodbye')"
    assert failures[1] == "transfer_queue: expected 'billing', saw None"
    assert failures[2].startswith("final_node: expected 'transfer_billing', saw 'goodbye'")
    assert failures[3].startswith("prompts_contain: 'no such prompt' not found in any prompt from #")
    assert "['collect_account_billing'] not visited" in failures[4]
    assert failures[5].startswith("handle time: expected at most 10.0s, saw")


def test_prompts_contain_must_appear_in_order(care_v2):
    result = run_script(care_v2, [Dtmf("1"), Dtmf("3")], ani=AVERY_ANI)
    in_order = Expectation(outcome="contained", prompts_contain=["Welcome back", "balance", "Goodbye"])
    assert check_expectation(in_order, result) == []
    out_of_order = Expectation(outcome="contained", prompts_contain=["Goodbye", "Welcome back"])
    failures = check_expectation(out_of_order, result)
    assert len(failures) == 1 and "'Welcome back' not found" in failures[0]


def test_case_files_resolve_flow_paths_relative_to_the_file(tmp_path: Path):
    (tmp_path / "flow.json").write_text((FLOWS_DIR / "care_v1.json").read_text())
    suite = tmp_path / "suite"
    suite.mkdir()
    case = {
        "name": "relative path",
        "flow": "../flow.json",
        "ani": UNKNOWN_ANI,
        "inputs": ["hangup"],
        "expect": {"outcome": "abandoned"},
    }
    (suite / "one.json").write_text(json.dumps(case))
    (suite / "two.json").write_text(json.dumps({"cases": [dict(case, name="second")]}))
    cases = load_cases(suite)
    assert [c.name for c in cases] == ["relative path", "second"]
    assert cases[0].flow_path == (tmp_path / "flow.json").resolve()
    assert cases[0].source.endswith("one.json")
    assert all(item.passed for item in run_cases(cases))
    assert load_cases(suite / "two.json")[0].name == "second"


@pytest.mark.parametrize(
    "raw, fragment",
    [
        ({"name": "x", "flow": "f", "ani": "1", "inputs": ["dtmf:1"]}, "missing required field 'expect'"),
        ({"name": "x", "flow": "f", "ani": "1", "inputs": "dtmf:1", "expect": {"outcome": "contained"}},
         "'inputs' must be a list"),
        ({"name": "x", "flow": "f", "ani": "1", "inputs": ["dtmf:1"], "expect": {"outcome": "stuck"}},
         "expect.outcome must be one of"),
        ({"name": "x", "flow": "f", "ani": "1", "inputs": ["dtmf:1"],
          "expect": {"outcome": "contained", "queue": "billing"}}, "unknown expect field"),
        ({"name": "x", "flow": "f", "ani": "1", "inputs": ["jump"], "expect": {"outcome": "contained"}},
         "cannot parse input 'jump'"),
        ({"name": "", "flow": "f", "ani": "1", "inputs": [], "expect": {"outcome": "contained"}},
         "'name' must be a non-empty string"),
    ],
)
def test_case_schema_errors(tmp_path: Path, raw, fragment):
    path = tmp_path / "case.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(CaseError, match=fragment):
        load_cases(path)
    (tmp_path / "bad.json").write_text("{nope")
    with pytest.raises(CaseError, match="invalid JSON"):
        load_cases(tmp_path / "bad.json")


def test_run_case_reports_an_unloadable_flow_as_a_failure(tmp_path: Path):
    path = tmp_path / "case.json"
    path.write_text(json.dumps({"name": "ghost", "flow": "missing.json", "ani": "1", "inputs": [],
                                "expect": {"outcome": "contained"}}))
    item = run_case(load_cases(path)[0])
    assert not item.passed and item.result is None
    assert item.failures[0].startswith("could not load flow")
    report = format_results([item])
    assert report.splitlines()[0] == "FAIL  ghost" and "0 of 1 scripted cases passed" in report
