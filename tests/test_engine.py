"""Engine semantics: substitution, retries, timeouts, collect rules, actions, outcomes, timing."""

from __future__ import annotations

import pytest
from conftest import (
    AVERY_ACCOUNT,
    AVERY_ANI,
    HANGUP_DONE,
    JORDAN_ANI,
    RILEY_ANI,
    TRANSFER_FALLBACK,
    UNKNOWN_ANI,
    build_flow,
    collect,
    menu,
)

from ivr.engine import (
    DTMF_PRESS_SECONDS,
    MAX_NODE_ENTRIES,
    PROMPT_CHARS_PER_SECOND,
    SPEECH_SECONDS,
    Call,
    CallEndedError,
    CallResult,
    Dtmf,
    Hangup,
    SimClock,
    Speech,
    Timeout,
    render_prompt,
    run_script,
)


def kinds(result: CallResult) -> list[str]:
    return [event.kind for event in result.events]


def simple_menu_flow():
    return build_flow(
        {
            "start": {"type": "play", "prompt": "Hi.", "next": "m"},
            "m": menu("Pick 1 or 2.", {"1": ("done", ["one", "finish"]), "2": ("fallback", ["two", "agent"])}),
            "done": HANGUP_DONE,
            "fallback": TRANSFER_FALLBACK,
        }
    )


def test_play_runs_through_and_menu_waits_for_input():
    call = Call(simple_menu_flow(), UNKNOWN_ANI)
    assert call.phase == "idle" and call.current_node is None
    call.step(None)
    assert call.phase == "menu" and call.waiting_for_input and not call.ended
    assert call.current_node.id == "m"
    assert call.nodes_visited == ["start", "m"]
    call.step(None)  # no input: still waiting, nothing changes
    assert call.phase == "menu" and len(call.events) == 4
    assert kinds(call.result()) == ["node_entered", "prompt_played", "node_entered", "prompt_played"]
    assert call.result().outcome == "in_progress"


def test_render_prompt_substitutes_and_formats_values():
    variables = {"name": "Avery", "balance": 87.5, "limit": 1000.0, "outage": True, "count": 3}
    text, missing = render_prompt("{name}: {balance} of {limit}, outage {outage}, {count}, {nothing}", variables)
    assert text == "Avery: 87.50 of 1000, outage yes, 3, ?"
    assert missing == ["nothing"]


def test_missing_prompt_variable_emits_event_and_renders_question_mark():
    flow = build_flow({"start": {"type": "play", "prompt": "Hello {customer.first_name}.", "next": "done"},
                       "done": HANGUP_DONE})
    result = run_script(flow, [], ani=UNKNOWN_ANI)
    missing = [e for e in result.events if e.kind == "prompt_variable_missing"]
    assert [e.data["variable"] for e in missing] == ["customer.first_name"]
    assert result.prompts()[0] == "Hello ?."
    assert result.outcome == "contained"


def test_menu_dtmf_and_speech_select_options():
    flow = simple_menu_flow()
    by_key = run_script(flow, [Dtmf("1")], ani=UNKNOWN_ANI)
    assert by_key.outcome == "contained" and by_key.final_node == "done"
    received = next(e for e in by_key.events if e.kind == "input_received")
    assert received.data == {"kind": "dtmf", "value": "1", "matched": "1"}
    by_speech = run_script(flow, [Speech("I want an agent please")], ani=UNKNOWN_ANI)
    assert by_speech.outcome == "transferred" and by_speech.transfer_queue == "general"
    spoken_key = run_script(flow, [Speech("press one")], ani=UNKNOWN_ANI)
    assert spoken_key.final_node == "done"


