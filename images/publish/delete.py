"""Delete one complete or incomplete publication using its saved record."""

from __future__ import annotations

import argparse
from pathlib import Path

from .core import cleanup_record, run_cli


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record", type=Path, required=True, help="Publication JSON record to delete")
    parser.add_argument("--profile", help="AWS profile; omitted uses ambient credentials")
    args = parser.parse_args(argv)

    def run() -> None:
        cleanup_record(args.record, args.profile)
        print(f"Deleted publication recorded in {args.record}")

    return run_cli(run)


if __name__ == "__main__":
    raise SystemExit(main())
