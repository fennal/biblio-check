"""Multi-API verification.

Strategy, in priority order:
  1. If the parsed entry has a DOI, hit CrossRef directly. Authoritative.
  2. If it has an arXiv ID, hit the arXiv API directly. Authoritative.
  3. If it has a PMID, hit PubMed ESummary directly. Authoritative.
  4. Otherwise, search by title across CrossRef + OpenAlex + Semantic Scholar
     + PubMed + arXiv, score each returned candidate by title/year/author
     fit, and pick the best. We treat agreement across two or more sources
     as 'verified' and a single-source match as 'likely'.
  5. Optional last-resort: scholarly (Google Scholar scraper). Behind a flag.

Why multi-source? In practice every single API has data quality issues. We
observed OpenAlex returning corrupted DOI/year fields on a real paper during
this skill's development. The only way to be robust is to require
independent agreement, not trust any single source.
"""
from __future__ import annotations

import os
import re
import time
import json
import logging
import hashlib
import threading
import urllib.parse
import urllib.request
import urllib.error
import datetime
from pathlib import Path
from typing import List, Optional, Dict, Any, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
import xml.etree.ElementTree as ET

from rapidfuzz import fuzz

from .models import (
    ParsedReference,
    CanonicalMetadata,
    VerificationResult,
    Confidence,
    Discrepancy,
    StyleEdit,
    Author,
    CitationType,
)
from .normalize import (
    normalize_title,
    normalize_author_surname,
    normalize_journal,
    author_surname_overlap,
    is_institutional_author,
    journal_abbreviation_match,
)

log = logging.getLogger("biblio-check.verify")

# Polite contact email surfaced in the User-Agent; CrossRef and OpenAlex both
# prefer this so they can route you to a faster pool.
DEFAULT_MAILTO = os.environ.get("BIBLIO_CHECK_MAILTO", "biblio-check@example.org")
USER_AGENT = f"biblio-check/0.1 (mailto:{DEFAULT_MAILTO})"


# ---------- persistent response cache ---------------------------------------
# Re-running the audit on the same bibliography is a common workflow (you fix
# one citation, re-run, fix another). Caching API responses to disk makes
# re-runs near-instant and dodges most Semantic Scholar rate-limit hits.

CACHE_DIR = Path(os.environ.get(
    "BIBLIO_CHECK_CACHE",
    str(Path.home() / ".cache" / "biblio-check"),
))
# Cache TTL is configurable via env var (in days). Default 7 days.
try:
    CACHE_TTL_SECONDS = int(os.environ.get("BIBLIO_CHECK_CACHE_TTL_DAYS", "7")) * 24 * 3600
except ValueError:
    CACHE_TTL_SECONDS = 7 * 24 * 3600
_CACHE_ENABLED = os.environ.get("BIBLIO_CHECK_NO_CACHE") not in ("1", "true", "yes")


# Per-API semaphores. Each verify_one runs in its own thread; without these
# we get nested concurrency (4 verify workers x 6 candidate-gather workers =
# 24 simultaneous HTTP requests, all hitting Semantic Scholar at once). The
# semaphores cap concurrent requests per API to polite levels.
_CROSSREF_SEM = threading.Semaphore(4)
_OPENALEX_SEM = threading.Semaphore(4)
_PUBMED_SEM = threading.Semaphore(3)      # NCBI suggests <=3/sec without API key
_S2_SEM = threading.Semaphore(2)           # Semantic Scholar rate-limits hard
_ARXIV_SEM = threading.Semaphore(2)
_DOIORG_SEM = threading.Semaphore(4)
_URL_SEM = threading.Semaphore(6)


# Sticky per-run disable flags. Once an API returns failures repeatedly during
# a single audit, we stop hitting it. Otherwise every subsequent reference
# pays the full retry-backoff penalty (3-7 seconds) for no result, and a 12-
# reference audit balloons to 60+ seconds even when 90% of work is cached.
_API_DISABLED: Dict[str, threading.Event] = {
    "crossref": threading.Event(),
    "openalex": threading.Event(),
    "pubmed": threading.Event(),
    "semantic_scholar": threading.Event(),
    "arxiv": threading.Event(),
}

# Failure counters per API within a run. Core authoritative APIs (CrossRef,
# OpenAlex, PubMed) are reliable; a single failure is almost always a transient
# network blip, NOT a rate-limit, so we require several failures before
# disabling them. Disabling a core API mid-run causes real papers to show as
# HALLUCINATED, which is far worse than a few wasted retries.
_API_FAILURES: Dict[str, int] = {k: 0 for k in _API_DISABLED}
_API_FAILURE_LOCK = threading.Lock()
_DISABLE_THRESHOLD = {
    "crossref": 4,        # reliable; only give up after sustained failure
    "openalex": 4,
    "pubmed": 4,
    "semantic_scholar": 1,  # rate-limits hard and persistently; bail immediately
    "arxiv": 2,
}

# Only these APIs get a PERSISTENT cross-run cooldown marker. Semantic Scholar
# rate-limits aggressively and persistently, so a cooldown genuinely helps.
# CrossRef / OpenAlex / PubMed / arXiv failures are transient and must NOT
# poison future runs -- arXiv in particular is the sole verification path for
# many preprints, so persistently disabling it causes false hallucinations.
_PERSIST_COOLDOWN_APIS = {"semantic_scholar"}

# Persistent rate-limit cooldown. When a rate-limited API gets disabled mid-run,
# we write a small marker file with timestamp. Subsequent runs within
# COOLDOWN_SECONDS skip that API up-front, saving the first-reference retry
# penalty that would otherwise hit on every fresh run.
_RATE_LIMIT_COOLDOWN_SECONDS = int(os.environ.get("BIBLIO_CHECK_RATELIMIT_COOLDOWN", "300"))


def _ratelimit_marker_path(api_name: str) -> Path:
    return CACHE_DIR / f"_ratelimit_{api_name}.marker"


def _check_persistent_cooldown(api_name: str) -> None:
    """If the API marker exists and is recent, mark API disabled for this run.
    Only the rate-limited APIs ever have markers."""
    if not _CACHE_ENABLED or api_name not in _PERSIST_COOLDOWN_APIS:
        return
    p = _ratelimit_marker_path(api_name)
    if not p.exists():
        return
    try:
        age = time.time() - p.stat().st_mtime
    except OSError:
        return
    if age < _RATE_LIMIT_COOLDOWN_SECONDS:
        log.debug("%s still in cooldown (age %.0fs); skipping for this run", api_name, age)
        _API_DISABLED[api_name].set()
    else:
        try:
            p.unlink()
        except OSError:
            pass


def _write_persistent_cooldown(api_name: str) -> None:
    if not _CACHE_ENABLED or api_name not in _PERSIST_COOLDOWN_APIS:
        return
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _ratelimit_marker_path(api_name).touch()
    except OSError:
        pass


def _api_disabled(name: str) -> bool:
    return _API_DISABLED[name].is_set()


def _api_disable(name: str) -> None:
    """Record a failure for `name`. Only actually disable the API once its
    failure count crosses the per-API threshold -- core APIs tolerate several
    transient failures before being given up on."""
    with _API_FAILURE_LOCK:
        _API_FAILURES[name] = _API_FAILURES.get(name, 0) + 1
        count = _API_FAILURES[name]
    if count >= _DISABLE_THRESHOLD.get(name, 1) and not _API_DISABLED[name].is_set():
        log.debug("Disabling %s after %d failures this run", name, count)
        _API_DISABLED[name].set()
        _write_persistent_cooldown(name)


def reset_api_disable_state() -> None:
    """Clear the per-run disable flags and failure counters. Call from
    verify_all to prepare for a fresh audit run (otherwise state persists within
    a single Python process across invocations, which is wrong for library use)."""
    for ev in _API_DISABLED.values():
        ev.clear()
    with _API_FAILURE_LOCK:
        for k in _API_FAILURES:
            _API_FAILURES[k] = 0
    # Consult persistent cooldowns (rate-limited APIs only) to seed state.
    for name in _API_DISABLED:
        _check_persistent_cooldown(name)


