"""Every static validation rule, positive and negative."""

from __future__ import annotations

from conftest import FLOWS_DIR, HANGUP_DONE, TRANSFER_FALLBACK, build_flow, collect, menu

from ivr.flow import load_flow
from ivr.validate import Issue, errors, format_issues, has_errors, validate


def codes(flow, level: str | None = None) -> set[str]:
    return {issue.code for issue in validate(flow) if level is None or issue.level == level}


def test_care_flows_validate_without_any_issue(care_v1, care_v2):
    assert validate(care_v1) == []
    assert validate(care_v2) == []


def test_broken_example_reports_the_documented_issues():
    issues = validate(load_flow(FLOWS_DIR / "broken_example.json"))
    assert len(issues) >= 3
    assert has_errors(issues)
    assert {issue.code for issue in errors(issues)} == {
        "max_retries_out_of_range",
        "missing_max_retries_target",
        "dangling_target",
        "input_free_cycle",
        "unknown_queue",
    }
    assert {issue.code for issue in issues if issue.level == "warning"} == {
        "unset_prompt_variable",
        "missing_invalid_prompt",
        "missing_timeout_prompt",
        "option_without_intents",
        "unreachable_node",
    }
    # errors come first, then warnings, each sorted by node id
    assert [issue.level for issue in issues] == ["error"] * 5 + ["warning"] * 5


def test_unknown_start_node_is_an_error():
    flow = build_flow({"a": HANGUP_DONE}, start="missing")
    found = [issue for issue in validate(flow) if issue.code == "unknown_start"]
    assert len(found) == 1 and found[0].level == "error" and found[0].node_id is None


def test_dangling_targets_are_errors_for_every_edge_kind():
    flow = build_flow(
        {
            "start": menu("Pick.", {"1": ("ghost_option", ["one"])}, on_max_retries="ghost_fallback"),
            "b": {"type": "branch", "conditions": [{"when": "x exists", "next": "ghost_branch"}],
                  "default": "done"},
            "done": HANGUP_DONE,
        }
    )
    dangling = {issue.message for issue in validate(flow) if issue.code == "dangling_target"}
    assert dangling == {
        "points at unknown node 'ghost_option'",
        "points at unknown node 'ghost_fallback'",
        "points at unknown node 'ghost_branch'",
    }


def test_unreachable_node_is_a_warning_not_an_error():
    flow = build_flow(
        {"start": {"type": "play", "prompt": "Hi.", "next": "done"}, "done": HANGUP_DONE,
         "island": {"type": "play", "prompt": "Lonely.", "next": "done"}}
    )
    issues = validate(flow)
    assert [i.node_id for i in issues if i.code == "unreachable_node"] == ["island"]
    assert not has_errors(issues)


def test_missing_retry_handling_on_menu_and_collect():
    flow = build_flow(
        {
            "start": menu("Pick.", {"1": ("c", ["one"])}, on_max_retries=None, invalid_prompt=None,
                          timeout_prompt=None),
            "c": collect(on_max_retries=None, invalid_prompt=None, timeout_prompt=None),
            "done": HANGUP_DONE,
        }
    )
    issues = validate(flow)
    by_code = {}
    for issue in issues:
        by_code.setdefault(issue.code, set()).add((issue.node_id, issue.level))
    assert by_code["missing_max_retries_target"] == {("start", "error"), ("c", "error")}
    assert by_code["missing_invalid_prompt"] == {("start", "warning"), ("c", "warning")}
    assert by_code["missing_timeout_prompt"] == {("start", "warning"), ("c", "warning")}


def test_max_retries_out_of_range_both_ends():
    low = build_flow({"start": menu("Pick.", {"1": ("done", ["one"])}, max_retries=0),
                      "done": HANGUP_DONE, "fallback": TRANSFER_FALLBACK})
    high = build_flow({"start": collect(max_retries=11), "done": HANGUP_DONE, "fallback": TRANSFER_FALLBACK})
    fine = build_flow({"start": collect(max_retries=10), "done": HANGUP_DONE, "fallback": TRANSFER_FALLBACK})
    assert "max_retries_out_of_range" in codes(low, "error")
    assert "max_retries_out_of_range" in codes(high, "error")
    assert "max_retries_out_of_range" not in codes(fine)


def test_transfer_to_unknown_queue():
    flow = build_flow({"start": {"type": "transfer", "queue": "nowhere", "priority": 1, "reason": "x"}})
    assert codes(flow, "error") == {"unknown_queue"}
    assert codes(build_flow({"start": TRANSFER_FALLBACK})) == set()


