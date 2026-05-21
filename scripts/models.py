"""Data models used across the pipeline.

Everything is plain dataclasses so it survives JSON round-trips and is easy
to inspect from a debugger. No business logic lives here.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import List, Optional, Dict, Any
from enum import Enum


class CitationType(str, Enum):
    JOURNAL_ARTICLE = "journal-article"
    BOOK = "book"
    BOOK_CHAPTER = "book-chapter"
    CONFERENCE_PAPER = "proceedings-article"
    PREPRINT = "posted-content"
    REPORT = "report"
    WEBSITE = "webpage"
    DATASET = "dataset"
    THESIS = "thesis"
    UNKNOWN = "unknown"


class Confidence(str, Enum):
    """User-facing confidence tiers.

    Three primary tiers (VERIFIED / PARTIAL / HALLUCINATED) with two sub-flavors
    of VERIFIED for entries that are real but need annotation:

      * VERIFIED_WITH_STYLE_EDITS -- substantive content correct, cosmetic
        polish suggested for strict style conformity.
      * VERIFIED_VIA_URL -- not in any academic database (no DOI/PMID/arXiv),
        but the cited URL resolves to a real page. Common for government
        reports, NGO publications, agency websites, and other grey literature
        that legitimately exists but isn't indexed in CrossRef / OpenAlex /
        PubMed / Semantic Scholar / arXiv. This is a real source, not a
        fabrication.
    """
    VERIFIED = "verified"                                    # paper exists, every substantive field correct
    VERIFIED_WITH_STYLE_EDITS = "verified_with_style_edits"  # content right, cosmetic polish suggested
    VERIFIED_VIA_URL = "verified_via_url"                    # grey literature -- URL resolves, no academic record
    PARTIAL = "partial"                                      # paper exists, citation has substantive errors
    HALLUCINATED = "hallucinated"                            # no plausible match found


@dataclass
class Author:
    """A single author. `family` is the surname for matching purposes."""
    family: str = ""
    given: str = ""
    orcid: Optional[str] = None

    @property
    def full(self) -> str:
        return f"{self.given} {self.family}".strip()

    @classmethod
    def from_string(cls, s: str) -> "Author":
        """Parse 'Vaswani, Ashish', 'Ashish Vaswani', 'A. Vaswani', or 'Wade G' (AMA).

        AMA/Vancouver convention is 'Surname Initials' with no comma:
          'Wade G'           -> family='Wade', given='G'
          'Doudna JA'        -> family='Doudna', given='JA'
          'Garcia-Grossman I' -> family='Garcia-Grossman', given='I'

        APA convention is 'Surname, F. M.' with a comma:
          'Vaswani, A.'     -> family='Vaswani', given='A.'

        Western convention is 'Given Family' with no comma and a multi-letter
        given name:
          'Ashish Vaswani'  -> family='Vaswani', given='Ashish'
        """
        import re as _re
        s = s.strip().rstrip(",.;")
        if not s:
            return cls()
        if "," in s:
            family, _, given = s.partition(",")
            return cls(family=family.strip(), given=given.strip())
        parts = s.split()
        if len(parts) == 1:
            return cls(family=parts[0])
        # AMA / Vancouver: trailing token is short uppercase letters (initials).
        # E.g. 'Wade G', 'Doudna JA', 'Hauer M'.
        last = parts[-1].rstrip(".")
        if _re.fullmatch(r"[A-Z]{1,4}", last):
            return cls(family=" ".join(parts[:-1]), given=last)
        # Otherwise treat last token as family (western form).
        return cls(family=parts[-1], given=" ".join(parts[:-1]))


@dataclass
class ParsedReference:
    """What we extracted from the user's document, before any API check."""
    raw_text: str = ""
    authors: List[Author] = field(default_factory=list)
    title: str = ""
    year: Optional[int] = None
    container_title: str = ""   # journal / book / conference name
    volume: str = ""
    issue: str = ""
    pages: str = ""
    publisher: str = ""
    doi: str = ""
    url: str = ""
    arxiv_id: str = ""
    pmid: str = ""
    isbn: str = ""
    type: CitationType = CitationType.UNKNOWN
    detected_style: str = ""    # best guess of the style used in the source
    in_text_keys: List[str] = field(default_factory=list)  # e.g. "(Vaswani et al., 2017)"

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["type"] = self.type.value
        return d


@dataclass
class CanonicalMetadata:
    """Authoritative metadata returned by one of the verification APIs."""
    source: str = ""            # crossref | openalex | pubmed | semantic_scholar | arxiv | scholarly
    source_id: str = ""         # DOI, OpenAlex ID, PMID, arXiv ID, S2 paper ID
    source_url: str = ""        # the canonical landing URL we can cite as evidence
    authors: List[Author] = field(default_factory=list)
    title: str = ""
    year: Optional[int] = None
    container_title: str = ""
    volume: str = ""
    issue: str = ""
    pages: str = ""
    publisher: str = ""
    doi: str = ""
    arxiv_id: str = ""
    pmid: str = ""
    type: CitationType = CitationType.UNKNOWN
    raw: Dict[str, Any] = field(default_factory=dict)  # the unprocessed API response, kept for audit

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["type"] = self.type.value
        d.pop("raw", None)  # raw is huge, omit from default serialization
        return d


@dataclass
class Discrepancy:
    """A substantive content error -- changes meaning, downgrades to PARTIAL."""
    field: str
    parsed_value: str
    canonical_value: str
    severity: str  # "minor" | "major"


@dataclass
class StyleEdit:
    """A cosmetic edit -- does not change meaning, keeps tier at VERIFIED.

    `description` is the human-readable instruction (e.g. "Italicize the
    journal name 'Nature'"). `field` and the two values are kept too so
    the JSON output is machine-readable.
    """
    field: str
    original: str
    corrected: str
    description: str


@dataclass
class VerificationResult:
    """The full audit record for one reference."""
    parsed: ParsedReference
    matches: List[CanonicalMetadata] = field(default_factory=list)
    canonical: Optional[CanonicalMetadata] = None  # the chosen best match for re-formatting
    confidence: Confidence = Confidence.HALLUCINATED
    discrepancies: List[Discrepancy] = field(default_factory=list)  # substantive errors -> PARTIAL
    style_edits: List[StyleEdit] = field(default_factory=list)      # cosmetic edits -> still VERIFIED
    notes: List[str] = field(default_factory=list)  # human-readable extra context
    formatted: Dict[str, str] = field(default_factory=dict)  # style -> formatted citation

    def to_dict(self) -> Dict[str, Any]:
        return {
            "parsed": self.parsed.to_dict(),
            "matches": [m.to_dict() for m in self.matches],
            "canonical": self.canonical.to_dict() if self.canonical else None,
            "confidence": self.confidence.value,
            "discrepancies": [asdict(d) for d in self.discrepancies],
            "style_edits": [asdict(e) for e in self.style_edits],
            "notes": list(self.notes),
            "formatted": dict(self.formatted),
        }
