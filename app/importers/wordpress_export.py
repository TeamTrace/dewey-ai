"""
WordPress export (WXR) importer: published posts from the XML file that
wp-admin > Tools > Export downloads.

    python app/import_archive.py wordpress-export --file example.WordPress.2024-05-01.xml --limit 20

Works offline, so it suits sites whose REST API is turned off or locked down,
sites that no longer exist, and archives handed over as a file. Accepts the
.xml file, a gzipped .xml.gz, a .zip of export files (as WordPress.com hands
them out), a folder of them, or several files at once (WP-CLI's `wp export`
splits big sites into numbered files). Run
`python app/import_archive.py wordpress-export --help` for where to find each
value in WordPress.

The file is read twice, streaming, never loaded whole: a first pass collects
the author list, Co-Authors Plus guest-author profiles and counts, and a second
pass converts posts one at a time. Bylines come from (in order) a custom field
named with --byline-meta, Co-Authors Plus author terms, then the post author.
A byline that can't be resolved to a person's name is left out, never guessed.

IDs are wp-<site>-<post id>, the same as the wordpress-api importer, so an
article imported both ways is one article in the index. Standard library only.
"""
import argparse
import codecs
import email.utils
import gzip
import json
import re
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
import zlib
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Deque, Dict, IO, Iterator, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo

from importers.common import ImporterError, clean_text, html_to_text

NAME = "wordpress-export"
HELP = "WordPress export file (wp-admin > Tools > Export), read offline; for sites whose API is closed."

CHUNK = 1 << 20  # bytes per read; tests lower it to exercise chunk boundaries
_PREAMBLE_SCAN = 65536  # how far to look for junk before '<?xml'
PROGRESS_EVERY = 2000  # items between progress lines in the second pass
CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"
DC_NS = "http://purl.org/dc/elements/1.1/"
_WP_NS = re.compile(r"^https?://wordpress\.org/export/[^/]+/?(excerpt/?)?$", re.I)

# Bytes that are never allowed in XML 1.0 (tab, LF and CR are). Pasted-in text
# sometimes carries them, and expat stops the whole file at the first one.
_XML_ILLEGAL = bytes(range(0, 9)) + b"\x0b\x0c" + bytes(range(0x0E, 0x20))
# wxr_cdata() runs utf8_encode() on text that isn't valid UTF-8, which turns
# Windows-1252 curly quotes and dashes into C1 control characters. Map them back.
_C1_TO_CP1252 = {}
for _b in range(0x80, 0xA0):
    try:
        _C1_TO_CP1252[_b] = bytes([_b]).decode("cp1252")
    except UnicodeDecodeError:
        _C1_TO_CP1252[_b] = None  # undefined in cp1252: drop it
_C1_RE = re.compile("[\u0080-\u009f]")

_ITEM_FIELDS = {"title", "link", "pubDate", "dc:creator", "guid", "content:encoded", "wp:post_id",
                "wp:post_date", "wp:post_date_gmt", "wp:post_name", "wp:status", "wp:post_type",
                "wp:post_password"}
_CHANNEL_FIELDS = {"title", "link", "wp:wxr_version", "wp:base_site_url", "wp:base_blog_url"}
# wp:author_email is deliberately not read: nothing here needs it.
_AUTHOR_FIELDS = {"wp:author_id", "wp:author_login", "wp:author_display_name", "wp:author_first_name",
                  "wp:author_last_name"}

GUEST_AUTHOR_TYPE = "guest-author"  # Co-Authors Plus guest author profiles
_GUEST_META = {"cap-display_name", "cap-user_login", "cap-first_name", "cap-last_name"}
JETPACK_ACCESS = "_jetpack_newsletter_access"
JETPACK_PAID = "_jetpack_memberships_contains_paid_content"
JETPACK_PAYWALLED = "_jetpack_memberships_contains_paywalled_content"
_MEMBERS_META = {JETPACK_ACCESS, JETPACK_PAID, JETPACK_PAYWALLED}

_EPILOG = """\
where to find each value:
  --file          In wp-admin go to Tools > Export, choose "All content" (best: it
                  includes the guest-author profiles that Co-Authors Plus bylines
                  need) or "Posts", and click "Download Export File". You get one
                  file named like yoursite.WordPress.2024-05-01.xml; pass it as is.
                  Also accepted: .xml.gz, a .zip of export files (WordPress.com
                  hands exports out as a .zip), a folder of them, or several files.
                  If the export stops or the file is cut off on a big site, export
                  "Posts" one date range at a time, or ask your host to run WP-CLI's
                  `wp export`, which splits the archive into numbered files. Pass
                  all the files in ONE run: each run for a site replaces that
                  site's earlier output files (use --prefix to keep both).
  --post-type     Only for sites that publish stories as a custom post type. The
                  report from a first run lists the types it skipped and how many
                  of each, e.g. "post type 'story' not selected (add --post-type
                  story ...)". In the file it is the <wp:post_type> of each item.
  --timezone      Settings > General > Timezone, e.g. America/New_York or UTC-5.
                  Only used for posts the export has no UTC date for (often posts
                  brought in from an older CMS); without it they are skipped.
  --site-url      Only if the export came from a staging or old address: the
                  importer prints the address found in the file, and links that
                  start with it are rewritten to this one.
  --byline-meta   Only if bylines are typed into a custom field: the field name,
                  shown in the post editor's Custom Fields panel (or as
                  <wp:meta_key> in the file). Text such as "By Jane Doe and Sam Lee"
                  becomes two authors; user IDs are looked up in the export.
  --byline-taxonomy  Only if a byline plugin other than Co-Authors Plus stores
                  authors as terms: the taxonomy name, as in <category domain="...">.
  --exclude-meta  To leave out posts a plugin marks in a custom field, e.g. a
                  paywall plugin: KEY skips posts that have a non-empty KEY,
                  KEY=VALUE only those with exactly that value.
  No password or API key is needed: everything is in the file. The export also
  holds author e-mail addresses and commenters' details; they are never copied
  into the archive or printed.

what is imported:
  Published posts only (post type "post" unless --post-type says otherwise).
  Drafts, pending, scheduled and trashed posts are always skipped, as are
  password-protected posts. Private posts are skipped unless --include-private.
  Posts that Jetpack marks for subscribers or paid members only, or that contain
  its paywall or Paid Content block, are skipped unless --include-members-only. Other paywall
  and membership plugins are not detected: if one marks restricted posts with a
  custom field, leave them out with --exclude-meta. Every skipped post is
  counted in the report with the reason.
  Pages, media, menus and other site parts are skipped and counted by type.

examples:
  python app/import_archive.py wordpress-export --file ~/Downloads/ledger.WordPress.2024-05-01.xml --limit 20
  python app/import_archive.py wordpress-export --file exports/ --timezone America/New_York
  python app/import_archive.py wordpress-export --file ledger.zip --post-type post --post-type story
"""


# ---------------------------------------------------------------- arguments

def _post_type_arg(value: str) -> str:
    value = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", value):
        raise argparse.ArgumentTypeError(f"expected a post type name such as post or story, got {value!r}")
    return value.lower()


def _meta_key_arg(value: str) -> str:
    value = value.strip()
    if not value or len(value) > 255:
        raise argparse.ArgumentTypeError("expected a custom field name, e.g. byline")
    return value