def test_menu_retries_then_falls_back_after_max_retries_plus_one_attempts():
    flow = simple_menu_flow()  # max_retries = 2, so three failed attempts
    result = run_script(flow, [Dtmf("9"), Speech("blah"), Dtmf("#"), Dtmf("1")], ani=UNKNOWN_ANI)
    assert result.outcome == "transferred" and result.final_node == "fallback"
    invalid = [e.data for e in result.events if e.kind == "input_invalid"]
    assert [d["reason"] for d in invalid] == ["no_match", "no_match", "no_match"]
    assert [d["attempt"] for d in invalid] == [1, 2, 3]
    retries = [e.data for e in result.events if e.kind == "retry"]
    assert [(r["attempt"], r["retries_left"]) for r in retries] == [(1, 1), (2, 0)]
    assert result.count("max_retries_reached") == 1
    # each retry replays the invalid prompt and then the menu prompt
    assert result.prompts() == ["Hi.", "Pick 1 or 2.", "Invalid.", "Pick 1 or 2.", "Invalid.", "Pick 1 or 2."]


def test_menu_timeout_uses_timeout_prompt_and_charges_timeout_seconds():
    flow = simple_menu_flow()
    call = Call(flow, UNKNOWN_ANI)
    call.step(None)
    before = call.clock.now
    call.step(Timeout())
    assert call.clock.now == pytest.approx(before + 5 + len("Silence.") / 15 + len("Pick 1 or 2.") / 15)
    assert call.result().prompts()[-2:] == ["Silence.", "Pick 1 or 2."]
    assert [e.data["reason"] for e in call.events if e.kind == "input_invalid"] == ["timeout"]


def test_ambiguous_speech_is_invalid_with_reason_ambiguous():
    flow = build_flow(
        {"start": menu("Pick.", {"1": ("done", ["billing"]), "2": ("done", ["payment"])}),
         "done": HANGUP_DONE, "fallback": TRANSFER_FALLBACK}
    )
    result = run_script(flow, [Speech("pay my bill"), Dtmf("1")], ani=UNKNOWN_ANI)
    assert [e.data["reason"] for e in result.events if e.kind == "input_invalid"] == ["ambiguous"]
    assert result.outcome == "contained"


@pytest.mark.parametrize(
    "overrides, digits, problem",
    [
        ({}, "123#", "account numbers have 10 digits"),
        ({}, "41555501234#", "account numbers have 10 digits"),
        ({"validation": "luhn", "min_digits": 4, "max_digits": 12}, "1234567#", "failed the Luhn check"),
        ({"validation": "digits", "min_digits": 5, "max_digits": 5}, "1234#", "expected 5 to 5 digits"),
        ({"validation": "digits", "min_digits": 1, "max_digits": 5}, "12a4#", "not digits"),
        ({"validation": "digits"}, "#", "not digits"),
    ],
)
def test_collect_validation_failures(overrides, digits, problem):
    flow = build_flow({"start": collect(**overrides), "done": HANGUP_DONE, "fallback": TRANSFER_FALLBACK})
    result = run_script(flow, [Dtmf(digits)], ani=UNKNOWN_ANI)
    invalid = next(e for e in result.events if e.kind == "input_invalid")
    assert invalid.data["reason"] == "validation_failed" and invalid.data["detail"] == problem
    assert result.prompts()[1:3] == ["Bad digits.", "Enter digits."]


def test_collect_accepts_valid_digits_and_strips_the_terminator():
    flow = build_flow({"start": collect(), "done": HANGUP_DONE, "fallback": TRANSFER_FALLBACK})
    result = run_script(flow, [Dtmf(AVERY_ACCOUNT + "#")], ani=UNKNOWN_ANI)
    assert result.variables["account"] == AVERY_ACCOUNT
    assert result.outcome == "contained"
    luhn = build_flow({"start": collect(validation="luhn", min_digits=4, max_digits=12),
                       "done": HANGUP_DONE, "fallback": TRANSFER_FALLBACK})
    assert run_script(luhn, [Dtmf("79927398713#")], ani=UNKNOWN_ANI).variables["account"] == "79927398713"
    spoken = run_script(flow, [Speech("four one five five five five zero one two four pound")], ani=UNKNOWN_ANI)
    assert spoken.variables["account"] == AVERY_ACCOUNT
    words = run_script(flow, [Speech("I do not know it"), Dtmf(AVERY_ACCOUNT + "#")], ani=UNKNOWN_ANI)
    assert [e.data["detail"] for e in words.events if e.kind == "input_invalid"] == ["no digits heard"]


