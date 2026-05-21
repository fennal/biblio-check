"""Extraction tests: entry splitting, field parsing, and the specific
regressions found across the real-paper iterations."""
from scripts.extract import (
    extract_from_text, split_into_entries, _find_references_block,
    _parse_one_entry, extract_intext_citations,
)


def test_apa_entry_fields():
    raw = ("LeCun, Y., Bengio, Y., & Hinton, G. (2015). Deep learning. "
           "Nature, 521(7553), 436-444. https://doi.org/10.1038/nature14539")
    ref = _parse_one_entry(raw)
    assert ref.year == 2015
    assert "deep learning" in ref.title.lower()
    assert ref.doi == "10.1038/nature14539"
    assert ref.authors[0].family == "LeCun"


def test_short_title_not_rejected():
    # 'Deep learning' is 13 chars -- must still parse as a title.
    raw = "LeCun, Y. (2015). Deep learning. Nature, 521(7553), 436-444."
    ref = _parse_one_entry(raw)
    assert ref.title.lower().startswith("deep learning")


def test_ama_abbreviated_journal():
    raw = ("Tsoi AH, et al. Establishing and implementing a responsible AI framework. "
           "J. Am. Med. Inform. Assoc. 32, 1778-1784 (2025).")
    ref = _parse_one_entry(raw)
    assert ref.year == 2025
    assert ref.authors and ref.authors[0].family == "Tsoi"
    # journal should be the full abbreviation, not just 'Med'
    assert "Am" in ref.container_title and "Med" in ref.container_title


def test_author_chunk_not_mistaken_for_title():
    # '1. Chatterjee, S., Fruhling, A. ...' -- title must not be 'Chatterjee, S'
    raw = ("Chatterjee, S., Fruhling, A. & Gartner, D. Towards new frontiers of "
           "healthcare systems research. Health Systems 13, 263-273 (2024).")
    ref = _parse_one_entry(raw)
    assert ref.title.lower().startswith("towards new frontiers")


def test_institutional_author_parsed_whole():
    raw = ("National Committee for Quality Assurance. HEDIS Measurement Year 2025. "
           "Washington, DC: NCQA; 2025.")
    ref = _parse_one_entry(raw)
    assert ref.authors and ref.authors[0].family == "National Committee for Quality Assurance"


def test_url_not_captured_as_title():
    raw = "Smith J. (2025). https://doi.org/10.1234/x"
    ref = _parse_one_entry(raw)
    assert not ref.title.lower().startswith("http")


def test_numbered_entry_splitting():
    block = [
        "1. Smith J. Title one. Journal A. 2020;1(1):1-2.",
        "2. Jones K. Title two. Journal B. 2021;2(2):3-4.",
        "3. Brown L. Title three. Journal C. 2022;3(3):5-6.",
    ]
    entries = split_into_entries(block)
    assert len(entries) == 3


def test_paragraph_per_reference_splitting():
    # docx-style: each ref on its own line, no blank lines, no numbers.
    block = [
        "Smith, J. (2020). Alpha study. Journal A, 1(1), 1-2.",
        "Jones, K. (2021). Beta study. Journal B, 2(2), 3-4.",
    ]
    entries = split_into_entries(block)
    assert len(entries) == 2


def test_references_block_stops_at_appendix_letter():
    lines = [
        "References",
        "1. Smith J. Title. Journal. 2020;1:1-2.",
        "2. Jones K. Title. Journal. 2021;2:3-4.",
        "C. Appendix material that is not a reference at all.",
        "More appendix text.",
    ]
    block = _find_references_block(lines)
    joined = " ".join(block)
    assert "Smith" in joined and "Jones" in joined
    assert "Appendix material" not in joined


def test_intext_multi_citation_split():
    body = "Prior work (Smith et al., 2020; Jones, 2021) established this."
    cites = extract_intext_citations(body)
    # Both citations should be extracted individually.
    joined = " ".join(cites)
    assert "Smith" in joined and "Jones" in joined


def test_intext_institutional():
    body = "Per agency guidance (Bureau of Justice Statistics, 2024) the rate rose."
    cites = extract_intext_citations(body)
    assert any("Bureau of Justice Statistics" in c for c in cites)


def test_extract_from_text_returns_refs():
    text = (
        "References\n"
        "Smith, J. (2020). A study of things. Journal of Things, 1(2), 3-4.\n"
        "Jones, K. (2021). Another study. Journal of More, 2(3), 5-6.\n"
    )
    refs, intext = extract_from_text(text)
    assert len(refs) == 2