def _exclude_meta_arg(value: str) -> Tuple[str, Optional[str]]:
    key, sep, val = value.partition("=")
    key = key.strip()
    if not key or len(key) > 255:
        raise argparse.ArgumentTypeError(f"expected KEY or KEY=VALUE, got {value!r}")
    return key, (val.strip() if sep else None)


def _taxonomy_arg(value: str) -> str:
    value = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
        raise argparse.ArgumentTypeError(f"expected a taxonomy name such as author, got {value!r}")
    return value


def _timezone_arg(value: str) -> tzinfo:
    """An IANA zone name, or a fixed offset as WordPress offers it: UTC, UTC-5, UTC+5:30, -05:00."""
    raw = value.strip()
    m = re.fullmatch(r"(?:UTC|GMT)?\s*([+-])\s*(\d{1,2})(?:\.(5|75|25)|[:.](\d{2}))?", raw, flags=re.I)
    if raw.upper() in ("UTC", "GMT", "Z"):
        return timezone.utc
    if m:
        hours = int(m.group(2))
        minutes = int(m.group(4)) if m.group(4) else {"5": 30, "75": 45, "25": 15}.get(m.group(3) or "", 0)
        offset = timedelta(hours=hours, minutes=minutes)
        if offset > timedelta(hours=14) or minutes >= 60:
            raise argparse.ArgumentTypeError(f"{value!r} is not a valid UTC offset")
        return timezone(-offset if m.group(1) == "-" else offset)
    try:
        return ZoneInfo(raw)
    except (ValueError, KeyError, OSError):
        raise argparse.ArgumentTypeError(
            f"unknown time zone {value!r}; use a name like America/New_York (as in Settings > General) or an "
            "offset like UTC-5. On Windows, zone names need the tzdata package.") from None


def _site_url_arg(value: str) -> str:
    raw = value.strip()
    if raw and not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", raw):
        raw = "https://" + raw
    parts = urllib.parse.urlsplit(raw)
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise argparse.ArgumentTypeError(f"expected your site's address, like https://www.example.com, got {value!r}")
    return urllib.parse.urlunsplit((parts.scheme.lower(), parts.netloc, parts.path.rstrip("/"), "", ""))


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.formatter_class = argparse.RawDescriptionHelpFormatter
    parser.epilog = _EPILOG
    parser.add_argument("--file", dest="files", nargs="+", action="extend", type=Path, required=True,
                        metavar="PATH",
                        help="the export from Tools > Export (.xml, .xml.gz or .zip), several files, or a folder")
    parser.add_argument("--post-type", dest="post_types", action="append", type=_post_type_arg, metavar="TYPE",
                        help="post type to import (default: post); repeat for several, e.g. --post-type story")
    parser.add_argument("--include-private", action="store_true",
                        help="also import private posts (visible only to logged-in editors on the site)")
    parser.add_argument("--include-members-only", action="store_true",
                        help="also import posts Jetpack marks for subscribers or paid members only")
    parser.add_argument("--exclude-meta", action="append", type=_exclude_meta_arg, metavar="KEY[=VALUE]",
                        help="skip posts that have this custom field (or this value of it); repeat for several")
    parser.add_argument("--byline-meta", type=_meta_key_arg, metavar="KEY",
                        help="read bylines from this custom field first, for sites that type bylines in by hand")
    parser.add_argument("--byline-taxonomy", type=_taxonomy_arg, default="author", metavar="NAME",
                        help="taxonomy that holds author terms (default: author, as Co-Authors Plus uses)")
    parser.add_argument("--ignore-author", action="append", metavar="NAME",
                        help="leave this account out of bylines (login or display name), e.g. admin; repeatable")
    parser.add_argument("--timezone", type=_timezone_arg, metavar="ZONE",
                        help="the site's time zone (Settings > General), for posts with no UTC date in the export")
    parser.add_argument("--site-url", type=_site_url_arg, metavar="URL",
                        help="your live site's address, if the export came from a staging or old address")


# ---------------------------------------------------------------- site keys and prefix

def _site_key(url: str) -> str:
    """'https://www.example.com/news/' -> 'example-com-news'. Must match wordpress_api._site_key."""
    try:
        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return ""
    if host.startswith("www."):
        host = host[4:]
    if port and port not in (80, 443):
        host += f"-{port}"
    path = re.split(r"/wp-json(?:/|$)|/index\.php(?:/|$)", parts.path, maxsplit=1)[0]
    return re.sub(r"[^a-z0-9]+", "-", (host + path).lower()).strip("-")


def _stem_key(path: Path) -> str:
    """'ledger.WordPress.2024-05-01.000.xml.gz' -> 'ledger'. Fallback when the file names no site."""
    name = path.name
    name = re.sub(r"\.(gz|zip)$", "", name, flags=re.I)
    name = re.sub(r"\.xml$", "", name, flags=re.I)
    name = re.sub(r"\.wordpress\.\d{4}-\d{2}-\d{2}(\.\d+)?$", "", name, flags=re.I)
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def default_prefix(args) -> str:
    """wordpress-export-<site>, from the export's own site address; stable across export dates."""
    key = ""
    site_url = getattr(args, "site_url", None)
    files = getattr(args, "files", None) or []
    try:
        if site_url:
            key = _site_key(site_url)
        elif files:
            sources = _sources(files)
            if sources:
                key = _site_key(_peek_site_url(sources[0]))
    except Exception:  # noqa: BLE001 - the prefix must never stop the import; iter_articles reports errors
        key = ""
    if not key and files:
        key = _stem_key(Path(files[0]))
    return f"wordpress-export-{key or 'site'}"


# ---------------------------------------------------------------- input files

@dataclass
class _Source:
    label: str
    path: Path
    member: Optional[str] = None  # name inside a .zip
    gzipped: bool = False

    def open(self) -> IO[bytes]:
        if self.member is not None:
            zf = zipfile.ZipFile(self.path)
            try:
                inner = zf.open(self.member)
            except Exception:
                zf.close()
                raise
            return _ZipMember(zf, inner)
        if self.gzipped:
            return gzip.open(self.path, "rb")
        return open(self.path, "rb")


class _ZipMember:
    """A file inside a zip that closes the zip with it."""

    def __init__(self, zf: zipfile.ZipFile, inner: IO[bytes]):
        self.zf, self.inner = zf, inner

    def read(self, n: int = -1) -> bytes:
        return self.inner.read(n)

    def close(self) -> None:
        try:
            self.inner.close()
        finally:
            self.zf.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


_EXPORT_SUFFIXES = (".xml", ".xml.gz", ".zip")  # in folders; a file named on its own may be called anything


def _magic(path: Path) -> bytes:
    with open(path, "rb") as fh:
        return fh.read(4)


def _file_sources(path: Path) -> List[_Source]:
    magic = _magic(path)
    if magic.startswith(b"PK\x03\x04") or magic.startswith(b"PK\x05\x06"):
        try:
            with zipfile.ZipFile(path) as zf:
                names = sorted(n for n in zf.namelist()
                               if n.lower().endswith(".xml") and not n.startswith("__MACOSX/")
                               and not Path(n).name.startswith("."))
        except zipfile.BadZipFile as e:
            raise ImporterError(f"{path} looks like a .zip but can't be opened ({e}). Download it again.") from None
        if not names:
            raise ImporterError(f"{path} contains no .xml export files.")
        return [_Source(f"{path} > {n}", path, member=n) for n in names]
    return [_Source(str(path), path, gzipped=magic.startswith(b"\x1f\x8b"))]


