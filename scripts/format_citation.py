"""Format a canonical record into each supported citation style.

We render seven styles. Each style has its own quirks; the canonical
references for the rules are:

  * APA 7        -- https://apastyle.apa.org/style-grammar-guidelines/references
  * MLA 9        -- https://style.mla.org/works-cited-a-quick-guide/
  * AMA 11       -- https://www.amamanualofstyle.com/
  * Chicago 17   -- Author-Date system, Chicago Manual of Style 17th ed.
  * Vancouver    -- ICMJE Recommendations (NLM citation style)
  * IEEE         -- IEEE Editorial Style Manual
  * Harvard      -- Common Anglo-Australian author-date variant

Important caveat: 'Harvard' is not a single canonical style. We implement the
widely taught 'Harvard (author-date)' variant used by Cite Them Right and most
UK universities. If the user's institution requires a different Harvard
flavor, the output should be manually adjusted.

This module is intentionally rule-based, not LLM-based. Citation formatting
is deterministic. An LLM is the wrong tool for what is essentially a
templating problem with locale rules.
"""
from __future__ import annotations

import re
from typing import List, Optional

from .models import CanonicalMetadata, Author, CitationType


# ---------- author list formatting ------------------------------------------

def _initials(given: str) -> str:
    """'Jane Mary' -> 'J. M.' / 'J.-M.' if hyphenated.

    Robust against given names that already contain trailing periods or
    single-letter initials (e.g. 'Aidan N.' should produce 'A. N.', not
    'A.N.' or 'AN.').
    """
    if not given:
        return ""
    # Split on whitespace, dots, AND hyphens so each name token (or its
    # initial letter) becomes one part.
    raw_parts = re.split(r"[\s\.]+", given.strip())
    out: List[str] = []
    for p in raw_parts:
        if not p:
            continue
        if "-" in p:
            out.append("-".join(b[0].upper() + "." for b in p.split("-") if b))
        else:
            out.append(p[0].upper() + ".")
    return " ".join(out)


def _no_space_initials(given: str) -> str:
    """AMA/Vancouver use 'JM' without periods or spaces."""
    if not given:
        return ""
    parts = re.split(r"[\s\.\-]+", given.strip())
    return "".join(p[0].upper() for p in parts if p)


def _apa_authors(authors: List[Author]) -> str:
    """APA 7: 'Surname, F. M., Surname, G., & Surname, H.' up to 20; truncate after."""
    if not authors:
        return ""
    def one(a: Author) -> str:
        return f"{a.family}, {_initials(a.given)}".strip().rstrip(",")
    if len(authors) <= 20:
        names = [one(a) for a in authors if a.family]
        if len(names) > 1:
            return ", ".join(names[:-1]) + ", & " + names[-1]
        return names[0] if names else ""
    # APA 7: list first 19, then '...', then final author
    first_19 = [one(a) for a in authors[:19] if a.family]
    last = one(authors[-1])
    return ", ".join(first_19) + ", ... " + last


def _mla_authors(authors: List[Author]) -> str:
    """MLA 9: 'Surname, First Middle' for first author; 'First Middle Surname' for rest.
    More than 2 authors -> 'Surname, First, et al.'"""
    if not authors:
        return ""
    a0 = authors[0]
    first = f"{a0.family}, {a0.given}".strip().rstrip(",")
    if len(authors) == 1:
        return first
    if len(authors) == 2:
        a1 = authors[1]
        return f"{first}, and {a1.given} {a1.family}".strip()
    return f"{first}, et al."


def _ama_authors(authors: List[Author]) -> str:
    """AMA 11: 'Surname FM' (no periods on initials). >6 authors -> first 3, et al."""
    if not authors:
        return ""
    def one(a: Author) -> str:
        ini = _no_space_initials(a.given)
        return f"{a.family} {ini}".strip()
    names = [one(a) for a in authors if a.family]
    if len(names) > 6:
        return ", ".join(names[:3]) + ", et al"
    return ", ".join(names)


