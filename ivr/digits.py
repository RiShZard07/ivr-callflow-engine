"""Digit-string helpers shared by the engine, the directory and the backend."""

from __future__ import annotations

DIGITS = frozenset("0123456789")


def is_digit_string(text: str) -> bool:
    """Return True when ``text`` is non-empty and made only of ASCII digits.

    ``str.isdigit`` accepts characters such as superscripts, which a DTMF
    keypad cannot produce, so the check is done against an explicit set.
    """
    return bool(text) and all(ch in DIGITS for ch in text)


def luhn_check_digit(partial: str) -> str:
    """Return the Luhn check digit that makes ``partial + digit`` valid.

    Walking from the right end of ``partial``, every other digit (starting
    with the last one) is doubled and reduced by nine when it exceeds nine;
    the check digit brings the total to a multiple of ten.
    """
    if not is_digit_string(partial):
        raise ValueError(f"Luhn input must be digits, got {partial!r}")
    total = 0
    for position, char in enumerate(reversed(partial)):
        digit = int(char)
        if position % 2 == 0:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return str((10 - total % 10) % 10)


def luhn_valid(number: str) -> bool:
    """Return True when ``number`` (digits only, at least two) passes the Luhn check."""
    if not is_digit_string(number) or len(number) < 2:
        return False
    return luhn_check_digit(number[:-1]) == number[-1]


def normalize_ani(raw: str) -> str:
    """Reduce a caller number to its digits.

    Formatting such as ``555-012-3456`` or ``(555) 012-3456`` is dropped and a
    leading country code ``1`` is removed from eleven-digit numbers, so the
    directory can be keyed by ten-digit strings.
    """
    digits = "".join(ch for ch in str(raw) if ch in DIGITS)
    if len(digits) == 11 and digits[0] == "1":
        digits = digits[1:]
    return digits
