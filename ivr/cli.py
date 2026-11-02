"""Command-line interface: ``python -m ivr <command>``.

Commands: ``validate``, ``run``, ``test``, ``simulate``, ``report`` and
``demo``. Every command exits 0 on success and 1 when something it checked
failed, so the same commands can gate a CI pipeline.
"""

from __future__ import annotations

import argparse
import json
import textwrap
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ivr import analytics, simulate, testing, validate
from ivr.engine import CallResult, Event, run_script
from ivr.flow import Flow, FlowError, load_flow

DEMO_FLOWS = ("flows/care_v1.json", "flows/care_v2.json")
DEMO_CASES = "flows/tests"
DEMO_CALLS = 3000
DEMO_SEED = 7
DEMO_OUT = "output"
CONTAINMENT_P_THRESHOLD = 0.01
ABANDONMENT_TOLERANCE = 0.01


def format_event(event: Event) -> str:
    """One transcript line for an event."""
    stamp = f"[{event.time:6.1f}s]"
    data = event.data
    if event.kind == "node_entered":
        return f"{stamp} -> {event.node_id} ({data.get('type')})"
    if event.kind == "prompt_played":
        return f"{stamp} IVR: {data['text']}"
    if event.kind == "prompt_variable_missing":
        return f"{stamp} warning: prompt variable {{{data['variable']}}} is not set"
    if event.kind == "input_received":
        detail = f" {data['value']}" if "value" in data else ""
        matched = f" -> matched {data['matched']}" if data.get("matched") else ""
        return f"{stamp} caller: {data['kind']}{detail}{matched}"
    if event.kind == "input_invalid":
        extra = f", {data['detail']}" if "detail" in data else ""
        return f"{stamp} invalid input (attempt {data['attempt']}, {data['reason']}{extra})"
    if event.kind == "retry":
        return f"{stamp} retry {data['attempt']}, {data['retries_left']} left"
    if event.kind == "max_retries_reached":
        return f"{stamp} max retries reached after {data['attempts']} attempts"
    if event.kind == "variable_set":
        return f"{stamp} set {data['name']} = {data['value']!r}"
    if event.kind == "identify_result":
        return f"{stamp} identify: caller {'found' if data['found'] else 'not found'}"
    if event.kind == "action_called":
        verdict = "success" if data["success"] else "failure"
        return f"{stamp} action {data['name']}: {verdict} ({data['message']})"
    if event.kind == "transferred":
        return (
            f"{stamp} transferred to {data['queue']} "
            f"(priority {data['priority']}, reason {data['reason']})"
        )
    if event.kind == "hangup":
        return f"{stamp} IVR ended the call (contained)"
    if event.kind == "abandoned":
        return f"{stamp} caller hung up (abandoned)"
    return f"{stamp} {event.kind}: {data.get('message', '')}"


def format_transcript(result: CallResult) -> str:
    """Full readable transcript of one call."""
    lines = [format_event(event) for event in result.events]
    lines.append("")
    lines.append(
        f"outcome: {result.outcome}, handle time {result.handle_time_seconds:.1f}s, "
        f"path: {' > '.join(result.nodes_visited)}"
    )
    return "\n".join(lines)


def _load(path: str) -> Flow:
    try:
        return load_flow(path)
    except (OSError, FlowError) as exc:
        raise SystemExit(f"error: cannot load flow {path}: {exc}") from exc


def cmd_validate(args: argparse.Namespace) -> int:
    """Validate one or more flows; exit 1 if any has errors."""
    failed = False
    for path in args.flows:
        issues = validate.validate(_load(path))
        print(f"{path}: {len(validate.errors(issues))} error(s), "
              f"{len(issues) - len(validate.errors(issues))} warning(s)")
        if issues:
            print(textwrap.indent(validate.format_issues(issues), "  "))
        failed = failed or validate.has_errors(issues)
    return 1 if failed else 0


