"""CLI exit codes and the demo gate."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import AVERY_ANI, FLOWS_DIR, REPO_ROOT

from ivr import analytics, cli
from ivr.cli import demo_checks, format_transcript, main
from ivr.engine import Dtmf, run_script


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "ivr", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )


def test_validate_exit_codes_and_output():
    clean = run_cli("validate", "flows/care_v1.json", "flows/care_v2.json")
    assert clean.returncode == 0
    assert "flows/care_v2.json: 0 error(s), 0 warning(s)" in clean.stdout
    broken = run_cli("validate", "flows/broken_example.json")
    assert broken.returncode == 1
    assert "unknown_queue" in broken.stdout and "input_free_cycle" in broken.stdout
    missing = run_cli("validate", "flows/does_not_exist.json")
    assert missing.returncode == 1 and "cannot load flow" in missing.stderr


def test_run_prints_a_transcript_or_json(capsys):
    assert main(["run", str(FLOWS_DIR / "care_v2.json"), "--ani", AVERY_ANI, "--inputs", "dtmf:1,dtmf:3"]) == 0
    out = capsys.readouterr().out
    assert "IVR: Welcome back, Avery." in out
    assert "caller: dtmf 1 -> matched 1" in out
    assert "outcome: contained, handle time" in out
    assert main(["run", str(FLOWS_DIR / "care_v2.json"), "--ani", AVERY_ANI, "--inputs", "dtmf:0", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["outcome"] == "transferred" and data["transfer_queue"] == "general"
    with pytest.raises(SystemExit):
        main(["run", str(FLOWS_DIR / "care_v2.json"), "--inputs", "jump:3"])


def test_transcript_covers_every_event_kind(care_v2):
    result = run_script(care_v2, [Dtmf("1"), Dtmf("9"), Dtmf("3")], ani=AVERY_ANI)
    text = format_transcript(result)
    assert "identify: caller found" in text
    assert "action balance_inquiry: success (balance returned)" in text
    assert "invalid input (attempt 1, no_match)" in text and "retry 1, 0 left" in text
    assert "IVR ended the call (contained)" in text
    fallback = run_script(care_v2, [Dtmf("9"), Dtmf("9"), Dtmf("9")], ani=AVERY_ANI)
    fallback_text = format_transcript(fallback)
    assert "max retries reached after 3 attempts" in fallback_text
    assert "transferred to general (priority 4, reason caller_request_or_fallback)" in fallback_text


def test_test_command_exit_codes(tmp_path: Path, capsys):
    assert main(["test", str(FLOWS_DIR / "tests")]) == 0
    assert "scripted cases passed" in capsys.readouterr().out
    failing = {
        "name": "deliberately wrong",
        "flow": str(FLOWS_DIR / "care_v2.json"),
        "ani": AVERY_ANI,
        "inputs": ["dtmf:0"],
        "expect": {"outcome": "contained"},
    }
    (tmp_path / "fail.json").write_text(json.dumps(failing))
    assert main(["test", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "FAIL  deliberately wrong" in out
    assert "outcome: expected 'contained', saw 'transferred' (final node 'transfer_general')" in out
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(SystemExit):
        main(["test", str(empty)])


def test_simulate_then_report_one_and_two_files(tmp_path: Path, capsys):
    out = tmp_path / "out"
    assert main(["simulate", str(FLOWS_DIR / "care_v1.json"), "--calls", "50", "--seed", "3", "--out", str(out)]) == 0
    assert main(["simulate", str(FLOWS_DIR / "care_v2.json"), "--calls", "50", "--seed", "3", "--out", str(out)]) == 0
    capsys.readouterr()
    v1_file, v2_file = out / "care_v1.0_results.json", out / "care_v2.0_results.json"
    assert v1_file.exists() and v2_file.exists()
    assert len(json.loads(v1_file.read_text())["results"]) == 50
    assert main(["report", str(v1_file)]) == 0
    assert capsys.readouterr().out.startswith("care 1.0: 50 calls")
    assert main(["report", str(v1_file), str(v2_file), "--out", str(out)]) == 0
    text = capsys.readouterr().out
    assert "care 2.0 versus care 1.0:" in text
    report = json.loads((out / "report.json").read_text())
    assert report["a"]["calls"] == 50 and "containment" in report["rates"]
    assert (out / "summary.txt").read_text().startswith("care 1.0: 50 calls")
    with pytest.raises(SystemExit):
        main(["report", str(v1_file), str(v2_file), str(v1_file)])
    with pytest.raises(SystemExit):
        main(["report", str(tmp_path / "nothing.json")])


def test_demo_check_passes_on_a_small_run(tmp_path: Path, capsys, monkeypatch):
    monkeypatch.chdir(REPO_ROOT)
    assert main(["demo", "--check", "--calls", "400", "--seed", "7", "--out", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "all_passed: true" in out and "v2_containment_higher_p_below_0_01: true" in out
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["checks"]["all_passed"] is True
    assert report["scripted_cases"]["failed"] == 0 and report["validation_errors"] == 0
    assert (tmp_path / "summary.txt").exists()


def fake_report(v1_contained: int, v2_contained: int, v1_abandoned: int, v2_abandoned: int, errors: int = 0):
    """A compare() report built from counts, so the gate can be tested in isolation."""
    n = 1000
    rows = {
        "containment": (v1_contained, v2_contained),
        "abandonment": (v1_abandoned, v2_abandoned),
        "transfer": (n - v1_contained - v1_abandoned, n - v2_contained - v2_abandoned),
    }
    rates = {}
    for name, (a, b) in rows.items():
        z, p_value = analytics.two_proportion_z_test(a, n, b, n)
        rates[name] = {"difference": b / n - a / n, "z": z, "p_value": p_value}
    return {
        "a": {"rates": {"error": 0.0}},
        "b": {"rates": {"error": errors / n}},
        "rates": rates,
    }


def test_demo_gate_logic_with_synthetic_reports():
    good = demo_checks(fake_report(100, 500, 100, 90), validation_errors=0, failed_cases=0)
    assert good["all_passed"] is True
    assert demo_checks(fake_report(100, 500, 100, 90), 1, 0)["no_validation_errors"] is False
    assert demo_checks(fake_report(100, 500, 100, 90), 0, 2)["all_scripted_cases_pass"] is False
    not_significant = demo_checks(fake_report(600, 610, 100, 100), 0, 0)
    assert not_significant["v2_containment_higher_p_below_0_01"] is False
    assert not_significant["all_passed"] is False
    worse_abandonment = demo_checks(fake_report(100, 500, 100, 120), 0, 0)
    assert worse_abandonment["v2_abandonment_not_worse_by_more_than_1_point"] is False
    within_tolerance = demo_checks(fake_report(100, 500, 100, 110), 0, 0)
    assert within_tolerance["v2_abandonment_not_worse_by_more_than_1_point"] is True
    with_errors = demo_checks(fake_report(100, 500, 100, 90, errors=1), 0, 0)
    assert with_errors["zero_error_outcomes"] is False and with_errors["all_passed"] is False
    lower = demo_checks(fake_report(500, 100, 100, 90), 0, 0)
    assert lower["v2_containment_higher_p_below_0_01"] is False


def test_parser_requires_a_command():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([])