def _chicago_authors(authors: List[Author]) -> str:
    """Chicago author-date: 'Surname, First Middle, First Middle Surname, ...' """
    if not authors:
        return ""
    a0 = authors[0]
    parts = [f"{a0.family}, {a0.given}".strip().rstrip(",")]
    for a in authors[1:]:
        parts.append(f"{a.given} {a.family}".strip())
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return f"{parts[0]}, and {parts[1]}"
    return ", ".join(parts[:-1]) + ", and " + parts[-1]


def _vancouver_authors(authors: List[Author]) -> str:
    """Vancouver (ICMJE/NLM): 'Surname FM, Surname GH'. Up to 6 then 'et al.'"""
    return _ama_authors(authors)  # same convention


def _ieee_authors(authors: List[Author]) -> str:
    """IEEE: 'F. M. Surname, G. H. Surname, and I. Surname'."""
    if not authors:
        return ""
    def one(a: Author) -> str:
        return f"{_initials(a.given)} {a.family}".strip()
    names = [one(a) for a in authors if a.family]
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    if len(names) > 6:
        return f"{names[0]} et al."
    return ", ".join(names[:-1]) + ", and " + names[-1]


def _harvard_authors(authors: List[Author]) -> str:
    """Harvard (Cite Them Right): 'Surname, F.M., Surname, G.H. and Surname, I.J.'"""
    if not authors:
        return ""
    def one(a: Author) -> str:
        return f"{a.family}, {_initials(a.given)}".strip().rstrip(",")
    names = [one(a) for a in authors if a.family]
    if len(names) > 3:
        return f"{names[0]} et al."
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


# ---------- title casing ----------------------------------------------------

_LOWER_WORDS = {
    "a", "an", "the", "and", "but", "or", "for", "nor", "on", "at", "to",
    "from", "by", "of", "in", "with", "as", "is", "vs", "vs.",
}


def _title_case(s: str) -> str:
    """Approximate MLA/Chicago title case. First word capitalized; minor words lowercase
    unless they are at the start or end."""
    if not s:
        return ""
    words = s.split()
    out: List[str] = []
    for i, w in enumerate(words):
        bare = re.sub(r"[^A-Za-z]", "", w).lower()
        if 0 < i < len(words) - 1 and bare in _LOWER_WORDS:
            out.append(w.lower())
        else:
            # Capitalize first letter while preserving any internal capitals (e.g. DNA)
            out.append(w[0].upper() + w[1:] if w else w)
    return " ".join(out)


def _sentence_case(s: str) -> str:
    """Approximate APA sentence case while preserving embedded acronyms.

    Words are split on hyphens so compounds like 'dual-RNA-guided' are
    processed segment-by-segment. A segment is preserved verbatim if it is
    2+ letters all uppercase (RNA, DNA, CRISPR, MRI, etc.); otherwise it is
    lowercased -- except the very first segment of the first word, which is
    title-cased.
    """
    if not s:
        return ""

    def _is_acronym(segment: str) -> bool:
        letters = re.sub(r"[^A-Za-z]", "", segment)
        return len(letters) >= 2 and letters.isupper()

    def _transform_segment(seg: str, is_first: bool) -> str:
        if not seg:
            return seg
        if _is_acronym(seg):
            return seg
        if is_first:
            return seg[:1].upper() + seg[1:].lower()
        return seg.lower()

    def _transform_word(word: str, word_idx: int) -> str:
        # Split on hyphen-minus, en-dash, em-dash, slash -- any of these can
        # separate segments in a scientific compound title (e.g. 'Dual-RNA-Guided',
        # 'Dual-RNA–Guided', 'in vivo/in vitro').
        parts = re.split(r"([\-–—/])", word)
        out = []
        seg_idx = 0
        for p in parts:
            if p in ("-", "–", "—", "/"):
                out.append(p)
                continue
            is_first = (word_idx == 0 and seg_idx == 0)
            out.append(_transform_segment(p, is_first))
            seg_idx += 1
        return "".join(out)

    words = s.split()
    return " ".join(_transform_word(w, i) for i, w in enumerate(words))


