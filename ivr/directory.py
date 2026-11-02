"""Seeded synthetic customer directory.

Every customer here is invented. Caller numbers use the fictional 555-01xx
range (stored as ten digits such as ``5550123456``), account numbers are ten
digits that pass the Luhn check, and zip codes are the synthetic values
55501 to 55512. The same seed always produces the same directory.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from functools import lru_cache
from random import Random
from typing import Any

from ivr.digits import luhn_check_digit, normalize_ani

DEFAULT_SIZE = 400
DEFAULT_SEED = 20260101
REFERENCE_DATE = date(2026, 10, 1)
PLAN_DATA_LIMITS_GB: dict[str, float] = {"basic": 100.0, "plus": 300.0, "premium": 1000.0}
FIRST_NAMES = (
    "Avery", "Jordan", "Riley", "Casey", "Morgan", "Taylor", "Quinn", "Reese", "Rowan", "Sage",
    "Emerson", "Finley", "Harper", "Kendall", "Logan", "Parker", "Peyton", "Skyler", "Dakota", "Hayden",
)
ZIP_CODES = tuple(f"555{n:02d}" for n in range(1, 13))
DEFAULT_OUTAGE_ZIPS = frozenset({"55505", "55509", "55512"})

# Variables an identify node sets for a known caller; validate.py uses this list.
IDENTIFY_VARIABLES = (
    "account",
    "zip",
    "customer.first_name",
    "customer.plan",
    "customer.eligible_arrangement",
    "customer.has_voicemail",
)


@dataclass(frozen=True)
class Customer:
    """One synthetic customer record."""

    ani: str
    account: str
    first_name: str
    plan: str
    balance: float
    due_date: str
    data_used_gb: float
    data_limit_gb: float
    zip_code: str
    eligible_arrangement: bool
    has_voicemail: bool

    def as_variables(self) -> dict[str, Any]:
        """Call variables an identify node sets when this customer is found."""
        return {
            "account": self.account,
            "zip": self.zip_code,
            "customer.first_name": self.first_name,
            "customer.plan": self.plan,
            "customer.eligible_arrangement": self.eligible_arrangement,
            "customer.has_voicemail": self.has_voicemail,
        }


# Hand-written customers with known properties, used in the README and tests.
# Avery: known caller, balance due in the future, eligible for an arrangement.
# Jordan: past-due balance, not eligible, lives in an outage zip.
# Riley: nothing owed, no voicemail service.
SHOWCASE_CUSTOMERS: tuple[Customer, ...] = (
    Customer("5550123456", "4155550124", "Avery", "plus", 87.50, "2026-10-15",
             112.4, 300.0, "55501", True, True),
    Customer("5550100200", "4155550207", "Jordan", "basic", 142.10, "2026-09-20",
             96.0, 100.0, "55505", False, True),
    Customer("5550100300", "4155550306", "Riley", "premium", 0.0, "2026-10-20",
             410.7, 1000.0, "55503", True, False),
)
UNKNOWN_ANI = "5550100999"  # reserved: never generated, used to test unknown callers


class Directory:
    """A seeded, in-memory customer directory keyed by ANI and account number."""

    def __init__(
        self,
        size: int = DEFAULT_SIZE,
        seed: int = DEFAULT_SEED,
        outage_zips: frozenset[str] = DEFAULT_OUTAGE_ZIPS,
    ) -> None:
        if size < len(SHOWCASE_CUSTOMERS):
            raise ValueError(f"size must be at least {len(SHOWCASE_CUSTOMERS)}")
        self.outage_zips = frozenset(outage_zips)
        self._customers: list[Customer] = list(SHOWCASE_CUSTOMERS)
        self._by_ani: dict[str, Customer] = {c.ani: c for c in SHOWCASE_CUSTOMERS}
        self._by_account: dict[str, Customer] = {c.account: c for c in SHOWCASE_CUSTOMERS}
        rng = Random(seed)
        while len(self._customers) < size:
            customer = self._draw(rng)
            if customer.ani in self._by_ani or customer.account in self._by_account:
                continue
            self._customers.append(customer)
            self._by_ani[customer.ani] = customer
            self._by_account[customer.account] = customer

    @staticmethod
    def _draw(rng: Random) -> Customer:
        # Suffixes below 10000 are reserved for showcase and test numbers.
        ani = "55501" + str(rng.randint(10000, 99999))
        partial = str(rng.randint(1, 9)) + "".join(str(rng.randint(0, 9)) for _ in range(8))
        account = partial + luhn_check_digit(partial)
        plan = rng.choices(("basic", "plus", "premium"), weights=(45, 40, 15))[0]
        limit = PLAN_DATA_LIMITS_GB[plan]
        balance = 0.0 if rng.random() < 0.15 else round(rng.uniform(20.0, 260.0), 2)
        due = REFERENCE_DATE + timedelta(days=rng.randint(-25, 25))
        return Customer(
            ani=ani,
            account=account,
            first_name=rng.choice(FIRST_NAMES),
            plan=plan,
            balance=balance,
            due_date=due.isoformat(),
            data_used_gb=round(rng.uniform(0.0, limit * 1.1), 1),
            data_limit_gb=limit,
            zip_code=rng.choice(ZIP_CODES),
            eligible_arrangement=rng.random() < 0.75,
            has_voicemail=rng.random() < 0.8,
        )

    @property
    def customers(self) -> tuple[Customer, ...]:
        """All customers in generation order (showcase customers first)."""
        return tuple(self._customers)

    def __len__(self) -> int:
        return len(self._customers)

    def __iter__(self) -> Iterator[Customer]:
        return iter(self._customers)

    def by_ani(self, ani: str) -> Customer | None:
        """Look a caller up by phone number in any common formatting."""
        return self._by_ani.get(normalize_ani(ani))

    def by_account(self, account: str) -> Customer | None:
        """Look a customer up by ten-digit account number."""
        return self._by_account.get(str(account))

    def has_outage(self, zip_code: str) -> bool:
        """True when the zip code is in the seeded outage set."""
        return str(zip_code) in self.outage_zips


@lru_cache(maxsize=1)
def default_directory() -> Directory:
    """The shared default directory (400 customers, fixed seed), built once."""
    return Directory()
