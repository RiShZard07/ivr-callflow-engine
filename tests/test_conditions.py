"""The branch condition language."""

from __future__ import annotations

import pytest

from ivr.conditions import Condition, ConditionError, evaluate, parse_condition


@pytest.mark.parametrize(
    "text, expected",
    [
        ("outage == true", Condition("outage", "==", "true")),
        ("customer.plan != 'basic'", Condition("customer.plan", "!=", "basic")),
        ('plan == "plus"', Condition("plan", "==", "plus")),
        ("balance > 100", Condition("balance", ">", "100")),
        ("balance < 0.5", Condition("balance", "<", "0.5")),
        ("  account   exists ", Condition("account", "exists", None)),
    ],
)
def test_parse_accepted_forms(text, expected):
    assert parse_condition(text) == expected


@pytest.mark.parametrize(
    "text",
    ["balance >= 10", "balance > ten", "outage ==", "exists account", "account exists now", "", "1x == 2",
     "outage = true"],
)
def test_parse_rejects_anything_outside_the_grammar(text):
    with pytest.raises(ConditionError):
        parse_condition(text)


def test_equality_is_boolean_aware_numeric_aware_and_string_otherwise():
    variables = {"outage": True, "balance": 87.5, "account": "4155550124", "plan": "plus", "count": 10}
    assert evaluate(parse_condition("outage == true"), variables)
    assert evaluate(parse_condition("outage == yes"), variables)
    assert not evaluate(parse_condition("outage == false"), variables)
    assert evaluate(parse_condition("balance == 87.50"), variables)
    assert evaluate(parse_condition("count == 10.0"), variables)
    assert evaluate(parse_condition("account == 4155550124"), variables)
    assert evaluate(parse_condition("plan == plus"), variables)
    assert evaluate(parse_condition("plan != basic"), variables)
    assert not evaluate(parse_condition("plan == Plus"), variables)


def test_missing_variables_only_satisfy_not_equal():
    variables = {"set": "x", "nothing": None}
    assert not evaluate(parse_condition("missing exists"), variables)
    assert not evaluate(parse_condition("nothing exists"), variables)
    assert evaluate(parse_condition("set exists"), variables)
    assert not evaluate(parse_condition("missing == x"), variables)
    assert evaluate(parse_condition("missing != x"), variables)
    assert not evaluate(parse_condition("missing > 1"), variables)


def test_numeric_comparisons_accept_numeric_strings_and_reject_text():
    variables = {"balance": "150.25", "plan": "plus", "flag": True}
    assert evaluate(parse_condition("balance > 100"), variables)
    assert not evaluate(parse_condition("balance < 100"), variables)
    assert not evaluate(parse_condition("plan > 1"), variables)
    assert not evaluate(parse_condition("flag > 0"), variables)
