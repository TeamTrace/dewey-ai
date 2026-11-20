"""
Archive importers: convert a CMS export or API into Dewey's article JSON in data/.

Run `python app/import_archive.py --help` for the list. Each importer module
defines:
    NAME: str                       subcommand name
    HELP: str                       one-line description
    add_arguments(parser) -> None   its command-line options
    default_prefix(args) -> str     output file prefix in data/
    iter_articles(args) -> Iterator[dict]   raw articles (see common.normalize)
"""
IMPORTERS = [
    "wordpress_api",
    "wordpress_export",
    "feed",
    "csv_import",
    "drupal",
    "ghost",
]
