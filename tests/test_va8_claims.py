"""VA8: every factual claim in a report carries its evidence tag."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from turbine import claims

PAGES = {"https://example.org/spec": "<p>The budget is a hard stop.</p>\n<p>Cache reads are logged separately.</p>"}


def run_cli(report: str, pages: dict | None = None) -> tuple[int, str]:
    with tempfile.TemporaryDirectory() as d:
        r = Path(d, "report.md")
        r.write_text(report, encoding="utf-8")
        args = [str(r)]
        if pages is not None:
            p = Path(d, "pages.json")
            p.write_text(json.dumps(pages), encoding="utf-8")
            args += ["--pages", str(p)]
        out = io.StringIO()
        with redirect_stdout(out):
            code = claims.main(args)
    return code, out.getvalue()


class TestClaims(unittest.TestCase):
    def test_untagged_claim_flagged(self) -> None:
        report = "All good here.\nThe ledger caps spend at 50000 tokens.\nTurbine ships under MIT.\n"
        code, out = run_cli(report)
        self.assertEqual(code, 1)
        self.assertIn("report.md:2:", out)
        self.assertIn("report.md:3:", out)
        self.assertNotIn("report.md:1:", out)

    def test_source_excerpt_must_match(self) -> None:
        ok = 'Spend has a hard stop [source: https://example.org/spec "The budget is a hard stop."].\n'
        self.assertEqual(run_cli(ok, PAGES)[0], 0)
        bad = 'Spend has a hard stop [source: https://example.org/spec "The budget is a soft stop."].\n'
        code, out = run_cli(bad, PAGES)
        self.assertEqual(code, 1)
        self.assertIn("excerpt not found", out)
        unknown = 'Spend has a hard stop [source: https://example.org/other "The budget is a hard stop."].\n'
        code, out = run_cli(unknown, PAGES)
        self.assertEqual(code, 1)
        self.assertIn("could not read", out)

    def test_inferred_passes(self) -> None:
        report = "Three workers would finish by Monday [inferred].\n"
        self.assertEqual(run_cli(report)[0], 0)
        self.assertEqual(run_cli("Should Andres merge PR 4 first?\n")[0], 0)

    def test_seen_passes_and_needs_a_url(self) -> None:
        self.assertEqual(run_cli("CI is green on PR 4 [seen: https://github.com/x/y/pull/4].\n")[0], 0)
        code, out = run_cli("CI is green on PR 4 [seen: somewhere].\n")
        self.assertEqual(code, 1)
        self.assertIn("not a URL", out)

    def test_tag_covers_only_its_sentence(self) -> None:
        report = "Turbine uses MIT [seen: https://github.com/x/y]. It has 3 milestones left.\n"
        code, out = run_cli(report)
        self.assertEqual(code, 1)
        self.assertIn("3 milestones", out)

    def test_code_fences_and_links_are_not_claims(self) -> None:
        report = "```\nx = 42\nFooBar()\n```\nsee https://example.org/a1b2\n"
        self.assertEqual(run_cli(report)[0], 0)

    def test_markdown_and_html_are_not_prose(self) -> None:
        report = (
            "<!-- BODY_BEGIN -->\n## Summary\n"
            "- [x] Tests pass [seen: https://github.com/x/y/actions/runs/1].\n"
            "- [ ] CI green on `m3-branch`\n"
            '<div><a href="https://x.com/a"><img alt="Open in Web" width="114"></a></div>\n'
        )
        self.assertEqual(run_cli(report)[0], 0)
        self.assertEqual(run_cli("Generated with [Claude Code](https://claude.com/claude-code)\n")[0], 0)
        code, out = run_cli("- [x] Environ scrub test passes (33 OK)\n")
        self.assertEqual(code, 1)
        self.assertIn("report.md:1:", out)

    def test_excerpt_match_ignores_markup_and_spacing(self) -> None:
        f = claims.Findings()
        text = 'Logged [source: https://example.org/spec "Cache   reads are logged separately"].'
        claims.check_line(text, 1, PAGES.get, f)
        self.assertEqual(f.items, [])


if __name__ == "__main__":
    unittest.main()
