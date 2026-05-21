"""Command-line entry point.

Two subcommands:

  audit    -- the v1 deliverable. Read a file, extract references, verify each
              against authoritative metadata APIs, flag discrepancies, and
              emit Markdown / JSON / annotated-docx / BibTeX outputs.

  suggest  -- the v2 deliverable (lower priority). Read a paper that has no
              references, identify factual claims, search for candidate
              supporting sources, and return them with confidence scores.
              Important: this NEVER auto-inserts citations. It returns
              candidates that the human must review and accept.

Examples:
  python -m scripts.main audit examples/sample_bibliography.txt --style apa
  python -m scripts.main audit paper.pdf --style ama --out audit_out/
  python -m scripts.main suggest paper.docx --style mla --out suggestions.json
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import List, Optional

from .extract import extract
from .verify import verify_all, verify_one, detect_duplicate_entries
from .report import write_all, DEFAULT_FORMAT_BY_INPUT
from .format_citation import SUPPORTED_STYLES, format_reference
from .models import VerificationResult, Confidence
from .intext import cross_check, CrossCheckResult


# All supported output format keys. `auto` means: pick based on input extension.
ALL_FORMATS = ["markdown", "json", "docx", "bibtex", "ris", "html"]


def _resolve_formats(formats_arg: List[str], input_path: Path) -> List[str]:
    """Resolve --format into a concrete list. 'auto' uses input-aware defaults."""
    if not formats_arg or formats_arg == ["auto"]:
        return DEFAULT_FORMAT_BY_INPUT.get(input_path.suffix.lower(), ["docx"])
    # Allow 'all' as a shorthand for everything.
    if "all" in formats_arg:
        return list(ALL_FORMATS)
    return [f for f in formats_arg if f in ALL_FORMATS]


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _cmd_audit(args: argparse.Namespace) -> int:
    src = Path(args.input)
    if not src.exists():
        print(f"input not found: {src}", file=sys.stderr)
        return 2

    resolved_formats = _resolve_formats(args.formats, src)
    print(f"Extracting references from {src} ...")
    try:
        refs, intext = extract(src, parser_backend=args.parser)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 4
    if not refs:
        print("No references could be extracted. Is this the right file?", file=sys.stderr)
        return 3
    print(f"Extracted {len(refs)} references and {len(intext)} in-text citations.")
    print(f"Output formats: {', '.join(resolved_formats)}")

    def _progress(completed: int, n: int) -> None:
        print(f"  verified {completed}/{n} ...", file=sys.stderr)

    strictness = "strict" if args.strict else ("relaxed" if args.relaxed else "default")
    if strictness != "default":
        print(f"Strictness: {strictness}")
    print(f"Verifying against CrossRef, OpenAlex, PubMed, Semantic Scholar, arXiv ...")
    results = verify_all(refs, use_scholarly=args.use_scholarly, max_workers=args.workers,
                         progress=_progress, strictness=strictness,
                         timeout_seconds=args.timeout_seconds_per_ref)

    # In-text vs bibliography cross-check. Only meaningful when the input is a
    # full paper (has both body and references), not a bare bibliography.
    ccheck: Optional[CrossCheckResult] = None
    if intext:
        ccheck = cross_check(refs, intext)
        if ccheck.orphan_intext or ccheck.unused_bibliography:
            print(f"  cross-check: {len(ccheck.orphan_intext)} orphan in-text citations, "
                  f"{len(ccheck.unused_bibliography)} unused bibliography entries")

    # Duplicate bibliography entries.
    dups = detect_duplicate_entries(results)
    if dups:
        print(f"  duplicates: {len(dups)} pair(s) of bibliography entries reference the same paper")

    out_dir = Path(args.out)
    written = write_all(results, style=args.style, out_dir=out_dir, source_name=src.name,
                        formats=resolved_formats, crosscheck=ccheck, duplicates=dups)

    counts = {}
    for r in results:
        counts[r.confidence.value] = counts.get(r.confidence.value, 0) + 1
    print("")
    print("Verification summary:")
    for k, v in counts.items():
        print(f"  {k}: {v}")
    print("")
    print("Wrote:")
    for k, v in written.items():
        print(f"  {k}: {v}")
    return 0


def _cmd_suggest(args: argparse.Namespace) -> int:
    """Find candidate sources for claims in a paper that has no references.

    Implementation note: rather than doing full claim-extraction with an LLM
    inside this script, we extract candidate noun-phrase claims via simple
    heuristics (sentences containing 'studies show', 'research suggests',
    'evidence indicates', and a few patterns), then search OpenAlex by the
    claim text. The caller (typically Claude orchestrating the skill) is the
    right place to do smarter claim extraction; this script intentionally
    keeps that piece dumb so the LLM-side stays auditable.
    """
    src = Path(args.input)
    if not src.exists():
        print(f"input not found: {src}", file=sys.stderr)
        return 2

    # Read the source as text.
    if src.suffix.lower() == ".pdf":
        import pdfplumber
        with pdfplumber.open(str(src)) as pdf:
            text = "\n".join(p.extract_text() or "" for p in pdf.pages)
    elif src.suffix.lower() == ".docx":
        import docx
        text = "\n".join(p.text for p in docx.Document(str(src)).paragraphs)
    else:
        text = src.read_text(encoding="utf-8", errors="replace")

    # Either accept --claim repeatedly, or auto-detect.
    claims: List[str] = list(args.claim or [])
    if not claims:
        import re
        pat = re.compile(
            r"([^.\n]*?(?:studies? show|research suggests?|evidence (?:indicates?|shows?|suggests?)"
            r"|it (?:has been|is) (?:shown|demonstrated|reported)|according to)[^.\n]{0,300}\.)",
            re.IGNORECASE,
        )
        claims = [m.group(1).strip() for m in pat.finditer(text)]
        if not claims:
            print("No factual-claim sentences were auto-detected and --claim was not provided.", file=sys.stderr)
            print("Pass --claim 'sentence to search for' one or more times.", file=sys.stderr)
            return 3

    # For each claim, run a title-equivalent search (the verify module's
    # candidate gatherer is designed for that; we feed in a faux ParsedReference).
    from .models import ParsedReference
    suggestions = []
    for claim in claims:
        ref = ParsedReference(raw_text=claim, title=claim[:200])
        result = verify_one(ref, use_scholarly=args.use_scholarly)
        top = []
        for c in result.matches[:5]:
            top.append({
                "title": c.title,
                "authors": [a.full for a in c.authors[:10]],
                "year": c.year,
                "venue": c.container_title,
                "doi": c.doi,
                "url": c.source_url,
                "source": c.source,
                "formatted": format_reference(c, args.style) if c.title else "",
            })
        suggestions.append({
            "claim": claim,
            "candidates": top,
            "instruction": "Review each candidate's title and abstract before citing. "
                           "Existence is not the same as supporting the claim.",
        })

    out_path = Path(args.out) if args.out else None
    payload = json.dumps({"style": args.style, "suggestions": suggestions}, indent=2, ensure_ascii=False)
    if out_path:
        out_path.write_text(payload, encoding="utf-8")
        print(f"Wrote suggestions to {out_path}")
    else:
        print(payload)
    return 0


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="biblio-check", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    pa = sub.add_parser("audit", help="check an existing bibliography")
    pa.add_argument("input", help="path to .pdf | .docx | .txt | .md | .bib | .ris")
    pa.add_argument("--style", default="apa", choices=SUPPORTED_STYLES,
                    help="citation style for corrected output (default: apa)")
    pa.add_argument("--out", default="./biblio-check-output", help="directory to write reports to")
    pa.add_argument("--format", "--formats", dest="formats", nargs="+", default=["auto"],
                    choices=["auto", "all", "markdown", "json", "docx", "bibtex", "ris", "html"],
                    help="output format(s). 'auto' (default) picks based on input "
                         "(.docx->docx, .bib->bibtex, .ris->ris, others->docx). "
                         "'all' emits every format. You may also pass multiple values, "
                         "e.g. --format markdown html.")
    pa.add_argument("--workers", type=int, default=4, help="parallel API workers (default: 4)")
    pa.add_argument("--parser", default="auto",
                    choices=["auto", "regex", "anystyle", "grobid"],
                    help="reference parser backend. 'auto' (default) uses AnyStyle "
                         "or GROBID if installed, else the built-in regex parser. "
                         "'regex' forces the dependency-free parser. AnyStyle needs "
                         "the `anystyle` CLI on PATH; GROBID needs GROBID_URL set.")
    pa.add_argument("--use-scholarly", action="store_true",
                    help="enable scholarly (Google Scholar scraper) as last-resort fallback")
    strictness_grp = pa.add_mutually_exclusive_group()
    strictness_grp.add_argument(
        "--strict", action="store_true",
        help="refuse the VERIFIED (grey literature) tier; sources not in an "
             "academic database are classified HALLUCINATED for hand-review.",
    )
    strictness_grp.add_argument(
        "--relaxed", action="store_true",
        help="treat institutional + publisher entries (e.g. AMA government "
             "reports) as VERIFIED even without a URL. For policy / public-"
             "health workflows that cite a lot of agency sources.",
    )
    pa.add_argument(
        "--timeout-seconds-per-ref", type=float, default=None,
        help="per-reference verification time budget (seconds). If set, slow "
             "API calls are skipped and the reference is classified with "
             "whatever evidence was gathered before the deadline.",
    )
    pa.add_argument("--verbose", action="store_true")
    pa.set_defaults(func=_cmd_audit)

    ps = sub.add_parser("suggest", help="suggest candidate sources for claims in a paper")
    ps.add_argument("input", help="path to .pdf | .docx | .txt | .md")
    ps.add_argument("--claim", action="append",
                    help="explicit claim sentence (repeatable). If omitted, auto-detect.")
    ps.add_argument("--style", default="apa", choices=SUPPORTED_STYLES)
    ps.add_argument("--out", help="output JSON path (default: stdout)")
    ps.add_argument("--use-scholarly", action="store_true")
    ps.add_argument("--verbose", action="store_true")
    ps.set_defaults(func=_cmd_suggest)

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
