"""Synthetic directory and backend: determinism and action rules."""

from __future__ import annotations

from datetime import date

import pytest
from conftest import AVERY_ACCOUNT, AVERY_ANI, JORDAN_ACCOUNT, RILEY_ACCOUNT, UNKNOWN_ANI

from ivr.backend import ACTION_OUTPUTS, Backend
from ivr.digits import is_digit_string, luhn_check_digit, luhn_valid, normalize_ani
from ivr.directory import IDENTIFY_VARIABLES, SHOWCASE_CUSTOMERS, Directory
from ivr.flow import ACTION_NAMES


def test_digit_helpers():
    assert luhn_check_digit("7992739871") == "3"
    assert luhn_valid("79927398713") and not luhn_valid("79927398710")
    assert not luhn_valid("1") and not luhn_valid("12a4")
    assert is_digit_string("0123") and not is_digit_string("") and not is_digit_string("12²")
    assert normalize_ani("+1 (555) 012-3456") == "5550123456"
    assert normalize_ani("555.012.3456") == "5550123456"
    with pytest.raises(ValueError):
        luhn_check_digit("12x")


def test_directory_is_deterministic_for_a_seed_and_changes_with_it():
    a, b, c = Directory(size=60, seed=1), Directory(size=60, seed=1), Directory(size=60, seed=2)
    assert a.customers == b.customers
    assert a.customers != c.customers
    assert a.customers[: len(SHOWCASE_CUSTOMERS)] == SHOWCASE_CUSTOMERS == c.customers[: len(SHOWCASE_CUSTOMERS)]
    with pytest.raises(ValueError):
        Directory(size=1)


def test_every_customer_has_valid_unique_identifiers(directory):
    anis = [c.ani for c in directory]
    accounts = [c.account for c in directory]
    assert len(directory) == 400 == len(set(anis)) == len(set(accounts))
    assert all(len(a) == 10 and a.startswith("55501") for a in anis)
    assert all(len(acct) == 10 and luhn_valid(acct) for acct in accounts)
    assert all(c.plan in ("basic", "plus", "premium") and c.data_limit_gb > 0 for c in directory)
    assert all(len(c.zip_code) == 5 for c in directory)
    assert UNKNOWN_ANI not in anis


def test_lookups_by_ani_and_account(directory):
    avery = directory.by_ani(AVERY_ANI)
    assert avery is not None and avery.first_name == "Avery" and avery.account == AVERY_ACCOUNT
    assert directory.by_ani("555-012-3456") is avery
    assert directory.by_account(AVERY_ACCOUNT) is avery
    assert directory.by_ani(UNKNOWN_ANI) is None and directory.by_account("0000000000") is None
    assert set(avery.as_variables()) == set(IDENTIFY_VARIABLES)
    assert directory.has_outage("55505") and not directory.has_outage("55501")


def test_action_registry_matches_the_flow_schema(backend):
    assert set(ACTION_OUTPUTS) == set(ACTION_NAMES)
    with pytest.raises(KeyError):
        backend.call("launch_rocket", {})


def test_balance_inquiry_reports_past_due_relative_to_the_reference_date(backend, directory):
    current = backend.call("balance_inquiry", {"account": AVERY_ACCOUNT})
    assert current.success and current.variables == {"balance": 87.5, "due_date": "2026-10-15", "past_due": False}
    overdue = backend.call("balance_inquiry", {"account": JORDAN_ACCOUNT})
    assert overdue.variables["past_due"] is True
    paid = backend.call("balance_inquiry", {"account": RILEY_ACCOUNT})
    assert paid.variables["balance"] == 0.0 and paid.variables["past_due"] is False
    later = Backend(directory, today=date(2026, 11, 1))
    assert later.call("balance_inquiry", {"account": AVERY_ACCOUNT}).variables["past_due"] is True
    missing = backend.call("balance_inquiry", {})
    assert not missing.success and missing.message == "account not found" and missing.variables == {}


def test_payment_arrangement_eligibility_rules(backend):
    ok = backend.call("payment_arrangement", {"account": AVERY_ACCOUNT})
    assert ok.success and ok.variables == {"arrangement_date": "2026-10-29", "arrangement_amount": 87.5}
    ineligible = backend.call("payment_arrangement", {"account": JORDAN_ACCOUNT})
    assert not ineligible.success and "not eligible" in ineligible.message
    nothing_owed = backend.call("payment_arrangement", {"account": RILEY_ACCOUNT})
    assert not nothing_owed.success and "nothing is owed" in nothing_owed.message
    assert not backend.call("payment_arrangement", {"account": "1234567890"}).success


def test_outage_check_uses_zip_then_account_and_is_deterministic(backend):
    by_zip = backend.call("outage_check", {"zip": "55505"})
    assert by_zip.success and by_zip.variables["outage"] is True
    assert by_zip.variables["outage_eta"].endswith("PM today")
    assert backend.call("outage_check", {"zip": "55505"}) == by_zip
    by_account = backend.call("outage_check", {"account": JORDAN_ACCOUNT})
    assert by_account.variables == by_zip.variables
    clear = backend.call("outage_check", {"account": AVERY_ACCOUNT})
    assert clear.success and clear.variables == {"outage": False}
    nowhere = backend.call("outage_check", {})
    assert not nowhere.success and "no service address" in nowhere.message


def test_voicemail_pin_reset_and_data_usage(backend):
    reset = backend.call("reset_voicemail_pin", {"account": AVERY_ACCOUNT})
    assert reset.success and len(reset.variables["voicemail_pin"]) == 4
    assert reset.variables["voicemail_pin"].isdigit()
    assert backend.call("reset_voicemail_pin", {"account": AVERY_ACCOUNT}) == reset
    no_service = backend.call("reset_voicemail_pin", {"account": RILEY_ACCOUNT})
    assert not no_service.success and "no voicemail" in no_service.message
    usage = backend.call("data_usage", {"account": RILEY_ACCOUNT})
    assert usage.variables == {"data_used_gb": 410.7, "data_limit_gb": 1000.0, "data_remaining_gb": 589.3}
    assert backend.call("data_usage", {"account": JORDAN_ACCOUNT}).variables["data_remaining_gb"] == 4.0
    assert not backend.call("data_usage", {"account": None}).success
