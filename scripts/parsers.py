"""Optional citation-parser backends.

The built-in regex parser (in extract.py) always runs with zero external
dependencies. This module adds OPTIONAL accelerators that produce more
accurate field extraction when they happen to be installed:

  * AnyStyle  -- a trained CRF reference parser (Ruby gem `anystyle-cli`).
                 If the `anystyle` binary is on PATH, we shell out to it.
  * GROBID    -- a Java service for scholarly-document parsing. If the
                 GROBID_URL env var points to a running server, we POST
                 reference strings to /api/processCitationList.

Design contract: these are *accelerators*, never requirements. If neither is
available (the common case for a fresh install), `parse_entries` falls back to
the regex parser passed in by the caller. Nothing here can stop the tool from
running -- a missing/broken backend only changes parse accuracy, never
whether an audit completes.

Backend selection (env var BIBLIO_CHECK_PARSER or the `backend` argument):
  * 'auto'      -- AnyStyle if present, else GROBID if configured, else regex.
  * 'anystyle'  -- force AnyStyle (falls back to regex if unavailable).
  * 'grobid'    -- force GROBID (falls back to regex if unavailable).
  * 'regex'     -- force the built-in parser.
"""
from __future__ import annotations

import os
import json
import shutil
import logging
import subprocess
import urllib.request
import urllib.error
from typing import List, Optional, Callable
import xml.etree.ElementTree as ET

from .models import ParsedReference, Author, CitationType

log = logging.getLogger("biblio-check.parsers")

# Cache backend detection so we don't probe the filesystem / env on every call.
_ANYSTYLE_PATH: Optional[str] = None
_ANYSTYLE_PROBED = False


def _anystyle_binary() -> Optional[str]:
    """Return the path to the `anystyle` CLI if available, else None."""
    global _ANYSTYLE_PATH, _ANYSTYLE_PROBED
    if _ANYSTYLE_PROBED:
        return _ANYSTYLE_PATH
    _ANYSTYLE_PROBED = True
    override = os.environ.get("BIBLIO_CHECK_ANYSTYLE_BIN", "").strip()
    if override:
        _ANYSTYLE_PATH = override if (shutil.which(override) or os.path.exists(override)) else None
    else:
        _ANYSTYLE_PATH = shutil.which("anystyle")
    if _ANYSTYLE_PATH:
        log.debug("AnyStyle backend available at %s", _ANYSTYLE_PATH)
    return _ANYSTYLE_PATH


def _grobid_url() -> Optional[str]:
    url = os.environ.get("GROBID_URL", "").strip()
    return url.rstrip("/") if url else None


def available_backends() -> List[str]:
    """Return the list of usable backends, best-first, always including regex."""
    out: List[str] = []
    if _anystyle_binary():
        out.append("anystyle")
    if _grobid_url():
        out.append("grobid")
    out.append("regex")
    return out


# ---------- AnyStyle -------------------------------------------------------

# AnyStyle reference 'type' values mapped to our enum.
_ANYSTYLE_TYPE = {
    "article-journal": CitationType.JOURNAL_ARTICLE,
    "article": CitationType.JOURNAL_ARTICLE,
    "book": CitationType.BOOK,
    "chapter": CitationType.BOOK_CHAPTER,
    "paper-conference": CitationType.CONFERENCE_PAPER,
    "report": CitationType.REPORT,
    "thesis": CitationType.THESIS,
    "webpage": CitationType.WEBSITE,
    "dataset": CitationType.DATASET,
}


def _first(v):
    """AnyStyle wraps most field values in a list; unwrap to the first item."""
    if isinstance(v, list):
        return v[0] if v else None
    return v