def _cache_path_for(url: str) -> Path:
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
    return CACHE_DIR / f"{key}.json"


def _cache_get(url: str) -> Optional[Dict[str, Any]]:
    if not _CACHE_ENABLED:
        return None
    p = _cache_path_for(url)
    if not p.exists():
        return None
    try:
        if time.time() - p.stat().st_mtime > CACHE_TTL_SECONDS:
            return None
        with p.open() as f:
            data = json.load(f)
        return data.get("payload")
    except (json.JSONDecodeError, OSError):
        return None


def _cache_put(url: str, payload: Any) -> None:
    if not _CACHE_ENABLED:
        return
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        p = _cache_path_for(url)
        with p.open("w") as f:
            json.dump({"url": url, "payload": payload, "ts": time.time()}, f)
    except OSError:
        pass

# ---------- low-level HTTP helper ------------------------------------------

class APIError(Exception):
    pass


def _get(url: str, timeout: float = 15.0, max_retries: int = 3) -> Dict[str, Any]:
    """GET with retries, exponential backoff, and a persistent cache.

    The cache is checked before the network. Successful responses are cached
    for 7 days. Failures are never cached (so a flaky run doesn't poison
    future runs).
    """
    cached = _cache_get(url)
    if cached is not None:
        return cached
    delay = 1.0
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read().decode("utf-8", errors="replace")
                data = json.loads(body) if body else {}
                _cache_put(url, data)
                return data
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < max_retries - 1:
                time.sleep(delay)
                delay *= 2
                continue
            log.debug("HTTP error %s for %s", e.code, url)
            return {}
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            if attempt < max_retries - 1:
                time.sleep(delay)
                delay *= 2
                continue
            log.debug("Request failed: %s for %s", e, url)
            return {}
    return {}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Capture redirect targets without following them. doi.org redirects real
    DOIs to publisher sites; following the redirect can hang on slow publishers,
    and we only need to know whether doi.org issued a redirect at all."""
    def http_error_302(self, req, fp, code, msg, headers):
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)
    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirect())


def url_resolves(url: str, timeout: float = 4.0) -> bool:
    """True iff `url` resolves to a real page (any 2xx or 3xx).

    Results are cached on disk (same TTL as API responses) so re-runs are
    fast. HEAD-first, falling back to GET on servers that don't allow HEAD.
    """
    if not url:
        return False
    if not url.startswith(("http://", "https://")):
        return False

    cache_key = f"URL_RESOLVES:{url}"
    cached = _cache_get(cache_key)
    if isinstance(cached, bool):
        return cached

    result = False
    with _URL_SEM:
        for method in ("HEAD", "GET"):
            try:
                req = urllib.request.Request(url, method=method, headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "*/*",
                })
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    if 200 <= r.status < 400:
                        result = True
                    break
            except urllib.error.HTTPError as e:
                if e.code == 405 and method == "HEAD":
                    continue
                # 4xx final -- URL doesn't resolve. But 401/403 means the page
                # exists, server just doesn't want us specifically: still treat
                # as 'resolves'.
                if e.code in (401, 403):
                    result = True
                break
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                break
    _cache_put(cache_key, result)
    return result


# Domain trust hints. A URL on one of these top-level domains, or matching a
# well-known reputable host, is treated as authoritative grey literature even
# if the URL HEAD check happens to fail (e.g. transient outage). The tool
# never SKIPS the HEAD check, but uses this list to soften the language in
# notes when a known-reputable URL momentarily 404s.
_REPUTABLE_GREY_LIT_DOMAINS = (
    ".gov", ".edu", ".mil",
    "who.int", "un.org", "europa.eu",
    "nih.gov", "cdc.gov", "fda.gov", "nlm.nih.gov", "ncbi.nlm.nih.gov",
    "bjs.ojp.gov", "ojp.gov", "bls.gov", "census.gov",
    "sentencingproject.org", "prisonpolicy.org", "rand.org",
    "brookings.edu", "kff.org", "pewresearch.org",
    "americanheart.org", "aha.org", "ama-assn.org",
    "lancet.com", "nejm.org",
)


def is_reputable_domain(url: str) -> bool:
    if not url:
        return False
    try:
        host = urllib.parse.urlparse(url).hostname or ""
    except Exception:
        return False
    host = host.lower()
    return any(host == d.lstrip(".") or host.endswith(d) for d in _REPUTABLE_GREY_LIT_DOMAINS)


def doi_resolves(doi: str) -> bool:
    """True iff https://doi.org/<doi> resolves to a real publisher record.

    doi.org issues a 30x redirect for real DOIs and a 404 for nonexistent ones.
    We DO NOT follow the redirect: chasing it would hit publisher sites that
    may rate-limit, block HEAD, or hang. The presence of a 30x at doi.org is
    itself proof the DOI is registered.
    """
    if not doi:
        return False
    if not _looks_real_doi(doi):
        return False
    url = f"https://doi.org/{urllib.parse.quote(doi, safe='/')}"
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
        with _NO_REDIRECT_OPENER.open(req, timeout=5.0) as r:
            return 200 <= r.status < 400
    except urllib.error.HTTPError as e:
        # 30x is a "resolved" signal -- the handler above re-raises as HTTPError.
        return 300 <= e.code < 400 or e.code == 200
    except (urllib.error.URLError, TimeoutError):
        return False


def _get_text(url: str, timeout: float = 15.0, max_retries: int = 3) -> str:
    """GET raw text (used for arXiv's Atom feed). Cached like _get."""
    cached = _cache_get(url)
    if isinstance(cached, str):
        return cached
    delay = 1.0
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                txt = r.read().decode("utf-8", errors="replace")
                _cache_put(url, txt)
                return txt
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
            if attempt < max_retries - 1:
                time.sleep(delay)
                delay *= 2
                continue
            log.debug("Text fetch failed: %s for %s", e, url)
            return ""
    return ""


# ---------- CrossRef --------------------------------------------------------

def _crossref_normalize(item: Dict[str, Any]) -> CanonicalMetadata:
    authors = []
    for a in item.get("author", []) or []:
        authors.append(Author(family=a.get("family", ""), given=a.get("given", ""), orcid=a.get("ORCID")))
    issued = item.get("issued", {}).get("date-parts", [[None]])
    year = issued[0][0] if issued and issued[0] else None
    container = (item.get("container-title") or [""])[0]
    title = (item.get("title") or [""])[0]
    pages = item.get("page", "") or ""
    type_map = {
        "journal-article": CitationType.JOURNAL_ARTICLE,
        "book": CitationType.BOOK,
        "book-chapter": CitationType.BOOK_CHAPTER,
        "proceedings-article": CitationType.CONFERENCE_PAPER,
        "posted-content": CitationType.PREPRINT,
        "report": CitationType.REPORT,
        "dataset": CitationType.DATASET,
    }
    return CanonicalMetadata(
        source="crossref",
        source_id=item.get("DOI", ""),
        source_url=f"https://doi.org/{item['DOI']}" if item.get("DOI") else "",
        authors=authors,
        title=title,
        year=year,
        container_title=container,
        volume=item.get("volume", "") or "",
        issue=item.get("issue", "") or "",
        pages=pages,
        publisher=item.get("publisher", "") or "",
        doi=(item.get("DOI") or "").lower(),
        type=type_map.get(item.get("type", ""), CitationType.UNKNOWN),
        raw=item,
    )


def crossref_by_doi(doi: str) -> Optional[CanonicalMetadata]:
    if not doi or _api_disabled("crossref"):
        return None
    url = f"https://api.crossref.org/works/{urllib.parse.quote(doi, safe='/')}"
    cached = _cache_get(url)
    with _CROSSREF_SEM:
        d = _get(url, max_retries=2)
    if cached is None and not d:
        _api_disable("crossref")
    msg = d.get("message") if d else None
    return _crossref_normalize(msg) if msg else None


def crossref_search(ref: ParsedReference, rows: int = 5) -> List[CanonicalMetadata]:
    if not ref.title or _api_disabled("crossref"):
        return []
    params = {
        "query.bibliographic": ref.title,
        "rows": str(rows),
        "select": "DOI,title,author,issued,container-title,volume,issue,page,publisher,type",
    }
    if ref.authors:
        params["query.author"] = " ".join(a.family for a in ref.authors[:3] if a.family)
    url = "https://api.crossref.org/works?" + urllib.parse.urlencode(params)
    cached = _cache_get(url)
    with _CROSSREF_SEM:
        d = _get(url, max_retries=2)
    if cached is None and not d:
        _api_disable("crossref")
    items = d.get("message", {}).get("items", []) or [] if d else []
    return [_crossref_normalize(it) for it in items]


# ---------- OpenAlex --------------------------------------------------------

def _openalex_normalize(w: Dict[str, Any]) -> CanonicalMetadata:
    authors = []
    for au in w.get("authorships", []) or []:
        name = (au.get("author") or {}).get("display_name", "")
        a = Author.from_string(name)
        a.orcid = (au.get("author") or {}).get("orcid")
        authors.append(a)
    title = w.get("title") or w.get("display_name") or ""
    pl = w.get("primary_location") or {}
    src = pl.get("source") or {}
    biblio = w.get("biblio") or {}
    return CanonicalMetadata(
        source="openalex",
        source_id=(w.get("id") or "").replace("https://openalex.org/", ""),
        source_url=w.get("id", ""),
        authors=authors,
        title=title,
        year=w.get("publication_year"),
        container_title=src.get("display_name") or "",
        volume=str(biblio.get("volume") or ""),
        issue=str(biblio.get("issue") or ""),
        pages=("-".join([str(biblio.get("first_page") or ""), str(biblio.get("last_page") or "")]).strip("-")
               if biblio.get("first_page") or biblio.get("last_page") else ""),
        publisher=(src.get("host_organization_name") or ""),
        doi=(w.get("doi") or "").replace("https://doi.org/", "").lower(),
        type=_openalex_type(w),
        raw=w,
    )


def _openalex_type(w: Dict[str, Any]) -> CitationType:
    """OpenAlex uses many fine-grained type names; collapse them to our enum."""
    t = (w.get("type") or "").lower()
    if t in ("article", "journal-article", "review", "letter", "editorial", "erratum"):
        return CitationType.JOURNAL_ARTICLE
    if t == "book":
        return CitationType.BOOK
    if t in ("book-chapter", "reference-entry"):
        return CitationType.BOOK_CHAPTER
    if t in ("preprint", "posted-content"):
        return CitationType.PREPRINT
    if t == "dataset":
        return CitationType.DATASET
    if t == "report":
        return CitationType.REPORT
    if t == "dissertation":
        return CitationType.THESIS
    # Heuristic fallback for anything OpenAlex labels strangely: if the work
    # has a journal-like host and volume/pages, treat it as a journal article.
    biblio = w.get("biblio") or {}
    src = (w.get("primary_location") or {}).get("source") or {}
    if src.get("type") == "journal" or biblio.get("volume") or biblio.get("first_page"):
        return CitationType.JOURNAL_ARTICLE
    return CitationType.UNKNOWN


def openalex_search(ref: ParsedReference, per_page: int = 5) -> List[CanonicalMetadata]:
    if not ref.title or _api_disabled("openalex"):
        return []
    params = {"search": ref.title, "per-page": str(per_page)}
    if ref.year:
        params["filter"] = f"publication_year:{ref.year}"
    url = "https://api.openalex.org/works?" + urllib.parse.urlencode(params)
    cached = _cache_get(url)
    with _OPENALEX_SEM:
        d = _get(url, max_retries=2)
    if cached is None and not d:
        _api_disable("openalex")
    return [_openalex_normalize(w) for w in (d or {}).get("results", []) or []]


def openalex_by_doi(doi: str) -> Optional[CanonicalMetadata]:
    if not doi or _api_disabled("openalex"):
        return None
    url = f"https://api.openalex.org/works/https://doi.org/{urllib.parse.quote(doi, safe='/')}"
    cached = _cache_get(url)
    with _OPENALEX_SEM:
        d = _get(url, max_retries=2)
    if cached is None and not d:
        _api_disable("openalex")
    return _openalex_normalize(d) if d and d.get("id") else None


# ---------- PubMed ESearch + ESummary --------------------------------------

def _pubmed_type(rec: Dict[str, Any]) -> CitationType:
    """Map PubMed pubtype field to our CitationType enum."""
    pubtypes = [p.lower() for p in rec.get("pubtype", []) if isinstance(p, str)]
    if any(p in ("review", "systematic review", "meta-analysis", "journal article",
                  "clinical trial", "randomized controlled trial", "case reports",
                  "letter", "editorial", "comment") for p in pubtypes):
        return CitationType.JOURNAL_ARTICLE
    if any("book" in p for p in pubtypes):
        return CitationType.BOOK
    if any(p in ("preprint", "preprint version") for p in pubtypes):
        return CitationType.PREPRINT
    return CitationType.JOURNAL_ARTICLE  # PubMed is almost entirely journals


def _pubmed_normalize(summary: Dict[str, Any], pmid: str) -> CanonicalMetadata:
    rec = summary.get("result", {}).get(pmid, {})
    authors = [Author.from_string(a.get("name", "")) for a in rec.get("authors", []) or []]
    year = None
    pubdate = rec.get("pubdate") or rec.get("epubdate") or ""
    m_year = re.match(r"(\d{4})", pubdate)
    if m_year:
        year = int(m_year.group(1))
    return CanonicalMetadata(
        source="pubmed",
        source_id=pmid,
        source_url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
        authors=authors,
        title=rec.get("title", "").rstrip("."),
        year=year,
        container_title=rec.get("fulljournalname") or rec.get("source") or "",
        volume=rec.get("volume", "") or "",
        issue=rec.get("issue", "") or "",
        pages=rec.get("pages", "") or "",
        doi=next(
            (a.get("value", "").lower() for a in rec.get("articleids", []) if a.get("idtype") == "doi"),
            "",
        ),
        pmid=pmid,
        type=_pubmed_type(rec),
        raw=rec,
    )


def pubmed_by_pmid(pmid: str) -> Optional[CanonicalMetadata]:
    if not pmid:
        return None
    url = f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?db=pubmed&id={pmid}&retmode=json"
    with _PUBMED_SEM:
        d = _get(url)
    return _pubmed_normalize(d, pmid) if d.get("result") else None


def pubmed_search(ref: ParsedReference, retmax: int = 5) -> List[CanonicalMetadata]:
    if not ref.title or _api_disabled("pubmed"):
        return []
    # PubMed treats [] specially -- strip them from the title to avoid
    # injecting field tags. Also strip quotes and other special chars that
    # break Entrez parsing.
    safe_title = re.sub(r"[\[\]\"'(){}]", " ", ref.title)
    safe_title = re.sub(r"\s+", " ", safe_title).strip()
    if not safe_title:
        return []
    terms = [f"{safe_title}[Title]"]
    if ref.authors:
        terms.append(f"{ref.authors[0].family}[Author]")
    if ref.year:
        terms.append(f"{ref.year}[pdat]")
    q = " AND ".join(terms)
    s_url = (
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?"
        + urllib.parse.urlencode({"db": "pubmed", "term": q, "retmax": retmax, "retmode": "json"})
    )
    with _PUBMED_SEM:
        s = _get(s_url)
    ids = (s.get("esearchresult") or {}).get("idlist") or []
    if not ids:
        return []
    sum_url = (
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?"
        + urllib.parse.urlencode({"db": "pubmed", "id": ",".join(ids), "retmode": "json"})
    )
    with _PUBMED_SEM:
        sums = _get(sum_url)
    return [_pubmed_normalize(sums, pmid) for pmid in ids if sums.get("result", {}).get(pmid)]


# ---------- Semantic Scholar ------------------------------------------------

def _s2_normalize(p: Dict[str, Any]) -> CanonicalMetadata:
    authors = [Author.from_string(a.get("name", "")) for a in p.get("authors", []) or []]
    ext = p.get("externalIds") or {}
    return CanonicalMetadata(
        source="semantic_scholar",
        source_id=p.get("paperId", ""),
        source_url=f"https://www.semanticscholar.org/paper/{p.get('paperId', '')}",
        authors=authors,
        title=p.get("title", ""),
        year=p.get("year"),
        container_title=p.get("venue", "") or "",
        doi=(ext.get("DOI") or "").lower(),
        arxiv_id=(ext.get("ArXiv") or "").lower(),
        pmid=ext.get("PubMed", "") or "",
        type=CitationType.JOURNAL_ARTICLE,
        raw=p,
    )


def semantic_scholar_search(ref: ParsedReference, limit: int = 5) -> List[CanonicalMetadata]:
    if not ref.title or _api_disabled("semantic_scholar"):
        return []
    q = urllib.parse.urlencode({
        "query": ref.title,
        "limit": str(limit),
        "fields": "title,authors,year,venue,externalIds",
    })
    url = f"https://api.semanticscholar.org/graph/v1/paper/search?{q}"
    cached = _cache_get(url)
    with _S2_SEM:
        # Single attempt; S2 stays rate-limited for minutes once you cross
        # the quota, so backoff is wasted budget.
        d = _get(url, max_retries=1)
    if cached is None and not d:
        _api_disable("semantic_scholar")
        return []
    return [_s2_normalize(p) for p in (d or {}).get("data", []) or []]


# ---------- arXiv -----------------------------------------------------------

_ATOM_NS = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}