def test_collect_confirm_reads_back_and_accepts_or_reenters():
    flow = build_flow({"start": collect(confirm=True), "done": HANGUP_DONE, "fallback": TRANSFER_FALLBACK})
    call = Call(flow, UNKNOWN_ANI)
    call.step(None)
    call.step(Dtmf("4155550207#"))
    assert call.phase == "confirm" and call.pending_digits == "4155550207"
    assert call.result().prompts()[-1].startswith("You entered 4 1 5 5 5 5 0 2 0 7.")
    call.step(Speech("no that is wrong"))  # rejected: back to collecting, one retry used
    assert call.phase == "collect" and call.pending_digits is None
    assert [e.data for e in call.events if e.kind == "retry"][-1]["reason"] == "reentered"
    call.step(Dtmf(AVERY_ACCOUNT + "#"))
    call.step(Speech("yes"))
    assert call.ended and call.variables["account"] == AVERY_ACCOUNT
    assert "account" in [e.data["name"] for e in call.events if e.kind == "variable_set"]


def test_collect_confirm_rejections_and_bad_answers_use_retries_then_fall_back():
    flow = build_flow({"start": collect(confirm=True), "done": HANGUP_DONE, "fallback": TRANSFER_FALLBACK})
    # max_retries = 2: two rejections are allowed, the third one falls back
    result = run_script(
        flow, [Dtmf(AVERY_ACCOUNT + "#"), Dtmf("2"), Dtmf(AVERY_ACCOUNT + "#"), Dtmf("2"),
               Dtmf(AVERY_ACCOUNT + "#"), Dtmf("2")], ani=UNKNOWN_ANI,
    )
    assert result.outcome == "transferred" and result.final_node == "fallback"
    assert result.count("max_retries_reached") == 1
    assert "account" not in result.variables
    # an answer that is neither 1 nor 2 is an invalid attempt that replays the read-back
    bad_answer = run_script(flow, [Dtmf(AVERY_ACCOUNT + "#"), Dtmf("7"), Dtmf("1")], ani=UNKNOWN_ANI)
    assert bad_answer.outcome == "contained"
    assert [e.data["reason"] for e in bad_answer.events if e.kind == "input_invalid"] == ["no_match"]
    assert bad_answer.prompts()[-3:-1] == ["Bad digits.", bad_answer.prompts()[1]]
    timeout_in_confirm = run_script(flow, [Dtmf(AVERY_ACCOUNT + "#"), Timeout(), Dtmf("1")], ani=UNKNOWN_ANI)
    assert timeout_in_confirm.outcome == "contained"
    readback = "You entered 4 1 5 5 5 5 0 1 2 4. Press 1 if that is correct, or press 2 to enter it again."
    assert timeout_in_confirm.prompts()[-3:-1] == ["Nothing entered.", readback]


def test_identify_found_and_not_found():
    flow = build_flow(
        {
            "start": {"type": "identify", "on_found": "hi", "on_not_found": "done"},
            "hi": {"type": "play", "prompt": "Welcome back, {customer.first_name}.", "next": "done"},
            "done": HANGUP_DONE,
        }
    )
    found = run_script(flow, [], ani="(555) 012-3456")
    assert found.nodes_visited == ["start", "hi", "done"]
    assert found.variables["account"] == AVERY_ACCOUNT and found.variables["zip"] == "55501"
    assert found.variables["customer.first_name"] == "Avery"
    assert found.prompts()[0] == "Welcome back, Avery."
    assert next(e for e in found.events if e.kind == "identify_result").data == {"found": True}
    missing = run_script(flow, [], ani=UNKNOWN_ANI)
    assert missing.nodes_visited == ["start", "done"] and "account" not in missing.variables


