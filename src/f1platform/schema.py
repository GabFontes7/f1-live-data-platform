"""Print what actually landed in the bronze layer.

Written because guessing a source's columns is how a pipeline gets built
against a schema that does not exist. This reads what is on disk and says so,
which is also the fastest way to answer "what does this table hold again?"
months later.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

DEFAULT_BRONZE_DIR = Path("data/bronze")


def session_dir(slug: str, bronze_dir: Path) -> Path:
    path = bronze_dir / slug
    if not path.is_dir():
        available = sorted(p.name for p in bronze_dir.glob("*") if p.is_dir())
        raise FileNotFoundError(
            f"no bronze session at {path}. Available: {', '.join(available) or 'none'}"
        )
    return path


def tables_in(path: Path) -> list[Path]:
    return sorted(path.glob("*.parquet"))


def describe_table(path: Path) -> str:
    frame = pd.read_parquet(path)
    lines = [f"\n## {path.stem}  ({len(frame):,} rows x {len(frame.columns)} columns)"]
    width = max((len(str(c)) for c in frame.columns), default=0)
    for column in frame.columns:
        non_null = int(frame[column].notna().sum())
        lines.append(
            f"  {str(column).ljust(width)}  {str(frame[column].dtype).ljust(16)}"
            f"  {non_null:,}/{len(frame):,} non-null"
        )
    return "\n".join(lines)


def show_table(path: Path, rows: int | None) -> str:
    frame = pd.read_parquet(path)
    with pd.option_context(
        "display.max_columns", None, "display.width", 250, "display.max_rows", None
    ):
        body = frame.to_string() if rows is None else frame.head(rows).to_string()
    return f"\n## {path.stem}\n{body}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="f1-schema",
        description="Show the columns and dtypes of a bronze session.",
    )
    parser.add_argument("--session", required=True, help="bronze slug")
    parser.add_argument("--bronze-dir", type=Path, default=DEFAULT_BRONZE_DIR)
    parser.add_argument(
        "--show",
        action="append",
        default=[],
        help="also print this table's contents; repeatable",
    )
    parser.add_argument(
        "--rows",
        type=int,
        default=None,
        help="limit printed rows for --show (default: all)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        path = session_dir(args.session, args.bronze_dir)
    except FileNotFoundError as error:
        print(error, file=sys.stderr)
        return 1

    manifest_path = path / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        print(f"# {manifest['slug']}  ({manifest['event']} {manifest['session']})")

    for table in tables_in(path):
        print(describe_table(table))

    for name in args.show:
        table = path / f"{name}.parquet"
        if not table.exists():
            print(f"\n## {name}: not in this session", file=sys.stderr)
            continue
        print(show_table(table, args.rows))

    return 0


if __name__ == "__main__":
    sys.exit(main())
