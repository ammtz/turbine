"""CLI entrypoint placeholder (wired in a later milestone)."""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    _ = argv if argv is not None else sys.argv[1:]
    print("turbine: CLI arrives in M6. Use the library API for now.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
