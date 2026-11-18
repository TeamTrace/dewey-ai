"""
RSS / Atom feed importer: any CMS that publishes a feed (WordPress, Drupal,
Ghost, Blogger, Substack, Arc XP, ...).

Reads RSS 2.0 (and 0.9x), RSS 1.0 (RDF) and Atom 1.0, from a URL or from saved
feed files. Feeds usually carry only the latest posts, so from a URL it also
follows paged/archived feeds: RFC 5005 links (rel="next", "previous",
"prev-archive", "next-archive") when the feed has them and, for WordPress
feeds that have none, ?paged=2, 3, ...

Parsing is streaming (an XMLParser target that hands over one <item>/<entry>
at a time and then drops it), so a multi-GB saved feed is not loaded into
memory. Standard library only. Public feeds only: the importer takes no
passwords or keys and refuses URLs that embed a login.
"""
import argparse
import codecs
import gzip
import hashlib
import html
import html.entities
import re
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
import zlib
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Deque, Dict, Iterator, List, Optional, Set, Tuple

from .common import ImporterError, _redact_url, clean_text, html_to_text, http_get, parse_date

NAME = "feed"
HELP = "RSS or Atom feed from any CMS (WordPress, Drupal, Ghost, ...); follows paged and archived feeds"

ATOM = "http://www.w3.org/2005/Atom"
ATOM03 = "http://purl.org/atom/ns#"
RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
RSS1 = "http://purl.org/rss/1.0/"
RSS090 = "http://my.netscape.com/rdf/simple/0.9/"
CONTENT = "http://purl.org/rss/1.0/modules/content/"
DC = "http://purl.org/dc/elements/1.1/"
DCTERMS = "http://purl.org/dc/terms/"
FH = "http://purl.org/syndication/history/1.0"
APP = "http://www.w3.org/2007/app"
TOMBSTONES = "http://purl.org/atompub/tombstones/1.0"
FEEDBURNER = "http://rssnamespace.org/feedburner/ext/1.0"
XML_BASE = "{http://www.w3.org/XML/1998/namespace}base"
RDF_ABOUT = f"{{{RDF}}}about"
RDF_RESOURCE = f"{{{RDF}}}resource"

# RFC 5005 paging/archive relations. Following both directions (with a visited
# set) covers the whole chain whichever page the user starts from.
FOLLOW_RELS = ("next", "previous", "prev", "prev-archive", "next-archive")
FEED_TYPES = {"application/rss+xml", "application/atom+xml", "application/rdf+xml", "application/xml",
              "text/xml"}
CHUNK = 1 << 20
FUTURE_TOLERANCE = timedelta(days=1)

# HTML named entities, for old RSS 0.91 feeds that declare the Netscape DTD and use
# &nbsp; etc. (expat only consults this table when the document has an external DTD).
_HTML_ENTITIES = {name: chr(cp) for name, cp in html.entities.name2codepoint.items()}
# Bytes that XML 1.0 forbids; feeds often contain them (pasted from Word) and expat
# rejects the whole document. Stripped unless the file is UTF-16/32.
_XML_ILLEGAL = bytes(range(0, 9)) + b"\x0b\x0c" + bytes(range(0x0E, 0x20))

# Time-zone abbreviations that email.utils does not know (it knows UT/UTC/GMT/Z and the
# US zones EST/EDT/CST/CDT/MST/MDT/PST/PDT/AST/ADT) but that show up in RSS pubDate.
# Ambiguous ones (IST, ...) are deliberately absent: those dates fall back to --default-timezone.
_TZ_ABBREV = {
    "WET": "+0000", "WEST": "+0100", "BST": "+0100", "CET": "+0100", "CEST": "+0200", "MET": "+0100",
    "MEST": "+0200", "EET": "+0200", "EEST": "+0300", "MSK": "+0300", "HKT": "+0800", "SGT": "+0800",
    "AWST": "+0800", "JST": "+0900", "KST": "+0900", "ACST": "+0930", "ACDT": "+1030", "AEST": "+1000",
    "AEDT": "+1100", "NZST": "+1200", "NZDT": "+1300", "HST": "-1000", "AKST": "-0900", "AKDT": "-0800",
    "NST": "-0330", "NDT": "-0230",
}
_TRAILING_ZONE = re.compile(r"^(.*\d{1,2}:\d{2}(?::\d{2})?)\s+([A-Za-z]{2,5})$")
# Numeric zones email.utils misreads as "no zone": "+05:30" (colon), "GMT+1", "UTC-05:00".
_NUMERIC_ZONE = re.compile(r"^(.*\d:\d{2}(?::\d{2}(?:\.\d+)?)?)\s*(?:(?:GMT|UTC|UT)\s*)?([+-])(\d{1,2})(?::?(\d{2}))?$",
                           re.IGNORECASE)
_ISO_LIKE = re.compile(r"^\d{4}-\d{2}-\d{2}[Tt ]")
_URI_LIKE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:\S")
_ABS_HTTP = re.compile(r"^https?://", re.IGNORECASE)
# WordPress's own guid / Atom id for a post: https://site/?p=123 (a fingerprint when <generator> is removed).
_WP_POST_ID = re.compile(r"^https?://[^?#\s]+/\?p=\d+$", re.IGNORECASE)
# user:password@ at the start of a URL typed without a scheme, or in the netloc of one with a scheme.
_LOGIN_PREFIX = re.compile(r"^[^/\s:@]+:[^/\s@]*@")
_NETLOC_LOGIN = re.compile(r"^((?:[A-Za-z][A-Za-z0-9+.-]*:)?//)[^/?#]*@")
_COMMENTS_TITLE = re.compile(r"\bcomments?\b", re.IGNORECASE)
_XML_DECL_ENCODING = re.compile(rb"^<\?xml[^>]*?\sencoding\s*=\s*([\"'])([A-Za-z][A-Za-z0-9._:-]*)\1")
_XML_DECL_ENCODING_TEXT = re.compile(r"^(<\?xml[^>]*?\sencoding\s*=\s*)([\"'])[^\"']*\2")
# Declared encodings are read the way browsers read them (WHATWG Encoding Standard): Latin-1/ASCII labels
# mean Windows-1252, so curly quotes typed in Word don't turn into C1 control characters, and legacy CJK
# labels mean their Windows supersets. pyexpat can't decode multi-byte encodings at all, so every
# declared encoding other than UTF-8 is transcoded to UTF-8 before parsing.
_ENCODING_ALIASES = {
    "iso-8859-1": "cp1252", "iso8859-1": "cp1252", "iso_8859-1": "cp1252", "latin1": "cp1252",
    "latin-1": "cp1252", "l1": "cp1252", "us-ascii": "cp1252", "ascii": "cp1252",
    "shift_jis": "cp932", "shift-jis": "cp932", "sjis": "cp932", "x-sjis": "cp932", "ms_kanji": "cp932",
    "windows-31j": "cp932", "csshiftjis": "cp932",
    "euc-kr": "cp949", "ks_c_5601-1987": "cp949", "korean": "cp949",
    "gb2312": "gb18030", "gbk": "gb18030", "x-gbk": "gb18030", "cp936": "gb18030", "chinese": "gb18030",
    "big5": "big5hkscs", "cn-big5": "big5hkscs", "x-x-big5": "big5hkscs",
}
_EMAIL = re.compile(r"[^\s()<>\[\]]+@[^\s()<>\[\]]+")
_BY_PREFIX = re.compile(r"^by\s+", re.IGNORECASE)
# Yoast SEO's "The post X appeared first on Site." footer, appended to every item's content: a last
# paragraph that starts like this and contains "appeared first on". Found with _strip_yoast_footer,
# not one regex over the whole body: that backtracked cubically on crafted content (hours per item).
_YOAST_FOOTER_START = re.compile(r"<p>\s*The post\s+<a\b", re.IGNORECASE)
_PROTECTED_MARKERS = ("post-password-form", "There is no excerpt because this is a protected post.")
_TRUNCATED_END = re.compile(r"(\[\s*(?:…|\.\.\.)\s*\]|…|\.\.\.|continue reading\W*|read more\W*)$", re.IGNORECASE)

