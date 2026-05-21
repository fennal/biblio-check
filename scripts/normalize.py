"""Normalization helpers used by both extraction and verification.

Title/author/string normalization needs to be consistent across all stages
so that a string that came from a PDF matches the same string from CrossRef.
"""
from __future__ import annotations

import re
import unicodedata
from typing import List

# A loose set of journal abbreviations we expand on the way to canonical form.
# This is intentionally small; the verification APIs do the heavy lifting and we
# only need enough to avoid trivially failing matches.
_JOURNAL_ABBREV = {
    "j.": "journal",
    "j": "journal",
    "proc.": "proceedings",
    "proc": "proceedings",
    "conf.": "conference",
    "conf": "conference",
    "int.": "international",
    "int": "international",
    "natl.": "national",
    "rev.": "review",
    "amer.": "american",
    "am.": "american",
}

_STOPWORDS = {"a", "an", "the", "of", "and", "in", "on", "for", "to", "with"}


def strip_accents(s: str) -> str:
    """'Müller' -> 'Muller'. Used only for matching, never for display."""
    return "".join(
        c for c in unicodedata.normalize("NFKD", s)
        if not unicodedata.combining(c)
    )


def normalize_title(s: str) -> str:
    """Aggressive title normalization for fuzzy comparison."""
    s = strip_accents(s).lower()
    s = re.sub(r"<[^>]+>", " ", s)            # strip HTML/MathML tags
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    tokens = [t for t in s.split() if t not in _STOPWORDS]
    return " ".join(tokens)


def normalize_author_surname(s: str) -> str:
    """'Müller-Lyer' -> 'mullerlyer'. For surname overlap scoring."""
    s = strip_accents(s).lower()
    s = re.sub(r"[^a-z]+", "", s)
    return s


def normalize_journal(s: str) -> str:
    """Expand common abbreviations and lowercase for comparison."""
    s = strip_accents(s).lower()
    tokens = re.findall(r"[a-z]+\.?|[0-9]+", s)
    expanded = [_JOURNAL_ABBREV.get(t, t.rstrip(".")) for t in tokens]
    return " ".join(t for t in expanded if t and t not in _STOPWORDS)


def extract_doi(s: str) -> str:
    """Pull a DOI out of free text. CrossRef's regex with our tweaks."""
    if not s:
        return ""
    # 10.<registrant>/<suffix> -- registrant 4-9 digits, suffix anything not whitespace
    m = re.search(r"\b10\.\d{4,9}/[^\s\"'<>]+", s)
    if not m:
        return ""
    doi = m.group(0).rstrip(".,;:)")
    return doi.lower()


def extract_arxiv_id(s: str) -> str:
    """Pull an arXiv identifier. Handles old (cond-mat/9801001), new
    (1706.03762), and versioned new ids (1706.03762v7)."""
    if not s:
        return ""
    # Prefixed form, allowing an optional version suffix (v1, v7, ...).
    m = re.search(
        r"\barXiv:?\s*([a-z\-]+/\d{7}|\d{4}\.\d{4,5}(?:v\d+)?)\b", s, re.IGNORECASE
    )
    if m:
        return m.group(1).lower()
    # Bare new-style id (versioned or not).
    m = re.search(r"\b(\d{4}\.\d{4,5}(?:v\d+)?)\b", s)
    return m.group(1) if m else ""


def extract_pmid(s: str) -> str:
    """Pull a PMID (PubMed ID). Format: 'PMID: 12345678' or 'PMID 12345678'."""
    if not s:
        return ""
    m = re.search(r"\bPMID:?\s*(\d{1,9})\b", s, re.IGNORECASE)
    return m.group(1) if m else ""


def extract_year(s: str) -> int | None:
    """Find a 4-digit year between 1600 and 2099.

    Prefers parenthesized years '(YYYY)' over bare digits, because a bare
    4-digit number is often a page number or volume identifier.
    """
    if not s:
        return None
    # Parenthesized year first ('(2025)' or '(2025, March 11)').
    m = re.search(r"\((1[6-9]\d{2}|20\d{2})\b", s)
    if m:
        return int(m.group(1))
    # AMA pattern: 'Year;Vol(Issue):Pages.' -- year before semicolon.
    m = re.search(r"\b(1[6-9]\d{2}|20\d{2})\s*;", s)
    if m:
        return int(m.group(1))
    # Bare 4-digit year as last resort.
    m = re.search(r"\b(1[6-9]\d{2}|20\d{2})\b", s)
    return int(m.group(1)) if m else None


def initial_of(given: str) -> str:
    """'Ashish' -> 'A'. Handles 'A.' and 'A. K.' too."""
    g = given.strip()
    return g[0].upper() if g else ""


# Markers that strongly signal institutional/corporate authorship.
_INSTITUTIONAL_MARKERS = {
    "bureau", "centers", "center", "centre", "department", "institute",
    "foundation", "association", "initiative", "project", "organization",
    "organisation", "commission", "office", "agency", "council", "society",
    "group", "network", "hospital", "university", "college", "ministry",
    "service", "authority", "administration", "federation", "union", "trust",
    "corporation", "company", "academy", "school", "consortium", "task",
    "force", "committee", "panel", "alliance", "league", "register",
    "registry", "board", "court", "government", "republic",
    "states", "kingdom", "nations",
}

