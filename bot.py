# -*- coding: utf-8 -*-

import argparse

import config
from htxbot.app import HtxFuturesBot
from htxbot.combined import CombinedHtxFuturesBot
from htxbot.models import TradeState

__all__ = ["CombinedHtxFuturesBot", "HtxFuturesBot", "TradeState", "config", "main"]


def parse_args():
    parser = argparse.ArgumentParser(description="Run HTX long and short futures profiles in one process.")
    parser.add_argument(
        "--profiles",
        default="",
        help="Comma-separated profile names to run. Default is BOT_PROFILES or long,short.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    profiles = tuple(item.strip() for item in args.profiles.split(",") if item.strip())
    bot = CombinedHtxFuturesBot(profiles=profiles)
    bot.run()


if __name__ == "__main__":
    main()
