"""Speech intent matching."""

from __future__ import annotations

import pytest

from ivr.flow import MenuOption
from ivr.speech import (
    match_option,
    normalize,
    score_options,
    spoken_confirmation,
    spoken_digits,
    spoken_dtmf,
)

OPTIONS = {
    "1": MenuOption("1", "Billing", "a", ("billing", "bill", "balance")),
    "2": MenuOption("2", "Outage", "b", ("outage", "internet", "service down")),
    "3": MenuOption("3", "Payment", "c", ("payment", "pay", "arrangement")),
    "4": MenuOption("4", "Agent", "d", ("agent", "representative")),
    "5": MenuOption("5", "Mystery", "e", ()),
}


def test_normalize_lowercases_strips_punctuation_maps_synonyms_and_drops_stopwords():
    assert normalize("  I'd like to PAY my Bill, please!! ") == ["payment", "billing"]
    assert normalize("talk to a representative") == ["talk", "agent"]
    assert normalize("my wi-fi is down") == ["outage", "outage"]
    assert normalize("how much do I owe") == ["balance"]
    assert normalize("the the a") == []


def test_match_picks_the_option_sharing_the_most_tokens():
    match = match_option("my internet service is down", OPTIONS)
    assert match.key == "2" and match.reason == "matched"
    assert match.scores["2"] == 1 and match.scores["1"] == 0  # all tokens map to "outage"
    assert score_options("outage on the internet", OPTIONS)["2"] == 1
    assert match_option("I need a payment arrangement", OPTIONS).key == "3"
    assert match_option("get me a human", OPTIONS).key == "4"


def test_tie_between_two_options_is_ambiguous_and_returns_no_key():
    match = match_option("pay my bill", OPTIONS)
    assert match.key is None and match.reason == "ambiguous"
    assert match.scores["1"] == match.scores["3"] == 1


def test_no_shared_tokens_is_no_match():
    match = match_option("hello there", OPTIONS)
    assert match.key is None and match.reason == "no_match"
    assert score_options("hello there", OPTIONS) == {"1": 0, "2": 0, "3": 0, "4": 0, "5": 0}


@pytest.mark.parametrize(
    "utterance, key",
    [("press two", "2"), ("two", "2"), ("2", "2"), ("press the number four please", "4"),
     ("star", "*"), ("pound", "#"), ("zero", "0"), ("oh", "0"), ("press two or three", None),
     ("twenty", None), ("one moment", None), ("", None)],
)
def test_spoken_dtmf(utterance, key):
    assert spoken_dtmf(utterance) == key


def test_spoken_key_selects_an_option_or_is_rejected_when_the_key_is_not_offered():
    assert match_option("press one", OPTIONS).reason == "key"
    assert match_option("press one", OPTIONS).key == "1"
    assert match_option("press nine", OPTIONS).key is None
    assert match_option("press nine", OPTIONS).reason == "no_match"


def test_spoken_digits_concatenates_digit_words_and_rejects_other_words():
    assert spoken_digits("four one five five five five zero one two four") == "4155550124"
    assert spoken_digits("my account number is 4155550124 pound") == "4155550124#"
    assert spoken_digits("zip code five five five oh one") == "55501"
    assert spoken_digits("I do not remember") is None
    assert spoken_digits("") is None


def test_spoken_confirmation_maps_yes_and_no_style_answers():
    assert spoken_confirmation("yes") == "1"
    assert spoken_confirmation("yeah that's right") == "1"
    assert spoken_confirmation("press one") == "1"
    assert spoken_confirmation("no, that is wrong") == "2"
    assert spoken_confirmation("two") == "2"
    assert spoken_confirmation("yes no maybe") is None
    assert spoken_confirmation("hmm") is None