def _arxiv_normalize(entry: ET.Element) -> CanonicalMetadata:
    def t(path: str) -> str:
        el = entry.find(path, _ATOM_NS)
        return (el.text or "").strip() if el is not None and el.text else ""

    authors = []
    for a_el in entry.findall("a:author", _ATOM_NS):
        name = a_el.find("a:name", _ATOM_NS)
        if name is not None and name.text:
            authors.append(Author.from_string(name.text.strip()))

    id_url = t("a:id")
    arxiv_id = id_url.split("/abs/")[-1] if "/abs/" in id_url else ""
    pub = t("a:published")
    year = int(pub[:4]) if pub[:4].isdigit() else None
    doi_el = entry.find("arxiv:doi", _ATOM_NS)
    return CanonicalMetadata(
        source="arxiv",
        source_id=arxiv_id,
        source_url=id_url,
        authors=authors,
        title=t("a:title").replace("\n", " ").replace("  ", " ").strip(),
        year=year,
        container_title="arXiv",
        doi=(doi_el.text or "").lower() if doi_el is not None and doi_el.text else "",
        arxiv_id=arxiv_id,
        type=CitationType.PREPRINT,
        raw={},
    )


def arxiv_search(ref: ParsedReference, max_results: int = 5) -> List[CanonicalMetadata]:
    if (not ref.title and not ref.arxiv_id) or _api_disabled("arxiv"):
        return []
    if ref.arxiv_id:
        query = f"id_list={urllib.parse.quote(ref.arxiv_id)}"
    else:
        # arXiv query syntax uses double-quotes as phrase delimiters; strip
        # them from the title to avoid breaking the query.
        safe_title = ref.title.replace('"', '').replace("'", "")
        q = f'ti:"{safe_title}"'
        if ref.authors:
            q = q + " AND " + " AND ".join(
                f'au:"{a.family.replace(chr(34), "")}"' for a in ref.authors[:2] if a.family
            )
        query = "search_query=" + urllib.parse.quote(q) + f"&max_results={max_results}"
    url = f"https://export.arxiv.org/api/query?{query}"
    cached = _cache_get(url)
    with _ARXIV_SEM:
        text = _get_text(url, max_retries=2)
    if cached is None and not text:
        _api_disable("arxiv")
        return []
    if not text:
        return []
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    return [_arxiv_normalize(e) for e in root.findall("a:entry", _ATOM_NS)]