EPILOG = """\
Where to find the feed URL (--url):
  Most sites advertise their feed in the page source as
  <link rel="alternate" type="application/rss+xml" href="...">. You can pass the
  site's homepage and the importer will use the feed it advertises. Usual places:
    WordPress   https://SITE/feed/  (Atom: /feed/atom/; plain permalinks: /?feed=rss2;
                one section: /category/NAME/feed/)
    Drupal      https://SITE/rss.xml
    Ghost       https://SITE/rss/
    Blogger     https://SITE/feeds/posts/default
    Substack    https://NAME.substack.com/feed
    Others      ask your web team for the full-text RSS or Atom feed.
  Not the comments feed (WordPress /comments/feed/): that one is refused.

Saved files (--file): feed files you downloaded or archived (.xml, .rss, .atom,
.rdf, optionally gzipped), or a folder of them. Links to other pages inside a
file are not followed; use --url for that.

How much history you get:
  Feeds usually list only the latest posts (WordPress: Settings > Reading >
  "Syndication feeds show the most recent"). From a URL the importer follows
  RFC 5005 next/previous/prev-archive links when the feed has them; for a
  WordPress feed without them it requests ?paged=2, 3, ... until a page is
  missing, empty or repeats itself (--paging). For a complete archive, prefer
  the CMS's own importer if there is one (wordpress-api, wordpress-export,
  drupal, ghost).
  Re-running with the same --prefix replaces the files from the last run, so
  stories that have dropped out of a short feed since then are lost from
  data/. To build an archive up from a feed over time, give each run its own
  --prefix (e.g. --prefix feed-SITE-2024-03); ids stay the same, so a story
  in two runs is indexed once.

Full text: many feeds carry only an excerpt. WordPress: Settings > Reading >
  "For each post in a feed, include" > Full text. Items with only a summary are
  imported and reported; --require-full-text skips them instead.

Secrets: none. This importer reads public feeds only and takes no API key or
  password (so there is no flag or environment variable for one). URLs with an
  embedded login are refused, and members-only or private feeds are not
  supported. Token-like query parameters (key=, token=) are masked in messages.
"""


# ---------------------------------------------------------------- CLI

def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.formatter_class = argparse.RawDescriptionHelpFormatter
    parser.epilog = EPILOG
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--url", nargs="+", action="extend", metavar="URL",
                     help="feed URL(s), or a homepage that advertises its feed (see below)")
    src.add_argument("--file", nargs="+", action="extend", type=Path, metavar="PATH",
                     help="saved feed file(s) or a folder of them (.xml/.rss/.atom/.rdf, .gz ok)")
    parser.add_argument("--paging", choices=("auto", "links", "query", "none"), default="auto",
                        help="how to reach older posts from a URL: 'links' follows RFC 5005 "
                             "next/prev-archive links; 'query' requests ?<page-param>=2,3,...; "
                             "'auto' (default) uses links, or 'query' for WordPress feeds without "
                             "links; 'none' reads only the given URL")
    parser.add_argument("--page-param", default="paged", metavar="NAME",
                        help="query parameter for --paging query (default: paged, as WordPress uses)")
    parser.add_argument("--max-pages", type=int, default=0, metavar="N",
                        help="stop after N feed pages per URL (default: no limit)")
    parser.add_argument("--delay", type=float, default=0.5, metavar="SECONDS",
                        help="pause between page requests, to be gentle on the site (default: 0.5)")
    parser.add_argument("--require-full-text", action="store_true",
                        help="skip items that have only a description/summary (often an excerpt) "
                             "and no full-text content:encoded / atom:content")
    parser.add_argument("--inherit-feed-author", action="store_true",
                        help="Atom: give entries without an author the feed-level author. Off by "
                             "default because that is often the site name, not a byline")
    parser.add_argument("--use-updated-date", action="store_true",
                        help="Atom: use <updated> as the publish date for entries that have no "
                             "<published> (off by default: <updated> is the last edit, not publication)")
    parser.add_argument("--default-timezone", default="UTC", metavar="TZ",
                        help="IANA zone (e.g. America/New_York) for dates that carry no time zone "
                             "(default: UTC)")
    parser.add_argument("--include-future", action="store_true",
                        help="keep items dated more than a day in the future (skipped by default as "
                             "scheduled or mis-dated)")


def default_prefix(args) -> str:
    if getattr(args, "url", None):
        host = (urllib.parse.urlsplit(args.url[0].strip()).hostname or "").lower()
        host = host[4:] if host.startswith("www.") else host
        return f"feed-{host}" if host else "feed"
    if getattr(args, "file", None):
        name = Path(args.file[0]).name
        for suffix in (".gz", ".xml", ".rss", ".atom", ".rdf"):
            if name.lower().endswith(suffix):
                name = name[: -len(suffix)]
        return f"feed-{name}" if name else "feed"
    return "feed"


@dataclass
class _Options:
    paging: str = "auto"
    page_param: str = "paged"
    max_pages: int = 0
    delay: float = 0.5
    require_full_text: bool = False
    inherit_feed_author: bool = False
    use_updated_date: bool = False
    tz: Any = timezone.utc
    tz_name: str = "UTC"
    include_future: bool = False
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class _Stats:
    """Counts for the end-of-run notes, by item id so an item repeated on two pages counts once."""
    seen_ids: Set[str] = field(default_factory=set)
    summary_only: Set[str] = field(default_factory=set)
    looks_truncated: Set[str] = field(default_factory=set)
    naive_dates: Set[str] = field(default_factory=set)
    email_only_authors: Set[str] = field(default_factory=set)
    tombstones: int = 0