def _sources(paths: List[Path]) -> List[_Source]:
    out: List[_Source] = []
    for raw in paths:
        p = Path(raw).expanduser()
        try:
            if p.is_dir():
                found = sorted(f for f in p.iterdir() if f.is_file() and not f.name.startswith(".")
                               and f.name.lower().endswith(_EXPORT_SUFFIXES))
                if not found:
                    raise ImporterError(f"No export files (.xml, .xml.gz, .zip) in folder {p}.")
                for f in found:
                    out.extend(_file_sources(f))
            elif p.is_file():
                out.extend(_file_sources(p))
            else:
                raise ImporterError(f"Export file not found: {p}. Download it from wp-admin > Tools > Export "
                                    "(see --help).")
        except OSError as e:
            raise ImporterError(f"Couldn't read {p}: {e.strerror or e}") from None
    return out


# ---------------------------------------------------------------- streaming WXR parser

@lru_cache(maxsize=512)
def _key(tag: str) -> str:
    """'{http://wordpress.org/export/1.2/}status' -> 'wp:status'; works for WXR 1.0, 1.1 and 1.2."""
    if not tag.startswith("{"):
        return tag
    ns, local = tag[1:].split("}", 1)
    if _WP_NS.match(ns):
        return ("excerpt:" if "excerpt" in ns.lower() else "wp:") + local
    if ns == CONTENT_NS:
        return "content:" + local
    if ns == DC_NS:
        return "dc:" + local
    return tag


class _NotAnExport(ImporterError):
    pass


_DECLARED_ENCODING = re.compile(rb"^(?:\xef\xbb\xbf)?\s*<\?xml[^>]*?\bencoding\s*=\s*[\"']([A-Za-z0-9._:-]{1,40})[\"']")


def _single_byte_codec(first: bytes) -> Optional[str]:
    """
    The Python codec of a single-byte charset named in the XML declaration
    (ISO-8859-1, Windows-1252, ...), or None for UTF-8/UTF-16 and unknown names.
    """
    m = _DECLARED_ENCODING.match(first)
    if not m:
        return None
    try:
        name = codecs.lookup(m.group(1).decode("ascii")).name
    except LookupError:
        return None
    if name.startswith("utf") or name == "ascii":
        return None
    try:
        if len(bytes(range(256)).decode(name, "replace")) != 256:
            return None  # a multi-byte charset
    except (LookupError, ValueError):
        return None
    return name


def _undo_utf8_encode(text: str, codec: str) -> str:
    """
    In an export declared in a single-byte charset (blog_charset ISO-8859-1),
    wxr_cdata() still runs utf8_encode() on every string that isn't valid
    UTF-8, so CDATA holds UTF-8 bytes that the parser reads as 'cafÃ©'. Read
    those bytes as UTF-8 again. Text that wasn't converted (a raw Latin-1 title
    in an old export, character references) doesn't round-trip and is kept.
    """
    if text.isascii():
        return text
    try:
        fixed = text.encode(codec).decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text
    if codec != "iso8859-1" and all(ord(c) < 256 for c in fixed):
        # utf8_encode() treats the DB bytes as ISO-8859-1; give them back to the declared charset.
        try:
            fixed = fixed.encode("latin-1").decode(codec)
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return fixed


class _WxrTarget:
    """
    XMLParser target. Collects the channel header and the author list, and hands
    over one <item> at a time as a small dict of the fields Dewey uses. Comments,
    e-mail addresses and unneeded custom fields are never kept, and with
    want_content=False post bodies aren't either.
    """

    def __init__(self, label: str, want_content: bool, meta_keys: Set[str]):
        self.label = label
        self.want_content = want_content
        self.meta_keys = meta_keys
        self._value_keys = set(meta_keys) | {""}  # "": value before key (not WordPress's order, but valid XML)
        self.path: List[str] = []
        self.root: Optional[str] = None
        self.channel: Dict[str, str] = {}
        self.authors: List[Dict[str, str]] = []
        self.ready: Deque[Dict[str, Any]] = deque()
        self.item: Optional[Dict[str, Any]] = None
        self.item_depth = -1
        self.author: Optional[Dict[str, str]] = None
        self.author_depth = -1
        self.meta: Optional[Dict[str, str]] = None
        self.cat: Tuple[str, str] = ("", "")
        self.buf: Optional[List[str]] = None
        self.buf_depth = -1
        self.items_seen = 0
        self.wp_items = 0  # items with WordPress fields (post id, type or status)
        self.codec: Optional[str] = None  # set for exports declared in a single-byte charset

    def doctype(self, name, pubid, system):
        if (name or "").lower() == "html":
            raise _NotAnExport(f"{self.label} is a web page, not a WordPress export. Download the file again from "
                               "wp-admin > Tools > Export (see --help).")
        raise _NotAnExport(f"{self.label} has a DOCTYPE declaration, which WordPress exports never contain "
                           "(WordPress's own importer refuses such files too). Export the site again.")

    @property
    def done(self) -> bool:
        """True once the root element (</rss>) has closed."""
        return self.root is not None and not self.path

    def _capture(self, depth: int) -> None:
        self.buf = []
        self.buf_depth = depth

    def start(self, tag, attrib):
        key = _key(tag)
        depth = len(self.path)
        self.path.append(key)
        if depth == 0:
            self.root = key
            if key != "rss":
                raise _not_rss_error(self.label, key)
            return
        if self.buf is not None:
            return  # markup nested inside a captured field: its text is kept by data()
        if self.item is not None:
            rel = depth - self.item_depth
            if rel == 1:
                if key in _ITEM_FIELDS:
                    if key != "content:encoded" or self.want_content:
                        self._capture(depth)
                elif key == "category":
                    self.cat = (attrib.get("domain") or "", attrib.get("nicename") or "")
                    self._capture(depth)
                elif key == "wp:postmeta":
                    self.meta = {}
            elif rel == 2 and self.meta is not None and self.path[-2] == "wp:postmeta":
                if key == "wp:meta_key":
                    self._capture(depth)
                elif key == "wp:meta_value" and self.meta.get("key", "") in self._value_keys:
                    self._capture(depth)
            return
        if self.author is not None:
            if depth == self.author_depth + 1 and key in _AUTHOR_FIELDS:
                self._capture(depth)
            return
        if depth == 2 and self.path[1] == "channel":
            if key == "item":
                self.item = {"categories": [], "meta": {}}
                self.item_depth = depth
                self.items_seen += 1
            elif key == "wp:author":
                self.author = {}
                self.author_depth = depth
            elif key in _CHANNEL_FIELDS:
                self._capture(depth)

    def data(self, text):
        if self.buf is not None:
            self.buf.append(text)

    def end(self, tag):
        key = self.path.pop()
        depth = len(self.path)
        if self.buf is not None:
            if depth != self.buf_depth:
                return
            text = "".join(self.buf)
            self.buf = None
            self._store(key, text)
            return
        if self.item is not None:
            if depth == self.item_depth and key == "item":
                item, self.item = self.item, None
                if any(k in item for k in ("wp:post_id", "wp:post_type", "wp:status")):
                    self.wp_items += 1
                self.ready.append(item)
            elif depth == self.item_depth + 1 and key == "wp:postmeta" and self.meta is not None:
                meta_key, value = self.meta.get("key"), self.meta.get("value")
                if meta_key in self.meta_keys and value is not None:
                    self.item["meta"].setdefault(meta_key, []).append(value)
                self.meta = None
        elif self.author is not None and depth == self.author_depth and key == "wp:author":
            self.authors.append(self.author)
            self.author = None

    def _store(self, key: str, text: str) -> None:
        if self.codec:
            text = _undo_utf8_encode(text, self.codec)
        if self.item is not None:
            if self.meta is not None:
                if key == "wp:meta_key":
                    self.meta["key"] = text.strip()
                elif key == "wp:meta_value":
                    self.meta["value"] = text
            elif key == "category":
                self.item["categories"].append((self.cat[0], self.cat[1], text))
            else:
                self.item.setdefault(key, text)
        elif self.author is not None:
            self.author.setdefault(key, text)
        else:
            self.channel.setdefault(key, text)

    def close(self):
        return None