def _anystyle_record_to_ref(rec: dict) -> ParsedReference:
    """Map one AnyStyle JSON record to a ParsedReference."""
    ref = ParsedReference()

    # Authors: AnyStyle gives a list of {family, given} dicts under 'author'.
    for a in rec.get("author", []) or []:
        if isinstance(a, dict):
            fam = (a.get("family") or "").strip()
            giv = (a.get("given") or "").strip()
            if fam or giv:
                ref.authors.append(Author(family=fam, given=giv))
        elif isinstance(a, str):
            ref.authors.append(Author.from_string(a))

    ref.title = (_first(rec.get("title")) or "").strip()
    # Date can be '2020', '2020-05', or a full date; pull the year.
    date_val = _first(rec.get("date")) or _first(rec.get("issued")) or ""
    import re as _re
    m_year = _re.search(r"\b(1[6-9]\d{2}|20\d{2})\b", str(date_val))
    if m_year:
        ref.year = int(m_year.group(1))

    ref.container_title = (_first(rec.get("container-title"))
                           or _first(rec.get("journal"))
                           or _first(rec.get("booktitle")) or "").strip()
    ref.volume = str(_first(rec.get("volume")) or "").strip()
    ref.issue = str(_first(rec.get("issue")) or _first(rec.get("number")) or "").strip()
    ref.pages = str(_first(rec.get("pages")) or "").strip()
    ref.publisher = (_first(rec.get("publisher")) or "").strip()
    doi = (_first(rec.get("doi")) or "").strip().lower()
    # AnyStyle sometimes leaves the doi: prefix or a URL in the field.
    import re as _re2
    m_doi = _re2.search(r"10\.\d{4,9}/[^\s\"'<>]+", doi)
    ref.doi = m_doi.group(0).rstrip(".,;:)") if m_doi else ""
    url = (_first(rec.get("url")) or "").strip()
    if url and not ref.doi:
        ref.url = url

    type_val = (_first(rec.get("type")) or "").strip().lower()
    ref.type = _ANYSTYLE_TYPE.get(type_val, CitationType.UNKNOWN)

    # Reconstruct a raw_text for display if AnyStyle didn't carry one.
    ref.raw_text = (_first(rec.get("original")) or "").strip()
    return ref


def parse_entries_anystyle(entries: List[str]) -> Optional[List[ParsedReference]]:
    """Parse reference strings with the AnyStyle CLI. Returns None on any
    failure so the caller can fall back to regex."""
    binary = _anystyle_binary()
    if not binary or not entries:
        return None
    payload = "\n".join(e.replace("\n", " ").strip() for e in entries if e.strip())
    if not payload:
        return None
    try:
        # `anystyle -f json parse <file>`; we feed the file via stdin using '-'.
        proc = subprocess.run(
            [binary, "-f", "json", "parse", "--stdout", "-"],
            input=payload.encode("utf-8"),
            capture_output=True,
            timeout=60,
        )
        if proc.returncode != 0:
            log.debug("anystyle returned %s: %s", proc.returncode, proc.stderr[:200])
            return None
        data = json.loads(proc.stdout.decode("utf-8", errors="replace"))
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError, OSError) as e:
        log.debug("anystyle invocation failed: %s", e)
        return None
    if not isinstance(data, list):
        return None
    refs: List[ParsedReference] = []
    for i, rec in enumerate(data):
        if not isinstance(rec, dict):
            continue
        try:
            ref = _anystyle_record_to_ref(rec)
        except Exception as e:  # pragma: no cover - defensive
            log.debug("anystyle record map failed: %s", e)
            ref = ParsedReference()
        # Preserve original wording from the input if AnyStyle didn't echo it.
        if not ref.raw_text and i < len(entries):
            ref.raw_text = entries[i]
        refs.append(ref)
    # Sanity: AnyStyle should return one record per input line. If counts
    # diverge wildly, distrust it and fall back.
    if not refs or abs(len(refs) - len([e for e in entries if e.strip()])) > max(2, len(entries) // 2):
        log.debug("anystyle record count mismatch; falling back")
        return None
    return refs


# ---------- GROBID citation list -------------------------------------------

_TEI_NS = {"tei": "http://www.tei-c.org/ns/1.0"}


def _grobid_biblstruct_to_ref(bibl: ET.Element) -> ParsedReference:
    ref = ParsedReference()
    title_el = bibl.find(".//tei:title[@level='a']", _TEI_NS) or bibl.find(".//tei:title", _TEI_NS)
    if title_el is not None and title_el.text:
        ref.title = title_el.text.strip()
    for pers in bibl.findall(".//tei:author/tei:persName", _TEI_NS):
        surname = pers.find("tei:surname", _TEI_NS)
        given = pers.find("tei:forename", _TEI_NS)
        if surname is not None and surname.text:
            ref.authors.append(Author(
                family=surname.text.strip(),
                given=(given.text.strip() if given is not None and given.text else ""),
            ))
    date_el = bibl.find(".//tei:date[@type='published']", _TEI_NS)
    if date_el is not None:
        import re as _re
        when = date_el.get("when") or (date_el.text or "")
        m = _re.match(r"(\d{4})", when)
        if m:
            ref.year = int(m.group(1))
    j = bibl.find(".//tei:title[@level='j']", _TEI_NS) or bibl.find(".//tei:title[@level='m']", _TEI_NS)
    if j is not None and j.text:
        ref.container_title = j.text.strip()
    bs = bibl.find(".//tei:biblScope[@unit='volume']", _TEI_NS)
    if bs is not None and bs.text:
        ref.volume = bs.text.strip()
    bs = bibl.find(".//tei:biblScope[@unit='issue']", _TEI_NS)
    if bs is not None and bs.text:
        ref.issue = bs.text.strip()
    bs = bibl.find(".//tei:biblScope[@unit='page']", _TEI_NS)
    if bs is not None:
        if bs.text:
            ref.pages = bs.text.strip()
        elif bs.get("from") and bs.get("to"):
            ref.pages = f"{bs.get('from')}-{bs.get('to')}"
    doi_el = bibl.find(".//tei:idno[@type='DOI']", _TEI_NS)
    if doi_el is not None and doi_el.text:
        ref.doi = doi_el.text.strip().lower()
    return ref


def parse_entries_grobid(entries: List[str]) -> Optional[List[ParsedReference]]:
    """Parse reference strings via GROBID /api/processCitationList. Returns
    None on any failure."""
    base = _grobid_url()
    if not base or not entries:
        return None
    endpoint = base + "/api/processCitationList"
    # GROBID accepts repeated 'citations' form fields.
    boundary = "----biblio-check-grobid-cit"
    parts = []
    for e in entries:
        if not e.strip():
            continue
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(b'Content-Disposition: form-data; name="citations"\r\n\r\n')
        parts.append(e.replace("\n", " ").strip().encode("utf-8") + b"\r\n")
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(b'Content-Disposition: form-data; name="consolidateCitations"\r\n\r\n1\r\n')
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)
    req = urllib.request.Request(
        endpoint, data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                 "Accept": "application/xml"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60.0) as r:
            xml_text = r.read().decode("utf-8", errors="replace")
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as e:
        log.debug("GROBID processCitationList failed: %s", e)
        return None
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    refs: List[ParsedReference] = []
    bibls = list(root.iter("{http://www.tei-c.org/ns/1.0}biblStruct"))
    for i, bibl in enumerate(bibls):
        try:
            ref = _grobid_biblstruct_to_ref(bibl)
        except Exception:  # pragma: no cover - defensive
            ref = ParsedReference()
        if i < len(entries):
            ref.raw_text = entries[i]
        refs.append(ref)
    if not refs:
        return None
    return refs


