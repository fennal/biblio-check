"""Citation formatting across all seven styles + BibTeX/RIS rendering."""
import pytest

from scripts.format_citation import (
    format_reference, format_intext, to_bibtex, SUPPORTED_STYLES,
    _sentence_case, _initials,
)
from scripts.models import CanonicalMetadata, Author, CitationType


def _canon():
    return CanonicalMetadata(
        source="crossref", source_id="10.1234/x", source_url="https://doi.org/10.1234/x",
        authors=[Author(family="Smith", given="Jane Mary"), Author(family="Doe", given="John")],
        title="A study of widgets", year=2020,
        container_title="Journal of Widgets", volume="12", issue="3", pages="45-67",
        doi="10.1234/x", type=CitationType.JOURNAL_ARTICLE,
    )


@pytest.mark.parametrize("style", SUPPORTED_STYLES)
def test_every_style_renders_nonempty(style):
    out = format_reference(_canon(), style, index=1)
    assert out and isinstance(out, str)
    # No dangling separators left behind.
    assert ", ." not in out
    assert ";:" not in out


@pytest.mark.parametrize("style", SUPPORTED_STYLES)
def test_every_style_handles_missing_fields(style):
    c = CanonicalMetadata(source="x", title="Bare title", authors=[Author(family="Smith", given="J")])
    out = format_reference(c, style, index=1)
    assert "Bare title" in out or "bare title" in out.lower()


def test_apa_uses_en_dash_and_doi():
    out = format_reference(_canon(), "apa", index=1)
    assert "45–67" in out
    assert "https://doi.org/10.1234/x" in out


def test_sentence_case_preserves_acronyms():
    assert _sentence_case("A Programmable Dual-RNA-Guided DNA Endonuclease") \
        == "A programmable dual-RNA-guided DNA endonuclease"


def test_initials_handles_trailing_period():
    # 'Aidan N.' should give 'A. N.', not garble.
    assert _initials("Aidan N.") == "A. N."


def test_intext_forms():
    c = _canon()
    assert format_intext(c, "apa") == "(Smith & Doe, 2020)"
    assert format_intext(c, "ieee", index=3) == "[3]"


def test_bibtex_roundtrips_fields():
    bib = to_bibtex(_canon())
    assert "@article" in bib
    assert "title = {A study of widgets}" in bib
    assert "doi = {10.1234/x}" in bib


def test_unknown_style_raises():
    with pytest.raises(ValueError):
        format_reference(_canon(), "vancouverish", index=1)