def _not_rss_error(label: str, root: str) -> ImporterError:
    local = root.rsplit("}", 1)[-1]
    if local.lower() == "html":
        return _NotAnExport(f"{label} is a web page, not a WordPress export. Download the file again from "
                            "wp-admin > Tools > Export (see --help).")
    if local in ("feed", "RDF"):
        return _NotAnExport(f"{label} is an Atom/RSS feed, not a WordPress export. Use the feed importer for "
                            "feeds, or export the full archive from wp-admin > Tools > Export.")
    return _NotAnExport(f"{label} isn't a WordPress export (its root element is <{local[:40]}>, not <rss>). "
                        "Export the site from wp-admin > Tools > Export.")


def _trim_preamble(first: bytes, label: str, announce: bool) -> bytes:
    """
    Drop junk before '<?xml': a PHP notice or blank line that a plugin printed
    into the download. XML allows nothing before the declaration, and this is a
    common reason an otherwise good export won't open.
    """
    bom = b"\xef\xbb\xbf" if first.startswith(b"\xef\xbb\xbf") else b""
    body = first[len(bom):]
    at = body.find(b"<?xml", 0, _PREAMBLE_SCAN)
    if at <= 0:
        return first
    junk = body[:at]
    if b"<rss" in junk:  # a real document already started; leave it to the parser
        return first
    if junk.strip() and announce:
        _log(f"Note: ignored {len(junk)} byte(s) before the XML declaration in {label} (usually a PHP notice or "
             "blank line that a plugin printed into the export).")
    return body[at:]


def _parse_items(source: _Source, target: _WxrTarget, stop: Optional[Callable[[_WxrTarget], bool]] = None,
                 announce: bool = False) -> Iterator[Dict[str, Any]]:
    """Stream one export file, yielding raw item dicts. Raises ImporterError with advice on bad input."""
    parser = ET.XMLParser(target=target)
    head = b""
    try:
        with source.open() as fh:
            # Look further ahead once, to find the XML declaration, then feed the parser CHUNK at a time.
            first = fh.read(max(CHUNK, _PREAMBLE_SCAN))
            head = first[:512]
            strip = not (first[:2] in (b"\xff\xfe", b"\xfe\xff") or b"\x00" in first[:4])  # UTF-16: leave bytes be
            if strip:
                first = _trim_preamble(first, source.label, announce)
                target.codec = _single_byte_codec(first)

            def blocks() -> Iterator[bytes]:
                for i in range(0, len(first), CHUNK):
                    yield first[i:i + CHUNK]
                while True:
                    block = fh.read(CHUNK)
                    if not block:
                        return
                    yield block

            chunks = blocks()
            prev_tail = b""
            for chunk in chunks:
                data = chunk.translate(None, _XML_ILLEGAL) if strip else chunk
                ended_before = target.done
                try:
                    parser.feed(data)
                except ET.ParseError as e:
                    if not target.done:
                        raise _parse_error(e, source.label, target, head, at_end=False) from None
                    # </rss> was already read, so every item is in. What follows is junk (a PHP notice
                    # printed at shutdown) or a second export appended to this one.
                    while target.ready:
                        yield target.ready.popleft()
                    at = data.find(b"</rss")
                    tail = data[at:] if not ended_before and at >= 0 else prev_tail + data
                    _after_root_end(source.label, tail, chunks, announce)
                    return
                prev_tail = data[-64:]
                while target.ready:
                    yield target.ready.popleft()
                if stop is not None and stop(target):
                    return
            try:
                parser.close()
            except ET.ParseError as e:
                if not target.done:
                    raise _parse_error(e, source.label, target, head, at_end=True) from None
                _after_root_end(source.label, prev_tail, iter(()), announce)
            while target.ready:
                yield target.ready.popleft()
    except (OSError, EOFError, zipfile.BadZipFile, zlib.error) as e:
        raise ImporterError(f"Couldn't read {source.label}: {e}. If the download was interrupted, download "
                            "the export again.") from None


_DOCUMENT_MARKERS = (b"<?xml", b"<rss", b"<channel", b"<item")


def _after_root_end(label: str, tail: bytes, rest: Iterator[bytes], announce: bool) -> None:
    """
    Text after </rss>. WordPress's own importer copes with a PHP notice there
    (its fallback parser), so it is ignored with a note. If another export
    follows, joined on by hand, stop: ignoring it would silently drop its posts.
    """
    window, block = b"", tail
    while block:
        hay = window + block
        if any(marker in hay for marker in _DOCUMENT_MARKERS):
            raise ImporterError(f"{label} holds more than one export joined together (another export starts after "
                                "its closing </rss>). Pass each export file separately; several files or a folder "
                                "can go in one run.")
        window = hay[-8:]
        block = next(rest, b"")
    if announce:
        _log(f"Note: ignored text after the end of the export (</rss>) in {label}; usually a PHP notice that a "
             "plugin printed into the download.")


def _parse_error(e: ET.ParseError, label: str, target: _WxrTarget, head: bytes, at_end: bool) -> ImporterError:
    line, col = getattr(e, "position", (None, None))
    msg = str(e).split(":")[0]
    where = f" (line {line}, column {col})" if line else ""
    sniff = head.lstrip().lower()
    if target.root is None and (sniff.startswith(b"<!doctype html") or sniff.startswith(b"<html")):
        return ImporterError(f"{label} is a web page, not a WordPress export. Download the file again from "
                             "wp-admin > Tools > Export (see --help).")
    if target.root is None and not head.strip():
        return ImporterError(f"{label} is empty. Download the export again from wp-admin > Tools > Export.")
    if target.root is None:
        return ImporterError(f"{label} isn't a WordPress export (it isn't XML{where}). Download the export from "
                             "wp-admin > Tools > Export (see --help).")
    done = f" It stopped after {target.items_seen} item(s)." if target.items_seen else ""
    if at_end and target.root is not None:
        return ImporterError(f"{label} ends unexpectedly{where}: the export or its download was cut off.{done} "
                             "On big sites the export can time out: export \"Posts\" one date range at a time "
                             "(Tools > Export), or ask your host for a WP-CLI `wp export`, and pass all the files "
                             "in one run.")
    if "unknown encoding" in msg:
        hint = " Its XML declaration names a character encoding Python can't read; re-save it as UTF-8."
    elif "undefined entity" in msg:
        hint = (" It uses an entity (like &nbsp;) outside the post text, which XML doesn't allow; WordPress "
                "itself doesn't write these, so another tool may have edited the file.")
    else:
        hint = (" If that line holds a PHP 'Warning' or 'Notice', a plugin printed an error into the export; "
                "ask your web team to fix or disable it and export again. Otherwise the file may be damaged: "
                "download it again.")
    return ImporterError(f"Couldn't read {label} as a WordPress export: {msg}{where}.{hint}{done}")


