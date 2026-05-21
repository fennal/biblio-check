"""Tests for normalization, author detection, and journal matching."""
from scripts.normalize import (
    strip_accents, normalize_title, normalize_author_surname, extract_doi,
    extract_arxiv_id, extract_pmid, extract_year, is_institutional_author,
    normalize_institutional_name, journal_abbreviation_match,
    author_surname_overlap,
)
from scripts.models import Author


def test_strip_accents():
    assert strip_accents("Müller") == "Muller"
    assert strip_accents("García") == "Garcia"
    assert strip_accents("Jínek") == "Jinek"


def test_normalize_title_drops_stopwords_and_punct():
    assert normalize_title("The Impact of AI, on Care!") == "impact ai care"


def test_extract_year_prefers_parenthesized():
    # Page number 1778 must NOT win over the real (2025) year.
    assert extract_year("32, 1778-1784 (2025).") == 2025


def test_extract_year_ama_semicolon():
    assert extract_year("Science. 2012;337(6096):816-821.") == 2012


def test_extract_year_none_when_absent():
    assert extract_year("no year here") is None


def test_extract_doi_strips_trailing_punct():
    assert extract_doi("see https://doi.org/10.1126/science.1225829.") == "10.1126/science.1225829"


def test_extract_arxiv_id():
    assert extract_arxiv_id("arXiv:1706.03762") == "1706.03762"
    assert extract_arxiv_id("arXiv: 1706.03762v7") == "1706.03762v7"
    # Bare new-style id (no prefix) is still recognized.
    assert extract_arxiv_id("see 1706.03762 for details") == "1706.03762"


def test_extract_pmid():
    assert extract_pmid("PMID: 22745249") == "22745249"


# ---- institutional author detection ----

def test_institutional_true_cases():
    assert is_institutional_author("Bureau of Justice Statistics")
    assert is_institutional_author("Centers for Disease Control and Prevention")
    assert is_institutional_author("National Committee for Quality Assurance")
    assert is_institutional_author("Joint Commission")
    assert is_institutional_author("Lucian Leape Institute")


def test_institutional_false_cases():
    # AMA-style personal authors must NOT be flagged institutional.
    assert not is_institutional_author("Wade G")
    assert not is_institutional_author("Doudna JA")
    assert not is_institutional_author("Smith, J.")
    assert not is_institutional_author("Hassanpour S, Langlotz CP")
    assert not is_institutional_author("Smith and Jones")


def test_institutional_name_normalization_matches():
    a = normalize_institutional_name("Centers for Disease Control and Prevention")
    b = normalize_institutional_name("Centers Disease Control Prevention")
    assert a == b  # connectives dropped


# ---- journal abbreviation matching ----

def test_journal_abbreviation_match_jamia():
    assert journal_abbreviation_match(
        "J. Am. Med. Inform. Assoc.",
        "Journal of the American Medical Informatics Association",
    )


def test_journal_abbreviation_match_npj():
    assert journal_abbreviation_match("npj Digit. Med.", "npj Digital Medicine")


def test_journal_abbreviation_no_false_match():
    assert not journal_abbreviation_match("Nature", "Science")


# ---- author surname overlap ----

def test_author_surname_overlap():
    a = [Author(family="Smith"), Author(family="Jones")]
    b = [Author(family="Smith"), Author(family="Brown")]
    assert author_surname_overlap(a, b) == 0.5
    assert author_surname_overlap([], b) == 0.0
