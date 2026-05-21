"""Parser backend abstraction. The regex fallback always works; AnyStyle is
tested via a fake binary so we exercise the integration path without needing
Ruby installed."""
import json
import os
import stat
from pathlib import Path

from scripts.parsers import (
    parse_entries, _anystyle_record_to_ref, available_backends,
)
from scripts.extract import _safe_parse_one_entry


def test_available_backends_always_includes_regex():
    assert "regex" in available_backends()


def test_parse_entries_regex_fallback():
    entries = [
        "Smith, J. (2020). Alpha. Journal A, 1(1), 1-2.",
        "Jones, K. (2021). Beta. Journal B, 2(2), 3-4.",
    ]
    refs = parse_entries(entries, backend="regex", regex_parser=_safe_parse_one_entry)
    assert len(refs) == 2
    assert refs[0].authors[0].family == "Smith"


def test_anystyle_record_mapping():
    # AnyStyle wraps scalar values in lists and gives author as {family, given}.
    rec = {
        "author": [{"family": "Smith", "given": "Jane"}, {"family": "Doe", "given": "John"}],
        "title": ["A study of widgets"],
        "date": ["2020"],
        "container-title": ["Journal of Widgets"],
        "volume": ["12"],
        "pages": ["45-67"],
        "doi": ["10.1234/x"],
        "type": ["article-journal"],
    }
    ref = _anystyle_record_to_ref(rec)
    assert ref.title == "A study of widgets"
    assert ref.year == 2020
    assert ref.doi == "10.1234/x"
    assert ref.container_title == "Journal of Widgets"
    assert [a.family for a in ref.authors] == ["Smith", "Doe"]


def test_anystyle_backend_via_fake_binary(tmp_path, monkeypatch):
    """Install a fake `anystyle` that echoes a known JSON payload, and verify
    parse_entries routes through it and maps the result."""
    fake_json = json.dumps([
        {"author": [{"family": "Real", "given": "Author"}],
         "title": ["Properly parsed title"], "date": ["2019"],
         "container-title": ["Real Journal"], "doi": ["10.5555/real"]},
    ])
    fake = tmp_path / "anystyle"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        f"cat <<'EOF'\n{fake_json}\nEOF\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

    # Point the parser at our fake binary and reset the cached probe.
    import scripts.parsers as p
    monkeypatch.setenv("BIBLIO_CHECK_ANYSTYLE_BIN", str(fake))
    p._ANYSTYLE_PROBED = False
    p._ANYSTYLE_PATH = None

    refs = parse_entries(["Some raw reference 2019"], backend="anystyle",
                         regex_parser=_safe_parse_one_entry)
    assert len(refs) == 1
    assert refs[0].title == "Properly parsed title"
    assert refs[0].doi == "10.5555/real"

    # Reset probe so other tests aren't affected.
    p._ANYSTYLE_PROBED = False
    p._ANYSTYLE_PATH = None


def test_backend_falls_back_when_unavailable(monkeypatch):
    # Force-select anystyle but with no binary -> must fall back to regex.
    import scripts.parsers as p
    monkeypatch.setenv("BIBLIO_CHECK_ANYSTYLE_BIN", "/nonexistent/anystyle")
    p._ANYSTYLE_PROBED = False
    p._ANYSTYLE_PATH = None
    refs = parse_entries(["Smith, J. (2020). Alpha. Journal A, 1(1), 1-2."],
                         backend="anystyle", regex_parser=_safe_parse_one_entry)
    assert len(refs) == 1
    assert refs[0].authors[0].family == "Smith"
    p._ANYSTYLE_PROBED = False
    p._ANYSTYLE_PATH = None
