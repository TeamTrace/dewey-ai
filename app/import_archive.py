"""
Import an archive from a CMS into data/ as Dewey article JSON.

    python app/import_archive.py --help
    python app/import_archive.py wordpress-api --site https://example.com --limit 20

Then index it with app/setup_postgres.py (or app/setup.py for the Azure stack).
Importers use only the Python standard library.
"""
import argparse
import importlib
import sys
from pathlib import Path

from importers import IMPORTERS
from importers.common import ArticleWriter, ImporterError, ImportReport

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def _publisher_arg(value: str) -> str:
    name = " ".join(value.split())
    if not name:
        raise argparse.ArgumentTypeError("expected a publisher's name, e.g. \"Partner Daily\"")
    return name


def build_parser():
    parser = argparse.ArgumentParser(description="Import a newsroom archive into Dewey's data/ folder.")
    sub = parser.add_subparsers(dest="source", required=True, metavar="SOURCE")
    modules = {}
    for module_name in IMPORTERS:
        module = importlib.import_module(f"importers.{module_name}")
        p = sub.add_parser(module.NAME, help=module.HELP, description=module.HELP)
        module.add_arguments(p)
        p.add_argument("--limit", type=int, help="stop after this many articles (try a small import first)")
        p.add_argument("--out-dir", type=Path, default=DATA_DIR, help="where to write JSON (default: data/)")
        p.add_argument("--prefix", help="output file name prefix (default depends on the source)")
        p.add_argument("--publisher", type=_publisher_arg, metavar="NAME",
                       help="who published these articles, for a source that isn't your own newsroom (a "
                            "partner's feed, a wire service): written to every article so answers credit "
                            "them. Leave it out for your own archive; indexing labels those with "
                            "NEWSROOM_NAME")
        modules[module.NAME] = module
    return parser, modules


def main(argv=None) -> int:
    parser, modules = build_parser()
    args = parser.parse_args(argv)
    module = modules[args.source]
    report = ImportReport(source=args.source, publisher=args.publisher)
    prefix = args.prefix or module.default_prefix(args)
    if args.limit is not None and not args.prefix:
        # A trial run must not replace a full import that used the same default prefix.
        prefix += "-sample"
        print(f"--limit run: writing to {prefix}-*.json so earlier full imports are left alone "
              "(pass --prefix to choose the name).", file=sys.stderr)
    try:
        with ArticleWriter(args.out_dir, prefix, report, limit=args.limit, publisher=args.publisher) as writer:
            writer.add_all(module.iter_articles(args))
    except ImporterError as e:
        print(f"Import failed: {e}", file=sys.stderr)
        print("Nothing was changed in the output folder.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled; nothing was changed in the output folder.", file=sys.stderr)
        return 130
    print(report.summary())
    if report.written == 0:
        print("No articles were imported; check the source settings above.", file=sys.stderr)
        return 1
    print("Next: index them with  python app/setup_postgres.py   (Azure stack: python app/setup.py)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