# ---------- optional: scholarly (Google Scholar scraper) -------------------

def scholarly_search(ref: ParsedReference, limit: int = 3) -> List[CanonicalMetadata]:
    """Best-effort fallback. Disabled by default; enable with --use-scholarly."""
    try:
        from scholarly import scholarly  # type: ignore
    except Exception:
        log.debug("scholarly not installed; skipping")
        return []
    if not ref.title:
        return []
    out: List[CanonicalMetadata] = []
    try:
        results = scholarly.search_pubs(ref.title)
        for _ in range(limit):
            try:
                r = next(results)
            except StopIteration:
                break
            bib = r.get("bib", {}) if isinstance(r, dict) else {}
            authors = [Author.from_string(a) for a in (bib.get("author") or [])]
            out.append(CanonicalMetadata(
                source="scholarly",
                source_id=r.get("pub_url", "") if isinstance(r, dict) else "",
                source_url=r.get("pub_url", "") if isinstance(r, dict) else "",
                authors=authors,
                title=bib.get("title", "") or "",
                year=int(bib["pub_year"]) if str(bib.get("pub_year", "")).isdigit() else None,
                container_title=bib.get("venue", "") or "",
                type=CitationType.JOURNAL_ARTICLE,
                raw=r if isinstance(r, dict) else {},
            ))
    except Exception as e:
        log.debug("scholarly failed: %s", e)
    return out


# ---------- candidate scoring -----------------------------------------------

def _score(ref: ParsedReference, cand: CanonicalMetadata) -> float:
    """Higher is better. Range roughly 0-100.

    We penalize candidates with CrossRef test-prefix DOIs because those are
    sandbox/duplicate registrations that should never be picked as the
    canonical when a legitimate record is available.
    """
    if not cand.title or not ref.title:
        return 0.0
    title_score = fuzz.token_set_ratio(normalize_title(ref.title), normalize_title(cand.title))
    author_overlap = author_surname_overlap(ref.authors, cand.authors) * 100
    year_score = 100 if (ref.year and cand.year and ref.year == cand.year) else (
        70 if (ref.year and cand.year and abs(ref.year - cand.year) <= 1) else 50
    )
    if not ref.year:
        year_score = 60
    if not ref.authors:
        author_overlap = 60
    doi_bonus = 30 if ref.doi and cand.doi and ref.doi.lower() == cand.doi.lower() else 0
    base = 0.55 * title_score + 0.25 * author_overlap + 0.15 * year_score + 0.05 * doi_bonus
    # Penalty for test/sandbox DOIs: drop the score so a legitimate record
    # outranks the test record even when fuzz numbers say otherwise.
    if cand.doi and not _looks_real_doi(cand.doi):
        base -= 15
    return base


def _pick_best(ref: ParsedReference, cands: List[CanonicalMetadata]) -> Tuple[Optional[CanonicalMetadata], float]:
    """Pick the highest-scoring candidate, with a hard exclusion for test DOIs
    when at least one legitimate candidate exists.

    The -15 penalty in _score isn't always enough: if a CrossRef test DOI
    fuzz-matches the title perfectly and a legitimate OpenAlex record matches
    less well, the test record can still win. Filter explicitly when we have
    a real alternative.
    """
    if not cands:
        return None, 0.0
    legitimate = [c for c in cands if not c.doi or _looks_real_doi(c.doi)]
    pool = legitimate if legitimate else cands
    scored = [(c, _score(ref, c)) for c in pool]
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[0][0], scored[0][1]


# ---------- diff detection: substantive vs style ----------------------------