def cmd_run(args: argparse.Namespace) -> int:
    """Run one scripted call and print the transcript."""
    flow = _load(args.flow)
    texts = [part.strip() for part in args.inputs.split(",") if part.strip()] if args.inputs else []
    try:
        inputs = [testing.parse_input(text) for text in texts]
    except testing.CaseError as exc:
        raise SystemExit(f"error: {exc}") from exc
    result = run_script(flow, inputs, ani=args.ani)
    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        print(format_transcript(result))
    return 0


def cmd_test(args: argparse.Namespace) -> int:
    """Run scripted call-flow cases; exit 1 on any failure."""
    try:
        cases = testing.load_cases(args.path)
    except (OSError, testing.CaseError) as exc:
        raise SystemExit(f"error: {exc}") from exc
    if not cases:
        raise SystemExit(f"error: no cases found under {args.path}")
    results = testing.run_cases(cases)
    print(testing.format_results(results))
    return 0 if all(item.passed for item in results) else 1


def _results_path(out_dir: Path, flow: Flow) -> Path:
    return out_dir / f"{flow.name}_v{flow.version}_results.json"


def cmd_simulate(args: argparse.Namespace) -> int:
    """Simulate calls through a flow and write the results."""
    flow = _load(args.flow)
    results = simulate.simulate(flow, args.calls, args.seed)
    summary = analytics.summarize(results, flow.label)
    print(analytics.format_summary(summary))
    if args.out:
        out_dir = Path(args.out)
        path = _results_path(out_dir, flow)
        analytics.write_json(path, {"flow": flow.label, "seed": args.seed,
                                    "results": [r.to_dict() for r in results]})
        print(f"wrote {path}")
    return 0


def _read_results(path: str) -> tuple[str, list[CallResult]]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data["flow"], [CallResult.from_dict(item) for item in data["results"]]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise SystemExit(f"error: cannot read results file {path}: {exc}") from exc


def cmd_report(args: argparse.Namespace) -> int:
    """Report metrics for one results file, or compare two."""
    label_a, results_a = _read_results(args.results[0])
    if len(args.results) == 1:
        report: dict[str, Any] = analytics.summarize(results_a, label_a)
        text = analytics.format_summary(report)
    else:
        label_b, results_b = _read_results(args.results[1])
        report = analytics.compare(results_a, results_b, label_a, label_b)
        text = analytics.format_comparison(report)
    print(text)
    if args.out:
        out_dir = Path(args.out)
        analytics.write_json(out_dir / "report.json", report)
        (out_dir / "summary.txt").write_text(text + "\n", encoding="utf-8")
        print(f"wrote {out_dir / 'report.json'} and {out_dir / 'summary.txt'}")
    return 0


def demo_checks(report: dict[str, Any], validation_errors: int, failed_cases: int) -> dict[str, bool]:
    """The release gate, as named boolean checks.

    ``report`` is the output of ``analytics.compare(v1, v2)``.
    """
    containment = report["rates"]["containment"]
    abandonment = report["rates"]["abandonment"]
    checks = {
        "no_validation_errors": validation_errors == 0,
        "all_scripted_cases_pass": failed_cases == 0,
        "v2_containment_higher_p_below_0_01": (
            containment["difference"] > 0 and containment["p_value"] < CONTAINMENT_P_THRESHOLD
        ),
        "v2_abandonment_not_worse_by_more_than_1_point": abandonment["difference"] <= ABANDONMENT_TOLERANCE,
        "zero_error_outcomes": report["a"]["rates"]["error"] == 0 and report["b"]["rates"]["error"] == 0,
    }
    checks["all_passed"] = all(checks.values())
    return checks


