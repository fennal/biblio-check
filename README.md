# biblio-check

Verify, correct, and reformat academic bibliographies. It treats citation
checking as a structured-data problem: extract references from a document,
look each one up across multiple authoritative metadata APIs, and report a
verdict with evidence. Built to catch fabricated/hallucinated citations in
LLM-assisted drafts, and to flag real-but-mis-cited references.

## What's guaranteed vs best-effort

This distinction matters if you're going to rely on it.

**Guaranteed (works anywhere Python runs):**

- It runs with only pip-installable dependencies. No Ruby, no Java, no Docker.
- It never crashes on bad input. Malformed entries, empty files, garbage,
  non-English text, and unusual formats degrade to "flagged for review,"
  never an unhandled exception. This is enforced by the test suite.
- The verification verdict is conservative: a citation is only marked
  VERIFIED when independent sources agree, so the failure mode is
  "flagged for manual review," not "confidently wrong."

**Best-effort (improves with optional tools):**

- Parse accuracy on exotic citation formats. The built-in parser is
  regex-based and handles APA, MLA, AMA, Vancouver, IEEE, numbered, and
  superscript citations plus institutional/grey-literature entries. It will
  still mis-parse some unusual inputs. When it does, the mis-parse usually
  surfaces as a "PARTIAL, please review" rather than a wrong verdict.
- For higher parse accuracy, install one of the optional backends below.

## Confidence tiers

| Tier | Meaning |
|---|---|
| VERIFIED | Two or more independent APIs agree on every substantive field. Safe as written. |
| VERIFIED (style edits) | Content correct; cosmetic fixes suggested (en-dash, missing initial, etc.). |
| VERIFIED (grey literature) | Not in academic databases (government/NGO/agency reports); URL resolves or the source is clearly institutional. |
| PARTIAL | A real paper was found but the citation has substantive errors. A corrected form is provided. |
| HALLUCINATED | No source returned a confident match. Treat as fabricated until manually confirmed. |

## Install

```
pip install -r requirements.txt
```

That's everything you need. Python 3.9+.

## Use

```
python -m scripts.main audit my_paper.docx --style ama
```

Inputs: `.pdf`, `.docx`, `.txt`, `.md`, `.bib`, `.ris`.
Styles: `apa`, `mla`, `ama`, `chicago`, `vancouver`, `ieee`, `harvard`.
Output formats: `markdown`, `json`, `docx` (tracked changes), `html`, `bibtex`,
`ris`. Default is chosen from the input type; override with `--format`.

Useful flags:

- `--strict` — refuse the grey-literature tier; surface every non-academic
  source as HALLUCINATED for hand review.
- `--relaxed` — treat institutional sources (agency/NGO reports) as VERIFIED.
- `--parser {auto,regex,anystyle,grobid}` — choose the extraction backend.
- `--timeout-seconds-per-ref N` — cap per-reference verification time.
- `--workers N` — parallel API workers.

## Optional parser backends

These are *accelerators*, never required. If neither is present, the built-in
parser is used and the tool works exactly the same, just with slightly lower
parse accuracy on unusual formats.

- **AnyStyle** (trained CRF reference parser): install the Ruby gem
  (`gem install anystyle-cli`) so the `anystyle` binary is on PATH. The tool
  auto-detects it. Note: AnyStyle needs a native build toolchain, which is why
  it isn't a hard dependency.
- **GROBID** (scholarly-document parser): run the service
  (`docker run --rm -p 8070:8070 lfoppiano/grobid:0.8.1`) and set
  `GROBID_URL=http://localhost:8070`. Used for PDFs and reference strings.

## What verification does NOT do

Verification confirms a cited work *exists* and that its metadata is correct.
It does not confirm the cited work actually *supports* the claim it's attached
to in the body. That semantic check is the author's responsibility.

## Tests

```
pip install -r requirements-dev.txt
pytest
```

The suite is fully offline (network calls are monkeypatched), deterministic,
and runs in well under a second. Every bug found in real-world use has a
regression test. CI runs it on Python 3.9–3.12.

## Configuration (environment variables)

- `BIBLIO_CHECK_MAILTO` — contact email for the CrossRef/OpenAlex polite pool.
- `BIBLIO_CHECK_CACHE` — cache directory (default `~/.cache/biblio-check`).
- `BIBLIO_CHECK_CACHE_TTL_DAYS` — cache TTL in days (default 7).
- `BIBLIO_CHECK_NO_CACHE=1` — disable the on-disk cache.
- `BIBLIO_CHECK_PARSER` — default parser backend (overridden by `--parser`).
- `GROBID_URL` — enable the GROBID backend.

## Known limitations

- Conference proceedings cited with journal-style abbreviations may flag as
  PARTIAL (the venue names differ substantially from the canonical form).
- Semantic Scholar rate-limits aggressively without an API key; if many
  references come back missing only from S2, that's a rate-limit issue, the
  other four sources still verify them.
- The regex parser is heuristic; the optional backends exist precisely to
  raise the ceiling on parse accuracy for those who need it.
