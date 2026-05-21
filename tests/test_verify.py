"""Verification / classification tests. All offline -- candidates are injected
via the `offline` fixture so no network is touched.

These guard the three core invariants:
  * a fabricated citation never comes back VERIFIED,
  * a correct citation comes back VERIFIED,
  * a real-paper-with-errors comes back PARTIAL (not HALLUCINATED).
"""
from scripts.verify import verify_one
from scripts.models import ParsedReference, Author, Confidence, CitationType


def _ref(**kw):
    kw.setdefault("authors", [Author(family="Smith", given="John")])
    return ParsedReference(raw_text=kw.get("title", ""), **kw)


def test_verified_when_two_sources_agree(offline, make_canonical):
    c1 = make_canonical(source="crossref")
    c2 = make_canonical(source="openalex")
    offline([c1, c2])
    ref = _ref(title="A Test Paper", year=2020, doi="10.1234/test",
               container_title="Journal of Testing", volume="1", issue="2", pages="3-4")
    res = verify_one(ref)
    assert res.confidence in (Confidence.VERIFIED, Confidence.VERIFIED_WITH_STYLE_EDITS)


def test_hallucinated_when_no_candidates(offline, monkeypatch):
    import scripts.verify as v
    monkeypatch.setattr(v, "url_resolves", lambda url, timeout=4.0: False)
    offline([])
    ref = _ref(title="Totally Fabricated Nonexistent Paper About Nothing", year=2019)
    res = verify_one(ref)
    assert res.confidence == Confidence.HALLUCINATED


def test_partial_when_real_paper_wrong_year(offline, make_canonical):
    c1 = make_canonical(source="crossref", year=2012)
    c2 = make_canonical(source="openalex", year=2012)
    offline([c1, c2])
    # User cited the same paper but with the wrong year (2014).
    ref = _ref(title="A Test Paper", year=2014, doi="10.1234/test",
               container_title="Journal of Testing", volume="1", issue="2", pages="3-4")
    res = verify_one(ref)
    assert res.confidence == Confidence.PARTIAL
    assert any("year" in d.field for d in res.discrepancies)


def test_grey_literature_via_url(offline, monkeypatch):
    # No academic candidates, but the URL resolves and author is institutional.
    offline([])
    ref = ParsedReference(
        raw_text="Bureau of Justice Statistics. Report. 2024. https://bjs.ojp.gov/x",
        title="Report", year=2024, url="https://bjs.ojp.gov/x",
        authors=[Author(family="Bureau of Justice Statistics", given="")],
    )
    res = verify_one(ref)
    assert res.confidence == Confidence.VERIFIED_VIA_URL


def test_strict_refuses_grey_literature(offline):
    offline([])
    ref = ParsedReference(
        raw_text="Bureau of Justice Statistics. Report. 2024. https://bjs.ojp.gov/x",
        title="Report", year=2024, url="https://bjs.ojp.gov/x",
        authors=[Author(family="Bureau of Justice Statistics", given="")],
    )
    res = verify_one(ref, strictness="strict")
    assert res.confidence == Confidence.HALLUCINATED


def test_relaxed_promotes_institutional_to_verified(offline, monkeypatch):
    import scripts.verify as v
    monkeypatch.setattr(v, "url_resolves", lambda url, timeout=4.0: False)
    offline([])
    ref = ParsedReference(
        raw_text="National Committee for Quality Assurance. HEDIS. NCQA; 2025.",
        title="HEDIS", year=2025, publisher="NCQA",
        authors=[Author(family="National Committee for Quality Assurance", given="")],
    )
    res = verify_one(ref, strictness="relaxed")
    assert res.confidence == Confidence.VERIFIED


def test_fake_doi_flagged(offline, monkeypatch, make_canonical):
    import scripts.verify as v
    # DOI does not resolve.
    monkeypatch.setattr(v, "doi_resolves", lambda doi: False)
    c1 = make_canonical(source="crossref", doi="10.9999/real")
    c2 = make_canonical(source="openalex", doi="10.9999/real")
    offline([c1, c2])
    ref = _ref(title="A Test Paper", year=2020, doi="10.1234/fake",
               container_title="Journal of Testing", volume="1", issue="2", pages="3-4")
    res = verify_one(ref)
    assert any("doi" in d.field.lower() for d in res.discrepancies)


def test_et_al_suppresses_author_count(offline, make_canonical):
    # Canonical has 7 authors; user wrote one + 'et al.' -> no author-count flag.
    many = tuple((f"Author{i}", "X") for i in range(7))
    c1 = make_canonical(source="crossref", authors=many)
    c2 = make_canonical(source="openalex", authors=many)
    offline([c1, c2])
    ref = ParsedReference(
        raw_text="Author0 X, et al. A Test Paper. Journal of Testing. 2020;1(2):3-4.",
        title="A Test Paper", year=2020,
        authors=[Author(family="Author0", given="X")],
        container_title="Journal of Testing", volume="1", issue="2", pages="3-4",
        doi="10.1234/test",
    )
    res = verify_one(ref)
    assert not any("author count" in d.field for d in res.discrepancies)


def test_pmc_id_in_pages_flagged(offline, make_canonical):
    c1 = make_canonical(source="crossref", pages="816-821")
    c2 = make_canonical(source="openalex", pages="816-821")
    offline([c1, c2])
    ref = _ref(title="A Test Paper", year=2020, doi="10.1234/test",
               container_title="Journal of Testing", volume="1", issue="2",
               pages="PMC1234567")
    res = verify_one(ref)
    assert any("PMC" in d.field for d in res.discrepancies)