def test_action_success_and_failure_paths_store_variables():
    flow = build_flow(
        {
            "start": {"type": "identify", "on_found": "pay", "on_not_found": "pay"},
            "pay": {"type": "action", "name": "payment_arrangement", "on_success": "ok", "on_failure": "fallback"},
            "ok": {"type": "play", "prompt": "Due {arrangement_date} for {arrangement_amount}.", "next": "done"},
            "done": HANGUP_DONE,
            "fallback": TRANSFER_FALLBACK,
        }
    )
    eligible = run_script(flow, [], ani=AVERY_ANI)
    assert eligible.outcome == "contained"
    assert eligible.prompts()[0] == "Due 2026-10-29 for 87.50."
    called = next(e for e in eligible.events if e.kind == "action_called")
    assert called.data == {"name": "payment_arrangement", "success": True, "message": "arrangement created"}
    ineligible = run_script(flow, [], ani=JORDAN_ANI)
    assert ineligible.outcome == "transferred"
    assert next(e for e in ineligible.events if e.kind == "action_called").data["success"] is False
    no_account = run_script(flow, [], ani=UNKNOWN_ANI)
    assert next(e for e in no_account.events if e.kind == "action_called").data["message"] == "account not found"


def test_branch_routes_on_first_true_condition_or_default():
    flow = build_flow(
        {
            "start": {"type": "identify", "on_found": "b", "on_not_found": "b"},
            "b": {
                "type": "branch",
                "conditions": [
                    {"when": "customer.plan == premium", "next": "premium"},
                    {"when": "account exists", "next": "known"},
                ],
                "default": "unknown",
            },
            "premium": {"type": "hangup", "prompt": "Premium."},
            "known": {"type": "hangup", "prompt": "Known."},
            "unknown": {"type": "hangup", "prompt": "Unknown."},
        }
    )
    assert run_script(flow, [], ani=RILEY_ANI).final_node == "premium"
    assert run_script(flow, [], ani=AVERY_ANI).final_node == "known"
    assert run_script(flow, [], ani=UNKNOWN_ANI).final_node == "unknown"


def test_bad_condition_at_runtime_ends_the_call_with_error():
    flow = build_flow(
        {"start": {"type": "branch", "conditions": [{"when": "x >= 1", "next": "done"}], "default": "done"},
         "done": HANGUP_DONE}
    )
    result = run_script(flow, [], ani=UNKNOWN_ANI)
    assert result.outcome == "error" and "bad condition" in result.events[-1].data["message"]


def test_transfer_and_hangup_outcomes_and_events():
    flow = build_flow(
        {"start": menu("Pick.", {"1": ("done", ["one"]), "2": ("fallback", ["two"])}),
         "done": HANGUP_DONE, "fallback": TRANSFER_FALLBACK}
    )
    contained = run_script(flow, [Dtmf("1")], ani=UNKNOWN_ANI)
    assert contained.outcome == "contained" and contained.transfer_queue is None
    assert contained.events[-1].kind == "hangup" and contained.prompts()[-1] == "Bye."
    transferred = run_script(flow, [Dtmf("2")], ani=UNKNOWN_ANI)
    assert transferred.outcome == "transferred" and transferred.transfer_queue == "general"
    assert transferred.events[-1].data == {"queue": "general", "reason": "fallback", "priority": 4}