def test_input_free_cycle_is_an_error_but_a_cycle_through_a_menu_is_fine():
    looping = build_flow(
        {
            "start": {"type": "play", "prompt": "A.", "next": "b"},
            "b": {"type": "branch", "conditions": [], "default": "start"},
            "exit": HANGUP_DONE,
        }
    )
    issue = next(i for i in validate(looping) if i.code == "input_free_cycle")
    assert issue.level == "error"
    assert "start -> b -> start" in issue.message
    through_menu = build_flow(
        {
            "start": {"type": "play", "prompt": "A.", "next": "m"},
            "m": menu("Again?", {"1": ("start", ["again"]), "2": ("done", ["done"])}),
            "done": HANGUP_DONE,
            "fallback": TRANSFER_FALLBACK,
        }
    )
    assert "input_free_cycle" not in codes(through_menu)


def test_self_loop_play_node_is_reported_once():
    flow = build_flow({"start": {"type": "play", "prompt": "Forever.", "next": "start"}, "done": HANGUP_DONE})
    cycles = [i for i in validate(flow) if i.code == "input_free_cycle"]
    assert len(cycles) == 1 and cycles[0].node_id == "start"


def test_option_without_intents_is_a_warning():
    flow = build_flow(
        {"start": menu("Pick.", {"1": ("done", ["one"]), "2": ("done", [])}), "done": HANGUP_DONE,
         "fallback": TRANSFER_FALLBACK}
    )
    found = [i for i in validate(flow) if i.code == "option_without_intents"]
    assert len(found) == 1 and found[0].level == "warning" and "option 2" in found[0].message


def test_prompt_variable_must_be_set_upstream():
    unset = build_flow(
        {"start": {"type": "play", "prompt": "Hello {customer.first_name}, {ani} calling.", "next": "done"},
         "done": HANGUP_DONE}
    )
    warnings = [i for i in validate(unset) if i.code == "unset_prompt_variable"]
    assert [w.message for w in warnings] == ["prompt uses {customer.first_name} but no upstream node sets it"]

    via_identify = build_flow(
        {
            "start": {"type": "identify", "on_found": "hi", "on_not_found": "done"},
            "hi": {"type": "play", "prompt": "Hello {customer.first_name}.", "next": "bal"},
            "bal": {"type": "action", "name": "balance_inquiry", "on_success": "say", "on_failure": "done"},
            "say": {"type": "play", "prompt": "Balance {balance} due {due_date}.", "next": "done"},
            "done": {"type": "hangup", "prompt": "Bye {account}."},
        }
    )
    assert "unset_prompt_variable" not in codes(via_identify)

    via_collect = build_flow(
        {"start": collect("zip", next="say"), "say": {"type": "play", "prompt": "Zip {zip}.", "next": "done"},
         "done": HANGUP_DONE, "fallback": TRANSFER_FALLBACK}
    )
    assert "unset_prompt_variable" not in codes(via_collect)


def test_flow_without_terminal_node_is_an_error():
    flow = build_flow({"start": menu("Pick.", {"1": ("start", ["again"])}, on_max_retries="start")})
    assert "no_terminal_node" in codes(flow, "error")


def test_invalid_branch_condition_is_an_error():
    flow = build_flow(
        {"start": {"type": "branch", "conditions": [{"when": "balance >= 10", "next": "done"}],
                   "default": "done"}, "done": HANGUP_DONE}
    )
    found = [i for i in validate(flow) if i.code == "invalid_condition"]
    assert len(found) == 1 and "cannot parse condition" in found[0].message


def test_empty_node_id_is_an_error():
    flow = build_flow({"start": {"type": "play", "prompt": "Hi.", "next": "done"}, "done": HANGUP_DONE,
                       "": HANGUP_DONE})
    found = [i for i in validate(flow) if i.code == "empty_node_id"]
    assert len(found) == 1 and found[0].level == "error" and found[0].node_id == ""


def test_issue_formatting():
    issue = Issue("error", "main_menu", "something is off", "some_code")
    assert str(issue) == "error: [main_menu] something is off (some_code)"
    assert str(Issue("error", None, "no start", "unknown_start")).startswith("error: [flow]")
    assert format_issues([]) == "no issues found"
    assert format_issues([issue]).splitlines() == [str(issue)]