def _detect_substantive_discrepancies(
    ref: ParsedReference, canon: CanonicalMetadata,
    doi_resolution_status: Optional[bool] = None,
) -> List[Discrepancy]:
    """Errors that change the meaning of the citation. Downgrade to PARTIAL.

    `doi_resolution_status` is the pre-computed result of `doi_resolves(ref.doi)`.
    Passed in so we don't HTTP-fetch inside the diff detection (caller does it
    once per reference). None means 'not checked'.
    """
    diffs: List[Discrepancy] = []

    # Title: only flag if the normalized forms differ.
    if ref.title and canon.title:
        sim = fuzz.token_set_ratio(normalize_title(ref.title), normalize_title(canon.title))
        if sim < 92:
            diffs.append(Discrepancy("title", ref.title, canon.title, "major"))

    if ref.year and canon.year and ref.year != canon.year:
        diffs.append(Discrepancy("year", str(ref.year), str(canon.year),
                                 "major" if abs(ref.year - canon.year) > 1 else "minor"))

    # DOI: prefer the resolution check when available -- a non-resolving DOI
    # is a much stronger signal than a DOI mismatch.
    if ref.doi and doi_resolution_status is False:
        diffs.append(Discrepancy(
            "doi (does not resolve)",
            ref.doi,
            canon.doi or "(canonical record has no DOI)",
            "major",
        ))
    elif ref.doi and canon.doi and ref.doi.lower() != canon.doi.lower():
        diffs.append(Discrepancy("doi", ref.doi, canon.doi, "major"))

    if ref.container_title and canon.container_title:
        journal_fuzz = fuzz.token_set_ratio(
            normalize_journal(ref.container_title), normalize_journal(canon.container_title)
        )
        # Accept abbreviation matches ('J. Am. Med. Inform. Assoc.' vs the full
        # 'Journal of the American Medical Informatics Association') so AMA /
        # Vancouver abbreviated journal names don't flag as substantive errors.
        if journal_fuzz < 80 and not journal_abbreviation_match(
            ref.container_title, canon.container_title
        ):
            diffs.append(Discrepancy(
                "journal", ref.container_title, canon.container_title, "major"
            ))

    if ref.authors and canon.authors:
        overlap = author_surname_overlap(ref.authors, canon.authors)
        if overlap < 0.5:
            diffs.append(Discrepancy(
                "authors (surnames disagree)",
                "; ".join(a.full for a in ref.authors[:5]),
                "; ".join(a.full for a in canon.authors[:5]),
                "major",
            ))
        elif len(canon.authors) > len(ref.authors) + 1:
            # Author list shorter than canonical. Only flag if the user did NOT
            # use 'et al.' -- that explicitly signals intentional truncation and
            # is correct style (AMA lists 3-6 then 'et al.'; APA up to 20).
            used_et_al = bool(re.search(r"\bet\s+al\.?", ref.raw_text, re.IGNORECASE))
            if not used_et_al:
                diffs.append(Discrepancy(
                    "author count",
                    f"{len(ref.authors)} listed",
                    f"{len(canon.authors)} on the canonical record",
                    "minor",
                ))

    # Volume / issue: compare numerically when both sides look like integers so
    # zero-padding ('2' vs '02') doesn't flag as a substantive error. Some
    # publishers (Karger, Thieme, etc.) zero-pad volumes in their metadata.
    def _eq_volume_issue(a: str, b: str) -> bool:
        a, b = a.strip(), b.strip()
        if a == b:
            return True
        if a.isdigit() and b.isdigit():
            return int(a) == int(b)
        return False

    if ref.volume and canon.volume and not _eq_volume_issue(ref.volume, canon.volume):
        diffs.append(Discrepancy("volume", ref.volume, canon.volume, "major"))

    if ref.issue and canon.issue and not _eq_volume_issue(ref.issue, canon.issue):
        diffs.append(Discrepancy("issue", ref.issue, canon.issue, "major"))

    # Pages: only the numeric range matters; hyphen-vs-en-dash is handled in
    # style edits below.
    if ref.pages and canon.pages:
        ref_pages_norm = re.sub(r"[\s\-–—]", "-", ref.pages).strip()
        canon_pages_norm = re.sub(r"[\s\-–—]", "-", canon.pages).strip()
        if ref_pages_norm != canon_pages_norm:
            diffs.append(Discrepancy("pages", ref.pages, canon.pages, "major"))

    # Sanity checks independent of canonical record.
    current_year = datetime.datetime.now().year
    if ref.year and ref.year > current_year + 1:
        diffs.append(Discrepancy(
            "year (future)", str(ref.year), f"<= {current_year}",
            "major",
        ))
    if ref.year and ref.year < 1665:  # Phil Trans Royal Soc founded 1665
        diffs.append(Discrepancy(
            "year (implausibly historical)", str(ref.year), ">= 1665",
            "major",
        ))
    # Page range: backwards or absurdly long.
    if ref.pages and re.match(r"\s*\d+\s*[-–]\s*\d+", ref.pages):
        try:
            sp, ep = re.split(r"[-–]", ref.pages.strip(), maxsplit=1)
            sp_n, ep_n = int(re.sub(r"\D", "", sp)), int(re.sub(r"\D", "", ep))
            if sp_n > ep_n:
                diffs.append(Discrepancy(
                    "pages (backwards)", ref.pages, "start > end is impossible",
                    "major",
                ))
            if ep_n - sp_n > 1500:
                diffs.append(Discrepancy(
                    "pages (implausibly long)", ref.pages,
                    f"range spans {ep_n - sp_n} pages",
                    "minor",
                ))
        except ValueError:
            pass

    # PMC ID in pages slot: a strong fabrication / parse-quality signal.
    # PubMed Central IDs (PMC1234567) belong in a separate field; if they
    # appear in the pages slot, the citation was likely auto-generated by
    # something that confused field positions. Flag as major.
    if ref.pages and re.search(r"\bPMC\d{5,}\b", ref.pages):
        diffs.append(Discrepancy(
            "pages (PMC ID in pages slot)", ref.pages,
            "PMC IDs do not belong in the pages field",
            "major",
        ))

    return diffs


def detect_duplicate_entries(results: List["VerificationResult"]) -> List[Tuple[int, int, str]]:
    """Find duplicate bibliography entries.

    Two entries are duplicates if they share a canonical DOI, OR share a
    canonical OpenAlex/PubMed/arXiv ID, OR have title fuzz >= 95 and matching
    first-author surnames + year. Returns list of (idx1, idx2, reason) tuples
    where indices are 1-based.
    """
    dups: List[Tuple[int, int, str]] = []
    n = len(results)
    for i in range(n):
        ri = results[i]
        for j in range(i + 1, n):
            rj = results[j]
            ci, cj = ri.canonical, rj.canonical
            if ci and cj:
                if ci.doi and cj.doi and ci.doi.lower() == cj.doi.lower():
                    dups.append((i + 1, j + 1, f"shared DOI: {ci.doi}"))
                    continue
                if ci.source_id and cj.source_id and ci.source == cj.source and ci.source_id == cj.source_id:
                    dups.append((i + 1, j + 1, f"shared {ci.source} ID: {ci.source_id}"))
                    continue
                if ci.title and cj.title:
                    if fuzz.token_set_ratio(normalize_title(ci.title), normalize_title(cj.title)) >= 95:
                        if (ci.year == cj.year) and author_surname_overlap(ci.authors, cj.authors) >= 0.5:
                            dups.append((i + 1, j + 1, "identical canonical record"))
                            continue
            else:
                # Both unverified -- compare parsed forms as a fallback.
                pi, pj = ri.parsed, rj.parsed
                if pi.title and pj.title and fuzz.token_set_ratio(
                    normalize_title(pi.title), normalize_title(pj.title)) >= 95:
                    if pi.year == pj.year:
                        dups.append((i + 1, j + 1, "duplicate parsed title and year"))
    return dups


