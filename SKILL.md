---
name: biblio-check
description: Verify, correct, and reformat academic bibliographies. Use when the user asks to check, audit, validate, or verify citations or references in a paper, or asks whether a bibliography contains hallucinated, fabricated, or wrong sources. Also triggers on "check my references", "audit my works cited", "are these citations real", "fix the bibliography", "convert citations to APA/MLA/AMA/Chicago/Vancouver/IEEE/Harvard", "format my works cited", or any task that involves a .bib file, .ris file, or a references/works-cited section of a paper. Use this skill whenever the user mentions citations, references, bibliography, works cited, DOI lookup, or worries about hallucinated sources, even if they do not explicitly use the word "check". For papers that have no references and need them, use the suggest subcommand to find candidate sources, but never auto-insert citations without human review.
---

# biblio-check

Verifies academic citations against authoritative metadata APIs, classifies each entry into a three-tier confidence system, and re-emits references in the user's chosen style. Treats reference verification as a structured-data problem: the language model orchestrates, but the verdicts come from CrossRef, OpenAlex, PubMed, Semantic Scholar, and arXiv.

## When to use this skill

Use it whenever the user wants to:

- check whether the references in their paper are real and correctly cited
- detect hallucinated or fabricated citations (especially in LLM-assisted drafts)
- reformat a bibliography into APA, MLA, AMA, Chicago, Vancouver, IEEE, or Harvard
- convert between bibliography formats
- audit a `.bib` or `.ris` file before submission
- find candidate supporting sources for a paper that has no references yet

If the user mentions any of citations, references, bibliography, works cited, DOI lookup, journal articles, in-text citations, or expresses worry about hallucinated sources, this is the right skill.

## What this skill does NOT do

Reference verification is not the same as fact-checking. This skill confirms that a cited work exists and that its metadata (title, authors, year, journal, DOI) is correct. It does not confirm that the cited work actually supports the claim it is attached to in the body text. Make this distinction clear to the user when reporting results.

## The three-tier classification

Every reference lands in one of three tiers (the first tier has three sub-flavors):

- **VERIFIED** -- all substantive components (authors, title, year, journal, volume, issue, pages, DOI) match the canonical record returned by at least two independent verification APIs. The citation can stay as written.
  - **VERIFIED (style edits suggested)** -- substantive content is correct, but cosmetic edits would bring it into strict conformity with the requested style (e.g. add a missing middle initial, italicize the journal name, convert hyphen to en-dash in page ranges). The original is still accurate; the edits are polish.
  - **VERIFIED (grey literature / non-academic source)** -- not indexed in any academic database, which is normal for government reports, NGO publications, agency websites, and standards documents. The tool either confirmed existence via URL HEAD-resolution OR detected an institutional author plus named publisher (e.g. 'National Committee for Quality Assurance. ... Washington, DC: NCQA; 2025.'). Kept verbatim; no canonical reformat is attempted because there is no authoritative metadata to draw from. Adding the publication URL to citations of this kind makes verification more direct on future runs.
- **PARTIAL** -- a real paper matching most fields was found, but the citation as written has substantive errors (wrong year, wrong author surname, wrong DOI, wrong volume, etc.). The report shows a field-by-field diff table with severity and proposes a corrected form.
- **HALLUCINATED** -- no verification API returned a record that confidently matches the citation. This is the strongest signal of fabrication.

Single-source matches are treated as HALLUCINATED unless backed by an exact identifier (DOI, arXiv ID, PMID) the user provided. We require this because every individual API has data quality issues; consensus across sources is the only thing we trust.

## Usage

The skill bundles a Python module `scripts.main` with two subcommands.

### Audit an existing bibliography

```bash
python -m scripts.main audit <input> --style <style> --format <format> --out <dir>
```

- `<input>` is a path to a `.pdf`, `.docx`, `.txt`, `.md`, `.bib`, or `.ris` file
- `<style>` is one of `apa`, `mla`, `ama`, `chicago`, `vancouver`, `ieee`, `harvard`
- `<format>` controls which output artifacts are produced. Defaults to `auto`, which picks based on the input:
  - `.docx` input -> annotated `.docx` with tracked changes
  - `.bib` input -> corrected `.bib`
  - `.ris` input -> corrected `.ris`
  - `.pdf`, `.txt`, `.md` input -> annotated `.docx` with tracked changes
- You can request multiple formats: `--format markdown docx`. `--format all` emits everything.
- Available formats: `markdown`, `json`, `docx`, `bibtex`, `ris`.

### Suggest sources for a paper that has no references

```bash
python -m scripts.main suggest <input> --style apa --out suggestions.json
```

Finds candidate sources for factual claims in the paper. NEVER auto-inserts. The user must review each candidate and decide whether it actually supports the claim.

## Installation

Before first use, install Python dependencies:

```bash
pip install -r requirements.txt
```

## How Claude should drive this skill

When a user wants their bibliography checked:

