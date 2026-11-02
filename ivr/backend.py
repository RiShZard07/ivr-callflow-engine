"""Synthetic self-service backend.

The five actions a flow can call. Each one is a deterministic function of the
customer record, so the same flow, caller and inputs always produce the same
call. In a real deployment these would be calls into billing, network and
account systems; here they read the seeded directory.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from ivr.directory import REFERENCE_DATE, Customer, Directory, default_directory
from ivr.flow import ACTION_NAMES

# Variables each action can set on success; validate.py uses this table to
# warn about prompts that read a variable no upstream node can set.
ACTION_OUTPUTS: dict[str, tuple[str, ...]] = {
    "balance_inquiry": ("balance", "due_date", "past_due"),
    "data_usage": ("data_used_gb", "data_limit_gb", "data_remaining_gb"),
    "outage_check": ("outage", "outage_eta"),
    "payment_arrangement": ("arrangement_date", "arrangement_amount"),
    "reset_voicemail_pin": ("voicemail_pin",),
}
ARRANGEMENT_EXTENSION_DAYS = 14


@dataclass(frozen=True)
class ActionResult:
    """What an action returned: a success flag, variables to store, a short message."""

    success: bool
    message: str
    variables: dict[str, Any] = field(default_factory=dict)


def _failure(message: str) -> ActionResult:
    return ActionResult(False, message)


class Backend:
    """Dispatches action names to their synthetic implementations."""

    def __init__(self, directory: Directory | None = None, today: date = REFERENCE_DATE) -> None:
        self.directory = directory if directory is not None else default_directory()
        self.today = today
        self._actions: dict[str, Callable[[Mapping[str, Any]], ActionResult]] = {
            "balance_inquiry": self._balance_inquiry,
            "data_usage": self._data_usage,
            "outage_check": self._outage_check,
            "payment_arrangement": self._payment_arrangement,
            "reset_voicemail_pin": self._reset_voicemail_pin,
        }

    def call(self, name: str, variables: Mapping[str, Any]) -> ActionResult:
        """Run the named action against the current call variables."""
        if name not in ACTION_NAMES:
            raise KeyError(f"unknown action {name!r}")
        return self._actions[name](variables)

    def _customer(self, variables: Mapping[str, Any]) -> Customer | None:
        account = variables.get("account")
        if account is None:
            return None
        return self.directory.by_account(str(account))

    def _balance_inquiry(self, variables: Mapping[str, Any]) -> ActionResult:
        customer = self._customer(variables)
        if customer is None:
            return _failure("account not found")
        due = date.fromisoformat(customer.due_date)
        past_due = customer.balance > 0 and due < self.today
        return ActionResult(
            True,
            "balance returned",
            {"balance": customer.balance, "due_date": customer.due_date, "past_due": past_due},
        )

    def _data_usage(self, variables: Mapping[str, Any]) -> ActionResult:
        customer = self._customer(variables)
        if customer is None:
            return _failure("account not found")
        remaining = round(max(0.0, customer.data_limit_gb - customer.data_used_gb), 1)
        return ActionResult(
            True,
            "usage returned",
            {
                "data_used_gb": customer.data_used_gb,
                "data_limit_gb": customer.data_limit_gb,
                "data_remaining_gb": remaining,
            },
        )

    def _outage_check(self, variables: Mapping[str, Any]) -> ActionResult:
        zip_code = variables.get("zip")
        if zip_code is None:
            customer = self._customer(variables)
            if customer is None:
                return _failure("no service address to check")
            zip_code = customer.zip_code
        zip_code = str(zip_code)
        if not self.directory.has_outage(zip_code):
            return ActionResult(True, "no outage", {"outage": False})
        # A deterministic restoration estimate between 2 PM and 9 PM.
        hour = 2 + sum(int(ch) for ch in zip_code if ch.isdigit()) % 8
        return ActionResult(True, "outage found", {"outage": True, "outage_eta": f"{hour} PM today"})

    def _payment_arrangement(self, variables: Mapping[str, Any]) -> ActionResult:
        customer = self._customer(variables)
        if customer is None:
            return _failure("account not found")
        if not customer.eligible_arrangement:
            return _failure("account not eligible for a payment arrangement")
        if customer.balance <= 0:
            return _failure("nothing is owed on this account")
        new_due = date.fromisoformat(customer.due_date) + timedelta(days=ARRANGEMENT_EXTENSION_DAYS)
        return ActionResult(
            True,
            "arrangement created",
            {"arrangement_date": new_due.isoformat(), "arrangement_amount": customer.balance},
        )

    def _reset_voicemail_pin(self, variables: Mapping[str, Any]) -> ActionResult:
        customer = self._customer(variables)
        if customer is None:
            return _failure("account not found")
        if not customer.has_voicemail:
            return _failure("no voicemail service on this account")
        pin = f"{(int(customer.account) * 7919) % 10000:04d}"
        return ActionResult(True, "temporary pin issued", {"voicemail_pin": pin})