def _detect_style_edits(
    ref: ParsedReference, canon: CanonicalMetadata
) -> List[StyleEdit]:
    """Cosmetic differences that don't change meaning.

    These keep the entry at VERIFIED but are surfaced as 'polish suggestions'.
    Examples we detect:
      * an author's middle initial(s) is present in the canonical record but
        omitted in the source (e.g. 'Hinton, G.' vs 'Hinton, G. E.')
      * page ranges use a hyphen where the style requires an en-dash
      * journal name is not italicized in the source

    The italics detection only works for source text formats that carry
    formatting (BibTeX with `\\emph{}` etc); for plain-text source we cannot
    know whether the user already italicized in their styled doc, so we flag
    it as a suggestion rather than asserting it's wrong.
    """
    edits: List[StyleEdit] = []

    # Middle initials -- compare INITIAL COUNTS, not full given names.
    # Most styles use initials, not full given names. We flag a missing initial
    # only when canonical has MORE distinct given-name tokens than source, AND
    # the source's initial-letters are a prefix of canonical's initial-letters
    # (so it's truly a missing middle, not a different person).
    if ref.authors and canon.authors:
        canon_by_surname = {a.family.lower(): a for a in canon.authors if a.family}
        for src_a in ref.authors:
            if not src_a.family:
                continue
            canon_a = canon_by_surname.get(src_a.family.lower())
            if not canon_a or not canon_a.given:
                continue
            src_tokens = [t for t in re.split(r"[\s\.\-]+", src_a.given or "") if t]
            canon_tokens = [t for t in re.split(r"[\s\.\-]+", canon_a.given or "") if t]
            if len(canon_tokens) <= len(src_tokens):
                continue  # source already has at least as many name tokens
            src_initials = "".join(t[0].upper() for t in src_tokens)
            canon_initials = "".join(t[0].upper() for t in canon_tokens)
            if not src_initials or not canon_initials.startswith(src_initials):
                continue
            # Build the suggested form using just initials (matches what most
            # styles want -- the formatter will render the final style).
            suggested_given = ". ".join(canon_initials) + "."
            edits.append(StyleEdit(
                field=f"author '{canon_a.family}'",
                original=f"{src_a.family}, {src_a.given}",
                corrected=f"{canon_a.family}, {suggested_given}",
                description=(
                    f"Add missing middle initial(s) for {canon_a.family}: "
                    f"'{src_a.given}' has {len(src_tokens)} initial(s), canonical record "
                    f"shows {len(canon_tokens)} ({canon_initials})."
                ),
            ))

    # Pages: ASCII hyphen vs en-dash. Both APA, MLA, Chicago expect en-dash.
    if ref.pages and "-" in ref.pages and "–" not in ref.pages:
        # Only flag if the underlying numeric range is right (else it's a
        # substantive discrepancy already).
        if canon.pages:
            ref_norm = re.sub(r"[\s\-–—]", "-", ref.pages).strip()
            canon_norm = re.sub(r"[\s\-–—]", "-", canon.pages).strip()
            same_range = ref_norm == canon_norm
        else:
            same_range = True
        if same_range:
            edits.append(StyleEdit(
                field="pages",
                original=ref.pages,
                corrected=ref.pages.replace("-", "–"),
                description=f"Use en-dash in page range: '{ref.pages}' -> '{ref.pages.replace('-', '–')}'.",
            ))

    # Title case: deliberately NOT flagged here. Title casing is style-dependent
    # (APA/Vancouver/AMA: sentence case; MLA/Chicago: title case). The formatter
    # applies the correct case for the chosen style automatically, so flagging
    # case differences as 'edits' is misleading.

    # Journal italics: deliberately NOT flagged here. From plain text we can't
    # tell whether the user's source already had italics in their styled doc.
    # The formatter emits the journal italicized for every style that requires
    # it, so flagging this as an edit produces false positives.

    return edits


# ---------- top-level verification ------------------------------------------

def _list_or_empty(fn, *args):
    """Helper used by _gather_candidates: call fn, wrap a single result in a list,
    return [] on None. Replaces the walrus-in-lambda pattern that was hard to read.
    """
    try:
        r = fn(*args)
    except Exception as e:
        log.debug("source task %s failed: %s", fn.__name__, e)
        return []
    return [r] if r else []


def _gather_candidates(
    ref: ParsedReference,
    use_scholarly: bool,
    deadline: Optional[float] = None,
) -> List[CanonicalMetadata]:
    """Query available sources in parallel and merge results.

    Optimization: when ref has a DOI/arXiv/PMID, run the ID-based lookups
    FIRST. If those return a high-quality match (DOI lookup succeeded AND
    title fuzz-matches the user's input), we skip the broad title searches.
    This cuts API calls roughly in half for well-cited papers, which is
    most of them.

    `deadline`: optional absolute time (time.time() value). If we're past it
    after phase 1, the phase-2 title sweep is skipped.
    """
    out: List[CanonicalMetadata] = []
    # Phase 1: identifier-based fast paths. These are the most authoritative
    # and let us short-circuit the broad title sweep when they succeed.
    if ref.doi or ref.arxiv_id or ref.pmid:
        with ThreadPoolExecutor(max_workers=4) as ex:
            tasks = []
            if ref.doi:
                tasks.append(ex.submit(_list_or_empty, crossref_by_doi, ref.doi))
                tasks.append(ex.submit(_list_or_empty, openalex_by_doi, ref.doi))
            if ref.arxiv_id:
                tasks.append(ex.submit(arxiv_search, ref))
            if ref.pmid:
                tasks.append(ex.submit(_list_or_empty, pubmed_by_pmid, ref.pmid))
            for f in as_completed(tasks):
                try:
                    out.extend(f.result() or [])
                except Exception as e:
                    log.debug("ID-based source task failed: %s", e)

        # Short-circuit check: if the user's DOI resolved to a canonical record
        # AND its title strongly matches what they cited, we have enough
        # evidence to classify. Skip the broad title sweep.
        if ref.doi and ref.title and out:
            has_strong_id_match = any(
                c.doi and c.doi.lower() == ref.doi.lower()
                and fuzz.token_set_ratio(
                    normalize_title(c.title or ""), normalize_title(ref.title)
                ) >= 92
                for c in out
            )
            if has_strong_id_match and len(out) >= 2:
                # We have 2+ sources by DOI lookup; no need for title sweep.
                return out

    # Deadline check: if the per-ref budget is already spent, skip the broad
    # title sweep and classify on the identifier-based evidence we have.
    if deadline is not None and time.time() > deadline:
        log.debug("Per-ref deadline exceeded; skipping title sweep for %r", ref.title[:40])
        return out

    # Phase 2: broad title-based sweep. Runs when no ID was given or the ID
    # lookup didn't decisively succeed.
    with ThreadPoolExecutor(max_workers=5) as ex:
        tasks = []
        tasks.append(ex.submit(crossref_search, ref))
        tasks.append(ex.submit(openalex_search, ref))
        tasks.append(ex.submit(pubmed_search, ref))
        tasks.append(ex.submit(semantic_scholar_search, ref))
        if not ref.arxiv_id:
            tasks.append(ex.submit(arxiv_search, ref))
        if use_scholarly:
            tasks.append(ex.submit(scholarly_search, ref))
        for f in as_completed(tasks):
            try:
                out.extend(f.result() or [])
            except Exception as e:
                log.debug("title-search task failed: %s", e)
    return out


