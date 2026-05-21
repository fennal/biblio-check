"""Shared pytest fixtures.

The whole suite runs OFFLINE. Any test that would otherwise hit the network
monkeypatches the relevant verify-module function, so the suite is
deterministic and runs in CI with no API keys or connectivity.
"""
import sys
from pathlib import Path

import pytest

# Make the `scripts` package importable when tests run from the repo root.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.models import CanonicalMetadata, Author, CitationType  # noqa: E402


@pytest.fixture
def make_canonical():
    """Factory for CanonicalMetadata records used as fake API results."""
    def _make(title="A Test Paper", authors=(("Smith", "John"),), year=2020,
              journal="Journal of Testing", volume="1", issue="2", pages="3-4",
              doi="10.1234/test", source="crossref",
              ctype=CitationType.JOURNAL_ARTICLE):
        return CanonicalMetadata(
            source=source,
            source_id=doi,
            source_url=f"https://doi.org/{doi}" if doi else "",
            authors=[Author(family=f, given=g) for f, g in authors],
            title=title,
            year=year,
            container_title=journal,
            volume=volume,
            issue=issue,
            pages=pages,
            doi=doi,
            type=ctype,
        )
    return _make


@pytest.fixture
def offline(monkeypatch):
    """Disable all real network calls in the verify module.

    By default: DOI/URL resolution return True, and candidate gathering
    returns nothing. Individual tests override `_gather_candidates` via the
    returned helper to inject specific candidate sets.
    """
    import scripts.verify as v

    state = {"candidates": []}

    def fake_gather(ref, use_scholarly=False, deadline=None):
        return list(state["candidates"])

    monkeypatch.setattr(v, "_gather_candidates", fake_gather)
    monkeypatch.setattr(v, "doi_resolves", lambda doi: True)
    monkeypatch.setattr(v, "url_resolves", lambda url, timeout=4.0: True)

    def set_candidates(cands):
        state["candidates"] = cands

    return set_candidates
