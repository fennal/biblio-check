"""In-text citation parsing and cross-check, including the numeric-range crash."""
from scripts.intext import parse_intext, cross_check
from scripts.models import ParsedReference, Author


def test_parse_numeric_range_does_not_crash():
    # Regression: '[1-3]' previously crashed with a ValueError on unpacking.
    c = parse_intext("[1-3]")
    assert c is not None
    assert c.indices == [1, 2, 3]


def test_parse_numeric_list():
    c = parse_intext("[1, 2, 4]")
    assert c.indices == [1, 2, 4]


def test_parse_numeric_mixed_range_and_list():
    c = parse_intext("[1, 3-5]")
    assert c.indices == [1, 3, 4, 5]


def test_parse_author_year():
    c = parse_intext("(Smith et al., 2020)")
    assert c.year == 2020
    assert "smith" in c.surnames


def test_parse_two_authors():
    c = parse_intext("(Patel and Sharma, 2022)")
    assert c.year == 2022
    assert "patel" in c.surnames and "sharma" in c.surnames


def test_parse_institutional():
    c = parse_intext("(Bureau of Justice Statistics, 2024)")
    assert c.year == 2024
    assert c.institutional  # normalized institution key set


def test_crosscheck_orphan_and_unused():
    bib = [
        ParsedReference(title="A", year=2020, authors=[Author(family="Smith", given="J")]),
        ParsedReference(title="B", year=2021, authors=[Author(family="Jones", given="K")]),
    ]
    intext = ["(Smith, 2020)", "(Brown, 2019)"]  # Brown is orphan; Jones unused
    result = cross_check(bib, intext)
    assert any("Brown" in c.raw for c in result.orphan_intext)
    assert 2 in result.unused_bibliography  # Jones (index 2) never cited


def test_crosscheck_numeric():
    bib = [ParsedReference(title=f"T{i}", year=2020, authors=[Author(family=f"A{i}", given="X")])
           for i in range(1, 4)]
    result = cross_check(bib, ["[1]", "[3]"])
    assert result.unused_bibliography == [2]


def test_crosscheck_empty_intext():
    bib = [ParsedReference(title="A", year=2020, authors=[Author(family="Smith", given="J")])]
    result = cross_check(bib, [])
    assert result.orphan_intext == []
    assert result.unused_bibliography == []