def _peek_site_url(source: _Source) -> str:
    """The site address from an export's channel header, reading only as far as needed."""
    target = _WxrTarget(source.label, want_content=False, meta_keys=set())

    def enough(t: _WxrTarget) -> bool:
        return bool(t.channel.get("wp:base_blog_url") or t.items_seen)

    for _ in _parse_items(source, target, stop=enough):
        break
    return _site_url_from(target.channel)


def _site_url_from(channel: Dict[str, str]) -> str:
    for key in ("wp:base_blog_url", "link"):
        value = (channel.get(key) or "").strip()
        if value.lower().startswith(("http://", "https://")):
            return value.rstrip("/")
    return ""


# ---------------------------------------------------------------- pass 1: people and counts

@dataclass
class _People:
    """Names for one site: WordPress users from <wp:author>, and Co-Authors Plus guest authors."""
    users_by_login: Dict[str, str] = field(default_factory=dict)
    users_by_id: Dict[str, str] = field(default_factory=dict)
    guests_by_login: Dict[str, str] = field(default_factory=dict)
    guests_by_slug: Dict[str, str] = field(default_factory=dict)
    guests_by_id: Dict[str, str] = field(default_factory=dict)

    def add_user(self, author: Dict[str, str]) -> None:
        login = clean_text(author.get("wp:author_login")).lower()
        name = _repair(clean_text(author.get("wp:author_display_name")))
        if not name:
            name = _repair(clean_text(" ".join(filter(None, (author.get("wp:author_first_name"),
                                                             author.get("wp:author_last_name"))))))
        if not login or not name:
            return
        self.users_by_login.setdefault(login, name)
        uid = (author.get("wp:author_id") or "").strip()
        if uid.isdigit():
            self.users_by_id.setdefault(uid, name)

    def add_guest(self, item: Dict[str, Any]) -> None:
        meta = item.get("meta") or {}
        name = _repair(clean_text(_first(meta.get("cap-display_name")) or item.get("title")))
        if not name:
            first_last = " ".join(filter(None, (_first(meta.get("cap-first_name")), _first(meta.get("cap-last_name")))))
            name = _repair(clean_text(first_last))
        if not name:
            return
        login = clean_text(_first(meta.get("cap-user_login"))).lower()
        if login:
            self.guests_by_login.setdefault(login, name)
            self.guests_by_slug.setdefault("cap-" + _slug(login), name)
        slug = clean_text(item.get("wp:post_name")).lower()
        if slug:
            self.guests_by_slug.setdefault(slug, name)
        pid = (item.get("wp:post_id") or "").strip()
        if pid.isdigit():
            self.guests_by_id.setdefault(pid, name)

    @property
    def has_users(self) -> bool:
        return bool(self.users_by_login)


def _slug(text: str) -> str:
    """A rough sanitize_title(): enough to match guest-author slugs written by WordPress."""
    return re.sub(r"[^a-z0-9_-]+", "-", text.lower()).strip("-")


@dataclass
class _FileInfo:
    site_url: str = ""  # address used for links and ids (--site-url wins)
    found_url: str = ""  # address the export itself names
    site_key: str = ""
    wxr_version: str = ""
    items: int = 0


@dataclass
class _Scan:
    files: Dict[str, _FileInfo] = field(default_factory=dict)
    people: Dict[str, _People] = field(default_factory=dict)
    counts: Counter = field(default_factory=Counter)  # (post_type, status) -> n
    total_items: int = 0


def _scan(sources: List[_Source], opts: "_Options") -> _Scan:
    scan = _Scan()
    for source in sources:
        _log(f"Scanning {source.label} for authors and post counts...")
        target = _WxrTarget(source.label, want_content=False, meta_keys=_GUEST_META)
        info = _FileInfo()
        guests: List[Dict[str, Any]] = []
        first_link = ""
        for item in _parse_items(source, target, announce=True):
            ptype = (clean_text(item.get("wp:post_type")) or "post").lower()
            status = clean_text(item.get("wp:status")).lower()
            scan.counts[(ptype, status)] += 1
            if ptype == GUEST_AUTHOR_TYPE:
                guests.append(item)
            if not first_link:
                first_link = (item.get("link") or "").strip()
        if target.items_seen and not target.wp_items and not target.channel.get("wp:wxr_version"):
            raise ImporterError(f"{source.label} is an RSS feed, not a WordPress export: it has no WordPress "
                                "fields. Use the feed importer for feeds, or export the full archive from "
                                "wp-admin > Tools > Export.")
        info.items = target.items_seen
        info.wxr_version = clean_text(target.channel.get("wp:wxr_version"))
        found_url = _site_url_from(target.channel)
        info.site_url = opts.site_url or found_url
        if not info.site_url and first_link.lower().startswith(("http://", "https://")):
            parts = urllib.parse.urlsplit(first_link)
            info.site_url = f"{parts.scheme}://{parts.netloc}"
        info.site_key = _site_key(info.site_url) if info.site_url else _stem_key(source.path)
        info.found_url = found_url
        people = scan.people.setdefault(info.site_key, _People())
        for author in target.authors:
            people.add_user(author)
        for guest in guests:
            people.add_guest(guest)
        scan.files[source.label] = info
        scan.total_items += target.items_seen
        title = clean_text(target.channel.get("title"))
        version = f"WXR {info.wxr_version}" if info.wxr_version else "no WXR version"
        _log(f"  {target.items_seen:,} item(s); site {found_url or 'address not in the file'}"
             + (f" ({title})" if title else "") + f"; {version}")
        if info.wxr_version and not re.fullmatch(r"1\.[0-9]+", info.wxr_version):
            _log(f"  Note: WXR version {info.wxr_version[:20]} is newer than this importer knows (1.0-1.2); "
                 "reading it the same way.")
        if opts.site_url and found_url and found_url.rstrip("/").lower() != opts.site_url.lower():
            _log(f"  Links starting with {found_url} will point to {opts.site_url} (--site-url).")
    return scan


def _describe_counts(counts: Counter, post_types: List[str]) -> str:
    by_type: Counter = Counter()
    published: Counter = Counter()
    for (ptype, status), n in counts.items():
        by_type[ptype] += n
        if status == "publish":
            published[ptype] += n
    parts = []
    for ptype, n in sorted(by_type.items(), key=lambda kv: (kv[0] not in post_types, -kv[1], kv[0])):
        label = _display_type(ptype)
        parts.append(f"{label} {n:,}" + (f" ({published[ptype]:,} published)" if ptype in post_types else ""))
    return ", ".join(parts[:12]) + (", ..." if len(parts) > 12 else "")


# ---------------------------------------------------------------- options

@dataclass
class _Options:
    post_types: List[str]
    include_private: bool
    include_members_only: bool
    exclude_meta: List[Tuple[str, Optional[str]]]
    byline_meta: Optional[str]
    byline_taxonomy: str
    ignore_authors: Set[str]
    tz: Optional[tzinfo]
    site_url: Optional[str]

    @property
    def meta_keys(self) -> Set[str]:
        keys = set(_MEMBERS_META) | {k for k, _ in self.exclude_meta}
        if self.byline_meta:
            keys.add(self.byline_meta)
        return keys


