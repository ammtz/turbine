"""Spend monitor: charged vs budget; WARN at 80%, HALT at 100%."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Protocol


class BudgetLike(Protocol):
    charged: int
    max_tokens: int
    cache_read_total: int


def format_report(ledger: BudgetLike) -> str:
    charged, cap = ledger.charged, ledger.max_tokens
    cache_reads = ledger.cache_read_total
    pct = (100.0 * charged / cap) if cap else 100.0
    flag = " HALT" if pct >= 100.0 else (" WARN" if pct >= 80.0 else "")
    return f"spend {charged}/{cap} ({pct:.1f}%) cache_read {cache_reads}{flag}"


class _View:
    def __init__(self, data: dict[str, Any]) -> None:
        self.charged = int(data.get("charged", 0))
        self.max_tokens = int(data.get("max_tokens", 0))
        self.cache_read_total = int(data.get("cache_read_total", 0))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="turbine.monitor")
    p.add_argument("--budget", default="turbine-spend.budget.json")
    args = p.parse_args(argv)
    path = Path(args.budget)
    if not path.is_file():
        print(f"monitor: budget file not found: {path}", file=sys.stderr)
        return 1
    print(format_report(_View(json.loads(path.read_text(encoding="utf-8")))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