_CONNECTIVES = {"of", "for", "and", "the", "&", "on", "in", "to"}


def is_institutional_author(s: str) -> bool:
    """Detect institutional/corporate author names.

    Returns True for things like:
      - 'Bureau of Justice Statistics'
      - 'Centers for Disease Control and Prevention'
      - 'World Health Organization'
      - 'Prison Policy Initiative'
      - 'U.S. Department of Justice'
      - 'American Heart Association'

    Returns False for personal names like:
      - 'Smith, J.'
      - 'Smith J'
      - 'Smith and Jones'
      - 'Smith Jr.'

    Heuristic combines:
      1. Presence of known institution markers (Bureau/Centers/Institute/etc.)
      2. Structural shape: 3+ words, no comma+initial pattern, mostly
         capitalized words connected by short lowercase function words.
    """
    s = s.strip().rstrip(".,;")
    if not s:
        return False
    # A comma followed by initials ('Smith, J.') is a strong personal-author signal.
    if re.search(r",\s*[A-Z]\.", s):
        return False
    # AMA-style comma separator without initial periods ('Smith J, Jones K').
    # Pattern: <comma>+<space>+<Surname-shaped word>+<space>+<initials>
    if re.search(r",\s+[A-Z][a-z\-']+\s+[A-Z]{1,4}\b", s):
        return False
    # AMA-style trailing initial group ('Smith J' or 'Doudna JA') -- the
    # whole string looks like 'Surname Initials' with no other content.
    if re.fullmatch(r"[A-Z][a-z\-']+\s+[A-Z]{1,4}", s):
        return False
    # Known marker word anywhere in the name.
    words = [w.rstrip(".,;").lower() for w in s.split()]
    if any(w in _INSTITUTIONAL_MARKERS for w in words):
        return True
    # Structural heuristic: 3+ words, every word either capitalized or a
    # short lowercase connective. Single capitalized word ('Smith') is NOT
    # institutional. Two capitalized words like 'John Smith' is NOT either
    # because it has no connectives.
    raw_words = s.split()
    if len(raw_words) < 3:
        return False
    cap_count = sum(1 for w in raw_words if w[:1].isupper())
    conn_count = sum(1 for w in raw_words if w.lower() in _CONNECTIVES)
    if cap_count >= 3 and (cap_count + conn_count) >= len(raw_words) - 1:
        return True
    return False


def normalize_institutional_name(s: str) -> str:
    """Aggressive normalization of an institutional name for matching.

    'Bureau of Justice Statistics' -> 'bureauofjusticestatistics'
    'U.S. Department of Justice' -> 'usdepartmentofjustice'
    Drops connectives so 'Centers for Disease Control' matches 'Centers Disease Control'.
    """
    s = strip_accents(s).lower()
    s = re.sub(r"[^a-z\s]+", "", s)
    tokens = [t for t in s.split() if t and t not in _CONNECTIVES]
    return "".join(tokens)


def _journal_tokens(s: str) -> List[str]:
    """Tokenize a journal name for abbreviation comparison: lowercase, strip
    periods, drop connective stopwords ('of', 'the', 'and', '&')."""
    s = strip_accents(s).lower()
    raw = re.findall(r"[a-z]+", s)
    drop = {"of", "the", "and", "for", "in", "on", "a", "an"}
    return [t for t in raw if t not in drop]


def journal_abbreviation_match(cited: str, canonical: str) -> bool:
    """True if `cited` is plausibly an abbreviation of `canonical`.

    AMA / Vancouver styles abbreviate journal names ('J. Am. Med. Inform.
    Assoc.') while CrossRef / OpenAlex return the full name ('Journal of the
    American Medical Informatics Association'). These should be treated as the
    same journal, not flagged as an error.

    Algorithm: tokenize both (dropping connectives). Walk the canonical tokens
    in order; for each cited token, find the next canonical token that it is a
    prefix of. If every cited token is consumed this way, it's an abbreviation
    match. We also accept the reverse (canonical abbreviated vs cited full).
    """
    if not cited or not canonical:
        return False
    a = _journal_tokens(cited)
    b = _journal_tokens(canonical)
    if not a or not b:
        return False

    def _prefix_subsequence(short: List[str], long: List[str]) -> bool:
        # Every token in `short` must be a prefix of a token in `long`, in order.
        j = 0
        for tok in short:
            matched = False
            while j < len(long):
                if long[j].startswith(tok) or tok.startswith(long[j]):
                    matched = True
                    j += 1
                    break
                j += 1
            if not matched:
                return False
        return True

    # Try cited-abbreviates-canonical and the reverse.
    return _prefix_subsequence(a, b) or _prefix_subsequence(b, a)


def author_surname_overlap(a: List, b: List) -> float:
    """Fraction of surnames in the smaller list that match the larger list.

    Both inputs are List[Author]. Matching is on normalized surname only,
    because given names are often abbreviated and unreliable.
    """
    if not a or not b:
        return 0.0
    sa = {normalize_author_surname(au.family) for au in a if au.family}
    sb = {normalize_author_surname(au.family) for au in b if au.family}
    if not sa or not sb:
        return 0.0
    inter = sa & sb
    return len(inter) / min(len(sa), len(sb))