def iter_articles(args) -> Iterator[Dict[str, Any]]:
    opts = _options(args)
    stats = _Stats()
    if getattr(args, "file", None):
        for path in _expand_files(args.file):
            yield from _iter_file(path, opts, stats)
    else:
        for url in args.url or []:
            yield from _iter_url(url, opts, stats)
    _print_notes(stats, opts)


def _options(args) -> _Options:
    tz_name = (getattr(args, "default_timezone", None) or "UTC").strip()
    if tz_name.upper() in ("UTC", "Z", "GMT"):
        tz = timezone.utc
    else:
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(tz_name)
        except Exception:
            raise ImporterError(f"Unknown time zone {tz_name!r} for --default-timezone. Use an IANA name "
                                "such as America/New_York or Europe/London (on Windows, `pip install "
                                "tzdata` first).") from None
    max_pages = getattr(args, "max_pages", 0) or 0
    delay = getattr(args, "delay", 0.5)
    if max_pages < 0 or (delay is not None and delay < 0):
        raise ImporterError("--max-pages and --delay can't be negative.")
    param = (getattr(args, "page_param", None) or "paged").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.\-\[\]]+", param):
        raise ImporterError(f"--page-param {param!r} doesn't look like a query parameter name.")
    return _Options(paging=getattr(args, "paging", "auto") or "auto", page_param=param, max_pages=max_pages,
                    delay=0.5 if delay is None else delay,
                    require_full_text=bool(getattr(args, "require_full_text", False)),
                    inherit_feed_author=bool(getattr(args, "inherit_feed_author", False)),
                    use_updated_date=bool(getattr(args, "use_updated_date", False)),
                    tz=tz, tz_name=tz_name, include_future=bool(getattr(args, "include_future", False)))


def _print_notes(stats: _Stats, opts: _Options) -> None:
    if stats.summary_only:
        truncated = len(stats.looks_truncated)
        extra = f" ({truncated} end like an excerpt, e.g. with [...])" if truncated else ""
        _log(f"Note: {len(stats.summary_only)} item(s) had no full-text field (content:encoded / atom:content), so "
             f"their text is the feed's description/summary, which may be only an excerpt{extra}. For full text "
             "in WordPress: Settings > Reading > 'For each post in a feed, include' > Full text. "
             "--require-full-text skips such items instead.")
    if stats.naive_dates:
        _log(f"Note: {len(stats.naive_dates)} item date(s) had no time zone and were read as {opts.tz_name} "
             "(change with --default-timezone).")
    if stats.email_only_authors:
        _log(f"Note: {len(stats.email_only_authors)} item(s) had an RSS <author> that was only an email address; "
             "it was not used as a byline.")
    if stats.tombstones:
        _log(f"Note: {stats.tombstones} deleted-entry marker(s) in the feed were ignored.")


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- URLs and pages

def _display(url: str) -> str:
    """Text safe to print for a URL (any scheme, or none) or a file path: no user:password@, and
    token-like query values masked. Plain file paths pass through unchanged."""
    text = str(url)
    stripped = _NETLOC_LOGIN.sub(r"\1", text)
    if stripped == text:
        stripped = _LOGIN_PREFIX.sub("", text)
    if "://" not in stripped and not stripped.startswith("//"):
        return stripped
    try:
        return _redact_url(stripped)
    except ValueError:
        return "<unparseable URL>"


def _resolve(base: str, ref: str) -> str:
    """ref resolved against base; '' when the result isn't a well-formed URL (e.g. an unbalanced IPv6
    bracket or a non-numeric port), so one malformed link skips one item instead of the whole import."""
    ref = (ref or "").strip()
    if not ref:
        return ""
    try:
        url = urllib.parse.urljoin(base, ref) if base else ref
        _ = urllib.parse.urlsplit(url).port
    except ValueError:
        return ""
    return url


def _check_url(raw: str) -> str:
    url = (raw or "").strip()
    try:
        parts = urllib.parse.urlsplit(url)
        _ = parts.port
    except ValueError:
        raise ImporterError(f"{_display(url)} isn't a valid URL.") from None
    if _LOGIN_PREFIX.match(url) or parts.username is not None or parts.password is not None:
        raise ImporterError(f"The URL for {parts.hostname or _display(url)} contains a username/password. This "
                            "importer reads public feeds only; remove the login from the URL (and change that "
                            "password if it was shared).")
    if parts.scheme.lower() not in ("http", "https"):
        if not parts.scheme and "." in (parts.path.split("/")[0] if parts.path else ""):
            raise ImporterError(f"--url needs the full address, including https:// "
                                f"(e.g. {_display('https://' + url)}).")
        raise ImporterError(f"--url must start with http:// or https:// (got {_display(url)}).")
    if not parts.hostname:
        raise ImporterError(f"{_display(url)} has no host name.")
    return url


def _host_key(url: str) -> str:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _visit_key(url: str) -> str:
    return urllib.parse.urldefrag(url)[0]


def _with_query(url: str, param: str, value: int) -> str:
    parts = urllib.parse.urlsplit(url)
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True) if k != param]
    query.append((param, str(value)))
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query), fragment=""))


def _is_not_found(error: ImporterError) -> bool:
    return bool(re.match(r"HTTP (404|410)\b", str(error)))


def _sniff(body: bytes) -> bytes:
    return body[:2048].lstrip(b"\xef\xbb\xbf \t\r\n").lower()


def _looks_like_html(body: bytes, headers: Dict[str, str]) -> bool:
    head = _sniff(body)
    if head.startswith(b"<!doctype html") or head.startswith(b"<html"):
        return True
    ctype = headers.get("content-type", "").lower()
    return "text/html" in ctype and not any(tag in head for tag in (b"<rss", b"<feed", b"<rdf:rdf"))


