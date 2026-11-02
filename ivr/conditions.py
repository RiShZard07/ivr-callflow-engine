"""The tiny condition language used by branch nodes.

Five forms are allowed, and nothing else::

    var == value      var != value      var exists
    var > number      var < number

``var`` is a variable name such as ``outage`` or ``customer.plan``. Values
for ``==`` and ``!=`` may be quoted. Equality is numeric when both sides look
like numbers, boolean-aware for true/false variables, and plain string
comparison otherwise. A missing variable is never equal to anything, so
``var != value`` is true when ``var`` was never set.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

_PATTERN = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_.]*)\s+(==|!=|>|<|exists)(?:\s+(.+?))?\s*$")
_TRUE_WORDS = frozenset({"true", "yes", "1"})
_FALSE_WORDS = frozenset({"false", "no", "0"})


class ConditionError(ValueError):
    """Raised when a ``when`` expression does not follow the grammar."""


@dataclass(frozen=True)
class Condition:
    """A parsed ``when`` expression."""

    variable: str
    operator: str
    value: str | None


def parse_condition(text: str) -> Condition:
    """Parse a ``when`` expression, raising ``ConditionError`` on bad syntax."""
    match = _PATTERN.match(text)
    if not match:
        raise ConditionError(
            f"cannot parse condition {text!r}: expected 'var == value', 'var != value', "
            "'var > number', 'var < number' or 'var exists'"
        )
    variable, operator, value = match.groups()
    if operator == "exists":
        if value is not None:
            raise ConditionError(f"condition {text!r}: 'exists' takes no value")
        return Condition(variable, operator, None)
    if value is None:
        raise ConditionError(f"condition {text!r}: operator {operator} needs a value")
    if operator in ("<", ">") and _as_number(value) is None:
        raise ConditionError(f"condition {text!r}: {operator} needs a numeric value")
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return Condition(variable, operator, value)


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _equal(actual: Any, expected: str) -> bool:
    if isinstance(actual, bool):
        words = _TRUE_WORDS if actual else _FALSE_WORDS
        return expected.lower() in words
    actual_number = _as_number(actual)
    expected_number = _as_number(expected)
    if actual_number is not None and expected_number is not None:
        return actual_number == expected_number
    return str(actual) == expected


def evaluate(condition: Condition, variables: Mapping[str, Any]) -> bool:
    """Evaluate a parsed condition against the call's variables."""
    present = condition.variable in variables and variables[condition.variable] is not None
    if condition.operator == "exists":
        return present
    if not present:
        return condition.operator == "!="
    actual = variables[condition.variable]
    value = condition.value or ""
    if condition.operator in ("<", ">"):
        number = _as_number(actual)
        if number is None:
            return False
        limit = float(value)
        return number > limit if condition.operator == ">" else number < limit
    matches = _equal(actual, value)
    return matches if condition.operator == "==" else not matches
