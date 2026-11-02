"""Simulator determinism and persona policy."""

from __future__ import annotations

from collections import Counter
from random import Random

from conftest import AVERY_ACCOUNT, AVERY_ANI, HANGUP_DONE, TRANSFER_FALLBACK, UNKNOWN_ANI, build_flow, collect, menu

from ivr.engine import Call, Dtmf, Hangup, Speech, Timeout
from ivr.simulate import (
    INTENT_PHRASES,
    INTENTS,
    MUMBLES,
    Persona,
    draw_personas,
    next_input,
    simulate,
    simulate_persona,
)


def persona(**overrides) -> Persona:
    base = dict(
        intent="billing", ani=AVERY_ANI, account=AVERY_ACCOUNT, zip_code="55501", knows_account=True,
        miskey_probability=0.0, prefers_speech=False, patience_seconds=600.0, patience_attempts=10,
        in_directory=True, seed=1,
    )
    base.update(overrides)
    return Persona(**base)


def test_simulation_is_deterministic_for_a_seed_and_differs_across_seeds(care_v2):
    first = simulate(care_v2, 40, seed=3)
    second = simulate(care_v2, 40, seed=3)
    assert [r.to_dict() for r in first] == [r.to_dict() for r in second]
    other = simulate(care_v2, 40, seed=4)
    assert [r.outcome for r in first] != [r.outcome for r in other] or [r.ani for r in first] != [r.ani for r in other]


def test_persona_population_covers_the_designed_mix(directory):
    personas = draw_personas(600, seed=11, directory=directory)
    intents = Counter(p.intent for p in personas)
    assert set(intents) == set(INTENTS) and intents["billing"] > intents["voicemail"]
    in_directory = sum(p.in_directory for p in personas) / len(personas)
    assert 0.55 < in_directory < 0.80  # 0.90 customers x 0.75 registered phone
    assert all(directory.by_ani(p.ani) is not None for p in personas if p.in_directory)
    assert all(directory.by_ani(p.ani) is None for p in personas if not p.in_directory)
    assert all(p.account is None or directory.by_account(p.account) is not None for p in personas)
    assert all(not p.knows_account for p in personas if p.account is None)
    assert all(0.0 <= p.miskey_probability <= 0.15 and 60 <= p.patience_seconds <= 240 for p in personas)
    assert len({p.seed for p in personas}) == len(personas)


def test_persona_chooses_the_menu_option_matching_its_intent(care_v2, directory, backend):
    call = Call(care_v2, AVERY_ANI, directory, backend)
    call.step(None)
    assert next_input(call, persona(intent="billing"), Random(0)) == Dtmf("1")
    assert next_input(call, persona(intent="outage"), Random(0)) == Dtmf("2")
    assert next_input(call, persona(intent="data"), Random(0)) == Dtmf("3")
    assert next_input(call, persona(intent="payment"), Random(0)) == Dtmf("4")
    assert next_input(call, persona(intent="voicemail"), Random(0)) == Dtmf("5")
    assert next_input(call, persona(intent="agent"), Random(0)) == Dtmf("0")
    spoken = next_input(call, persona(intent="outage", prefers_speech=True), Random(0))
    assert isinstance(spoken, Speech) and spoken.utterance in INTENT_PHRASES["outage"]
    mumbled = next_input(call, persona(intent="outage", prefers_speech=True, miskey_probability=1.0), Random(0))
    assert isinstance(mumbled, Speech) and mumbled.utterance in MUMBLES


def test_persona_with_no_matching_option_asks_for_an_agent(care_v1, directory, backend):
    call = Call(care_v1, AVERY_ANI, directory, backend)
    call.step(None)
    assert next_input(call, persona(intent="voicemail"), Random(0)) == Dtmf("4")
    no_agent = build_flow({"start": menu("Pick.", {"1": ("done", ["billing"])}), "done": HANGUP_DONE,
                           "fallback": TRANSFER_FALLBACK})
    silent = Call(no_agent, AVERY_ANI, directory, backend)
    silent.step(None)
    assert next_input(silent, persona(intent="voicemail"), Random(0)) == Timeout()


def test_persona_keys_known_digits_and_stays_silent_for_unknown_ones(directory, backend):
    flow = build_flow({"start": collect(confirm=True), "done": HANGUP_DONE, "fallback": TRANSFER_FALLBACK})
    call = Call(flow, UNKNOWN_ANI, directory, backend)
    call.step(None)
    assert next_input(call, persona(), Random(0)) == Dtmf(AVERY_ACCOUNT + "#")
    assert next_input(call, persona(knows_account=False), Random(0)) == Timeout()
    miskeyed = next_input(call, persona(miskey_probability=1.0), Random(0))
    assert isinstance(miskeyed, Dtmf) and miskeyed.digits != AVERY_ACCOUNT + "#"
    assert len(miskeyed.digits) == 11 and sum(a != b for a, b in zip(miskeyed.digits, AVERY_ACCOUNT, strict=False)) == 1
    call.step(miskeyed)
    assert call.phase == "confirm"
    assert next_input(call, persona(), Random(0)) == Dtmf("2")  # read-back does not match: re-enter
    call.step(Dtmf("2"))
    call.step(Dtmf(AVERY_ACCOUNT + "#"))
    assert next_input(call, persona(), Random(0)) == Dtmf("1")


def test_persona_hangs_up_when_patience_runs_out(care_v1, directory, backend):
    call = Call(care_v1, UNKNOWN_ANI, directory, backend)
    call.step(None)
    assert next_input(call, persona(patience_seconds=1.0), Random(0)) == Hangup()
    call.step(Dtmf("9"))
    call.step(Dtmf("9"))
    assert next_input(call, persona(patience_attempts=2), Random(0)) == Hangup()
    assert next_input(call, persona(patience_attempts=3), Random(0)) == Dtmf("1")


def test_persona_presses_finished_once_its_goal_is_met(care_v2, directory, backend):
    result = simulate_persona(care_v2, persona(intent="billing"), directory, backend)
    assert result.outcome == "contained" and result.final_node == "goodbye"
    assert "after_balance_menu" in result.nodes_visited
    agent = simulate_persona(care_v2, persona(intent="agent"), directory, backend)
    assert agent.outcome == "transferred" and agent.transfer_queue == "general"
    speaking = simulate_persona(care_v2, persona(intent="payment", prefers_speech=True), directory, backend)
    assert speaking.outcome == "contained" and "arrangement_ok" in speaking.nodes_visited


def test_v2_beats_v1_on_a_small_sample_with_no_error_outcomes(care_v1, care_v2):
    v1 = simulate(care_v1, 300, seed=5)
    v2 = simulate(care_v2, 300, seed=5)
    assert all(r.outcome in ("contained", "transferred", "abandoned") for r in v1 + v2)
    assert sum(r.outcome == "contained" for r in v2) > sum(r.outcome == "contained" for r in v1)