def _is_comments_url(url: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    return bool(re.search(r"/comments/feed(/|$)", parts.path)) or "feed=comments-" in parts.query


class _FeedLinkFinder(HTMLParser):
    """Collects <link rel="alternate" type="application/rss+xml|atom+xml" href> from a web page."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.found: List[Tuple[str, str, str]] = []

    def handle_starttag(self, tag, attrs):
        if tag != "link":
            return
        a = {k.lower(): (v or "") for k, v in attrs}
        rels = a.get("rel", "").lower().split()
        ctype = a.get("type", "").lower().split(";")[0].strip()
        if "alternate" in rels and ctype in FEED_TYPES and a.get("href", "").strip():
            self.found.append((a["href"].strip(), a.get("title", ""), ctype))


def _discover_feed(page_url: str, body: bytes) -> Optional[str]:
    finder = _FeedLinkFinder()
    try:
        finder.feed(body.decode("utf-8", "replace"))
        finder.close()
    except Exception:  # a broken page just means no advertised feed
        return None
    preferred = ("application/rss+xml", "application/atom+xml")
    ranked = sorted(finder.found, key=lambda found: found[2] not in preferred)
    for href, title, _ in ranked:
        url = _resolve(page_url, href)
        # "Comments Feed" titles and /comments/ URLs, but not a site that is merely named "Commentary".
        if (_COMMENTS_TITLE.search(title) or _is_comments_url(url)
                or re.search(r"/comments?/", urllib.parse.urlsplit(url).path, re.IGNORECASE)):
            continue
        if _ABS_HTTP.match(url):
            return url
    return None


def _fetch_start(url: str) -> Tuple[str, bytes]:
    """Fetch the first page; if it's a web page, switch to the feed it advertises."""
    if _is_comments_url(url):
        raise _comments_error(url)
    body, headers = http_get(url)
    if _looks_like_html(body, headers):
        feed_url = _discover_feed(url, body)
        if not feed_url:
            raise ImporterError(
                f"{_display(url)} is a web page, not a feed, and it doesn't advertise one "
                "(no <link rel=\"alternate\" type=\"application/rss+xml\">). Try the CMS's usual feed address: "
                "WordPress /feed/, Drupal /rss.xml, Ghost /rss/ (see --help).")
        _log(f"  {_display(url)} is a web page; using the feed it advertises: {_display(feed_url)}")
        url = _check_url(feed_url)
        if _is_comments_url(url):
            raise _comments_error(url)
        body, headers = http_get(url)
        if _looks_like_html(body, headers):
            raise ImporterError(f"{_display(url)} (advertised as the site's feed) returned a web page, not a feed.")
    _refuse_json(url, body, headers)
    return url, body


def _refuse_json(label: str, body: bytes, headers: Dict[str, str]) -> None:
    if _sniff(body).startswith((b"{", b"[")) or "json" in headers.get("content-type", "").lower():
        raise ImporterError(f"{_display(label)} returned JSON (a JSON Feed or an API), not RSS/Atom. Use the "
                            "site's RSS or Atom feed instead, or the CMS's API importer (e.g. wordpress-api).")


def _comments_error(label: str) -> ImporterError:
    return ImporterError(f"{_display(label)} is a comments feed, not a feed of articles. Use the posts feed "
                         "instead (WordPress: https://SITE/feed/).")


def _sleep(opts: _Options) -> None:
    if opts.delay:
        time.sleep(opts.delay)


def _iter_url(start: str, opts: _Options, stats: _Stats) -> Iterator[Dict[str, Any]]:
    url, body = _fetch_start(_check_url(start))
    # Item ids read from this URL's pages only. "Nothing new on this page" must mean this feed is
    # repeating itself, not that another --url in the same run already had those stories.
    seen: Set[str] = set()
    doc, count, _ = yield from _read_page(body, url, 1, opts, stats, seen)
    links = _paging_links(doc)
    mode = opts.paging
    if mode == "auto":
        if links:
            mode = "links"
        elif doc.is_wordpress and not doc.complete:
            _log(f"  WordPress feed without paging links: also requesting ?{opts.page_param}=2, 3, ... "
                 "(use --paging none to read only the first page)")
            mode = "query"
        else:
            mode = "none"
    if mode == "links" and links:
        yield from _follow_links(url, links, opts, stats, seen)
    elif mode == "query" and count:
        yield from _follow_query(url, opts, stats, seen)
    else:
        why = ("--paging none" if opts.paging == "none" else "it has no items" if not count
               else "the feed links to no older pages (if the site runs WordPress, --paging query "
                    f"requests ?{opts.page_param}=2, 3, ...)")
        _log(f"Note: read 1 page ({count} item(s)) from {_display(url)}; {why}. Feeds usually list only "
             "recent posts, so this may not be the whole archive (see --help: How much history you get).")


def _paging_links(doc: "_FeedDoc") -> List[str]:
    return [u for rel in FOLLOW_RELS for u in doc.links.get(rel, [])]


def _follow_links(start: str, first_links: List[str], opts: _Options, stats: _Stats,
                  seen: Set[str]) -> Iterator[Dict[str, Any]]:
    visited = {_visit_key(start)}
    queue: Deque[str] = deque(first_links)
    pages = 1
    host = _host_key(start)
    while queue:
        url = queue.popleft()
        key = _visit_key(url)
        if key in visited:
            continue
        visited.add(key)
        if not url.lower().startswith(("http://", "https://")):
            continue
        if _host_key(url) != host:
            _log(f"  not following {_display(url)}: it is on a different site than {host}")
            continue
        if opts.max_pages and pages >= opts.max_pages:
            _log(f"Note: stopped after {pages} page(s) (--max-pages); more pages are linked.")
            return
        _sleep(opts)
        try:
            body, headers = http_get(url)
        except ImporterError as e:
            if _is_not_found(e):
                _log(f"  {_display(url)} is linked from the feed but missing (HTTP 404/410); skipping it")
                continue
            raise
        if _looks_like_html(body, headers):
            _log(f"  {_display(url)} is linked from the feed but is a web page, not a feed; skipping it")
            continue
        pages += 1
        doc, _, new = yield from _read_page(body, url, pages, opts, stats, seen)
        if not new:
            # A CMS that ignores its own page parameter but always links to page N+1 would otherwise be
            # followed forever.
            _log(f"  page {pages} has no items that weren't already read, so its paging links are not followed")
            continue
        queue.extend(_paging_links(doc))
    _log(f"  followed the feed's paging links: {pages} page(s) in all")


def _follow_query(start: str, opts: _Options, stats: _Stats, seen: Set[str]) -> Iterator[Dict[str, Any]]:
    page = 1
    while True:
        page += 1
        if opts.max_pages and page > opts.max_pages:
            _log(f"Note: stopped after {opts.max_pages} page(s) (--max-pages); there may be more.")
            return
        url = _with_query(start, opts.page_param, page)
        _sleep(opts)
        try:
            body, headers = http_get(url)
        except ImporterError as e:
            if _is_not_found(e):
                _log(f"  page {page}: not found; that was the last page")
                return
            raise
        if _looks_like_html(body, headers):
            _log(f"  page {page}: the site returned a web page instead of a feed; stopping")
            return
        _, count, new = yield from _read_page(body, url, page, opts, stats, seen)
        if count == 0:
            _log(f"  page {page}: no items; that was the last page")
            return
        if new == 0:
            _log(f"  page {page}: only items already seen, so the site seems to ignore "
                 f"?{opts.page_param}=; stopping")
            return


def _read_page(body: bytes, url: str, page: int, opts: _Options, stats: _Stats, seen: Set[str]):
    """Yield one page's items; returns (doc, item count, count of ids not in `seen` before)."""
    counter = [0, 0]
    doc = yield from _iter_document([body], url, url, opts, stats, counter, check_comments=(page == 1),
                                    seen=seen)
    _log(f"  page {page}: {_display(url)}: {counter[0]} item(s)")
    return doc, counter[0], counter[1]


# ---------------------------------------------------------------- files

def _expand_files(paths: List[Path]) -> List[Path]:
    out: List[Path] = []
    for p in paths:
        p = Path(p).expanduser()
        if p.is_dir():
            found = sorted(f for f in p.iterdir() if f.is_file() and f.name.lower().endswith(
                (".xml", ".rss", ".atom", ".rdf", ".gz")))
            if not found:
                raise ImporterError(f"No feed files (.xml, .rss, .atom, .rdf, .gz) in folder {p}.")
            out.extend(found)
        elif p.is_file():
            out.append(p)
        else:
            raise ImporterError(f"Feed file not found: {p}")
    return out


def _file_chunks(path: Path) -> Iterator[bytes]:
    try:
        with open(path, "rb") as fh:
            magic = fh.read(2)
        opener = gzip.open if magic == b"\x1f\x8b" else open
        with opener(path, "rb") as fh:
            while True:
                chunk = fh.read(CHUNK)
                if not chunk:
                    return
                yield chunk
    except (OSError, EOFError, zlib.error) as e:
        raise ImporterError(f"Couldn't read {path}: {e}. If it is gzipped, the file may be truncated or "
                            "corrupt; try downloading it again.") from None


def _iter_file(path: Path, opts: _Options, stats: _Stats) -> Iterator[Dict[str, Any]]:
    _log(f"  reading {path}")
    counter = [0, 0]
    doc = yield from _iter_document(_file_chunks(path), "", str(path), opts, stats, counter, check_comments=True)
    _log(f"  {path}: {counter[0]} item(s)")
    if _paging_links(doc):
        _log(f"Note: {path} links to more feed pages; links in files aren't followed. Use --url with the "
             "feed's address to follow them.")


# ---------------------------------------------------------------- streaming XML

class _FeedDoc:
    """
    XMLParser target. Builds the tree with TreeBuilder but detaches each finished
    item/entry (queued in `ready` with its xml:base) and each feed-level element
    (after copying what's needed), so memory stays flat however long the feed is.
    """

    def __init__(self, doc_url: str, label: str):
        self.doc_url = doc_url
        self.label = label
        self.tb = ET.TreeBuilder()
        self.stack: List[Tuple[ET.Element, str]] = []
        self.kind: Optional[str] = None  # "rss" | "rdf" | "atom"
        self.ready: Deque[Tuple[ET.Element, str]] = deque()
        self.links: Dict[str, List[str]] = {}
        self.generator = ""
        self.title = ""
        self.site_link = ""
        self.feed_authors: List[str] = []
        self.complete = False
        self.tombstones = 0
        self.items_seen = 0
        self.wp_post_ids = False  # an item guid / entry id shaped like WordPress's https://site/?p=123

    @property
    def is_wordpress(self) -> bool:
        # Security plugins commonly strip <generator>, so the guid fingerprint counts too.
        return "wordpress" in self.generator.lower() or self.wp_post_ids

    @property
    def is_comments_feed(self) -> bool:
        return bool(re.match(r"\s*comments (for|on)\b", self.title, re.IGNORECASE))

    def start(self, tag, attrib):
        if not self.stack:
            self.kind = _root_kind(tag, self.label)
        parent_base = self.stack[-1][1] if self.stack else self.doc_url
        xml_base = attrib.get(XML_BASE)
        base = (_resolve(parent_base, xml_base) or parent_base) if xml_base else parent_base
        self.stack.append((self.tb.start(tag, attrib), base))

    def end(self, tag):
        el = self.tb.end(tag)
        _, base = self.stack.pop()
        if not self.stack:
            return el
        parent = self.stack[-1][0]
        depth = len(self.stack)
        if self._is_item(el, parent, depth):
            self.items_seen += 1
            self.ready.append((el, base))
            parent.remove(el)
        elif self._is_feed_level(parent, depth):
            self._feed_meta(el, base)
            parent.remove(el)
        return el

    def data(self, text):
        self.tb.data(text)

    def close(self):
        return self.tb.close()

    def _is_item(self, el, parent, depth) -> bool:
        if self.kind == "rss":
            return el.tag == "item" and (depth == 2 and parent.tag == "channel" or depth == 1)
        if self.kind == "rdf":
            return depth == 1 and el.tag in (f"{{{RSS1}}}item", f"{{{RSS090}}}item")
        return depth == 1 and el.tag == f"{{{ATOM}}}entry"

    def _is_feed_level(self, parent, depth) -> bool:
        if self.kind == "atom":
            return depth == 1
        return depth == 2 and _local(parent.tag) == "channel"

    def _feed_meta(self, el, base) -> None:
        name, ns = _local(el.tag), _ns(el.tag)
        if name == "link" and ns == ATOM:
            rel = (el.get("rel") or "alternate").strip().lower()
            url = _resolve(base, el.get("href") or "")
            if url:
                self.links.setdefault(rel, []).append(url)
                if rel == "alternate" and not self.site_link:
                    self.site_link = url
        elif name == "link" and ns in ("", RSS1, RSS090):
            self.site_link = self.site_link or _resolve(base, _text(el))
        elif name in ("generator", "generatorAgent"):
            self.generator += " ".join(v for v in (_text(el), el.get("uri", ""), el.get(RDF_RESOURCE, "")) if v)
        elif name == "title" and not self.title:
            self.title = _text(el)
        elif name == "author" and ns == ATOM:
            self.feed_authors.extend(_atom_person_names([el]))
        elif name == "complete" and ns == FH:
            self.complete = True
        elif name == "deleted-entry" and ns == TOMBSTONES:
            self.tombstones += 1


def _root_kind(tag: str, label: str) -> str:
    if tag == "rss":
        return "rss"
    if tag == f"{{{RDF}}}RDF":
        return "rdf"
    if tag == f"{{{ATOM}}}feed":
        return "atom"
    if tag == f"{{{ATOM03}}}feed":
        raise ImporterError(f"{_display(label)} is an Atom 0.3 feed, which isn't supported; most CMSs also "
                            "offer RSS 2.0 or Atom 1.0 (WordPress: /feed/).")
    if _local(tag).lower() == "html":
        raise ImporterError(f"{_display(label)} is a web page, not a feed. See --help for where to find the "
                            "feed URL.")
    raise ImporterError(f"{_display(label)} isn't an RSS or Atom feed (its root element is <{_local(tag)}>).")


def _strip_illegal(data: bytes) -> bytes:
    return data.translate(None, _XML_ILLEGAL)


def _prepare_chunks(chunks, label: str) -> Iterator[bytes]:
    """
    Bytes ready for expat, still streamed chunk by chunk:
    - blank lines (or a BOM plus blank lines) before <?xml ...?> are removed: a common WordPress
      fault (a stray newline in a theme or plugin file) that makes expat reject the whole feed;
    - a declared encoding other than UTF-8 is transcoded to UTF-8 (see _ENCODING_ALIASES), since
      pyexpat can't decode multi-byte encodings such as Shift_JIS, GB2312, Big5 or EUC-KR;
    - bytes XML 1.0 forbids are stripped.
    UTF-16/32 input passes through untouched.
    """
    it = iter(chunks)
    head = b""
    for chunk in it:
        head += chunk
        if b">" in head or len(head) >= 4096:  # the XML declaration (or first tag) is complete
            break
    if head[:2] in (b"\xff\xfe", b"\xfe\xff") or b"\x00" in head[:4]:
        if head:
            yield head
        yield from it
        return
    bom = b""
    if head.startswith(b"\xef\xbb\xbf"):
        bom, head = head[:3], head[3:]
    head = head.lstrip(b" \t\r\n")
    match = None if bom else _XML_DECL_ENCODING.match(head)
    codec = None
    if match:
        declared = match.group(2).decode("ascii")
        try:
            info = codecs.lookup(_ENCODING_ALIASES.get(declared.lower(), declared))
        except LookupError:
            raise ImporterError(f"{_display(label)} says its text is in the character encoding {declared!r}, "
                                "which Python doesn't know. Ask the web team to fix the encoding=\"...\" in the "
                                "feed's first line, or save the feed, convert it to UTF-8 and use --file.") from None
        if info.name != "utf-8" and not info.name.startswith(("utf-16", "utf-32")):
            codec = info.name
        elif info.name == "utf-8" and declared.lower() != "utf-8":
            # e.g. encoding="utf8", which expat doesn't recognise as UTF-8.
            head = _XML_DECL_ENCODING_TEXT.sub('\\1"UTF-8"', head.decode("latin-1"), count=1).encode("latin-1")
    if codec is None:
        yield _strip_illegal(bom + head)
        for chunk in it:
            yield _strip_illegal(chunk)
        return
    decoder = codecs.getincrementaldecoder(codec)(errors="replace")
    text = _XML_DECL_ENCODING_TEXT.sub('\\1"UTF-8"', decoder.decode(head), count=1)
    yield _strip_illegal(text.encode("utf-8"))
    for chunk in it:
        text = decoder.decode(chunk)
        if text:
            yield _strip_illegal(text.encode("utf-8"))
    text = decoder.decode(b"", final=True)
    if text:
        yield _strip_illegal(text.encode("utf-8"))


def _iter_document(chunks, doc_url: str, label: str, opts: _Options, stats: _Stats, counter: List[int],
                   check_comments: bool, seen: Optional[Set[str]] = None):
    doc = _FeedDoc(doc_url, label)
    parser = ET.XMLParser(target=doc)
    parser.entity.update(_HTML_ENTITIES)
    seen = stats.seen_ids if seen is None else seen
    checked = False
    finished = False
    chunk_iter = _prepare_chunks(chunks, label)
    while True:
        if finished and not doc.ready:
            break
        if not doc.ready:
            chunk = next(chunk_iter, None)
            try:
                if chunk is None:
                    parser.close()
                    finished = True
                else:
                    parser.feed(chunk)
            except ET.ParseError as e:
                raise _parse_error(e, label, doc) from None
            except LookupError as e:  # an encoding expat asked Python for and Python doesn't know
                raise ImporterError(f"Couldn't read {_display(label)}: {e}. Convert the feed to UTF-8 and "
                                    "use --file.") from None
            continue
        el, base = doc.ready.popleft()
        if check_comments and not checked:
            checked = True
            if doc.is_comments_feed:
                raise _comments_error(label)
        raw, item_id = _convert(el, base, doc, opts, stats)
        counter[0] += 1
        if item_id and item_id not in seen:
            seen.add(item_id)
            counter[1] += 1
        yield raw
        if counter[0] % 5000 == 0:
            _log(f"  ... {counter[0]} item(s) read from {_display(label)}")
    stats.tombstones += doc.tombstones
    return doc


def _parse_error(e: ET.ParseError, label: str, doc: _FeedDoc) -> ImporterError:
    line, col = getattr(e, "position", (None, None))
    msg = str(e).split(":")[0]
    if doc.kind is None and "no element found" in msg:
        return ImporterError(f"{_display(label)} is empty, not a feed.")
    hint = ""
    if "undefined entity" in msg:
        hint = (" The feed uses an HTML entity (like &nbsp;) that XML doesn't allow; ask the web team to fix the "
                "feed, or save it and replace the entity.")
    elif "not well-formed" in msg or "mismatched tag" in msg:
        hint = (" The feed isn't well-formed XML (often a stray '&' or '<', or text in the wrong encoding); "
                "check it with an XML or feed validator.")
    where = f" (line {line}, column {col})" if line else ""
    after = f" It failed after {doc.items_seen} item(s)." if doc.items_seen else ""
    return ImporterError(f"Couldn't read {_display(label)} as RSS/Atom: {msg}{where}.{hint}{after}")


# ---------------------------------------------------------------- items

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _ns(tag: str) -> str:
    return tag[1:].split("}", 1)[0] if isinstance(tag, str) and tag.startswith("{") else ""


def _text(el: Optional[ET.Element]) -> str:
    return "".join(el.itertext()).strip() if el is not None else ""


def _texts(parent: ET.Element, tag: str) -> List[str]:
    return [t for t in (_text(e) for e in parent.findall(tag)) if t]


def _serialize_children(el: ET.Element) -> str:
    """Inline (X)HTML child elements back to markup, namespaces dropped, text escaped."""
    try:
        parts = [html.escape(el.text or "", quote=False)]
        for child in el:
            name = _local(child.tag)
            name = name if re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*", name) else "span"
            parts.append(f"<{name}>{_serialize_children(child)}</{name}>")
            parts.append(html.escape(child.tail or "", quote=False))
        return "".join(parts)
    except RecursionError:
        return html.escape(" ".join(el.itertext()), quote=False)


def _markup(el: Optional[ET.Element]) -> str:
    """An RSS element holding HTML: escaped/CDATA text, or (rarely) raw inline markup."""
    if el is None:
        return ""
    return _serialize_children(el) if len(el) else (el.text or "")


def _atom_text(el: Optional[ET.Element]) -> Optional[Tuple[str, str]]:
    """An Atom text construct / content as ("html", markup) or ("text", plain); None if not inline text."""
    if el is None or el.get("src"):
        return None
    ctype = (el.get("type") or "text").strip().lower()
    if ctype == "xhtml" or ctype.endswith("+xml") or ctype.endswith("/xml"):
        children = list(el)
        if len(children) == 1 and _local(children[0].tag) == "div" and not (el.text or "").strip():
            return "html", _serialize_children(children[0])
        return "html", _serialize_children(el)
    if ctype in ("html", "text/html"):
        return "html", _markup(el)
    if ctype == "text" or ctype.startswith("text/"):
        return "text", "".join(el.itertext())
    return None  # base64-encoded media: not article text


def _atom_person_names(people: List[ET.Element]) -> List[str]:
    return [n for n in (_text(p.find(f"{{{ATOM}}}name")) for p in people) if n]


def _split_byline(text: str) -> List[str]:
    """'By Jane Doe, John Roe and Ann Lee' -> three names. Byline plugins such as Co-Authors Plus put
    every byline in one dc:creator / atom:name, and Dewey's author filter matches names exactly.
    Commas split only a list that also has 'and'/'&', so 'Doe, Jane' stays one name."""
    text = _BY_PREFIX.sub("", clean_text(text)).strip()
    if not text:
        return []
    has_conjunction = re.search(r"\s(?:and|&)\s", text, re.IGNORECASE)
    separator = r"\s*(?:;|\||,?\s+(?:and|&)\s+" + (r"|," if has_conjunction else "") + r")\s*"
    return [part.strip(" ,") for part in re.split(separator, text, flags=re.IGNORECASE) if part.strip(" ,")]


def _clean_names(names: List[str]) -> List[str]:
    out: List[str] = []
    for value in names:
        for name in _split_byline(value):
            if name not in out:
                out.append(name)
    return out


def _rss_author_names(values: List[str]) -> Tuple[List[str], bool]:
    """RSS <author> is an email address, often 'jane@example.com (Jane Doe)'; keep only the name.
    Returns (names, whether some value was only an email address)."""
    names, email_only = [], False
    for value in values:
        name = re.sub(r"[()<>\[\]]", " ", _EMAIL.sub(" ", value))
        name = " ".join(name.split())
        if name:
            names.append(name)
        elif "@" in value:
            email_only = True
    return names, email_only


def _make_id(key: str, url: str, doc_url: str) -> str:
    """Stable id from the guid/atom:id (or permalink). Non-URI guids are namespaced by host."""
    key = (key or "").strip()
    if not key:
        return ""
    if not _URI_LIKE.match(key):
        host = urllib.parse.urlsplit(url).netloc or urllib.parse.urlsplit(doc_url).netloc
        key = f"{host.lower()}|{key}"
    return "feed-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]


def _parse_feed_date(value: str, opts: _Options) -> Tuple[Optional[datetime], bool]:
    """(UTC datetime or None, whether the value had no time zone and opts.tz was assumed)."""
    s = (value or "").strip()
    if not s:
        return None, False
    if _ISO_LIKE.match(s):
        s = s[:10] + s[10:].upper()  # RFC 3339 allows a lowercase "t" and "z"
    m = _TRAILING_ZONE.match(s)
    if m and m.group(2).upper() in _TZ_ABBREV:
        s = f"{m.group(1)} {_TZ_ABBREV[m.group(2).upper()]}"
    aware = parse_date(s, assume_utc=False)
    if aware is not None:
        return aware, False
    # email.utils reads "+05:30", "GMT+1" or "UTC-05:00" as no zone at all, which would silently
    # shift the time to --default-timezone. Rewrite them as RFC 822 "+0530" (or ISO "+05:30").
    m = _NUMERIC_ZONE.match(s)
    if m:
        sign, hours, minutes = m.group(2), int(m.group(3)), int(m.group(4) or 0)
        # "-0000" means "zone unknown" in RFC 2822, so it stays naive.
        if hours <= 14 and minutes < 60 and (hours or minutes or sign == "+"):
            for candidate in (f"{m.group(1)} {sign}{hours:02d}{minutes:02d}",
                              f"{m.group(1)}{sign}{hours:02d}:{minutes:02d}"):
                aware = parse_date(candidate, assume_utc=False)
                if aware is not None:
                    return aware, False
    as_utc = parse_date(s, assume_utc=True)
    if as_utc is None:
        return None, False
    return as_utc.replace(tzinfo=opts.tz).astimezone(timezone.utc), True


def _url_key(url: str) -> str:
    return (url or "").strip().rstrip("/").lower()


def _convert(el: ET.Element, base: str, doc: _FeedDoc, opts: _Options, stats: _Stats):
    """One <item>/<entry> -> (raw article dict or {"_skip": reason}, its id)."""
    parts = _atom_entry(el, base, doc, opts) if doc.kind == "atom" else _rss_item(el, base, doc)
    item_id = parts.get("id", "")
    if parts.get("_skip"):
        return {"_skip": parts["_skip"]}, item_id
    content_html = parts.get("content_html") or ""
    summary_html = parts.get("summary_html") or ""
    if any(marker in content_html or marker in summary_html for marker in _PROTECTED_MARKERS):
        return {"_skip": "password-protected post (the feed carries only a password prompt)"}, item_id
    has_text = bool(content_html.strip() or (parts.get("content") or "").strip())
    summary_only = has_text and not parts["full"]
    if summary_only and opts.require_full_text:
        return {"_skip": "only a summary/description in the feed, no full text (--require-full-text)"}, item_id

    date_value, date_source = parts.get("date", ""), parts.get("date_source", "")
    published, naive = _parse_feed_date(date_value, opts) if date_value else (None, False)
    if date_value and published is None:
        return {"_skip": f"unparseable date in <{date_source}>"}, item_id
    if published is not None and not opts.include_future and published > opts.now + FUTURE_TOLERANCE:
        return {"_skip": "publish date more than a day in the future (scheduled or mis-dated; "
                         "--include-future keeps it)"}, item_id

    key = item_id or parts.get("url") or f"#{id(el)}"
    if naive:
        stats.naive_dates.add(key)
    if parts.get("email_only"):
        stats.email_only_authors.add(key)
    if summary_only:
        stats.summary_only.add(key)
        text = parts.get("content") or html_to_text(content_html)
        if _TRUNCATED_END.search(text.strip()):
            stats.looks_truncated.add(key)
    raw = {"id": item_id, "headline": parts["headline"], "url": parts["url"],
           "authors": parts["authors"], "publish_date": published}
    if parts.get("content") is not None:
        raw["content"] = parts["content"]
    else:
        raw["content_html"] = _strip_yoast_footer(content_html)
    return raw, item_id


def _strip_yoast_footer(html: str) -> str:
    """Remove a trailing '<p>The post <a ...>X</a> appeared first on <a ...>Site</a>.</p>'. Linear time."""
    body = html.rstrip()
    lower = body.lower()
    if not lower.endswith("</p>"):
        return html
    close = len(body) - len("</p>")
    # The footer is the last paragraph: it can't contain another </p>.
    start = lower.rfind("</p>", 0, close)
    start = start + len("</p>") if start >= 0 else 0
    m = _YOAST_FOOTER_START.search(body, start, close)
    if not m or "appeared first on" not in lower[m.end():close]:
        return html
    return body[:m.start()].rstrip()


def _rss_item(el: ET.Element, base: str, doc: _FeedDoc) -> Dict[str, Any]:
    ns = _ns(el.tag)
    core = (lambda name: f"{{{ns}}}{name}") if ns else (lambda name: name)
    guid_el = el.find("guid")
    guid = _text(guid_el)
    permalink_guid = guid_el is not None and (guid_el.get("isPermaLink") or "true").strip().lower() != "false"
    about = (el.get(RDF_ABOUT) or "").strip()
    link = _text(el.find(core("link")))
    if not link:
        atom_links = [a for a in el.findall(f"{{{ATOM}}}link")
                      if (a.get("rel") or "alternate").strip().lower() == "alternate" and a.get("href")]
        link = atom_links[0].get("href", "") if atom_links else ""
    link_base = base or doc.site_link
    # Some CMSs put the homepage (or the feed's own URL) in every item's <guid>: it identifies nothing,
    # and using it would merge every story into one.
    if guid and _url_key(_resolve(link_base, guid)) in {k for k in (_url_key(doc.site_link),
                                                                     _url_key(doc.doc_url)) if k}:
        guid = ""
    if _WP_POST_ID.match(guid):
        doc.wp_post_ids = True
    # A guid or rdf:about is a URL only if it already looks like one: resolving a bare "48207" against
    # the feed would invent https://site/feed/48207.
    guid_url = guid if permalink_guid and (_ABS_HTTP.match(guid) or guid.startswith("/")) else ""
    url = ""
    for candidate in (_text(el.find(f"{{{FEEDBURNER}}}origLink")), link, guid_url,
                      about if _ABS_HTTP.match(about) else ""):
        candidate = _resolve(link_base, candidate)
        if _ABS_HTTP.match(candidate):
            url = candidate
            break

    encoded = _markup(el.find(f"{{{CONTENT}}}encoded"))
    description = _markup(el.find(core("description")))
    full = bool(encoded.strip())
    authors = _texts(el, f"{{{DC}}}creator")
    email_only = False
    if not authors:
        authors, email_only = _rss_author_names(_texts(el, "author"))
    if not authors:
        authors = _atom_person_names(el.findall(f"{{{ATOM}}}author"))

    date, source = "", ""
    for tag, label in (("pubDate", "pubDate"), (f"{{{DC}}}date", "dc:date"),
                       (f"{{{DCTERMS}}}issued", "dcterms:issued"), (f"{{{DCTERMS}}}created", "dcterms:created")):
        date = _text(el.find(tag))
        if date:
            source = label
            break
    return {"id": _make_id(guid or about or link or url, url, doc.doc_url or doc.site_link),
            "headline": _text(el.find(core("title"))), "url": url, "authors": _clean_names(authors),
            "content_html": encoded if full else description, "summary_html": description, "full": full,
            "date": date, "date_source": source, "email_only": email_only}


def _atom_entry(el: ET.Element, base: str, doc: _FeedDoc, opts: _Options) -> Dict[str, Any]:
    a = lambda name: f"{{{ATOM}}}{name}"  # noqa: E731
    entry_id = _text(el.find(a("id")))

    url = _text(el.find(f"{{{FEEDBURNER}}}origLink"))
    if not url:
        best = None
        for link in el.findall(a("link")):
            rel = (link.get("rel") or "alternate").strip().lower()
            href = (link.get("href") or "").strip()
            if rel != "alternate" or not href:
                continue
            link_base = (_resolve(base, link.get(XML_BASE)) or base) if link.get(XML_BASE) else base
            candidate = _resolve(link_base or doc.site_link, href)
            if not _ABS_HTTP.match(candidate):
                continue
            ctype = (link.get("type") or "text/html").lower()
            if best is None or ("html" in ctype and "html" not in best[1]):
                best = (candidate, ctype)
        url = best[0] if best else ""
        if not url and _ABS_HTTP.match(entry_id):
            url = _resolve("", entry_id)
    if _WP_POST_ID.match(entry_id):
        doc.wp_post_ids = True
    item_id = _make_id(entry_id or url, url, doc.doc_url or doc.site_link)

    draft = _text(el.find(f"{{{APP}}}control/{{{APP}}}draft")).lower()
    if draft == "yes":
        return {"_skip": "draft (app:draft is yes)", "id": item_id}

    title = _atom_text(el.find(a("title")))
    headline = "" if title is None else (title[1] if title[0] == "html" else html.escape(title[1], quote=False))

    content = _atom_text(el.find(a("content")))
    summary = _atom_text(el.find(a("summary")))
    full = content is not None and bool(content[1].strip())
    chosen = content if full else summary
    parts: Dict[str, Any] = {"id": item_id, "headline": headline, "url": url, "full": full,
                             "summary_html": summary[1] if summary and summary[0] == "html" else ""}
    if chosen is None:
        parts["content_html"] = ""
    elif chosen[0] == "html":
        parts["content_html"] = chosen[1]
    else:
        parts["content"] = chosen[1].strip()

    authors = _atom_person_names(el.findall(a("author"))) or _texts(el, f"{{{DC}}}creator")
    if not authors and opts.inherit_feed_author:
        source = el.find(a("source"))
        authors = (_atom_person_names(source.findall(a("author"))) if source is not None else []) \
            or list(doc.feed_authors)
    parts["authors"] = _clean_names(authors)

    for tag, label in ((a("published"), "published"), (f"{{{DC}}}date", "dc:date"),
                       (f"{{{DCTERMS}}}issued", "dcterms:issued")):
        value = _text(el.find(tag))
        if value:
            parts["date"], parts["date_source"] = value, label
            break
    else:
        updated = _text(el.find(a("updated")))
        if updated and opts.use_updated_date:
            parts["date"], parts["date_source"] = updated, "updated"
        elif updated:
            return {"_skip": "Atom entry has no <published> date, only <updated> (--use-updated-date accepts it)",
                    "id": item_id}
    return parts
