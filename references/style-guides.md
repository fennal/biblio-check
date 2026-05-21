# Citation style notes

Per-style implementation notes for the seven styles this skill emits. These are working notes, not a substitute for the canonical style manuals.

## APA 7

- Authors: surname first, comma, initials with periods. Up to 20 authors listed; if more, list first 19, then `...`, then the final author.
- Sentence case for titles.
- Italic journal name, then volume in italics, issue in parentheses, comma, page range, period.
- DOI as bare URL `https://doi.org/...`.

## MLA 9

- First author: Surname, Full Given Name. Subsequent: Given Surname.
- More than two authors: `Surname, Given, et al.`
- Title case for titles, in double quotes.
- Italic container title.
- `vol. N, no. M, Year, pp. X-Y.`
- DOI as `doi:...` at the end.

## AMA 11

- Authors: `Surname FM` with no periods on initials and no commas between them.
- Up to six authors listed; if more, list first three, then `et al`.
- Sentence case titles; italicize journal name (we render with `*...*` markdown asterisks).
- `Journal. Year;Volume(Issue):Pages.`
- DOI as `doi:...` appended.
- Traditionally uses superscript numbers in text; we leave that to the consumer.

## Chicago 17 (author-date)

- First author surname-first; subsequent given-first.
- Title case in quotes.
- `Year. "Title." *Journal* Vol(Issue): Pages.`
- DOI as full URL.

## Vancouver / ICMJE

- Same author convention as AMA.
- Title case for titles, plain text (no italics, no quotes).
- Up to six authors; if more, first six then `et al.`
- Numbered list in citation order, not alphabetical.

## IEEE

- Authors as `F. M. Surname, G. H. Surname, and I. Surname`.
- Sentence-case titles in double quotes.
- Italic journal name, `vol. N, no. M, pp. X-Y, Year`.
- Numbered citations in text, e.g. `[3]`.

## Harvard

- Not standardized. We implement the Cite Them Right variant common in UK universities.
- `Surname, F.M., Surname, G.H. and Surname, I.J. (Year) 'Title', *Journal*, Volume(Issue), pp. X-Y. doi: ...`
- More than three authors: `Surname et al.`
- If the user's institution requires a different Harvard flavor, the output will need manual adjustment.

## When the source type matters

For all styles, the formatter switches on `CitationType`:

- journal-article: standard journal template
- conference-paper: similar to journal but uses `In Proceedings of ...` for some styles
- book: publisher, year, no journal/volume
- book-chapter: chapter title, then book title and editors
- preprint: arXiv-aware template
- report, thesis, website: generic templates

Edge cases the formatter does not fully handle:

- Translated works (translator credit)
- Multi-volume works with editor credit
- Edited collections with both author and editor for the same entry
- Online sources with no print equivalent (we emit a URL but the formatting is generic)

These are flagged in the audit's "notes" field and the user should manually adjust.