def cmd_demo(args: argparse.Namespace) -> int:
    """Validate, run the scripted cases, simulate both versions, compare, and gate."""
    started = time.perf_counter()
    out_dir = Path(args.out)
    flows = [_load(path) for path in DEMO_FLOWS]

    validation_errors = 0
    for path, flow in zip(DEMO_FLOWS, flows, strict=False):
        issues = validate.validate(flow)
        validation_errors += len(validate.errors(issues))
        print(f"validate {path}: {len(validate.errors(issues))} error(s), "
              f"{len(issues) - len(validate.errors(issues))} warning(s)")

    cases = testing.load_cases(DEMO_CASES)
    case_results = testing.run_cases(cases)
    failed_cases = sum(1 for item in case_results if not item.passed)
    print(f"scripted cases: {len(case_results) - failed_cases} of {len(case_results)} passed")
    for item in case_results:
        if not item.passed:
            print(f"  FAIL {item.case.name}: {'; '.join(item.failures)}")

    print(f"simulating {args.calls} calls per flow with seed {args.seed}...")
    results = [simulate.simulate(flow, args.calls, args.seed) for flow in flows]
    report = analytics.compare(results[0], results[1], flows[0].label, flows[1].label)
    text = analytics.format_comparison(report)
    print(text)

    checks = demo_checks(report, validation_errors, failed_cases)
    elapsed = time.perf_counter() - started
    report["checks"] = checks
    report["scripted_cases"] = {"total": len(case_results), "failed": failed_cases}
    report["validation_errors"] = validation_errors
    report["elapsed_seconds"] = round(elapsed, 2)
    analytics.write_json(out_dir / "report.json", report)
    (out_dir / "summary.txt").write_text(text + "\n", encoding="utf-8")
    print(f"wrote {out_dir / 'report.json'} and {out_dir / 'summary.txt'} in {elapsed:.1f}s")

    if args.check:
        print()
        for name, passed in checks.items():
            print(f"{name}: {'true' if passed else 'false'}")
        return 0 if checks["all_passed"] else 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    """The argparse parser for ``python -m ivr``."""
    parser = argparse.ArgumentParser(prog="python -m ivr", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p_validate = sub.add_parser("validate", help="statically validate flow files")
    p_validate.add_argument("flows", nargs="+", help="flow JSON file(s)")
    p_validate.set_defaults(func=cmd_validate)

    p_run = sub.add_parser("run", help="run one call and print the transcript")
    p_run.add_argument("flow", help="flow JSON file")
    p_run.add_argument("--ani", default="5550100999", help="caller number (default: an unknown caller)")
    p_run.add_argument("--inputs", default="",
                       help='comma-separated inputs, e.g. "dtmf:1,speech:pay my bill,timeout,hangup"')
    p_run.add_argument("--json", action="store_true", help="print the call result as JSON")
    p_run.set_defaults(func=cmd_run)

    p_test = sub.add_parser("test", help="run scripted call-flow test cases")
    p_test.add_argument("path", help="a case file or a directory of case files")
    p_test.set_defaults(func=cmd_test)

    p_sim = sub.add_parser("simulate", help="simulate synthetic callers through a flow")
    p_sim.add_argument("flow", help="flow JSON file")
    p_sim.add_argument("--calls", type=int, default=1000)
    p_sim.add_argument("--seed", type=int, default=DEMO_SEED)
    p_sim.add_argument("--out", default=None, help="directory for the results JSON")
    p_sim.set_defaults(func=cmd_simulate)

    p_report = sub.add_parser("report", help="metrics for one results file, or a comparison of two")
    p_report.add_argument("results", nargs="+", help="one or two results JSON files from simulate")
    p_report.add_argument("--out", default=None, help="directory for report.json and summary.txt")
    p_report.set_defaults(func=cmd_report)

    p_demo = sub.add_parser("demo", help="validate, test, simulate v1 and v2, compare and gate")
    p_demo.add_argument("--check", action="store_true", help="exit 1 unless every gate check passes")
    p_demo.add_argument("--calls", type=int, default=DEMO_CALLS)
    p_demo.add_argument("--seed", type=int, default=DEMO_SEED)
    p_demo.add_argument("--out", default=DEMO_OUT)
    p_demo.set_defaults(func=cmd_demo)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point; returns the process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "report" and len(args.results) > 2:
        parser.error("report takes one or two results files")
    return args.func(args)