# ---------- dispatcher ------------------------------------------------------

def parse_entries(
    entries: List[str],
    backend: str = "auto",
    regex_parser: Optional[Callable[[str], ParsedReference]] = None,
) -> List[ParsedReference]:
    """Parse reference strings into ParsedReference records.

    `backend` selects the strategy; 'auto' uses the best available. The
    `regex_parser` callable (the built-in per-entry parser) is the guaranteed
    fallback and is used for any entry the chosen backend can't handle.
    """
    backend = (os.environ.get("BIBLIO_CHECK_PARSER") or backend or "auto").lower()

    def _regex_all() -> List[ParsedReference]:
        if regex_parser is None:
            # Last-ditch: import the built-in parser lazily to avoid a cycle.
            from .extract import _safe_parse_one_entry as rp
            return [rp(e) for e in entries]
        return [regex_parser(e) for e in entries]

    if backend == "regex":
        return _regex_all()

    order: List[str] = []
    if backend == "auto":
        order = available_backends()  # e.g. ['anystyle', 'grobid', 'regex']
    elif backend in ("anystyle", "grobid"):
        order = [backend, "regex"]
    else:
        order = ["regex"]

    for b in order:
        if b == "anystyle":
            result = parse_entries_anystyle(entries)
            if result is not None:
                return _backfill(result, entries, regex_parser)
        elif b == "grobid":
            result = parse_entries_grobid(entries)
            if result is not None:
                return _backfill(result, entries, regex_parser)
        elif b == "regex":
            return _regex_all()
    return _regex_all()


def _backfill(
    parsed: List[ParsedReference],
    entries: List[str],
    regex_parser: Optional[Callable[[str], ParsedReference]],
) -> List[ParsedReference]:
    """If the backend produced an entry with no title AND no authors, the parse
    likely failed for that one -- re-parse it with the regex fallback so we
    never lose an entry to a backend hiccup."""
    if regex_parser is None:
        return parsed
    out: List[ParsedReference] = []
    for i, ref in enumerate(parsed):
        if not ref.title and not ref.authors and i < len(entries) and entries[i].strip():
            out.append(regex_parser(entries[i]))
        else:
            out.append(ref)
    return out
