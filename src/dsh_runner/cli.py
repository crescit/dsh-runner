"""Command line: dsh-runner run | sync | check."""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import sys

from . import __version__
from .cards import load_cards
from .config import load
from .driver import Driver


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dsh-runner", description=__doc__)
    parser.add_argument("--version", action="version", version=f"dsh-runner {__version__}")
    parser.add_argument("-C", "--repo", type=Path, default=Path.cwd(), help="repository root (default: cwd)")
    parser.add_argument("--config", type=Path, help="config file (default: <repo>/dsh-runner.toml)")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run ready cards until none remain (or one with --once)")
    run.add_argument("--once", action="store_true", help="run at most one card")
    run.add_argument("--card", help="run only this card id")
    sub.add_parser("sync", help="mirror card files onto the board and exit")
    sub.add_parser("check", help="validate config, cards and tools without running anything")
    args = parser.parse_args(argv)

    config = load(args.repo.resolve(), args.config)
    driver = Driver(config)
    if args.command == "sync":
        tasks = driver.sync_board(load_cards(config.cards_path, config.card_pattern))
        print(f"synced {len(tasks)} board cards")
        return 0
    if args.command == "check":
        return check(driver)
    return driver.run(once=args.once, only=args.card)


def check(driver: Driver) -> int:
    config, problems = driver.c, []
    cards = load_cards(config.cards_path, config.card_pattern)
    print(f"repo     {config.repo} (branch {config.branch})")
    print(f"board    {config.board}, assignee {config.assignee}")
    print(f"cards    {len(cards)} in {config.cards_path}")
    for card in cards.values():
        for dep in card.depends:
            if dep not in cards:
                problems.append(f"{card.id} depends on unknown card {dep}")
        problems += [f"{card.id}: {gap}" for gap in driver.invalid_card(card)]
    dsh = config.dsh_command()
    print(f"dsh      {dsh} (DSH_HOME {config.dsh_home})")
    if not shutil.which(dsh) and not Path(dsh).is_file():
        problems.append("dsh not found: install the DeepSeek Harness CLI or set [dsh] bin")
    if not shutil.which(driver.board.hermes):
        problems.append("hermes not found: install Hermes Agent or set DSH_RUNNER_HERMES")
    print(f"model    {driver.dsh_setting('model')} (headless profile default)")
    for problem in problems:
        print(f"problem: {problem}", file=sys.stderr)
    print("ok" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
