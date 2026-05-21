"""In-text citation vs bibliography cross-reference.

When a user gives us a full paper (not just a bare bibliography), we extract
both the body's in-text citations and the references list. This module
matches them and reports two failure modes that are extremely common in
LLM-drafted papers:

  * ORPHAN -- the body text cites '(Smith et al., 2019)' but no Smith 2019
    appears in the bibliography. The user has written about a paper they
    forgot to list, OR the LLM made up the in-text citation.

  * UNUSED -- the bibliography contains an entry that no in-text citation
    ever references. Either the user dropped the cite from the body or the
    LLM padded the bibliography with sources it didn't use.

Both are catchable mechanically once we parse each in-text citation into a
structured form and match against bibliography entries.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Tuple, Optional, Dict

from .models import ParsedReference
from .normalize import (
    normalize_author_surname, is_institutional_author, normalize_institutional_name,
)


@dataclass
class InTextCitation:
    """A parsed in-text citation. Either author-year (personal or institutional) OR numeric."""
    raw: str
    surnames: List[str]      # lowercase, normalized; empty for numeric citations
    year: Optional[int]
    indices: List[int]       # numeric references, e.g. [3] -> [3]
    institutional: str = ""  # normalized institution key, if this is an institutional citation


@dataclass
class CrossCheckResult:
    orphan_intext: List[InTextCitation]      # cited in body, not in bibliography
    unused_bibliography: List[int]            # bibliography indices never cited (1-based)
    matched: Dict[str, List[int]]             # surnameYYYY -> list of bib indices it resolved to


_NUMERIC_RE = re.compile(r"\[(\d{1,3}(?:\s*[,\-–]\s*\d{1,3})*)\]")
_YEAR_RE = re.compile(r"\b(\d{4})[a-z]?\b")
# A surname-looking word. Allows hyphens, apostrophes, accented characters.
_SURNAME_RE = re.compile(r"\b[A-Z][A-Za-z\-'À-ÖØ-öø-ÿ]{1,40}\b")
# Words we should NOT treat as surnames even though they're capitalized at the
# start of a citation (mostly noise from text bleed-in).
_SURNAME_STOPWORDS = {"et", "al", "and", "the", "in", "see", "cf"}


def parse_intext(raw: str) -> Optional[InTextCitation]:
    """Parse a raw in-text citation string into structured form.

    Strategy: find the year first, then collect capitalized surnames that
    appear before the year. Robust to the many variants of author-year
    formatting (parenthetical vs narrative, 'and' vs '&', 'et al.' suffix).

    Handles:
      * '(Smith, 2017)'            -> surnames=['smith'], year=2017
      * '(Smith & Jones, 2017)'    -> surnames=['smith', 'jones'], year=2017
      * '(Smith et al., 2017)'     -> surnames=['smith'], year=2017
      * 'Smith (2017)'             -> surnames=['smith'], year=2017
      * 'Smith et al. (2017)'      -> surnames=['smith'], year=2017
      * 'Patel and Sharma (2022)'  -> surnames=['patel', 'sharma'], year=2022
      * '[3]'                      -> indices=[3]
      * '[1, 2, 4]'                -> indices=[1, 2, 4]
      * '[1-3]'                    -> indices=[1, 2, 3]
    """
    raw = raw.strip()
    if not raw:
        return None

    # Numeric first.
    m = _NUMERIC_RE.search(raw)
    if m:
        indices: List[int] = []
        for chunk in re.split(r"\s*,\s*", m.group(1)):
            if "-" in chunk or "–" in chunk:
                parts = re.split(r"\s*[-–]\s*", chunk, maxsplit=1)
                if len(parts) != 2:
                    continue
                a, b = parts
                try:
                    indices.extend(range(int(a.strip()), int(b.strip()) + 1))
                except ValueError:
                    pass
            else:
                try:
                    indices.append(int(chunk.strip()))
                except ValueError:
                    pass
        return InTextCitation(raw=raw, surnames=[], year=None, indices=indices)

    # Author-year: find the year first, then take all surname-looking words
    # to its left as candidate authors.
    ym = _YEAR_RE.search(raw)
    if not ym:
        return None
    year = int(ym.group(1))
    before = raw[: ym.start()].strip(" (,")

    # If the text before the year looks like an institutional name (e.g.
    # 'Bureau of Justice Statistics'), record it as an institutional citation
    # instead of trying to coerce it into personal surnames.
    if is_institutional_author(before):
        return InTextCitation(
            raw=raw,
            surnames=[],
            year=year,
            indices=[],
            institutional=normalize_institutional_name(before),
        )

    surnames: List[str] = []
    for sm in _SURNAME_RE.finditer(before):
        word = sm.group(0)
        if word.lower() in _SURNAME_STOPWORDS:
            continue
        surnames.append(normalize_author_surname(word))
    if not surnames:
        return None
    return InTextCitation(raw=raw, surnames=surnames, year=year, indices=[])


def _bibliography_keys(
    refs: List[ParsedReference],
) -> Tuple[List[List[Tuple[str, int]]], List[List[Tuple[str, int]]]]:
    """For each reference, return (personal_keys, institutional_keys).

    personal_keys: list of (normalized_surname, year) for each personal author.
    institutional_keys: list of (normalized_institution_name, year).

    A reference may have either personal authors OR an institutional author --
    rarely both. Caller uses whichever matches the in-text citation type.
    """
    personal_out: List[List[Tuple[str, int]]] = []
    institutional_out: List[List[Tuple[str, int]]] = []
    for r in refs:
        p_keys: List[Tuple[str, int]] = []
        i_keys: List[Tuple[str, int]] = []
        if r.year and r.authors:
            for a in r.authors:
                if not a.family:
                    continue
                # Distinguish institutional from personal authors. The parser
                # marks institutional authors by storing the full name in
                # `family` with `given` empty.
                if not a.given and is_institutional_author(a.family):
                    i_keys.append((normalize_institutional_name(a.family), r.year))
                else:
                    p_keys.append((normalize_author_surname(a.family), r.year))
        personal_out.append(p_keys)
        institutional_out.append(i_keys)
    return personal_out, institutional_out


def cross_check(
    bibliography: List[ParsedReference],
    intext_raw: List[str],
) -> CrossCheckResult:
    """Run the cross-check.

    Numeric citations match by index (1-based).
    Personal author-year citations match by (surname, year).
    Institutional author-year citations match by (normalized_institution_name, year).
    """
    parsed_intext = [c for c in (parse_intext(r) for r in intext_raw) if c is not None]
    p_keys_per_ref, i_keys_per_ref = _bibliography_keys(bibliography)

    # Reverse indexes for each citation style.
    sn_year_to_idx: Dict[Tuple[str, int], List[int]] = {}
    for i, keys in enumerate(p_keys_per_ref, start=1):
        for k in keys:
            sn_year_to_idx.setdefault(k, []).append(i)
    inst_year_to_idx: Dict[Tuple[str, int], List[int]] = {}
    for i, keys in enumerate(i_keys_per_ref, start=1):
        for k in keys:
            inst_year_to_idx.setdefault(k, []).append(i)

    used: set = set()
    matched: Dict[str, List[int]] = {}
    orphans: List[InTextCitation] = []

    for c in parsed_intext:
        hit_idxs: List[int] = []
        if c.indices:
            for n in c.indices:
                if 1 <= n <= len(bibliography):
                    hit_idxs.append(n)
        elif c.institutional and c.year:
            key = (c.institutional, c.year)
            if key in inst_year_to_idx:
                hit_idxs.extend(inst_year_to_idx[key])
            else:
                # Fall back: match if the institutional key is a substring or
                # superset (handles 'CDC' vs 'Centers for Disease Control').
                for k, idxs in inst_year_to_idx.items():
                    if k[1] != c.year:
                        continue
                    if c.institutional in k[0] or k[0] in c.institutional:
                        hit_idxs.extend(idxs)
                        break
        elif c.year and c.surnames:
            for sn in c.surnames:
                key = (sn, c.year)
                if key in sn_year_to_idx:
                    hit_idxs.extend(sn_year_to_idx[key])
        if hit_idxs:
            matched[c.raw] = sorted(set(hit_idxs))
            used.update(hit_idxs)
        else:
            orphans.append(c)

    unused = [i for i in range(1, len(bibliography) + 1) if i not in used]

    if not parsed_intext:
        return CrossCheckResult(orphan_intext=[], unused_bibliography=[], matched={})

    return CrossCheckResult(
        orphan_intext=orphans,
        unused_bibliography=unused,
        matched=matched,
    )
