"""Robustness: no input should crash extraction. These are the 'just works'
guarantees -- garbage, empty, non-English, huge, malformed all degrade
gracefully rather than throwing."""
import pytest

from scripts.extract import extract_from_text, _safe_parse_one_entry, split_into_entries


def test_empty_input():
    refs, intext = extract_from_text("")
    assert refs == []
    assert intext == []


def test_whitespace_only():
    refs, intext = extract_from_text("   \n\n  \t  \n")
    assert refs == []


def test_garbage_input_does_not_crash():
    refs, intext = extract_from_text("@#$%^&*()_+{}|:<>?[];',./~`")
    assert isinstance(refs, list)


def test_non_english_authors():
    text = ("References\n"
            "Müller, H., García-López, J., & Łukasiewicz, K. (2021). "
            "Über die Wirkung. Zeitschrift für Dinge, 5(2), 10-20.\n")
    refs, _ = extract_from_text(text)
    assert len(refs) == 1
    # Accented surnames should survive.
    assert refs[0].authors and "ller" in refs[0].authors[0].family.lower()


def test_very_long_single_line():
    huge = "Smith J. " + ("word " * 5000) + ". Journal. 2020;1:1-2."
    ref = _safe_parse_one_entry(huge)
    assert ref is not None  # no crash


def test_safe_parse_never_raises_on_weird_input():
    for s in ["", ".", "()", "[1-]", "10.", "(2020", "????", "\x00\x01garbage"]:
        ref = _safe_parse_one_entry(s)
        assert ref is not None


def test_split_handles_single_blob():
    text = ("Smith, J. (2020). A. Journal, 1(1), 1-2. Jones, K. (2021). B. Journal, 2(2), 3-4.")
    entries = split_into_entries([text])
    assert len(entries) >= 1  # never errors, returns something


def test_malformed_doi_does_not_crash():
    ref = _safe_parse_one_entry("Smith J. Title. Journal. 2020. doi:10.")
    assert ref is not None


def test_only_year_no_other_fields():
    ref = _safe_parse_one_entry("(2020).")
    assert ref is not None