# ---------- pages utility ---------------------------------------------------

def _en_dash_pages(pages: str) -> str:
    return pages.replace("-", "–") if pages else pages


# ---------- per-style renderers --------------------------------------------

def _clean(s: str) -> str:
    """Strip dangling separators left behind when optional fields are empty."""
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r",\s*\.", ".", s)        # ", ." -> "."
    s = re.sub(r",\s*,", ",", s)         # ", ," -> ","
    s = re.sub(r":\s*\.", ".", s)        # ": ." -> "."
    s = re.sub(r";\s*\.", ".", s)        # "; ." -> "."
    s = re.sub(r"\s+([,.;:])", r"\1", s) # " ." -> "."
    s = re.sub(r"\.\.+", ".", s)         # collapse ".."
    s = re.sub(r"\(\s*\)", "", s)        # remove empty parens
    return s.strip()


def _apa(c: CanonicalMetadata) -> str:
    """APA 7th edition."""
    authors = _apa_authors(c.authors)
    year = f"({c.year})." if c.year else "(n.d.)."
    title = _sentence_case(c.title).rstrip(".") + "." if c.title else ""
    if c.type in (CitationType.JOURNAL_ARTICLE, CitationType.PREPRINT):
        venue = f"*{c.container_title}*" if c.container_title else ""
        vol_issue = c.volume + (f"({c.issue})" if c.issue else "")
        pages = _en_dash_pages(c.pages)
        # Assemble the post-title segment piecewise, joining with commas only
        # between non-empty parts.
        post = ", ".join(p for p in [venue, vol_issue, pages] if p)
        doi_tail = ""
        if c.doi:
            doi_tail = f" https://doi.org/{c.doi}"
        elif c.source == "openalex" and c.source_url:
            doi_tail = f" {c.source_url}"
        out = f"{authors} {year} {title} {post}.{doi_tail}"
        return _clean(out)
    if c.type == CitationType.BOOK:
        return _clean(f"{authors} {year} {title} {c.publisher}.")
    if c.type == CitationType.CONFERENCE_PAPER:
        venue = f"*{c.container_title}*" if c.container_title else ""
        return _clean(f"{authors} {year} {title} {venue}.")
    return _clean(f"{authors} {year} {title} {c.container_title or c.publisher or ''}")


def _mla(c: CanonicalMetadata) -> str:
    """MLA 9th edition."""
    authors = _mla_authors(c.authors)
    title = f'"{_title_case(c.title)}."' if c.title else ""
    if c.type in (CitationType.JOURNAL_ARTICLE, CitationType.PREPRINT):
        venue = f"*{_title_case(c.container_title)}*" if c.container_title else ""
        vol = f"vol. {c.volume}" if c.volume else ""
        issue = f"no. {c.issue}" if c.issue else ""
        year = str(c.year) if c.year else ""
        pages = f"pp. {_en_dash_pages(c.pages)}" if c.pages else ""
        parts = [authors + "." if authors else "", title, venue]
        rest = ", ".join(p for p in [vol, issue, year, pages] if p)
        if rest:
            parts.append(rest + ".")
        out = " ".join(p for p in parts if p)
        if c.doi:
            out += f" doi:{c.doi}."
        return _clean(out)
    if c.type == CitationType.BOOK:
        return _clean(f"{authors}. {_title_case(c.title)}. {c.publisher}, {c.year or ''}.")
    return _clean(f"{authors}. {title} {c.container_title or c.publisher or ''} {c.year or ''}.")


