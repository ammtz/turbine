"""VA8: every factual claim in a report carries its evidence.

    python -m turbine.claims REPORT.md [--pages PAGES.json] [--fetch]

A worker's report (a PR body, a task comment) says how it knows each fact, with one of
three tags in the same sentence:

    [seen: URL]                  I looked at it myself (a run, a page, a diff)
    [source: URL "excerpt"]      the page says so; the excerpt must be on that page
    [inferred]                   my reasoning, not an observation

A sentence is a factual claim when, with tags and bare URLs taken out, it holds a digit
or a capitalised word that doesn't open the sentence (a name); a question is never a
claim. That is a heuristic: a
false positive is fixed by tagging the sentence, never by loosening the rule. Lines in
``` fences are skipped.

An excerpt is matched against the page's text with markup removed and whitespace
collapsed; case and wording must match. Pages come from --pages (a JSON object, URL to
text: fixtures, or pages read earlier), else from the network with --fetch. Exit 0 when
every claim is tagged and every excerpt is found, 1 otherwise, one line per finding.
Standard library only.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import urllib.request
from pathlib import Path
from typing import Callable, Optional

TAG = re.compile(r'\[(?:seen:\s*(?P<seen>[^\]\s]+)|source:\s*(?P<src>\S+)\s+"(?P<quote>[^"]+)"|(?P<inf>inferred))\]')
URL = re.compile(r"https?://\S+")
SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\[(\"'])")
NAME = re.compile(r"\b[A-Z][A-Za-z0-9_-]*\b")
NOT_NAMES = {"I", "A"}

PageReader = Callable[[str], Optional[str]]


class Findings:
    def __init__(self) -> None:
        self.items: list[tuple[int, str]] = []

    def add(self, line: int, why: str) -> None:
        self.items.append((line, why))


def plain(text: str) -> str:
    """Page text for matching: tags dropped, entities decoded, whitespace collapsed."""
    text = re.sub(r"<(script|style)\b.*?</\1>", " ", text, flags=re.S | re.I)
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", text)).split())


def is_claim(sentence: str) -> bool:
    bare = URL.sub(" ", TAG.sub(" ", sentence)).strip()
    if bare.endswith("?"):  # a question asks; it doesn't claim
        return False
    if re.search(r"\d", bare):
        return True
    words = NAME.findall(bare)
    first = re.match(r"\W*(\w+)", bare)
    rest = words[1:] if words and first and words[0] == first.group(1) else words
    return any(w not in NOT_NAMES for w in rest)


def check_line(text: str, n: int, read: PageReader, f: Findings) -> None:
    for sentence in SENTENCE.split(text.strip()):
        tags = list(TAG.finditer(sentence))
        if not tags:
            if is_claim(sentence):
                f.add(n, f"untagged claim: {sentence.strip()[:80]}")
            continue
        for t in tags:
            if t["seen"] and not URL.fullmatch(t["seen"]):
                f.add(n, f"[seen: {t['seen']}] is not a URL")
            if t["src"]:
                page = read(t["src"])
                if page is None:
                    f.add(n, f"could not read {t['src']}")
                elif " ".join(t["quote"].split()) not in plain(page):
                    f.add(n, f"excerpt not found on {t['src']}: \"{t['quote'][:60]}\"")


def check(report: str, read: PageReader) -> Findings:
    f = Findings()
    fenced = False
    for n, line in enumerate(report.splitlines(), 1):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if not fenced and line.strip():
            check_line(line, n, read, f)
    return f


def fetch(url: str) -> Optional[str]:
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            return r.read().decode("utf-8", errors="replace")
    except (OSError, ValueError):
        return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m turbine.claims", description="Flag untagged or unsupported claims.")
    ap.add_argument("report")
    ap.add_argument("--pages", help="JSON object: URL -> page text")
    ap.add_argument("--fetch", action="store_true", help="read pages not in --pages from the network")
    a = ap.parse_args(argv)
    pages = json.loads(Path(a.pages).read_text(encoding="utf-8")) if a.pages else {}

    def read(url: str) -> Optional[str]:
        if url in pages:
            return pages[url]
        return fetch(url) if a.fetch else None

    name = Path(a.report).name
    f = check(Path(a.report).read_text(encoding="utf-8"), read)
    for n, why in f.items:
        print(f"{name}:{n}: {why}")
    return 1 if f.items else 0


if __name__ == "__main__":
    sys.exit(main())