def _options(args) -> _Options:
    return _Options(
        post_types=list(dict.fromkeys(getattr(args, "post_types", None) or ["post"])),
        include_private=bool(getattr(args, "include_private", False)),
        include_members_only=bool(getattr(args, "include_members_only", False)),
        exclude_meta=list(getattr(args, "exclude_meta", None) or []),
        byline_meta=getattr(args, "byline_meta", None),
        byline_taxonomy=getattr(args, "byline_taxonomy", None) or "author",
        ignore_authors={clean_text(n).casefold() for n in (getattr(args, "ignore_author", None) or []) if clean_text(n)},
        tz=getattr(args, "timezone", None),
        site_url=getattr(args, "site_url", None),
    )


# ---------------------------------------------------------------- dates

_WP_DATE = re.compile(r"^\s*(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?\s*$")
_RSS_DATE = re.compile(r"^\s*(?:[A-Za-z]{3},\s*)?\d{1,2}\s+[A-Za-z]{3}\s+(\d{4})\s+\d{1,2}:\d{2}(?::\d{2})?"
                       r"\s+(?:[+-]\d{4}|GMT|UTC|UT|Z|[ECMP][SD]T)\s*$")


def _wp_datetime(value: Any) -> Optional[datetime]:
    """'2024-04-30 16:00:00' -> naive datetime; None for WordPress's '0000-00-00 00:00:00' and junk."""
    m = _WP_DATE.match(str(value or ""))
    if not m:
        return None
    y, mo, d, h, mi, s = (int(g) if g else 0 for g in m.groups())
    if y < 1000:
        return None
    try:
        return datetime(y, mo, d, h, mi, s)
    except ValueError:
        return None