def _ama(c: CanonicalMetadata) -> str:
    """AMA 11th edition."""
    authors = _ama_authors(c.authors)
    title = _sentence_case(c.title).rstrip(".") + "." if c.title else ""
    if c.type in (CitationType.JOURNAL_ARTICLE, CitationType.PREPRINT):
        venue = f"*{c.container_title}*" if c.container_title else ""
        year = c.year or ""
        vol_issue = (c.volume or "") + (f"({c.issue})" if c.issue else "")
        pages = _en_dash_pages(c.pages) or ""
        tail_parts = [str(year), vol_issue, pages]
        # AMA uses semicolons then colons: 'Year;Volume(Issue):Pages.'
        if year and vol_issue and pages:
            tail = f"{year};{vol_issue}:{pages}."
        elif year and vol_issue:
            tail = f"{year};{vol_issue}."
        elif year:
            tail = f"{year}."
        else:
            tail = ""
        out = " ".join(p for p in [f"{authors}.", title, venue + "." if venue else "", tail] if p)
        if c.doi:
            out += f" doi:{c.doi}"
        return _clean(out)
    if c.type == CitationType.BOOK:
        return _clean(f"{authors}. *{title}* {c.publisher}; {c.year or ''}.")
    return _clean(f"{authors}. {title} {c.container_title or c.publisher or ''}. {c.year or ''}.")


def _chicago(c: CanonicalMetadata) -> str:
    """Chicago 17, author-date system."""
    authors = _chicago_authors(c.authors)
    year = str(c.year) if c.year else "n.d."
    title = f'"{_title_case(c.title)}."' if c.title else ""
    if c.type in (CitationType.JOURNAL_ARTICLE, CitationType.PREPRINT):
        venue = f"*{_title_case(c.container_title)}*" if c.container_title else ""
        vol = c.volume or ""
        issue = f", no. {c.issue}" if c.issue else ""
        pages = f": {_en_dash_pages(c.pages)}" if c.pages else ""
        biblio = f"{vol}{issue}{pages}".strip()
        out = " ".join(p for p in [f"{authors}.", f"{year}.", title, venue, biblio] if p).rstrip(",") + "."
        if c.doi:
            out += f" https://doi.org/{c.doi}."
        return _clean(out)
    if c.type == CitationType.BOOK:
        return _clean(f"{authors}. {year}. *{_title_case(c.title)}*. {c.publisher}.")
    return _clean(f"{authors}. {year}. {title} {c.container_title or c.publisher or ''}.")


def _vancouver(c: CanonicalMetadata) -> str:
    """Vancouver (ICMJE / NLM). Used in medicine, e.g. NEJM, Lancet."""
    authors = _vancouver_authors(c.authors)
    title = (c.title.rstrip(".") + ".") if c.title else ""
    if c.type in (CitationType.JOURNAL_ARTICLE, CitationType.PREPRINT):
        venue = c.container_title or ""
        year = c.year or ""
        vol_issue = (c.volume or "") + (f"({c.issue})" if c.issue else "")
        pages = _en_dash_pages(c.pages) or ""
        if year and vol_issue and pages:
            tail = f"{year};{vol_issue}:{pages}."
        elif year and vol_issue:
            tail = f"{year};{vol_issue}."
        elif year:
            tail = f"{year}."
        else:
            tail = ""
        out = " ".join(p for p in [f"{authors}.", title, (venue + "." if venue else ""), tail] if p)
        if c.doi:
            out += f" doi:{c.doi}"
        return _clean(out)
    if c.type == CitationType.BOOK:
        return _clean(f"{authors}. {title} {c.publisher}; {c.year or ''}.")
    return _clean(f"{authors}. {title} {c.container_title or c.publisher or ''}. {c.year or ''}.")