def test_caller_can_abandon_at_menu_collect_or_confirm():
    flow = build_flow(
        {"start": menu("Pick.", {"1": ("c", ["one"])}), "c": collect(confirm=True),
         "done": HANGUP_DONE, "fallback": TRANSFER_FALLBACK}
    )
    at_menu = run_script(flow, [Hangup()], ani=UNKNOWN_ANI)
    at_collect = run_script(flow, [Dtmf("1"), Hangup()], ani=UNKNOWN_ANI)
    at_confirm = run_script(flow, [Dtmf("1"), Dtmf(AVERY_ACCOUNT + "#"), Hangup()], ani=UNKNOWN_ANI)
    for result, phase, node in ((at_menu, "menu", "start"), (at_collect, "collect", "c"), (at_confirm, "confirm", "c")):
        assert result.outcome == "abandoned" and result.final_node == node
        assert result.events[-1].kind == "abandoned" and result.events[-1].data == {"phase": phase}


def test_loop_guard_ends_runaway_flow_with_error_outcome():
    flow = build_flow(
        {"start": {"type": "play", "prompt": "Again.", "next": "b"},
         "b": {"type": "play", "prompt": "And again.", "next": "start"}, "done": HANGUP_DONE}
    )
    result = run_script(flow, [], ani=UNKNOWN_ANI)
    assert result.outcome == "error"
    assert len(result.nodes_visited) == MAX_NODE_ENTRIES
    assert "loop guard" in result.events[-1].data["message"]


def test_flow_pointing_at_unknown_node_errors_instead_of_crashing():
    flow = build_flow({"start": {"type": "play", "prompt": "Hi.", "next": "nowhere"}, "done": HANGUP_DONE})
    result = run_script(flow, [], ani=UNKNOWN_ANI)
    assert result.outcome == "error" and "unknown node 'nowhere'" in result.events[-1].data["message"]


def test_handle_time_accounting_is_exact():
    flow = build_flow(
        {"start": menu("Pick.", {"1": ("c", ["one"])}), "c": collect(confirm=False, next="done"),
         "done": HANGUP_DONE, "fallback": TRANSFER_FALLBACK}
    )
    clock = SimClock()
    call = Call(flow, UNKNOWN_ANI, clock=clock)
    call.step(None)
    assert clock.now == pytest.approx(len("Pick.") / PROMPT_CHARS_PER_SECOND)
    call.step(Speech("one"))
    expected = len("Pick.") / 15 + SPEECH_SECONDS + len("Enter digits.") / 15
    assert clock.now == pytest.approx(expected)
    call.step(Dtmf(AVERY_ACCOUNT + "#"))
    expected += 11 * DTMF_PRESS_SECONDS + len("Bye.") / 15
    assert clock.now == pytest.approx(expected)
    assert call.result().handle_time_seconds == pytest.approx(expected)
    assert all(a.time <= b.time for a, b in zip(call.events, call.events[1:], strict=False))
    with pytest.raises(ValueError):
        clock.advance(-1)


def test_stepping_a_finished_call_raises():
    call = Call(simple_menu_flow(), UNKNOWN_ANI)
    call.step(Dtmf("1"))  # starting and answering in one step is allowed
    assert call.ended and call.outcome == "contained"
    with pytest.raises(CallEndedError):
        call.step(Dtmf("1"))


def test_run_script_treats_exhausted_inputs_as_a_hangup_and_ignores_extra_inputs():
    flow = simple_menu_flow()
    short = run_script(flow, [], ani=UNKNOWN_ANI)
    assert short.outcome == "abandoned" and short.final_node == "m"
    long = run_script(flow, [Dtmf("1"), Dtmf("2"), Dtmf("3")], ani=UNKNOWN_ANI)
    assert long.outcome == "contained" and long.count("input_received") == 1


def test_call_result_round_trips_through_dict(care_v2):
    result = run_script(care_v2, [Dtmf("1"), Dtmf("3")], ani=AVERY_ANI)
    data = result.to_dict()
    assert data["outcome"] == "contained" and data["flow_version"] == "2.0"
    restored = CallResult.from_dict(data)
    assert restored == result
    assert restored.prompts() == result.prompts()