def _rss_datetime(value: Any) -> Optional[datetime]:
    """
    An RSS pubDate, strictly. Old WordPress versions wrote 'Mon, 30 Nov -0001
    00:00:00 +0000' for posts without a date, which email.utils happily misreads
    as the year 2000; that and the 1970 epoch are rejected.
    """
    text = str(value or "")
    m = _RSS_DATE.match(text)
    if not m or int(m.group(1)) < 1000:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(text.strip())
    except (TypeError, ValueError, IndexError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    if dt == datetime(1970, 1, 1, tzinfo=timezone.utc):
        return None
    return dt


class _Dates:
    def __init__(self, tz: Optional[tzinfo]):
        self.tz = tz
        self.used_timezone = 0
        self.checked = 0
        self.disagree = 0

    def publish_date(self, item: Dict[str, Any]) -> Tuple[Optional[datetime], Optional[str]]:
        """(UTC datetime, None), (None, skip reason), or (None, None) to let normalize() count it."""
        local = _wp_datetime(item.get("wp:post_date"))
        gmt = _wp_datetime(item.get("wp:post_date_gmt"))
        if gmt is not None:
            gmt = gmt.replace(tzinfo=timezone.utc)
            if self.tz is not None and local is not None:
                self.checked += 1
                if abs(local.replace(tzinfo=self.tz).astimezone(timezone.utc) - gmt) > timedelta(minutes=1):
                    self.disagree += 1
            return gmt, None
        pub = _rss_datetime(item.get("pubDate"))
        if pub is not None:
            return pub, None
        if local is None:
            return None, None
        if self.tz is None:
            return None, ("no UTC publish date in the export (often posts brought in from an older CMS); pass "
                          "--timezone with the site's time zone (Settings > General) to use the local date")
        self.used_timezone += 1
        return local.replace(tzinfo=self.tz).astimezone(timezone.utc), None


# ---------------------------------------------------------------- text

def _repair(text: str) -> str:
    """Undo utf8_encode()'d Windows-1252 punctuation (C1 controls -> curly quotes, dashes)."""
    if not text or not _C1_RE.search(text):
        return text
    return text.translate(_C1_TO_CP1252)


_SC_NAME = r"[A-Za-z_][\w-]*"
# Shortcodes whose content is a media URL or player settings, not article text.
_SC_MEDIA = ("embed", "video", "audio", "playlist", "wpvideo", "youtube", "vimeo", "soundcloud", "tweet",
             "instagram", "googlemaps", "gallery")
# [video]...[/video]. The body may not run past another opening tag of the same name: [video src=...] and
# [youtube URL] can stand alone, and a lazy match from one of those to a later [/video] deleted the article
# text in between.
_SC_MEDIA_BLOCK = re.compile(r"\[(" + "|".join(_SC_MEDIA) + r")\b[^\]]*\](?:(?!\[\1\b).)*?\[/\1\]", re.I | re.S)
# A standalone media shortcode whose only argument is a URL: [youtube https://youtu.be/x]
_SC_MEDIA_URL = re.compile(r"\[(?:" + "|".join(_SC_MEDIA) + r")\s+https?://[^\s\]\[]*\s*/?\]", re.I)
_SC_CLOSING = re.compile(r"\[/(" + _SC_NAME + r")\]")
_SC_WITH_ATTRS = re.compile(r"\[" + _SC_NAME + r"\s+[^\]\[]*=[^\]\[]*\]")
# Page-builder and media shortcodes that stand alone: [vc_separator], [gallery], [et_pb_divider] ...
_SC_STANDALONE = re.compile(r"\[(?:" + "|".join(_SC_MEDIA) + r"|[a-z]+_[\w-]*)\s*/?\]", re.I)


def _strip_shortcodes(body: str) -> str:
    """
    Remove WordPress shortcode tags but keep the text they wrap ([caption]'s
    caption, a page builder's columns). Bracketed text that isn't a shortcode,
    like [sic] or [Editor's note: ...], is left alone.
    """
    if "[" not in body:
        return body
    body = _SC_MEDIA_BLOCK.sub(" ", body)
    body = _SC_MEDIA_URL.sub(" ", body)
    closed ={m.group(1).lower() for m in _SC_CLOSING.finditer(body)}
    if closed:
        names = "|".join(re.escape(n) for n in sorted(closed, key=len, reverse=True))
        body = re.sub(r"\[/?(?:" + names + r")(?:\s[^\]\[]*)?/?\]", " ", body, flags=re.I)
    body = _SC_WITH_ATTRS.sub(" ", body)
    return _SC_STANDALONE.sub(" ", body)


def _first(values: Optional[List[str]]) -> str:
    return values[0] if values else ""


_PHP_STRING = re.compile(r'^\s*s:\d+:"(.*)";\s*$', re.S)
# Serialized PHP values other than a plain string: arrays, objects, numbers, booleans, null.
_PHP_SERIALIZED = re.compile(r'^\s*(?:a:\d+:\{|O:\d+:"|C:\d+:"|[idb]:[^;{}]*;\s*$|N;\s*$)')


def _php_string(value: str) -> str:
    """'s:5:"hello";' -> 'hello', as get_post_meta() unserializes it; anything else unchanged."""
    m = _PHP_STRING.match(value or "")
    return m.group(1) if m else (value or "")


# ---------------------------------------------------------------- bylines

_BY_PREFIX = re.compile(r"^\s*(?:by|written by|story by)[:\s]+", re.I)


def _php_flat_array(value: str) -> Optional[List[str]]:
    """Values of a flat PHP-serialized array, e.g. a:2:{i:0;s:2:"12";i:1;i:15;} -> ['12', '15']."""
    data = value.strip().encode("utf-8")
    m = re.match(rb"a:(\d+):\{", data)
    if not m:
        return None
    pos, out = m.end(), []
    for i in range(int(m.group(1)) * 2):
        sm = re.match(rb's:(\d+):"', data[pos:])
        if sm:
            start = pos + sm.end()
            end = start + int(sm.group(1))
            if data[end:end + 2] != b'";':
                return None
            token, pos = data[start:end].decode("utf-8", "replace"), end + 2
        else:
            im = re.match(rb"(?:i|d):(-?[\d.]+);|b:([01]);|N;", data[pos:])
            if not im:
                return None  # nested arrays, objects: not a list of bylines
            token, pos = (im.group(1) or b"").decode("ascii"), pos + im.end()
        if i % 2 == 1 and token:
            out.append(token)
    return out


# Words of a job title in a byline ('Staff Writers', 'Senior Metro Reporter'), never a name on their own.
_ROLE_WORDS = {
    "staff", "writer", "writers", "reporter", "reporters", "correspondent", "correspondents", "contributor",
    "contributors", "contributing", "editor", "editors", "columnist", "columnists", "photographer",
    "photographers", "intern", "interns", "producer", "producers", "critic", "critics", "senior", "special",
    "chief", "associate", "assistant", "deputy", "managing", "metro", "sports", "business", "political",
    "investigative", "data", "digital", "web", "news", "photo", "video",
}
_EMAIL = re.compile(r"[^\s@()<>\[\],;:\"']+@[^\s@()<>\[\],;:\"']+\.[A-Za-z]{2,}")


def _is_role(part: str) -> bool:
    words = re.findall(r"[a-z]+", part.lower())
    return bool(words) and all(w in _ROLE_WORDS for w in words)


def _scrub_name(name: str) -> str:
    """A byline without e-mail addresses: 'Mia Wong (mia@example.com)' -> 'Mia Wong'; 'jd@example.com' -> ''."""
    if "@" not in name:
        return name
    name = _EMAIL.sub(" ", name)
    name = re.sub(r"\(\s*\)|<\s*>|\[\s*\]", " ", name)
    return re.sub(r"\s+", " ", name).strip(" ,;:-|/")


def _split_byline(text: str) -> List[str]:
    """
    'By Jane Doe and Sam Lee' -> ['Jane Doe', 'Sam Lee']. Commas split only a
    list with 'and'/'&'. A part that is only a job title ('Jane Doe | Staff
    Writer', 'A and B, Staff Writers') is dropped when a name is left.
    """
    text = _BY_PREFIX.sub("", clean_text(text))
    if not text:
        return []
    has_conjunction = re.search(r"\s(?:and|&)\s", text, flags=re.I)
    parts = re.split(r"\s*(?:;|\||\s&\s|\sand\s" + (r"|," if has_conjunction else "") + r")\s*", text,
                     flags=re.I)
    parts = [p.strip(" ,") for p in parts if p and p.strip(" ,")]
    names = [p for p in parts if not _is_role(p)]
    return names or parts


class _Bylines:
    def __init__(self, opts: _Options):
        self.opts = opts
        self.unresolved_terms = 0  # posts with author terms whose names aren't in the export
        self.unknown_creators = 0  # posts whose author login has no <wp:author> entry
        self.unresolved_ids = 0  # posts whose byline field holds IDs not found in the export
        self.structured_meta = 0  # posts whose byline field holds records, not names

    def _accept(self, name: str, login: str = "") -> Optional[str]:
        """The name as it goes into the article (e-mail addresses removed), or None to leave it out."""
        name = _scrub_name(name)
        ignored = self.opts.ignore_authors
        if not name or name.casefold() in ignored or (login and login.casefold() in ignored):
            return None
        return name

    def resolve(self, item: Dict[str, Any], people: _People) -> List[str]:
        if self.opts.byline_meta:
            names = self._from_meta(item["meta"].get(self.opts.byline_meta) or [], people)
            if names:
                return names
        terms = [(slug, name) for domain, slug, name in item["categories"] if domain == self.opts.byline_taxonomy]
        if terms:
            names, unresolved = [], False
            for slug, term_name in terms:
                login = clean_text(term_name)
                name = self._term_name(slug.strip().lower(), login, people)
                kept = self._accept(name, login) if name is not None else None
                if name is None:
                    unresolved = True
                elif kept:
                    names.append(kept)
            self.unresolved_terms += unresolved
            # The terms are the byline; the post author may just be whoever uploaded it.
            return names
        login = clean_text(item.get("dc:creator"))
        if not login:
            return []
        name = people.users_by_login.get(login.lower())
        if name is None:
            if people.has_users:
                self.unknown_creators += 1
                return []
            name = _repair(login)  # a file with no author list (not made by WordPress): the creator is all there is
        kept = self._accept(name, login)
        return [kept] if kept else []

    @staticmethod
    def _term_name(slug: str, login: str, people: _People) -> Optional[str]:
        lower = login.lower()
        name = people.guests_by_slug.get(slug) or people.guests_by_login.get(lower) or \
            people.users_by_login.get(lower)
        if name:
            return name
        if slug.startswith("cap-"):
            return None  # a Co-Authors Plus term is named by login; the person's profile isn't in the export
        # Other byline plugins (PublishPress Authors) name the term after the person. Co-Authors Plus terms
        # from before its 'cap-' prefix (3.0) are named by user_login with the nicename as slug: a term
        # named exactly like its slug, or after an e-mail address, is a login, not a name.
        name = _repair(login)
        if not name or name == slug or _EMAIL.search(name):
            return None
        return name

    def _from_meta(self, values: List[str], people: _People) -> List[str]:
        names: List[str] = []
        missing = structured = False
        for value in values:
            value = _php_string(value)
            stripped = value.strip()
            tokens = _php_flat_array(stripped)
            if tokens is None and stripped[:1] in ("[", "{"):
                try:
                    parsed = json.loads(stripped)
                except ValueError:
                    parsed = None
                if isinstance(parsed, list):
                    tokens = [str(v) for v in parsed if isinstance(v, (str, int)) and not isinstance(v, bool)]
                    structured = structured or (bool(parsed) and not tokens)
                elif isinstance(parsed, dict):
                    tokens, structured = [], True
            serialized_id = re.fullmatch(r"i:(\d+);", stripped)
            if tokens is None and serialized_id:
                tokens = [serialized_id.group(1)]  # a user or guest-author ID stored as a PHP integer
            if tokens is None and _PHP_SERIALIZED.match(stripped):
                # Nested arrays or objects (records with name/title fields): not a list of names, and
                # splitting the serialized text would make 'a:1:{i:0' an author.
                tokens, structured = [], True
            for token in (tokens if tokens is not None else [value]):
                token = token.strip()
                if token.isdigit():
                    name = people.users_by_id.get(token) or people.guests_by_id.get(token)
                    if not name:
                        missing = True
                        continue
                    found = [name]
                else:
                    found = _split_byline(_repair(token))
                for name in found:
                    kept = self._accept(name)
                    if kept and kept not in names:
                        names.append(kept)
        if missing and not names:
            self.unresolved_ids += 1
        elif structured and not names:
            self.structured_meta += 1
        return names

    def notes(self) -> List[str]:
        notes = []
        if self.unresolved_terms:
            notes.append(f"Note: {self.unresolved_terms} post(s) have Co-Authors Plus bylines whose names aren't in "
                         "this export (only a login is), so those authors were left out (no other name was "
                         "substituted). Guest-author profiles are only in an \"All content\" export, and a user "
                         "account is listed only if it is the main author of something exported: export again with "
                         "All content (Tools > Export) and re-run.")
        if self.unknown_creators:
            notes.append(f"Note: {self.unknown_creators} post(s) name an author account that isn't in the export's "
                         "author list (often a deleted user); they were imported without an author.")
        if self.unresolved_ids:
            notes.append(f"Note: {self.unresolved_ids} post(s) have a --byline-meta value made of IDs that don't "
                         "match any user or guest author in the export; they fell back to the post's other bylines.")
        if self.structured_meta:
            notes.append(f"Note: {self.structured_meta} post(s) have a --byline-meta value that holds records "
                         "(a PHP-serialized or JSON array of fields), not names, so it wasn't used; they fell back to "
                         "the post's other bylines.")
        return notes


# ---------------------------------------------------------------- items -> raw articles

def _display_type(ptype: str) -> str:
    return re.sub(r"[^a-z0-9_-]", "", ptype.lower())[:40] or "?"


def _type_reason(ptype: str) -> str:
    t = _display_type(ptype)
    if t == "attachment":
        return "media library item (attachment), not an article"
    if t == "page":
        return "page, not a post (add --post-type page to import pages)"
    if t == "nav_menu_item":
        return "menu item, not an article"
    if t == "revision":
        return "saved revision of another post"
    if t == GUEST_AUTHOR_TYPE:
        return "Co-Authors Plus guest author profile (used for bylines), not an article"
    if t.startswith("wp_") or t in ("custom_css", "customize_changeset", "oembed_cache", "user_request"):
        return f"site setting or template ({t}), not an article"
    return f"post type '{t}' not selected (add --post-type {t} to import it)"


def _status_reason(status: str) -> str:
    s = re.sub(r"[^a-z0-9_-]", "", status.lower())[:40]
    if s == "private":
        return "private post (use --include-private to import)"
    if s in ("draft", "auto-draft", "pending"):
        return "draft or pending review (not published)"
    if s == "future":
        return "scheduled, not yet published"
    if s == "trash":
        return "in the trash"
    if not s:
        return "no publish status in the export (wp:status), so it can't be confirmed as published"
    return f"status '{s}' (only published posts are imported)"


def _truthy(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


def _members_only_reason(item: Dict[str, Any], body: str) -> Optional[str]:
    meta = item["meta"]
    for access in meta.get(JETPACK_ACCESS) or []:
        # get_post_meta() unserializes: s:11:"subscribers"; is the string 'subscribers'.
        level = _php_string(access).strip().lower()
        # Jetpack itself treats an empty or non-text (serialized) value as "everybody".
        if level and level != "everybody" and not re.match(r"^[aOs]:\d+:", level):
            return "subscribers or paid members only (Jetpack); use --include-members-only to import"
    # Jetpack computes the paid-content flag on save, and only for 'post' and 'page'; a custom post type, or
    # a post not saved since, carries the Paid Content block without it.
    if any(_truthy(v) for key in (JETPACK_PAID, JETPACK_PAYWALLED) for v in meta.get(key) or []) \
            or "<!-- wp:premium-content/container" in body:
        return "has paid or subscriber-only content (Jetpack); use --include-members-only to import"
    if "<!-- wp:jetpack/paywall" in body:
        return "behind a Jetpack paywall block; use --include-members-only to import"
    return None


def _excluded_by(item: Dict[str, Any], rules: List[Tuple[str, Optional[str]]]) -> Optional[str]:
    for key, want in rules:
        for value in item["meta"].get(key) or []:
            if (want is None and value.strip()) or (want is not None and value.strip() == want):
                return f"excluded by --exclude-meta {key}" + (f"={want}" if want is not None else "")
    return None


def _rewrite_link(link: str, found_url: str, site_url: Optional[str]) -> str:
    if not (site_url and found_url):
        return link
    base = found_url.rstrip("/")
    if link.lower().startswith(base.lower()) and link[len(base):len(base) + 1] in ("", "/", "?", "#"):
        return site_url + link[len(base):]
    return link


@dataclass
class _Context:
    info: _FileInfo
    people: _People
    opts: _Options
    dates: _Dates
    bylines: _Bylines


def _to_raw(item: Dict[str, Any], ctx: _Context) -> Dict[str, Any]:
    opts = ctx.opts
    ptype = (clean_text(item.get("wp:post_type")) or "post").lower()  # WordPress's importer defaults to post too
    if ptype not in opts.post_types:
        return {"_skip": _type_reason(ptype)}
    status = clean_text(item.get("wp:status")).lower()
    if status != "publish" and not (status == "private" and opts.include_private):
        return {"_skip": _status_reason(status)}
    if (item.get("wp:post_password") or "").strip():
        return {"_skip": "password-protected (its text isn't public)"}
    body = item.get("content:encoded") or ""
    if not opts.include_members_only:
        reason = _members_only_reason(item, body)
        if reason:
            return {"_skip": reason}
    reason = _excluded_by(item, opts.exclude_meta)
    if reason:
        return {"_skip": reason}
    when, reason = ctx.dates.publish_date(item)
    if reason:
        return {"_skip": reason}
    pid = (item.get("wp:post_id") or "").strip()
    link = _rewrite_link((item.get("link") or "").strip(), ctx.info.found_url, opts.site_url)
    raw = {
        "id": f"wp-{ctx.info.site_key}-{pid}" if pid.isdigit() and ctx.info.site_key else "",
        "headline": _repair(item.get("title") or ""),
        "content": _repair(html_to_text(_strip_shortcodes(body))),
        "url": link,
        "authors": [],
        "publish_date": when,
    }
    if raw["id"] and raw["headline"].strip() and raw["content"]:
        raw["authors"] = ctx.bylines.resolve(item, ctx.people)
    return raw


# ---------------------------------------------------------------- main loop

def _log(message: str) -> None:
    print(message, file=sys.stderr)


def iter_articles(args) -> Iterator[Dict[str, Any]]:
    opts = _options(args)
    sources = _sources(getattr(args, "files", None) or [])
    if not sources:
        raise ImporterError("Pass the export file with --file (see --help for where to download it).")
    scan = _scan(sources, opts)
    if len(sources) > 1:
        _log(f"{scan.total_items:,} item(s) in {len(sources)} files.")
    if scan.total_items:
        _log("Contents: " + _describe_counts(scan.counts, opts.post_types))
    selected = sum(n for (ptype, _), n in scan.counts.items() if ptype in opts.post_types)
    if scan.total_items and not selected:
        _log(f"Note: the export has no items of type {', '.join(opts.post_types)}; see the counts above and "
             "--post-type.")

    dates = _Dates(opts.tz)
    bylines = _Bylines(opts)
    done, last_log = 0, time.monotonic()
    try:
        for source in sources:
            info = scan.files[source.label]
            ctx = _Context(info, scan.people[info.site_key], opts, dates, bylines)
            _log(f"Reading {source.label}...")
            target = _WxrTarget(source.label, want_content=True, meta_keys=opts.meta_keys)
            for item in _parse_items(source, target):
                yield _to_raw(item, ctx)
                done += 1
                now = time.monotonic()
                if done % PROGRESS_EVERY == 0 or now - last_log > 10:
                    last_log = now
                    pct = f" ({100 * done // scan.total_items}%)" if scan.total_items else ""
                    _log(f"  {done:,}/{scan.total_items:,} items{pct}")
    finally:
        for note in bylines.notes():
            _log(note)
        if dates.used_timezone:
            _log(f"Note: {dates.used_timezone} post(s) had no UTC date in the export; their local dates were read "
                 "in the --timezone you gave.")
        if dates.disagree:
            _log(f"Note: for {dates.disagree} of {dates.checked} post(s) that have both dates, --timezone doesn't "
                 "match the export's own UTC date (the site's time zone may have changed over the years). Those "
                 "posts used the export's UTC date.")