def _ieee(c: CanonicalMetadata, index: Optional[int] = None) -> str:
    """IEEE numeric style. If `index` is provided, prepend '[N]'."""
    authors = _ieee_authors(c.authors)
    title = f'"{_sentence_case(c.title)},"' if c.title else ""
    prefix = f"[{index}] " if index is not None else ""
    if c.type in (CitationType.JOURNAL_ARTICLE, CitationType.PREPRINT):
        venue = f"*{_title_case(c.container_title)}*" if c.container_title else ""
        vol = f"vol. {c.volume}" if c.volume else ""
        issue = f"no. {c.issue}" if c.issue else ""
        pages = f"pp. {_en_dash_pages(c.pages)}" if c.pages else ""
        year = str(c.year) if c.year else ""
        tail = ", ".join(p for p in [vol, issue, pages, year] if p)
        parts = [f"{prefix}{authors},", title, venue + ("," if tail else "") if venue else "", tail]
        out = " ".join(p for p in parts if p).rstrip(",") + "."
        if c.doi:
            out += f" doi: {c.doi}."
        return _clean(out)
    if c.type == CitationType.BOOK:
        return _clean(f"{prefix}{authors}, *{_title_case(c.title)}*. {c.publisher}, {c.year or ''}.")
    return _clean(f"{prefix}{authors}, {title} {c.container_title or c.publisher or ''}, {c.year or ''}.")


def _harvard(c: CanonicalMetadata) -> str:
    """Harvard (Cite Them Right). Closely related to APA but with different
    punctuation conventions."""
    authors = _harvard_authors(c.authors)
    year = f"({c.year})" if c.year else "(no date)"
    title = f"'{_sentence_case(c.title).rstrip('.')}'," if c.title else ""
    if c.type in (CitationType.JOURNAL_ARTICLE, CitationType.PREPRINT):
        venue = f"*{_title_case(c.container_title)}*" if c.container_title else ""
        vol = c.volume or ""
        issue = f"({c.issue})" if c.issue else ""
        pages = f", pp. {_en_dash_pages(c.pages)}" if c.pages else ""
        vol_issue_pages = f"{vol}{issue}{pages}".strip(", ")
        parts = [authors, year, title, venue + ("," if vol_issue_pages else "") if venue else "", vol_issue_pages]
        out = " ".join(p for p in parts if p).rstrip(",") + "."
        if c.doi:
            out += f" doi: {c.doi}."
        return _clean(out)
    if c.type == CitationType.BOOK:
        return _clean(f"{authors} {year} *{_title_case(c.title)}*. {c.publisher}.")
    return _clean(f"{authors} {year} {title} {c.container_title or c.publisher or ''}.")


# ---------- in-text forms ---------------------------------------------------

def _intext_authoryear(c: CanonicalMetadata, page: Optional[str] = None) -> str:
    """APA / Chicago / Harvard in-text form."""
    if not c.authors:
        return f"({c.year})" if c.year else ""
    surnames = [a.family for a in c.authors if a.family]
    year = c.year or "n.d."
    pg = f", p. {page}" if page else ""
    if len(surnames) == 1:
        return f"({surnames[0]}, {year}{pg})"
    if len(surnames) == 2:
        return f"({surnames[0]} & {surnames[1]}, {year}{pg})"
    return f"({surnames[0]} et al., {year}{pg})"


def _intext_mla(c: CanonicalMetadata, page: Optional[str] = None) -> str:
    if not c.authors:
        return f"({page})" if page else ""
    s = [a.family for a in c.authors if a.family]
    pg = f" {page}" if page else ""
    if len(s) == 1:
        return f"({s[0]}{pg})"
    if len(s) == 2:
        return f"({s[0]} and {s[1]}{pg})"
    return f"({s[0]} et al.{pg})"


def _intext_numeric(index: int, page: Optional[str] = None) -> str:
    return f"[{index}, p. {page}]" if page else f"[{index}]"


# ---------- public API ------------------------------------------------------

SUPPORTED_STYLES = ["apa", "mla", "ama", "chicago", "vancouver", "ieee", "harvard"]