1. **Confirm the style.** If they haven't specified, ask. The styles are `apa`, `mla`, `ama`, `chicago`, `vancouver`, `ieee`, `harvard`.
2. **Confirm the output format.** If the input is `.docx`, `.bib`, or `.ris`, the default is the same format back, and you can usually proceed without asking. If the input is `.pdf`, `.txt`, or `.md`, the default is an annotated `.docx` with tracked changes, but offer the markdown report as an alternative if the user prefers a read-only summary. Always honor an explicit request like "give me just a markdown report" or "I want a bibtex file".
3. **Locate the input.** If the user pasted a bibliography in chat, save it to a `.txt` first.
4. **Run the audit.** `python -m scripts.main audit <input> --style <style> --format <chosen> --out <dir>`
5. **Read `audit.md` (if produced)** or `audit.json` and report a short summary: total checked, count per tier, and call out by name anything classified `HALLUCINATED` or `PARTIAL`.
6. **Present the output files** using the `present_files` tool. Do NOT enumerate the contents of every entry inline; the user will read the report themselves.

### Style choice when the user is undecided

If the user doesn't know which style they need:

- Medical or biomedical paper -> AMA or Vancouver
- Psychology, education, social sciences -> APA
- Humanities, literature -> MLA
- History, some social sciences -> Chicago
- Engineering, computer science -> IEEE
- UK / Anglo-Australian context -> Harvard

When in doubt, ask which journal or institution the user is targeting and look up its required style.

### Examples of when to invoke

**Invoke**:

- "Can you check the references in my dissertation? It's a .docx. I'm worried some of them might be hallucinated, I used an LLM to draft."
- "Audit this bibliography for me, MLA style, and tell me which ones are fake."
- "Convert these references from APA to AMA."
- "Here's my works cited from my paper. Are these real?"
- "I have a .bib file. Verify every entry."
- "I have a paper draft with no citations yet. Suggest sources for the claims about CRISPR off-target effects."

**Do not invoke**:

- "Write me an essay on climate change." (drafting, not citation work)
- "What's the difference between APA 6 and APA 7?" (a knowledge question; just answer directly)
- "Fix the grammar in my paper." (proofreading, not citations)

## How verification works internally

For each reference, the tool queries multiple authoritative APIs in parallel:

- CrossRef (api.crossref.org) for DOIs and general scholarly metadata
- OpenAlex (api.openalex.org) for broad coverage including non-DOI works
- PubMed E-utilities for biomedical literature
- Semantic Scholar for CS/AI coverage and citation graph
- arXiv for preprints
- scholarly (Google Scholar scraper) only when `--use-scholarly` is set, as a last-resort fallback

Candidates are scored by a weighted formula:

```
0.55 * title_fuzz + 0.25 * author_surname_overlap + 0.15 * year_score + 0.05 * doi_bonus
```

A score of 88+ from the best candidate AND agreement from at least two independent sources (or an exact ID match) is the bar for VERIFIED. Substantive vs cosmetic differences are then separated: substantive ones move the entry to PARTIAL; cosmetic ones leave it under VERIFIED with style-edit suggestions.

## Output details

**Markdown report** (`audit.md`): the primary human-readable artifact. Per-entry display depends on tier:

- VERIFIED: one-line confirmation, canonical record link.
- VERIFIED (style edits): itemized list of cosmetic edits, followed by the resulting citation.
- PARTIAL: field-by-field diff table with severity, followed by proposed corrected citation.
- HALLUCINATED: warning, list of APIs queried, closest near-match for context only (never to be cited).

**Annotated .docx** (`audit_tracked.docx`): the original citation followed by the corrected form, with the changes marked using Word's tracked-changes XML so the user can accept or reject each correction in Word.

**JSON** (`audit.json`): structured data for programmatic consumers, including raw API evidence.

**Corrected .bib / .ris** (`corrected.bib`, `corrected.ris`): only canonical records for VERIFIED and PARTIAL entries; HALLUCINATED entries are written out as comments rather than dropped.

## Known limitations to surface to the user

- Semantic Scholar rate-limits aggressively without an API key. If many citations come back missing from S2, that's a rate-limit issue, not a hallucination signal. Cross-check against CrossRef and OpenAlex.
- For `.pdf` inputs, reference extraction depends on the PDF having a reasonably standard References section. Two-column layouts and figure-heavy papers may need a re-run after the user copies the raw bibliography into a `.txt`.
- The `scholarly` package scrapes Google Scholar and is unreliable; treat any `scholarly`-only match with caution.
- 'Harvard' style is not standardized. This skill implements the Cite Them Right variant. If the user's institution requires another variant, they should manually verify punctuation.
- The skill currently does not detect: duplicate entries within the bibliography, bibliography entries with no in-text citation, or in-text citations with no bibliography entry. These are planned enhancements.

## Files in this skill

- `SKILL.md` (this file)
- `scripts/extract.py` -- extract references from PDF/DOCX/BibTeX/RIS/text
- `scripts/verify.py` -- query verification APIs, score candidates, classify into tiers
- `scripts/format_citation.py` -- render canonical metadata in seven styles
- `scripts/report.py` -- emit Markdown, JSON, annotated DOCX, BibTeX, RIS outputs
- `scripts/main.py` -- CLI dispatcher with input-aware format defaulting
- `scripts/models.py` -- dataclasses shared across modules
- `scripts/normalize.py` -- string/title/author normalization
- `requirements.txt` -- Python dependencies
- `references/` -- additional reference material on per-style rules and API quirks
- `examples/` -- sample inputs for smoke-testing
