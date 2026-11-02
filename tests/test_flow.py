"""Flow loading and schema errors."""

from __future__ import annotations

import json

import pytest
from conftest import FLOWS_DIR, HANGUP_DONE, TRANSFER_FALLBACK, build_flow, collect, menu

from ivr.flow import (
    ACTION_NAMES,
    CollectNode,
    FlowError,
    MenuNode,
    PlayNode,
    TransferNode,
    load_flow,
    loads_flow,
)


def test_care_flows_load_with_expected_shape():
    v1 = load_flow(FLOWS_DIR / "care_v1.json")
    v2 = load_flow(FLOWS_DIR / "care_v2.json")
    assert v1.start == "greeting" and v2.start == "greeting"
    assert v1.label == "care 1.0" and v2.label == "care 2.0"
    assert set(v1.queues) == {"billing", "tech_support", "account_services", "general"}
    assert isinstance(v2.nodes["main_menu"], MenuNode)
    assert set(v2.nodes["main_menu"].options) == {"0", "1", "2", "3", "4", "5"}
    assert isinstance(v1.nodes["collect_zip"], CollectNode)
    assert v1.nodes["collect_zip"].validation == "digits"
    assert all(node.id == node_id for node_id, node in v2.nodes.items())


def test_node_targets_and_prompts_follow_every_edge():
    flow = build_flow(
        {
            "start": menu("Pick.", {"1": ("done", ["one"]), "2": ("fallback", [])}),
            "done": HANGUP_DONE,
            "fallback": TRANSFER_FALLBACK,
        }
    )
    start = flow.nodes["start"]
    assert start.targets() == ("done", "fallback", "fallback")
    assert start.prompts() == ("Pick.", "Invalid.", "Silence.")
    assert flow.nodes["done"].targets() == ()
    assert flow.nodes["fallback"].prompts() == ()
    assert isinstance(flow.nodes["fallback"], TransferNode)


def test_defaults_fill_optional_fields():
    flow = build_flow(
        {
            "start": {"type": "collect", "prompt": "Digits.", "variable": "x", "next": "done",
                      "max_retries": 1},
            "done": HANGUP_DONE,
        }
    )
    node = flow.nodes["start"]
    assert isinstance(node, CollectNode)
    assert (node.min_digits, node.max_digits, node.terminator, node.validation) == (1, 20, "#", "digits")
    assert node.confirm is False and node.on_max_retries is None and node.timeout_seconds == 5.0


@pytest.mark.parametrize(
    "node, fragment",
    [
        ({"type": "teleport", "next": "done"}, "unknown type"),
        ({"type": "play", "next": "done"}, "missing required field 'prompt'"),
        ({"type": "play", "prompt": "Hi.", "next": "done", "nxt": "done"}, r"unknown field\(s\) nxt"),
        ({"type": "play", "prompt": "", "next": "done"}, "non-empty string"),
        ({"type": "transfer", "queue": "general", "priority": 9, "reason": "x"}, "priority must be between 1 and 5"),
        ({"type": "transfer", "queue": "general", "priority": True, "reason": "x"}, "must be an integer"),
        ({"type": "action", "name": "launch_rocket", "on_success": "done", "on_failure": "done"}, "unknown action"),
        ({"type": "hangup", "prompt": 12}, "non-empty string"),
    ],
)
def test_bad_node_shapes_raise_flow_error(node, fragment):
    with pytest.raises(FlowError, match=fragment):
        build_flow({"start": node, "done": HANGUP_DONE})


@pytest.mark.parametrize(
    "overrides, fragment",
    [
        ({"min_digits": 6, "max_digits": 4}, "min_digits <= max_digits"),
        ({"validation": "regex"}, "validation must be one of"),
        ({"terminator": "A"}, "terminator must be"),
        ({"timeout_seconds": 0}, "greater than zero"),
        ({"confirm": "yes"}, "must be true or false"),
    ],
)
def test_bad_collect_fields_raise_flow_error(overrides, fragment):
    with pytest.raises(FlowError, match=fragment):
        build_flow({"start": collect(**overrides), "done": HANGUP_DONE, "fallback": TRANSFER_FALLBACK})


def test_menu_option_keys_must_be_dtmf_keys_and_menu_needs_options():
    with pytest.raises(FlowError, match="not a DTMF key"):
        build_flow({"start": menu("Pick.", {"A": ("done", ["a"])}), "done": HANGUP_DONE})
    with pytest.raises(FlowError, match="at least one option"):
        build_flow({"start": menu("Pick.", {}), "done": HANGUP_DONE})


def test_branch_conditions_must_be_objects_with_when_and_next():
    with pytest.raises(FlowError, match="missing required field 'next'"):
        build_flow(
            {
                "start": {"type": "branch", "conditions": [{"when": "x exists"}], "default": "done"},
                "done": HANGUP_DONE,
            }
        )


def test_top_level_shape_errors():
    with pytest.raises(FlowError, match="missing required field 'start'"):
        loads_flow(json.dumps({"name": "x", "version": "1", "nodes": {"a": HANGUP_DONE}}))
    with pytest.raises(FlowError, match="at least one node"):
        loads_flow(json.dumps({"name": "x", "version": "1", "start": "a", "nodes": {}}))
    with pytest.raises(FlowError, match="expected an object"):
        loads_flow("[]")
    with pytest.raises(FlowError, match="invalid JSON"):
        loads_flow("{not json")


def test_duplicate_node_ids_are_rejected_when_loading():
    text = (
        '{"name": "x", "version": "1", "start": "a", "nodes": {'
        '"a": {"type": "hangup"}, "a": {"type": "hangup", "prompt": "Bye."}}}'
    )
    with pytest.raises(FlowError, match="duplicate key 'a'"):
        loads_flow(text)


def test_play_node_is_frozen_and_typed():
    flow = build_flow({"start": {"type": "play", "prompt": "Hi.", "next": "done"}, "done": HANGUP_DONE})
    node = flow.nodes["start"]
    assert isinstance(node, PlayNode) and node.type == "play"
    with pytest.raises(AttributeError):
        node.prompt = "changed"  # type: ignore[misc]
    assert "balance_inquiry" in ACTION_NAMES