def _infer_type(c: CanonicalMetadata) -> CitationType:
    """If the API tagged a record as UNKNOWN but the fields say 'journal article',
    treat it as one. This guards against API type-field oddities."""
    if c.type and c.type != CitationType.UNKNOWN:
        return c.type
    if c.container_title and (c.volume or c.pages or c.doi or c.issue):
        return CitationType.JOURNAL_ARTICLE
    if c.arxiv_id:
        return CitationType.PREPRINT
    if c.publisher and not c.container_title:
        return CitationType.BOOK
    return c.type or CitationType.UNKNOWN


def format_reference(c: CanonicalMetadata, style: str, index: Optional[int] = None) -> str:
    """Render a single reference in the requested style.

    `index` is only used by numeric styles (IEEE, Vancouver, AMA when numbered).
    """
    # Normalize the type before dispatch so all formatters get a plausible category.
    c.type = _infer_type(c)
    s = style.lower()
    if s == "apa":
        return _apa(c)
    if s == "mla":
        return _mla(c)
    if s == "ama":
        # AMA traditionally uses superscript numbers; we still emit the full reference,
        # the calling code can prefix [N] if it wants.
        prefix = f"{index}. " if index is not None else ""
        return prefix + _ama(c)
    if s == "chicago":
        return _chicago(c)
    if s == "vancouver":
        prefix = f"{index}. " if index is not None else ""
        return prefix + _vancouver(c)
    if s == "ieee":
        return _ieee(c, index=index)
    if s == "harvard":
        return _harvard(c)
    raise ValueError(f"Unsupported style: {style}. Supported: {SUPPORTED_STYLES}")


def format_intext(c: CanonicalMetadata, style: str, index: Optional[int] = None,
                  page: Optional[str] = None) -> str:
    s = style.lower()
    if s in ("apa", "chicago", "harvard"):
        return _intext_authoryear(c, page=page)
    if s == "mla":
        return _intext_mla(c, page=page)
    if s in ("ama", "vancouver", "ieee"):
        return _intext_numeric(index or 0, page=page)
    raise ValueError(f"Unsupported style: {style}")


def to_bibtex(c: CanonicalMetadata) -> str:
    """Render canonical metadata as a BibTeX entry."""
    if not c:
        return ""
    etype = {
        CitationType.JOURNAL_ARTICLE: "article",
        CitationType.BOOK: "book",
        CitationType.BOOK_CHAPTER: "incollection",
        CitationType.CONFERENCE_PAPER: "inproceedings",
        CitationType.PREPRINT: "misc",
        CitationType.REPORT: "techreport",
        CitationType.THESIS: "phdthesis",
        CitationType.WEBSITE: "misc",
    }.get(c.type, "misc")
    first_surname = (c.authors[0].family if c.authors else "Anon").lower()
    key = re.sub(r"[^a-z0-9]+", "", first_surname) + str(c.year or "nd")
    lines = [f"@{etype}{{{key},"]
    if c.authors:
        lines.append(f"  author = {{{ ' and '.join(a.full for a in c.authors) }}},")
    if c.title:
        lines.append(f"  title = {{{c.title}}},")
    if c.container_title:
        field = "booktitle" if etype in ("inproceedings", "incollection") else "journal"
        lines.append(f"  {field} = {{{c.container_title}}},")
    if c.year:
        lines.append(f"  year = {{{c.year}}},")
    if c.volume:
        lines.append(f"  volume = {{{c.volume}}},")
    if c.issue:
        lines.append(f"  number = {{{c.issue}}},")
    if c.pages:
        lines.append(f"  pages = {{{c.pages}}},")
    if c.publisher:
        lines.append(f"  publisher = {{{c.publisher}}},")
    if c.doi:
        lines.append(f"  doi = {{{c.doi}}},")
    if c.arxiv_id:
        lines.append(f"  eprint = {{{c.arxiv_id}}},")
        lines.append("  archivePrefix = {arXiv},")
    if c.pmid:
        lines.append(f"  pmid = {{{c.pmid}}},")
    lines.append("}")
    return "\n".join(lines)
