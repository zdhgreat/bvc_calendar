"""Daily crawl entry. Hook this into cron / Task Scheduler."""
from __future__ import annotations

import argparse
from datetime import datetime

from app.crawler.runner import run


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD (default: today)")
    args = ap.parse_args()
    target = datetime.fromisoformat(args.date) if args.date else datetime.now()
    run(target)


if __name__ == "__main__":
    main()
