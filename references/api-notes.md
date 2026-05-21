# Notes on the verification APIs

Practical quirks we discovered while building this skill. None of these are documented in the official API references in this much detail, but they materially affect how the tool should be used.

## CrossRef

Endpoint: `https://api.crossref.org/works`.

- Include a `mailto` in your User-Agent. CrossRef routes politely-identified clients onto a faster pool. The script does this automatically via `BIBLIO_CHECK_MAILTO` env var.
- The top search result for a title is sometimes a test DOI (registrant `10.65215`) rather than the canonical record. Never trust top-1 by title alone. Always score candidates by title fuzz + author overlap + year.
- DOI lookups are reliable. If you have a DOI, prefer the direct lookup over search.
- Rate limits: ~50 req/s for the polite pool. We use 4 workers, which is well under.

## OpenAlex

Endpoint: `https://api.openalex.org/works`.

- Coverage is broader than CrossRef: includes preprints, books, datasets, and works without DOIs.
- Sometimes returns records where the metadata is partially corrupted (we observed wrong DOI and year fields on a real paper while testing). The OpenAlex Work ID (`W...`) is stable even when individual fields are wrong, so use the W-ID as the canonical identifier and cross-check fields against other sources.
- The `primary_location.source.display_name` is the container title (journal/conference). It can be null.
- Recommend including `mailto=` parameter for higher priority queueing.

## PubMed E-utilities

Endpoints: `esearch.fcgi`, `esummary.fcgi`.

- Free, no key required. With a free API key (NCBI), rate limit goes from 3/s to 10/s.
- The `result.<pmid>` block contains the actual record; the top-level `result.uids` is just the ordered list.
- Authors come back as a list of `{name: "Surname FM"}` objects.
- DOI is in the `articleids` array with `idtype == "doi"`.
- Use only for biomedical literature. Other topics will return zero results.

## Semantic Scholar

Endpoint: `https://api.semanticscholar.org/graph/v1/paper/search`.

- Aggressive rate limiting without an API key. We routinely got 429s during development.
- With backoff + retries, we get partial results most of the time; treat S2 as best-effort.
- Apply for a free API key at https://www.semanticscholar.org/product/api#api-key-form if you'll use this skill heavily. Set `S2_API_KEY` env var if we add support later.

## arXiv

Endpoint: `http://export.arxiv.org/api/query`.

- Returns Atom XML, not JSON. Parse with ElementTree.
- Title and abstract fields contain newlines and stray whitespace; normalize before comparing.
- DOI may appear in the `arxiv:doi` element if the paper has been published elsewhere.
- ID-based lookup (`id_list=`) is the reliable path. Title search can return loosely related papers.

## scholarly (Google Scholar scraper)

- Package: https://github.com/scholarly-python-package/scholarly
- Scrapes Google Scholar. Google blocks scrapers aggressively. Expect intermittent failures.
- Use only when nothing else has returned a match. Never treat a scholarly-only result as `VERIFIED`.
- Supports proxies (`scholarly.use_proxy()`). If using heavily, set up a proxy rotation.

## How we combine sources

We score each returned candidate against the parsed reference with a weighted formula:

```
0.55 * title_fuzz(0..100)
+ 0.25 * author_surname_overlap(0..100)
+ 0.15 * year_score(0..100)
+ 0.05 * doi_match_bonus(0..30)
```

A score of 88+ is treated as a strong match. We then require at least two independent sources to return a strong match before we call a reference `VERIFIED`. A single-source strong match is `LIKELY`.

This is deliberate. Any single API can be wrong. Consensus across CrossRef + OpenAlex + (PubMed | S2 | arXiv) is the only thing we trust.
