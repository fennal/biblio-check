"""Report rendering -- markdown/json/html/bibtex/ris all render without error
across every tier, and write_all degrades gracefully."""
import json
from pathlib import Path

from scripts.report import (
    render_markdown, render_json, render_html, render_bibtex, render_ris,
    render_clean_bibliography, write_all,
)
from scripts.models import (
    VerificationResult, ParsedReference, CanonicalMetadata, Author,
    Confidence, Discrepancy, StyleEdit, CitationType,
)


def _results_one_per_tier():
    canon = CanonicalMetadata(
        source="crossref", source_id="10.1/x", source_url="https://doi.org/10.1/x",
        authors=[Author(family="Smith", given="J")], title="Verified paper",
        year=2020, container_title="Journal", volume="1", issue="2", pages="3-4",
        doi="10.1/x", type=CitationType.JOURNAL_ARTICLE,
    )
    return [
        VerificationResult(parsed=ParsedReference(raw_text="Smith J. Verified paper.", title="Verified paper"),
                           canonical=canon, confidence=Confidence.VERIFIED, matches=[canon]),
        VerificationResult(parsed=ParsedReference(raw_text="Smith J. Style paper.", title="Style paper"),
                           canonical=canon, confidence=Confidence.VERIFIED_WITH_STYLE_EDITS,
                           style_edits=[StyleEdit("pages", "3-4", "3–4", "Use en-dash")], matches=[canon]),
        VerificationResult(parsed=ParsedReference(raw_text="Agency. Report. 2024. http://x", title="Report",
                                                  url="http://x"),
                           confidence=Confidence.VERIFIED_VIA_URL),
        VerificationResult(parsed=ParsedReference(raw_text="Smith J. Partial paper.", title="Partial paper"),
                           canonical=canon, confidence=Confidence.PARTIAL,
                           discrepancies=[Discrepancy("year", "2014", "2020", "major")], matches=[canon]),
        VerificationResult(parsed=ParsedReference(raw_text="Nobody. Fake thing.", title="Fake thing"),
                           confidence=Confidence.HALLUCINATED),
    ]


def test_markdown_all_tiers():
    md = render_markdown(_results_one_per_tier(), style="apa", source_name="x.docx")
    for label in ("VERIFIED", "PARTIAL", "HALLUCINATED", "grey literature", "Clean bibliography"):
        assert label in md


def test_json_valid():
    data = json.loads(render_json(_results_one_per_tier()))
    assert len(data) == 5
    assert all("confidence" in d for d in data)


def test_html_valid_structure():
    html = render_html(_results_one_per_tier(), style="apa", source_name="x.docx")
    assert html.startswith("<!doctype html>")
    assert "</html>" in html
    assert "Clean bibliography" in html
    # Hallucinated placeholder should be present in clean bib.
    assert "REMOVED" in html


def test_bibtex_and_ris():
    res = _results_one_per_tier()
    assert "@article" in render_bibtex(res)
    assert "TY  -" in render_ris(res)


def test_clean_bibliography_marks_hallucinated():
    clean = render_clean_bibliography(_results_one_per_tier(), "apa")
    assert "REMOVED" in clean
    # Grey-literature entry kept verbatim.
    assert "Agency. Report." in clean


def test_write_all_writes_requested_formats(tmp_path):
    written = write_all(_results_one_per_tier(), style="apa", out_dir=tmp_path,
                        source_name="x.docx", formats=["markdown", "json", "html", "bibtex", "ris"])
    for k in ("markdown", "json", "html", "bibtex", "ris"):
        assert k in written
        assert Path(written[k]).exists()
