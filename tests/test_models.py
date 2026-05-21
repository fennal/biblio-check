"""Tests for the Author.from_string parser (APA / AMA / Western forms)."""
from scripts.models import Author


def test_apa_comma_form():
    a = Author.from_string("Vaswani, A.")
    assert a.family == "Vaswani"
    # Trailing punctuation is normalized off; the formatter re-adds the period.
    assert a.given == "A"


def test_ama_surname_initials_no_comma():
    a = Author.from_string("Wade G")
    assert a.family == "Wade"
    assert a.given == "G"


def test_ama_multiple_initials():
    a = Author.from_string("Doudna JA")
    assert a.family == "Doudna"
    assert a.given == "JA"


def test_western_given_family():
    a = Author.from_string("Ashish Vaswani")
    assert a.family == "Vaswani"
    assert a.given == "Ashish"


def test_single_token():
    a = Author.from_string("Smith")
    assert a.family == "Smith"
    assert a.given == ""


def test_empty():
    a = Author.from_string("")
    assert a.family == ""
