"""
Look for PSE's new connection-queue files and rebuild the data when there is one.

    python etl/update.py              # daily: the monthly file(s) linked on PSE's page now
    python etl/update.py --backfill   # also every version of the old fixed-name files (about
                                      # 450 requests; PSE's firewall makes this take an hour)
    python etl/update.py --rebuild    # rebuild data/queue.json from the snapshots only

Run every day by .github/workflows/update.yml.  It exits with an error when
PSE's page no longer links a file in the expected form, so a change of
layout shows as a failed run instead of a quietly stale site.
"""

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import build  # noqa: E402
import pse  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--backfill", action="store_true", help="also crawl every version of the pre-July-2026 files")
    parser.add_argument("--rebuild", action="store_true", help="only rebuild from data/snapshots/")
    args = parser.parse_args()

    if not args.rebuild:
        sources = pse.load_sources()
        targets = pse.discover()
        if not targets:
            sys.exit("PSE's page links no 'Informacje_publikowane_..._stan_na_DD_MM_YYYY.xlsx' file: check the page and etl/pse.py")
        if args.backfill:
            targets = pse.LEGACY + targets
        new = []
        for doc, title, ext in targets:
            entries = pse.crawl(doc, title, ext, sources)
            sources.extend(entries)
            new.extend(entries)
            for entry in entries:
                print(f"{title}.{ext} v{entry['version']}: {entry.get('asof') or '-'}, {entry.get('rows', 0)} rows {entry.get('skipped', '')}")
        pse.save_sources(sources)
        if not new:
            print("Nothing new from PSE.")
            return
    build.main()


if __name__ == "__main__":
    main()
