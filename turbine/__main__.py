"""CLI entry: `python -m turbine monitor` (full CLI in M6)."""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "monitor":
        from turbine.monitor import main as monitor_main

        return monitor_main(args[1:])
    print(
        "turbine: try `python -m turbine monitor`. Full CLI arrives in M6.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