def _classify_three_tier(
    ref: ParsedReference,
    best: Optional[CanonicalMetadata],
    score: float,
    cands: List[CanonicalMetadata],
    substantive_diffs: List[Discrepancy],
    style_edits: List[StyleEdit],
) -> Confidence:
    """Three-tier classification with a VERIFIED sub-flavor for style edits.

    Decision flow:
      1. If no candidate at all or best score < 70 -> HALLUCINATED.
      2. Require multi-source agreement (>=2 distinct APIs returning the same
         paper) before we trust any single result. A lone match is treated as
         HALLUCINATED unless the paper has an exact ID match (DOI/arXiv/PMID)
         in the source -- those identifiers are themselves authoritative.
      3. If substantive content errors exist -> PARTIAL.
      4. If only cosmetic style edits remain -> VERIFIED_WITH_STYLE_EDITS.
      5. Otherwise -> VERIFIED.
    """
    if best is None or not cands or score < 70:
        return Confidence.HALLUCINATED

    # Count distinct sources whose returned record matches `best`.
    sources_agreeing = set()
    for c in cands:
        if c is best:
            sources_agreeing.add(c.source)
            continue
        title_match = fuzz.token_set_ratio(
            normalize_title(best.title or ""), normalize_title(c.title or "")
        ) >= 90
        year_match = (
            best.year is None or c.year is None or abs((best.year or 0) - (c.year or 0)) <= 1
        )
        author_match = author_surname_overlap(best.authors, c.authors) >= 0.5
        if title_match and year_match and (author_match or not best.authors or not c.authors):
            sources_agreeing.add(c.source)

    # Identifier-based lookups (DOI/arXiv/PMID) are self-corroborating.
    id_based = bool(
        (ref.doi and best.doi and ref.doi.lower() == best.doi.lower()) or
        (ref.arxiv_id and best.arxiv_id and ref.arxiv_id.lower() == best.arxiv_id.lower()) or
        (ref.pmid and best.pmid and ref.pmid == best.pmid)
    )

    # Strong title-based identity check. Two-stage:
    #   * title_author_identity: title fuzz >= 95 AND (matching first author
    #     OR shared surnames). Strongest signal -- accept from a single
    #     source.
    #   * title_only_identity: title fuzz >= 97 (essentially perfect) AND
    #     year agrees. Author mismatch alone shouldn't block detection: the
    #     downstream substantive-discrepancy detector will flag the author
    #     error as PARTIAL. Without this path, citations of real papers with
    #     wrong first authors get misclassified as HALLUCINATED.
    title_author_identity = False
    title_only_identity = False
    if ref.title and best.title:
        title_fuzz = fuzz.token_set_ratio(normalize_title(ref.title), normalize_title(best.title))
        first_author_match = (
            not ref.authors or not best.authors or
            (ref.authors[0].family and best.authors and
             normalize_author_surname(ref.authors[0].family) ==
             normalize_author_surname(best.authors[0].family))
        )
        any_author_match = (
            not ref.authors or not best.authors or
            author_surname_overlap(ref.authors, best.authors) >= 0.34
        )
        title_author_identity = (
            title_fuzz >= 95 and (first_author_match or any_author_match)
        )
        year_close = (
            not ref.year or not best.year or abs(ref.year - best.year) <= 1
        )
        title_only_identity = title_fuzz >= 97 and year_close

    real_paper = (
        (score >= 88 and len(sources_agreeing) >= 2)
        or id_based
        or title_author_identity
        or title_only_identity
    )

    if not real_paper:
        return Confidence.HALLUCINATED

    if substantive_diffs:
        return Confidence.PARTIAL
    if style_edits:
        return Confidence.VERIFIED_WITH_STYLE_EDITS
    return Confidence.VERIFIED


def _corroborating(best: CanonicalMetadata, cands: List[CanonicalMetadata]) -> List[CanonicalMetadata]:
    """Filter the candidate list to only those that genuinely corroborate `best`.

    A candidate corroborates `best` when its title fuzz-matches above 88 AND
    either its DOI matches OR its author surnames overlap >=0.5. Without this
    filter the report becomes noisy because title-search APIs return loose
    keyword matches we don't actually trust.
    """
    if not best:
        return []
    out: List[CanonicalMetadata] = []
    seen_sources_for_url = set()
    for c in cands:
        if c is best:
            continue
        if not c.title:
            continue
        title_score = fuzz.token_set_ratio(
            normalize_title(best.title), normalize_title(c.title)
        )
        if title_score < 88:
            continue
        doi_ok = bool(best.doi and c.doi and best.doi.lower() == c.doi.lower())
        author_ok = author_surname_overlap(best.authors, c.authors) >= 0.5
        year_ok = (
            best.year is None or c.year is None or abs((best.year or 0) - (c.year or 0)) <= 1
        )
        if not year_ok:
            continue
        if not (doi_ok or author_ok or not best.authors or not c.authors):
            continue
        # Dedupe by source + URL so we don't list the same record twice.
        key = (c.source, c.source_url)
        if key in seen_sources_for_url:
            continue
        seen_sources_for_url.add(key)
        out.append(c)
    return out


# CrossRef test/sandbox registrants we should never propagate into a corrected
# citation. CrossRef occasionally returns these for real titles, presumably
# because of test submissions or duplicate registrations.
_CROSSREF_TEST_PREFIXES = ("10.65215/",)


def _looks_real_doi(doi: str) -> bool:
    """Reject obvious test DOIs."""
    if not doi:
        return False
    low = doi.lower()
    return not any(low.startswith(p) for p in _CROSSREF_TEST_PREFIXES)


def _merge_metadata(best: CanonicalMetadata, corroborating: List[CanonicalMetadata]) -> CanonicalMetadata:
    """Fill in fields missing from `best` using values from corroborating sources.

    We never overwrite a non-empty field on `best` -- we only fill gaps. Each
    filled field is recorded back into best.raw['_merged_from'] for audit.

    Special handling for DOI: we skip test-prefix DOIs (CrossRef sandbox
    registrants) since they don't resolve to real records.
    """
    if not best:
        return best
    # If `best` itself has a test DOI, clear it before considering merges.
    if best.doi and not _looks_real_doi(best.doi):
        best.doi = ""
    merged_from: dict = {}
    fillable = ["doi", "container_title", "volume", "issue", "pages", "publisher",
                "arxiv_id", "pmid", "year"]
    for field_name in fillable:
        if getattr(best, field_name):
            continue
        for c in corroborating:
            v = getattr(c, field_name, None)
            if not v:
                continue
            if field_name == "doi" and not _looks_real_doi(v):
                continue
            setattr(best, field_name, v)
            merged_from[field_name] = c.source
            break
    if not best.authors:
        for c in corroborating:
            if c.authors:
                best.authors = list(c.authors)
                merged_from["authors"] = c.source
                break
    if merged_from:
        best.raw = dict(best.raw or {})
        best.raw["_merged_from"] = merged_from
    return best


