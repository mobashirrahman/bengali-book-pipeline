"""Entry point for the `book` and `publish` commands.

Run `python -m pdf_craft_tool <command> --help` for a command's options.
Cluster processing, gold-label adjudication, manual review, and
benchmarking are separate, independently runnable modules — see each
module's own docstring, or `python -m pdf_craft_tool.<module> --help`.
"""

from __future__ import annotations

import argparse

from .book import add_book_command
from .publish import add_publish_command


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", title="commands", required=True)
    add_book_command(commands)
    add_publish_command(commands)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = args.handler(args)
    return result if isinstance(result, int) else 0