def verify_one(
    ref: ParsedReference,
    use_scholarly: bool = False,
    strictness: str = "default",
    timeout_seconds: Optional[float] = None,
) -> VerificationResult:
    """Verify one reference.

    `strictness`:
      * 'default'  -- the normal three-tier flow with the grey-literature
                       fallback paths.
      * 'strict'   -- require multi-source agreement even for ID-based
                       lookups; refuse the VERIFIED_VIA_URL fallback so any
                       source that isn't in an academic database goes
                       HALLUCINATED. Useful when the user wants to surface
                       every grey-lit citation for hand review.
      * 'relaxed'  -- treat institutional+publisher refs as VERIFIED (not
                       VERIFIED_VIA_URL) even without a URL. Useful for
                       policy / public-health workflows that cite a lot of
                       government and NGO sources by convention.

    `timeout_seconds`: per-ref budget for candidate gathering. If the
    identifier-based lookups (phase 1) already exceed it, the broad title
    sweep (phase 2) is skipped and the reference is classified on whatever
    evidence was gathered. In-flight HTTP calls already have their own socket
    timeouts, so this bounds the wall-clock cost per reference.
    """
    deadline = (time.time() + timeout_seconds) if timeout_seconds is not None else None
    cands = _gather_candidates(ref, use_scholarly=use_scholarly, deadline=deadline)
    best, score = _pick_best(ref, cands)

    # Check DOI and URL resolution up front. Non-resolving DOIs are the
    # strongest fabrication signal; resolving URLs are the only verification
    # we have for grey literature (government reports, NGO publications, etc.).
    doi_status: Optional[bool] = None
    if ref.doi:
        doi_status = doi_resolves(ref.doi)
    url_status: Optional[bool] = None
    if ref.url:
        url_status = url_resolves(ref.url)

    # Filter the candidate list so only genuinely-corroborating sources are
    # surfaced in the report.
    corroborating = _corroborating(best, cands) if best else []
    if best:
        best = _merge_metadata(best, corroborating)

    substantive: List[Discrepancy] = []
    style_edits: List[StyleEdit] = []
    if best:
        substantive = _detect_substantive_discrepancies(ref, best, doi_resolution_status=doi_status)
        sub_fields = {d.field for d in substantive}
        style_raw = _detect_style_edits(ref, best)
        style_edits = [
            e for e in style_raw
            if not any(e.field.startswith(prefix.split(" ")[0]) for prefix in sub_fields)
        ]

    confidence = _classify_three_tier(
        ref, best, score, [best] + corroborating if best else [],
        substantive, style_edits,
    )

    # Grey-literature override paths.
    #
    # Path 1: URL provided AND it resolves. Strong evidence the source exists.
    # Skipped under --strict (refuses any non-academic-database verification).
    # Under --relaxed, bump all the way to VERIFIED (the user's workflow
    # treats grey lit as authoritative).
    if (strictness != "strict"
        and confidence == Confidence.HALLUCINATED
        and url_status is True
        and doi_status is not False):
        confidence = (
            Confidence.VERIFIED if strictness == "relaxed" else Confidence.VERIFIED_VIA_URL
        )

    # Path 2: the entry has an institutional author AND no fake-DOI red flag.
    # This catches grey literature where the institution is its own publisher
    # (AMA, Joint Commission, Lucian Leape Institute, CDC, etc.) -- they author
    # and publish reports/guidance/standards that no academic database indexes.
    # A separate publisher field is NOT required (the institution often IS the
    # publisher). Covers both the no-URL case and the URL-given-but-didn't-
    # resolve case (transient outages on agency sites are common).
    # Under --relaxed -> VERIFIED; otherwise -> VERIFIED_VIA_URL; never under --strict.
    elif (
        strictness != "strict"
        and confidence == Confidence.HALLUCINATED
        and doi_status is not False
        and ref.authors and ref.authors[0].family
        and is_institutional_author(ref.authors[0].family)
    ):
        confidence = (
            Confidence.VERIFIED if strictness == "relaxed" else Confidence.VERIFIED_VIA_URL
        )

    notes: List[str] = []
    if ref.doi and doi_status is False:
        notes.append(
            f"The cited DOI '{ref.doi}' does NOT resolve. doi.org returned 404 or failed. "
            f"This is a strong fabrication signal -- a real DOI on a published paper would always resolve."
        )

    if confidence == Confidence.VERIFIED_VIA_URL:
        if ref.url and url_status is True:
            rep = " (reputable domain)" if is_reputable_domain(ref.url) else ""
            notes.append(
                f"Confirmed via URL resolution{rep}: {ref.url}. The cited source is not indexed in "
                f"CrossRef, OpenAlex, PubMed, Semantic Scholar, or arXiv -- which is normal for grey "
                f"literature (government reports, agency websites, NGO publications, working papers). "
                f"The URL resolves to a real page; treat as legitimate. If the source is critical, "
                f"manually verify the title and date by opening the URL."
            )
        else:
            # Institutional author path (no resolving URL). The institution is
            # typically its own publisher.
            pub_clause = f" (publisher: '{ref.publisher}')" if ref.publisher else ""
            url_clause = (
                "the URL provided did not resolve (agency sites are often transiently down)"
                if ref.url else "no URL was provided in the citation"
            )
            notes.append(
                f"Institutional grey-literature citation. Author '{ref.authors[0].family}' is an "
                f"institution{pub_clause}; academic databases do not index this kind of source, and "
                f"{url_clause}. The entry is treated as legitimate based on its institutional "
                f"structure, but you should manually confirm it cites a real document. Adding a "
                f"working publication URL would let the tool verify it directly on future runs."
            )
    elif best is None:
        notes.append(
            "No candidate returned by any verification API and no resolving URL provided. "
            "The cited paper either does not exist, is so obscure that none of CrossRef, OpenAlex, "
            "PubMed, Semantic Scholar, or arXiv index it, or the title/authors are misspelled "
            "badly enough to defeat fuzzy search. Treat as fabricated until you manually confirm."
        )
    elif confidence == Confidence.HALLUCINATED:
        notes.append(
            f"Closest candidate scored only {score:.0f}/100 and could not be cross-corroborated by a "
            f"second verification source. This is the pattern typical of fabricated citations. "
            f"Manually confirm or remove."
        )
    elif confidence == Confidence.PARTIAL:
        n_sub = len(substantive)
        notes.append(
            f"The cited paper appears to be real (matched at score {score:.0f}/100) but the citation as written "
            f"has {n_sub} substantive error{'s' if n_sub != 1 else ''}. See the diff table below."
        )
        # Wrong-first-author case: title is essentially perfect but the
        # canonical's first author differs from what the user wrote. Likely
        # the user attached the right title to the wrong author list (or vice
        # versa). Flag this explicitly so the user knows to investigate.
        if (ref.title and best and best.title
            and fuzz.token_set_ratio(normalize_title(ref.title), normalize_title(best.title)) >= 97
            and ref.authors and best.authors
            and ref.authors[0].family and best.authors[0].family
            and normalize_author_surname(ref.authors[0].family)
                != normalize_author_surname(best.authors[0].family)
            and author_surname_overlap(ref.authors, best.authors) < 0.34):
            notes.append(
                f"Title matched the canonical record almost perfectly, but the author lists do "
                f"NOT overlap. The user cited the title with '{ref.authors[0].family}' as first author; "
                f"the canonical record's first author is '{best.authors[0].family}'. Either the title "
                f"was attached to the wrong author list, or the user is citing a different paper that "
                f"happens to share a title. Investigate the source manually."
            )
    elif confidence == Confidence.VERIFIED_WITH_STYLE_EDITS:
        notes.append(
            "All substantive citation components are correct. The suggestions below are cosmetic style edits "
            "that bring the entry into strict conformity with the chosen citation style."
        )

    final_matches = ([best] + corroborating) if best else cands

    return VerificationResult(
        parsed=ref,
        matches=final_matches,
        canonical=best,
        confidence=confidence,
        discrepancies=substantive,
        style_edits=style_edits,
        notes=notes,
    )


def verify_all(
    refs: List[ParsedReference],
    use_scholarly: bool = False,
    max_workers: int = 4,
    progress=None,
    strictness: str = "default",
    timeout_seconds: Optional[float] = None,
) -> List[VerificationResult]:
    """Verify a list of references. Limited concurrency so we stay polite to APIs.

    `progress` callback receives a MONOTONIC completion counter
    (1, 2, 3, ..., n_refs) -- not the input index of the completed ref.
    The latter is confusing because as_completed yields in arbitrary order.
    """
    # Fresh per-run state. The disable flags persist within a Python process,
    # so library callers and repeated CLI runs would otherwise carry state
    # over. Seed with any persistent cooldowns from previous recent runs.
    reset_api_disable_state()
    results: List[Optional[VerificationResult]] = [None] * len(refs)
    completed = 0
    n = len(refs)
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {
            ex.submit(verify_one, r, use_scholarly, strictness, timeout_seconds): i
            for i, r in enumerate(refs)
        }
        for f in as_completed(futures):
            i = futures[f]
            try:
                results[i] = f.result()
            except Exception as e:
                log.exception("verify_one failed for ref %d", i)
                results[i] = VerificationResult(parsed=refs[i], confidence=Confidence.HALLUCINATED,
                                                 notes=[f"verification error: {e}"])
            completed += 1
            if progress:
                progress(completed, n)
    return [r for r in results if r is not None]
