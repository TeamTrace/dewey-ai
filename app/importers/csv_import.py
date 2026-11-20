"""
CSV / spreadsheet importer: articles from a table exported by any CMS,
database or spreadsheet, one row per article.

    python app/import_archive.py csv --file articles.csv --limit 20

Columns are found by header name (title/headline, body/content/text, url/link/
permalink, author/byline, date/published/pub_date, id/guid, ...) or set with
--headline-col and friends, and the mapping is printed before anything is
imported so it can be checked. Run `python app/import_archive.py csv --help`
for every option and where to get an export.

Nothing is guessed: a date like 05/01/2024 (5 January or May 1?) stops the
import until --date-format says which; a row without a byline keeps
`authors: []`; rows a status, visibility or password column marks as not
public are skipped and counted. The file is streamed row by row, never loaded
whole. Standard library only.
"""
import argparse
import codecs
import csv
import gzip
import hashlib
import html
import itertools
import json
import re
import sys
import urllib.parse
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from importers.common import ImporterError, clean_text

NAME = "csv"
HELP = ("CSV/TSV table from any CMS, database or spreadsheet, with column mapping "
        "(detected from the header or set by flags).")

DEFAULT_ENCODING = "utf-8-sig"
DEFAULT_PUBLISHED = ("publish", "published", "live", "public", "1", "true", "yes")
DEFAULT_PUBLIC = ("public",)
SAMPLE_ROWS = 500            # rows read ahead to check the mapping and dates before importing
SAMPLE_CHARS = 8_000_000     # ...or this much text, whichever comes first
SNIFF_CHARS = 64 * 1024      # text given to csv.Sniffer
PROGRESS_EVERY = 5000        # rows between progress lines
EXCEL_CELL_LIMIT = 32767     # Excel truncates longer cells
FIELD_SIZE_LIMIT = 2**31 - 1  # largest value csv.field_size_limit accepts on every platform (C long)

# Cells that mean "no value" in database and spreadsheet exports (exact match only).
NULL_MARKERS = frozenset({"\\N", "NULL", "null", "None", "NaN", "nan", "#N/A", "N/A", "n/a"})

EPILOG = """\
Where to get the file (--file):
  Any table with one row per article and a header row works (.csv, .tsv, .txt;
  gzipped is fine). Usual routes:
    Spreadsheet  Excel: File > Save As > "CSV UTF-8 (Comma delimited) (*.csv)".
                 Google Sheets: File > Download > Comma Separated Values (.csv).
    Database     PostgreSQL: \\copy (SELECT ...) TO 'articles.csv' WITH (FORMAT csv, HEADER)
                 MySQL/MariaDB: a CSV export with the column names in the first row
                 (phpMyAdmin, MySQL Workbench and similar tools offer one).
    CMS          the CMS's own "export to CSV/spreadsheet" feature, or ask your web
                 team for a table of published articles with the columns below.
                 Drupal: a view with the Views Data Export module. WordPress: the
                 wordpress-api and wordpress-export importers read WordPress directly.
  Excel workbooks (.xlsx/.xls) must be saved as CSV first. Excel cuts every cell
  at 32,767 characters, so long articles that passed through Excel may be
  truncated (the importer warns); export straight from the CMS when you can.

Columns (headers are matched ignoring case, spaces and punctuation):
  headline  required  headline, title, post_title, article_title, hed
  content   required  the full text or HTML: content, body, text, article,
                      post_content, body_html, body_value. Never an excerpt or
                      summary column, unless you name it with --content-col.
  url       required  the public article address: url, permalink, link,
                      canonical_url, web_url. Paths such as /news/story need --base-url.
  date      required  the publish date: publish_date, published_at, published,
                      pub_date, publication_date, post_date, date; a *_gmt or *_utc
                      twin (post_date_gmt, date_gmt) is preferred when there is one
  authors   optional  byline names: authors, byline, author_name, author, writer,
                      creator (a byline column wins over 'author', which is often a
                      login). Rows without one keep no byline (none is made up);
                      values that are numbers (user ids) or e-mail addresses are dropped.
  id        optional  the CMS's article id: id, article_id, post_id, story_id, nid,
                      uuid, guid. Without one, ids are a hash of the URL.
  Choose a column yourself with --headline-col NAME (or a number, 1 = first
  column), or 'none' for an optional one. The mapping is printed before
  anything is imported: check it on a small run (--limit 20) first.

Publish status: only public, published rows are imported. When the table has
  a status column (status, post_status, publish_status, workflow_state, ...),
  only rows whose status is in --published-values are kept; drafts, private,
  pending, scheduled and trashed rows are skipped and counted. A visibility
  column (visibility, access, ...) keeps only 'public' rows (members-only and
  paid posts are skipped), and rows with a value in a password column
  (post_password, password) are skipped. A yes/no 'Published' column (Drupal
  Views) counts as a status column, and flag columns such as deleted_at,
  is_deleted, is_draft, is_private, hidden or members_only (application
  databases) skip every row where they are set (--hidden-col). Rows dated more
  than a day ahead are skipped as scheduled. --where COLUMN=VALUE keeps only
  matching rows, e.g. --where post_type=post for a WordPress database export.

Dates: ISO 8601 (2024-05-01T13:00:00Z, 2024-05-01 13:00:00), RFC 822
  (Wed, 01 May 2024 13:00:00 -0400), Unix timestamps, YYYY/MM/DD and dates with
  month names (May 1, 2024; 1 May 2024; Sept. 5, 2023) are read automatically.
  Numeric day/month dates such as 05/01/2024 are never guessed: the import
  stops and asks for --date-format '%m/%d/%Y' (US) or '%d/%m/%Y'.
  Dates without a time zone are taken as UTC unless you give
  --timezone America/New_York (daylight saving aware) or --timezone-offset=-05:00
  (write negative offsets with '='). Columns named *_gmt or *_utc (WordPress's
  post_date_gmt) are always read as UTC.

Encoding: UTF-8, with or without Excel's byte-order mark, by default. Files
  Excel on Windows saved as plain "CSV (Comma delimited)" are usually Windows-1252:
  use --encoding cp1252. UTF-16 files (Excel "Unicode Text") are recognized by
  their byte-order mark. A UTF-8 file read as cp1252 is refused (its accents
  would come out as 'CafÃ©'); a UTF-8 file with a few bad bytes stops at the
  line that has them, and --encoding-errors replace imports it anyway.

Re-importing: ids are --id-prefix (default 'csv') plus the id column, or a
  hash of the URL, so importing a newer export of the same table replaces
  articles instead of duplicating them. Use the same --prefix for each
  re-import, and give each different source its own --id-prefix
  (e.g. --id-prefix csv-inquirer) so ids from two CMSs can't collide.

Secrets: none. This importer reads a local file and takes no API key or
  password (so there is no flag or environment variable for one).
"""


# ---------------------------------------------------------------- columns

@dataclass(frozen=True)
class _Role:
    key: str
    flag: str
    label: str
    required: bool
    candidates: Tuple[str, ...]


# Order matters twice: roles are auto-detected in this order (a header goes to the
# first role that wants it), and each role takes its first matching candidate.
_ROLES: Tuple[_Role, ...] = (
    _Role("id", "--id-col", "id", False,
          ("id", "article_id", "story_id", "post_id", "node_id", "nid", "uuid", "guid")),
    _Role("url", "--url-col", "URL", True,
          ("url", "permalink", "link", "canonical_url", "canonical", "article_url", "story_url",
           "post_url", "web_url", "uri", "href")),
    _Role("headline", "--headline-col", "headline", True,
          ("headline", "title", "post_title", "article_title", "story_title", "hed")),
    _Role("date", "--date-col", "publish date", True,
          ("publish_date", "published_date", "published_at", "publication_date", "date_published",
           "pub_date", "pubdate", "post_date_gmt", "post_date", "first_published_at",
           "first_published", "published", "publish_time", "published_time", "date")),
    _Role("content", "--content-col", "article text", True,
          ("content", "body", "text", "article", "post_content", "article_body", "body_html",
           "content_html", "body_value", "body_text", "article_text", "story_text", "full_text",
           "content_encoded", "html", "story")),
    # Display-name columns before a bare 'author', which in application databases is often a login.
    _Role("authors", "--authors-col", "byline", False,
          ("authors", "byline", "bylines", "author_names", "author_name", "author_display_name", "author",
           "writer", "writers", "creator", "dc_creator", "by")),
    # A 'published' yes/no column (Drupal Views' label for the node status) comes last; the date role
    # is resolved first, so it only gets here when another column holds the date.
    _Role("status", "--status-col", "publish status", False,
          ("status", "post_status", "publish_status", "publication_status", "published_status",
           "workflow_state", "moderation_state", "is_published", "published_flag", "published")),
    _Role("visibility", "--visibility-col", "visibility", False,
          ("visibility", "post_visibility", "access", "access_level")),
    _Role("password", "--password-col", "password", False, ("post_password", "password")),
    # Flags meaning "not public": every matching column is used, and a set flag skips the row.
    _Role("hidden", "--hidden-col", "not-public flag", False,
          ("deleted_at", "is_deleted", "deleted", "trashed_at", "is_trashed", "trashed", "is_draft", "draft",
           "is_private", "private", "is_hidden", "hidden", "is_unpublished", "unpublished", "members_only",
           "is_members_only", "subscribers_only", "subscriber_only")),
)
_ROLE = {r.key: r for r in _ROLES}
_MULTI = ("authors", "hidden")  # roles that can use several columns
_DISPLAY_ORDER = ("headline", "content", "url", "date", "authors", "id", "status", "visibility", "password",
                  "hidden")
_UNSET_FLAGS = frozenset({"0", "0.0", "false", "f", "no", "n", "off"})


def _norm(name: str) -> str:
    """Header key for matching: case-folded, spaces and punctuation removed ('Publish Date' -> 'publishdate')."""
    return re.sub(r"[\W_]+", "", name.casefold())


def _describe_headers(headers: Sequence[str]) -> str:
    shown = ", ".join(f"{i} {h[:40]!r}" for i, h in enumerate(headers[:60], 1))
    more = f", ... ({len(headers)} in all)" if len(headers) > 60 else ""
    return f"Columns found: {shown or '(none)'}{more}."


def _lookup(spec: str, headers: Sequence[str], flag: str, has_header: bool,
            warnings: List[str]) -> int:
    """Index of the column named (or numbered) by a user's flag value."""
    s = spec.strip()
    hits: List[int] = []
    if has_header:
        tests = (lambda h: h == s, lambda h: h.casefold() == s.casefold(),
                 lambda h: bool(_norm(s)) and _norm(h) == _norm(s))
        for test in tests:
            hits = [i for i, h in enumerate(headers) if test(h)]
            if hits:
                break
    if not hits and re.fullmatch(r"#?\d+", s):
        n = int(s.lstrip("#"))
        if not 1 <= n <= len(headers):
            raise ImporterError(f"{flag} {s}: the file has {len(headers)} column(s); numbers start "
                                f"at 1. {_describe_headers(headers)}")
        hits = [n - 1]
    if not hits:
        raise ImporterError(f"{flag} {spec!r}: there is no column with that name. "
                            f"{_describe_headers(headers)}")
    if len(hits) > 1:
        warnings.append(f"{len(hits)} columns are named {headers[hits[0]]!r}; {flag} uses the first "
                        f"(column {hits[0] + 1}). Give a column number to pick another.")
    return hits[0]


@dataclass
class _Mapping:
    cols: Dict[str, Optional[int]]
    authors: List[int]
    headers: List[str]
    warnings: List[str] = field(default_factory=list)
    hidden: List[int] = field(default_factory=list)
    auto: set = field(default_factory=set)  # roles that were auto-detected, not named by a flag

    def name(self, idx: Optional[int]) -> str:
        return "(none)" if idx is None else f"{self.headers[idx]!r} (column {idx + 1})"


def _resolve_columns(headers: List[str], opts: "_Options") -> _Mapping:
    warnings: List[str] = []
    cols: Dict[str, Optional[int]] = {}
    multi: Dict[str, List[int]] = {k: [] for k in _MULTI}
    taken: set = set()
    disabled: set = set()
    auto: set = set()

    # Columns named on the command line first, so auto-detection can't take them.
    for role in _ROLES:
        specs = opts.multi_specs.get(role.key, []) if role.key in _MULTI else [opts.specs.get(role.key)]
        for spec in [s for s in specs if s is not None]:
            if spec.strip().lower() in ("none", ""):
                if role.required:
                    raise ImporterError(f"{role.flag} can't be 'none': every article needs a {role.label}.")
                disabled.add(role.key)
                continue
            idx = _lookup(spec, headers, role.flag, opts.has_header, warnings)
            taken.add(idx)
            if role.key in _MULTI:
                if idx not in multi[role.key]:
                    multi[role.key].append(idx)
            else:
                cols[role.key] = idx

    # Then the rest from common header names.
    norm_headers = [_norm(h) for h in headers] if opts.has_header else []
    for role in _ROLES:
        if role.key in disabled or (role.key in _MULTI and multi[role.key]) or cols.get(role.key) is not None:
            continue
        candidates = role.candidates
        if role.key == "date":  # a *_gmt / *_utc twin is the same moment without the local-time guesswork
            candidates = tuple(v for c in candidates for v in (c + "_gmt", c + "_utc", c))
        for cand in candidates:
            hits = [i for i, n in enumerate(norm_headers) if n == _norm(cand) and i not in taken]
            if not hits:
                continue
            auto.add(role.key)
            if role.key == "hidden":  # every flag column counts
                taken.add(hits[0])
                multi["hidden"].append(hits[0])
                continue
            if len(hits) > 1:
                warnings.append(f"{len(hits)} columns are named {headers[hits[0]]!r}; using the first "
                                f"(column {hits[0] + 1}) as the {role.label}. Use {role.flag} to choose.")
            taken.add(hits[0])
            if role.key == "authors":
                multi["authors"].append(hits[0])
            else:
                cols[role.key] = hits[0]
            break
        cols.setdefault(role.key, None)

    missing = [r for r in _ROLES if r.required and cols.get(r.key) is None]
    if missing:
        if not opts.has_header:
            raise ImporterError("With --no-header, give each required column by number (1 = first): "
                                + ", ".join(f"{r.flag} N" for r in missing) + ". "
                                + _describe_headers(headers))
        lines = [f"  {r.label}: looked for {', '.join(r.candidates[:6])}, ...; name it with {r.flag} COLUMN"
                 for r in missing]
        raise ImporterError("Couldn't find these required columns in the header row:\n" + "\n".join(lines)
                            + "\n" + _describe_headers(headers)
                            + ("" if len(headers) > 1 else
                               " Only one column was found: check --delimiter (and --encoding)."))
    return _Mapping(cols, multi["authors"], headers, warnings, hidden=multi["hidden"], auto=auto)


# ---------------------------------------------------------------- dates

# Day and month both as numbers (05/01/2024, 1.5.24, 05/01/2024 - 10:00), with the joiner before any time.
_NUMERIC_DM = re.compile(r"^(\d{1,2})([/.\-])(\d{1,2})\2(\d{4}|\d{2})(?:(\s*,\s*|\s+-\s+|\s+|T)(.+))?$")
_TIME_PART = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2})(\.\d+)?)?(\s*)([AaPp]\.?[Mm]\.?)?$")
_UTC_SUFFIX = re.compile(r"\s*(?:UTC|GMT|Z)$", re.I)

_DATE_PARTS = ("%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y", "%d %B %Y", "%d %b %Y",
               "%d %B, %Y", "%d %b, %Y", "%d-%b-%Y", "%d-%B-%Y", "%d %b %y",  # RFC 822 allows 2-digit years
               "%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d")
_TIME_PARTS = ("", " %H:%M", " %H:%M:%S", " %I:%M %p", " %I:%M:%S %p", " %I:%M%p", " %I:%M:%S%p",
               ", %I:%M %p", ", %H:%M")
# Unambiguous fallbacks: month names, or year first. Never DD/MM vs MM/DD.
_AUTO_FORMATS = tuple(d + t for d in _DATE_PARTS for t in _TIME_PARTS)

# Time zone abbreviations written after a time. Only unambiguous ones: an unknown abbreviation
# (IST is India, Ireland or Israel) makes the date unreadable rather than silently ignored.
_ZONE_OFFSETS = {"UT": 0, "UTC": 0, "GMT": 0, "Z": 0, "EST": -5, "EDT": -4, "CST": -6, "CDT": -5,
                 "MST": -7, "MDT": -6, "PST": -8, "PDT": -7, "AKST": -9, "AKDT": -8, "HST": -10,
                 "WET": 0, "WEST": 1, "CET": 1, "CEST": 2, "EET": 2, "EEST": 3}
_ZONE_REGIONS = {"ET": "America/New_York", "CT": "America/Chicago", "MT": "America/Denver",
                 "PT": "America/Los_Angeles"}  # AP style: daylight saving depends on the date


class _UnknownZone(Exception):
    def __init__(self, zone: str):
        super().__init__(zone)
        self.zone = zone


def _normalize_named(s: str) -> str:
    """AP-style and other prose dates into something strptime reads: 'Wed., Sept. 5th, 2023, 1:05 p.m.'."""
    s = re.sub(r"\s+", " ", s.strip())
    s = re.sub(r"^(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*\.?,?\s+", "", s, flags=re.I)
    s = re.sub(r"\bsept\b", "Sep", s, flags=re.I)
    s = re.sub(r"\b(jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec)\.", r"\1", s, flags=re.I)
    s = re.sub(r"(\d)(?:st|nd|rd|th)\b", r"\1", s, flags=re.I)
    s = re.sub(r"(?<=\d),? at (?=\d)", " ", s, flags=re.I)
    # Unix date(1) / C asctime(): 'May 1 09:00:00 EDT 2024' -> 'May 1 2024 09:00:00 EDT'
    s = re.sub(r"^([A-Za-z]+ \d{1,2}) (\d{1,2}:\d{2}(?::\d{2})?)((?: [A-Za-z]{1,5})?) (\d{4})$", r"\1 \4 \2\3", s)
    s = re.sub(r"\b([ap])\.?m\.?(?=\s|$)", lambda m: m.group(1).upper() + "M", s, flags=re.I)
    return s


def _split_zone(s: str) -> Tuple[str, Optional[tzinfo]]:
    """Split a time zone written after the time off a date: (rest, zone or None).
    Raises _UnknownZone for one the importer can't convert, so it is never silently dropped."""
    s = s.strip()
    descriptive = ""
    m = re.search(r"\s*\(([^()]*)\)$", s)  # JavaScript's '(Eastern Daylight Time)', or '(EDT)'
    if m:
        inner = m.group(1).strip()
        s = s[:m.start()].rstrip()
        if re.fullmatch(r"[A-Za-z]{1,5}", inner):
            s = f"{s} {inner}"
        else:
            descriptive = inner

    def after_time(head: str) -> bool:
        head = head.rstrip()
        return ":" in head and bool(re.search(r"(?:\d|(?<![A-Za-z])[AaPp]M)$", head))

    # '+0530', 'GMT-0400', 'UTC+05:30' after the time; a sign right after the time ('10:00-11:30') is a range.
    m = re.search(r"(?:\s*(?:GMT|UTC|UT)\s*|\s+)([+-])(\d{1,2}):?(\d{2})$", s, re.I)
    if m and after_time(s[:m.start()]):
        hours, minutes = int(m.group(2)), int(m.group(3))
        if hours > 14 or minutes > 59:
            raise _UnknownZone(s[m.start():].strip())
        delta = timedelta(hours=hours, minutes=minutes)
        return s[:m.start()].rstrip(), timezone(-delta if m.group(1) == "-" else delta)
    m = re.search(r"(?<![A-Za-z])([A-Za-z]{1,5})$", s)
    if m and m.group(1).upper() not in ("AM", "PM") and after_time(s[:m.start()]):
        word, head = m.group(1).upper(), s[:m.start()].rstrip()
        if word in _ZONE_OFFSETS:
            return head, timezone(timedelta(hours=_ZONE_OFFSETS[word]))
        if word in _ZONE_REGIONS:
            try:
                from zoneinfo import ZoneInfo
                return head, ZoneInfo(_ZONE_REGIONS[word])
            except Exception:  # no time zone database (Windows without tzdata)
                pass
        raise _UnknownZone(m.group(1))
    if descriptive:
        raise _UnknownZone(descriptive)
    return s, None


def _numeric_dm(value: str) -> Optional["re.Match"]:
    """The day/month-as-numbers match for a date value (after its weekday and zone), or None."""
    try:
        rest, _ = _split_zone(_normalize_named(value))
    except _UnknownZone:
        rest = _normalize_named(value)
    return _NUMERIC_DM.match(rest)


def _suggest_formats(m: "re.Match") -> Tuple[str, str]:
    """(month-first, day-first) strptime formats for an ambiguous numeric date match."""
    sep, year, joiner, rest = m.group(2), m.group(4), m.group(5), m.group(6)
    y = "%Y" if len(year) == 4 else "%y"
    time_fmt = ""
    if rest:
        joiner = "T" if joiner == "T" else re.sub(r"\s+", " ", joiner)
        t = _TIME_PART.match(rest.strip())
        if t:
            ampm = bool(t.group(6))
            time_fmt = joiner + ("%I" if ampm else "%H") + ":%M"
            if t.group(3):
                time_fmt += ":%S" + (".%f" if t.group(4) else "")
            if ampm:
                time_fmt += (" " if t.group(5) else "") + "%p"
        else:
            time_fmt = joiner + "..."
    return (f"%m{sep}%d{sep}{y}{time_fmt}", f"%d{sep}%m{sep}{y}{time_fmt}")


class _AmbiguousDate(Exception):
    def __init__(self, value: str, match: "re.Match"):
        super().__init__(value)
        self.value = value
        self.match = match


class _DateParser:
    def __init__(self, formats: Sequence[str], tz: tzinfo, column_is_utc: bool):
        self.formats = list(formats)
        self.tz = timezone.utc if column_is_utc else tz
        self.last_auto: Optional[str] = None

    def parse(self, value: str) -> Optional[datetime]:
        """Aware UTC datetime, or None if unreadable. Raises _AmbiguousDate for DD/MM-or-MM/DD values
        when no --date-format was given, and _UnknownZone for a zone name it can't convert."""
        s = value.strip()
        dt = self._parse_naive_or_aware(s)
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=self.tz)
        return dt.astimezone(timezone.utc)

    def _user_formats(self, s: str) -> Optional[datetime]:
        for fmt in self.formats:
            if fmt.lower() in ("unix", "epoch", "unix-ms", "epoch-ms"):
                try:
                    n = float(s)
                except ValueError:
                    continue
                return _from_epoch(n / 1000 if fmt.lower().endswith("-ms") else n)
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                continue
        return None

    def _parse_naive_or_aware(self, s: str) -> Optional[datetime]:
        dt = self._user_formats(s)
        if dt is not None:
            return dt
        if re.fullmatch(r"\d{9,10}(?:\.\d+)?", s):
            return _from_epoch(float(s))
        if re.fullmatch(r"\d{12,13}", s):
            return _from_epoch(int(s) / 1000)
        iso = _UTC_SUFFIX.sub("+00:00", s) if re.match(r"\d{4}-\d{2}-\d{2}", s) else s
        try:
            return datetime.fromisoformat(iso)
        except ValueError:
            pass
        # Prose dates: weekday, AP abbreviations and a.m./p.m. normalized, and the zone after the time
        # split off and applied (a zone in the text wins over --timezone, which is for dates without one).
        rest, zone = _split_zone(_normalize_named(s))
        if rest != s:
            dt = self._user_formats(rest)
            if dt is not None:
                return dt.replace(tzinfo=zone) if zone and dt.tzinfo is None else dt
        order = ([self.last_auto] if self.last_auto else []) + list(_AUTO_FORMATS)
        for fmt in order:
            try:
                dt = datetime.strptime(rest, fmt)
            except ValueError:
                continue
            self.last_auto = fmt
            return dt.replace(tzinfo=zone) if zone else dt
        # RFC 822 and Unix date(1) forms are covered above (see _normalize_named). email.utils is not used:
        # it ignores AM/PM, unknown zone names and trailing text without a word.
        m = _NUMERIC_DM.match(rest)
        if m and not self.formats:
            raise _AmbiguousDate(s, m)
        return None


def _from_epoch(n: float) -> Optional[datetime]:
    try:
        return datetime.fromtimestamp(n, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _ambiguous_error(column: str, err: _AmbiguousDate, where: str, evidence: Sequence[str]) -> ImporterError:
    month_first, day_first = _suggest_formats(err.match)
    msg = (f"Dates in column {column!r} are written like {err.value!r} ({where}), with numbers for both "
           "the day and the month. The importer won't guess which comes first (05/01 could be 5 January "
           "or May 1). Re-run with one of:\n"
           f"  --date-format '{month_first}'   (month first, as in the US)\n"
           f"  --date-format '{day_first}'   (day first, as in most other countries)")
    matches = [(v, _numeric_dm(v)) for v in evidence]
    matches = [(v, m) for v, m in matches if m]
    day_ev = [v for v, m in matches if int(m.group(1)) > 12]
    month_ev = [v for v, m in matches if int(m.group(3)) > 12]
    if day_ev and not month_ev:
        msg += f"\nIn the first rows, {day_ev[0]!r} only fits day first."
    elif month_ev and not day_ev:
        msg += f"\nIn the first rows, {month_ev[0]!r} only fits month first."
    elif day_ev and month_ev:
        msg += (f"\nThe column mixes both orders ({day_ev[0]!r} vs {month_ev[0]!r}), so its dates can't be "
                "read reliably; fix them in the export first.")
    return ImporterError(msg)


def _parse_tz(args) -> Tuple[tzinfo, str, bool]:
    offset = (getattr(args, "timezone_offset", None) or "").strip()
    zone = (getattr(args, "timezone", None) or "").strip()
    if offset:
        if offset.upper() in ("Z", "UTC", "GMT", "0", "+0", "-0", "+00:00", "-00:00"):
            return timezone.utc, "UTC", True
        m = re.fullmatch(r"(?:UTC|GMT)?\s*([+-])(\d{1,2})(?::?(\d{2}))?", offset, re.I)
        if not m or int(m.group(2)) > 14 or int(m.group(3) or 0) > 59:
            raise ImporterError(f"--timezone-offset {offset!r}: use +HH:MM or -HH:MM, e.g. "
                                "--timezone-offset=-05:00 or UTC+1 (or --timezone America/New_York, "
                                "which follows daylight saving).")
        delta = timedelta(hours=int(m.group(2)), minutes=int(m.group(3) or 0))
        tz = timezone(-delta if m.group(1) == "-" else delta)
        return tz, tz.tzname(None), True
    if zone:
        if zone.upper() in ("UTC", "GMT", "Z"):
            return timezone.utc, "UTC", True
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(zone), zone, True
        except Exception:  # unknown zone, or no time zone database (Windows without tzdata)
            raise ImporterError(f"Unknown time zone {zone!r} for --timezone. Use an IANA name such as "
                                "America/New_York or Europe/London (on Windows, `pip install tzdata` "
                                "first), or a fixed --timezone-offset.") from None
    return timezone.utc, "UTC", False


# ---------------------------------------------------------------- bylines, content

_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "phd", "ph.d", "md", "m.d", "esq"}
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}")
_HTML_HINT = re.compile(
    r"<(?:/?(?:p|br|div|span|a|em|strong|b|i|u|h[1-6]|ul|ol|li|blockquote|figure|figcaption|img|"
    r"table|tr|td|th|iframe|script|style|section|article|hr|sup|sub|pre|code)\b[^<>]*>|!--)"
    r"|&(?:[A-Za-z][A-Za-z0-9]{1,30}|#\d{1,7}|#[xX][0-9A-Fa-f]{1,6});", re.I)


def _blank(value: Optional[str]) -> bool:
    return value is None or not value.strip() or value.strip() in NULL_MARKERS


def _split_authors(cell: str, seps: Sequence[str]) -> List[str]:
    """Names in one byline cell: a JSON list, or text split on the separators. Cleaned, not validated."""
    v = cell.strip()
    if _blank(v):
        return []
    pieces: List[str] = []
    if v.startswith("[") and v.endswith("]"):
        try:
            data = json.loads(v)
        except ValueError:
            data = None
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    item = item.get("name") or item.get("display_name") or item.get("full_name")
                if isinstance(item, str):
                    pieces.append(item)
            v = ""
    if v:
        # Decode entities first: '&eacute;' must not be cut at its ';'. Line breaks inside a cell
        # (Alt+Enter in a spreadsheet) always separate names; joining them would invent a name.
        v = html.unescape(v) if "&" in v else v
        pattern = "|".join([r"[\r\n]+"] + [re.escape(s) for s in seps if s])
        pieces = [re.sub(r"^\s*by[:\s]+(?=\S)", "", p, flags=re.I) for p in re.split(pattern, v)]
        if any(s.strip() == "," for s in seps):
            joined: List[str] = []
            for p in pieces:
                if joined and p.strip().lower().rstrip(".") in _SUFFIXES:
                    joined[-1] = f"{joined[-1].rstrip()}, {p.strip()}"
                else:
                    joined.append(p)
            pieces = joined
    names: List[str] = []
    for p in pieces:
        name = clean_text(p)
        if not _blank(name) and name not in names:
            names.append(name)
    return names


# ---------------------------------------------------------------- options

@dataclass
class _Options:
    path: Path
    encoding: Optional[str]
    encoding_errors: str
    delimiter: Optional[str]
    has_header: bool
    specs: Dict[str, Optional[str]]
    multi_specs: Dict[str, List[str]]
    authors_seps: List[str]
    content_mode: str
    base_url: Optional[str]
    id_prefix: str
    date_formats: List[str]
    tz: tzinfo
    tz_label: str
    tz_given: bool
    include_future: bool
    published_values: frozenset
    public_values: frozenset
    where: List[Tuple[str, str]]


_DELIMITER_NAMES = {"tab": "\t", "\\t": "\t", "comma": ",", "semicolon": ";", "pipe": "|", "space": " "}


def _options(args) -> _Options:
    path = Path(getattr(args, "file"))
    if not path.exists():
        raise ImporterError(f"--file {str(path)!r}: no such file.")
    if not path.is_file():
        raise ImporterError(f"--file {str(path)!r} is a folder; give the exported .csv file itself.")

    encoding = getattr(args, "encoding", None)
    if encoding:
        try:
            codecs.lookup(encoding)
        except LookupError:
            raise ImporterError(f"--encoding {encoding!r} isn't a known text encoding. Try utf-8-sig "
                                "(the default), cp1252 (Excel on Windows) or latin-1.") from None

    delimiter = getattr(args, "delimiter", None)
    if delimiter is not None:
        delimiter = _DELIMITER_NAMES.get(delimiter.lower(), delimiter)
        if len(delimiter) != 1 or delimiter in ('"', "\n", "\r"):
            raise ImporterError(f"--delimiter {getattr(args, 'delimiter')!r}: give one character, such as "
                                "',' ';' or 'tab'.")

    base_url = (getattr(args, "base_url", None) or "").strip() or None
    if base_url and not re.match(r"https?://[^/\s]+", base_url, re.I):
        raise ImporterError(f"--base-url {base_url!r}: give the site's full address, e.g. "
                            "https://www.example.com")

    id_prefix = (getattr(args, "id_prefix", None) or "csv").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", id_prefix):
        raise ImporterError(f"--id-prefix {id_prefix!r}: use letters, digits, '-', '_' or '.', "
                            "e.g. csv-inquirer.")

    tz, tz_label, tz_given = _parse_tz(args)

    def values(raw: Optional[str], default: Sequence[str]) -> frozenset:
        if raw is None:
            return frozenset(default)
        return frozenset(v.strip().casefold() for v in raw.split(",") if v.strip())

    where: List[Tuple[str, str]] = []
    for item in getattr(args, "where", None) or []:
        col, sep, val = item.partition("=")
        if not sep or not col.strip():
            raise ImporterError(f"--where {item!r}: use COLUMN=VALUE, e.g. --where post_type=post")
        where.append((col.strip(), val.strip()))

    content_mode = ("html" if getattr(args, "content_is_html", False)
                    else "text" if getattr(args, "content_is_text", False) else "auto")
    seps = getattr(args, "authors_sep", None) or [";"]
    return _Options(
        path=path, encoding=encoding, encoding_errors=getattr(args, "encoding_errors", None) or "strict",
        delimiter=delimiter,
        has_header=not getattr(args, "no_header", False),
        specs={r.key: getattr(args, f"{r.key}_col", None) for r in _ROLES if r.key not in _MULTI},
        multi_specs={k: list(getattr(args, f"{k}_col", None) or []) for k in _MULTI},
        authors_seps=seps, content_mode=content_mode, base_url=base_url, id_prefix=id_prefix,
        date_formats=list(getattr(args, "date_format", None) or []),
        tz=tz, tz_label=tz_label, tz_given=tz_given,
        include_future=getattr(args, "include_future", False),
        published_values=values(getattr(args, "published_values", None), DEFAULT_PUBLISHED),
        public_values=values(getattr(args, "public_values", None), DEFAULT_PUBLIC),
        where=where)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.formatter_class = argparse.RawDescriptionHelpFormatter
    parser.epilog = EPILOG
    src = parser.add_argument_group("input file")
    src.add_argument("--file", type=Path, required=True, metavar="PATH",
                     help="the exported table (.csv, .tsv, .txt; .gz ok): see 'Where to get the file' below")
    src.add_argument("--encoding", metavar="NAME",
                     help="text encoding (default: utf-8-sig, i.e. UTF-8 with or without Excel's "
                          "byte-order mark); cp1252 for Excel on Windows 'CSV (Comma delimited)' files")
    src.add_argument("--encoding-errors", choices=("strict", "replace"), default="strict",
                     help="what to do with bytes that aren't valid in the encoding: stop with the line "
                          "number (strict, the default) or import the row with the replacement character "
                          "U+FFFD in their place (replace)")
    src.add_argument("--delimiter", metavar="CHAR",
                     help="field separator: ',' ';' 'tab' or another single character (default: "
                          "detected among , ; and tab, else ',')")
    src.add_argument("--no-header", action="store_true",
                     help="the first row is data, not column names: give each column by number (1 = first)")

    cols = parser.add_argument_group(
        "column mapping",
        "Each takes a header name (case, spaces and punctuation ignored) or a column number (1 = first);\n"
        "optional ones also take 'none'. Default: detected from common header names (listed below).")
    cols.add_argument("--headline-col", metavar="COLUMN", help="headline / title (required)")
    cols.add_argument("--content-col", metavar="COLUMN", help="full article text or HTML (required)")
    cols.add_argument("--url-col", metavar="COLUMN", help="public article URL (required)")
    cols.add_argument("--date-col", metavar="COLUMN", help="publish date (required)")
    cols.add_argument("--authors-col", metavar="COLUMN", action="append",
                      help="byline name(s) (optional); repeat for several columns, e.g. author1 author2")
    cols.add_argument("--id-col", metavar="COLUMN",
                      help="the CMS's own article id (optional; without one, ids are a hash of the URL)")
    cols.add_argument("--authors-sep", metavar="SEP", action="append",
                      help="separator between names in one byline cell (default: ';'). Use ',' for "
                           "'A, B' bylines (but it also splits 'Doe, Jane'; suffixes such as ', Jr.' "
                           "stay attached) or ' and ' for 'A and B'; repeat to use several")
    html_mode = cols.add_mutually_exclusive_group()
    html_mode.add_argument("--content-is-html", action="store_true",
                           help="treat content as HTML (default: detected per row from tags and entities)")
    html_mode.add_argument("--content-is-text", action="store_true",
                           help="treat content as plain text even if it contains <tags>")
    cols.add_argument("--base-url", metavar="URL",
                      help="site address for URLs given as paths such as /news/story, e.g. https://www.example.com")
    cols.add_argument("--id-prefix", default="csv", metavar="PREFIX",
                      help="start of every article id (default: csv); give each source its own, "
                           "e.g. csv-inquirer, when importing tables from more than one CMS")

    dates = parser.add_argument_group("dates")
    dates.add_argument("--date-format", action="append", metavar="FORMAT",
                       help="strptime format of the date column, e.g. '%%d/%%m/%%Y %%H:%%M' or "
                            "'%%m/%%d/%%Y'; 'unix' or 'unix-ms' for timestamps; repeat for a column that "
                            "mixes formats. Default: ISO 8601, RFC 822, Unix timestamps, YYYY/MM/DD and "
                            "month-name dates; DD/MM versus MM/DD is never guessed")
    tz = dates.add_mutually_exclusive_group()
    tz.add_argument("--assume-utc", action="store_true",
                    help="dates without a time zone are UTC (the default)")
    tz.add_argument("--timezone-offset", metavar="OFFSET",
                    help="fixed UTC offset for dates without a time zone: --timezone-offset=-05:00, "
                         "+01:00 or UTC-5 (use '=' before a negative offset)")
    tz.add_argument("--timezone", metavar="ZONE",
                    help="IANA time zone for dates without one, daylight-saving aware, e.g. America/New_York")
    dates.add_argument("--include-future", action="store_true",
                       help="keep rows dated more than a day ahead (skipped by default as scheduled)")

    filt = parser.add_argument_group(
        "publish status",
        "Only public, published rows are imported. These columns are detected by name; 'none' ignores one.")
    filt.add_argument("--status-col", metavar="COLUMN",
                      help="publish status column (default: detected: status, post_status, publish_status, ...)")
    filt.add_argument("--published-values", metavar="LIST",
                      help="comma-separated statuses that mean published, replacing the default "
                           f"{','.join(DEFAULT_PUBLISHED)}; e.g. add 'private' only if those may be imported")
    filt.add_argument("--visibility-col", metavar="COLUMN",
                      help="visibility / access column (default: detected: visibility, access, ...)")
    filt.add_argument("--public-values", metavar="LIST",
                      help="comma-separated visibility values that mean public (default: public); "
                           "members-only and paid rows are skipped")
    filt.add_argument("--password-col", metavar="COLUMN",
                      help="rows with a value here are password-protected and skipped (default: "
                           "detected: post_password, password)")
    filt.add_argument("--hidden-col", metavar="COLUMN", action="append",
                      help="a flag or date column marking rows as not public: rows where it holds anything "
                           "but blank, 0, false, f or no are skipped. Default: every column named deleted_at, "
                           "is_deleted, deleted, is_draft, draft, is_private, private, hidden, members_only "
                           "(and similar). Repeat for several; giving any replaces the detected ones; 'none' "
                           "ignores them all")
    filt.add_argument("--where", action="append", metavar="COLUMN=VALUE",
                      help="keep only rows whose COLUMN equals VALUE (ignoring case), e.g. "
                           "--where post_type=post; repeat a column to allow several values")


def default_prefix(args) -> str:
    name = Path(getattr(args, "file", None) or "").name
    while True:
        lower = name.lower()
        for suffix in (".gz", ".csv", ".tsv", ".tab", ".txt"):
            if lower.endswith(suffix):
                name = name[: -len(suffix)]
                break
        else:
            break
    return f"csv-{name}" if name else "csv"


# ---------------------------------------------------------------- reading the file

def _inspect(path: Path) -> Tuple[bool, bytes]:
    """(is gzipped, first bytes of the content). Refuses workbooks, saved web pages and XML with a hint."""
    with open(path, "rb") as f:
        head = f.read(1024)
    is_gz = head[:2] == b"\x1f\x8b"
    if is_gz:
        try:
            with gzip.open(path, "rb") as g:
                head = g.read(1024)
        except (OSError, EOFError) as e:
            raise ImporterError(f"{path.name} looks gzipped but can't be read ({e}).") from None
    if head.startswith(b"PK\x03\x04") or head.startswith(b"\xd0\xcf\x11\xe0"):
        raise ImporterError(f"{path.name} is an Excel workbook (or another zip file), not CSV. In Excel: "
                            "File > Save As > 'CSV UTF-8 (Comma delimited) (*.csv)', then import that file.")
    start = head.lstrip(codecs.BOM_UTF8).lstrip().lower()
    if start.startswith((b"<!doctype html", b"<html")):
        raise ImporterError(f"{path.name} is a web page, not a CSV table: probably a login or error page that "
                            "was saved instead of the export. Download the export again while signed in.")
    if start.startswith(b"<?xml"):
        raise ImporterError(f"{path.name} is an XML file, not a CSV table. For a WordPress export use the "
                            "wordpress-export importer; for an RSS or Atom feed use feed.")
    return is_gz, head


def _open(path: Path, is_gz: bool, encoding: str, errors: str = "strict"):
    if is_gz:
        return gzip.open(path, "rt", encoding=encoding, errors=errors, newline="")
    return open(path, "r", encoding=encoding, errors=errors, newline="")


_UTF8_CODECS = ("utf-8", "utf-8-sig")
# Single-byte encodings in which UTF-8 text decodes without an error, only garbled ('CafÃ©').
_SINGLE_BYTE_CODECS = frozenset({"cp1252", "iso8859-1", "iso8859-15", "cp1250", "iso8859-2", "mac-roman",
                                 "cp850", "cp437"})


def _decode_error(path: Path, is_gz: bool, encoding: str) -> ImporterError:
    """An actionable message for a file that isn't valid `encoding`: the exact line, and whether the file is
    really UTF-8 with a few bad bytes (then cp1252, which accepts any byte, would garble everything else)."""
    codec = codecs.lookup(encoding).name
    line_no, snippet, good, bad = 0, b"", 0, 0
    if not codec.startswith(("utf-16", "utf-32")):  # a line-by-line scan needs an ASCII-compatible encoding
        try:
            with (gzip.open(path, "rb") if is_gz else open(path, "rb")) as f:
                for n, raw in enumerate(f, 1):
                    try:
                        raw.decode(encoding)
                    except UnicodeDecodeError as e:
                        bad += 1
                        if not line_no:
                            line_no, snippet = n, raw[max(0, e.start - 15): e.end + 15]
                    else:
                        good += not (raw[3:] if n == 1 and raw.startswith(codecs.BOM_UTF8) else raw).isascii()
                    if line_no and n >= line_no + 2000:
                        break
        except (OSError, EOFError):
            pass
    where = f"line {line_no}: {snippet!r}" if line_no else "somewhere in the file"
    # Valid multi-byte UTF-8 almost never happens by accident in cp1252 text, so as many good lines as bad
    # ones means a UTF-8 file with stray bytes.
    if codec in _UTF8_CODECS and line_no and good >= max(1, bad):
        return ImporterError(
            f"{path.name} is UTF-8, but line {line_no} has bytes that aren't ({snippet!r}). Don't switch to --encoding cp1252: that "
            "would garble every other accented letter in the file ('Café' would become 'CafÃ©'). Fix that row "
            "in the export, or re-run with --encoding-errors replace to import it with U+FFFD in place of the "
            "bad bytes.")
    alt = "latin-1" if codec == "cp1252" else "cp1252"
    return ImporterError(f"{path.name} isn't valid {encoding} text ({where}). Files saved by Excel on Windows "
                         f"are often cp1252: try --encoding {alt} (or --encoding-errors replace to keep going).")


def _utf8_misread(sample: str, encoding: str) -> Optional[str]:
    """A line of the sample that is really UTF-8 garbled by a single-byte `encoding`, if at least half the
    non-ASCII lines are (valid UTF-8 almost never happens by accident in single-byte text)."""
    if codecs.lookup(encoding).name not in _SINGLE_BYTE_CODECS:
        return None
    lines = sample.splitlines()
    if len(sample) >= SNIFF_CHARS:
        lines = lines[:-1]  # the last line may be cut in the middle of a character
    utf8: List[str] = []
    other = 0
    for line in lines:
        if line.isascii():
            continue
        try:
            line.encode(encoding).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            other += 1
        else:
            utf8.append(line)
    return utf8[0] if len(utf8) >= max(1, other) else None


def _read_sample(path: Path, is_gz: bool, encoding: str, errors: str) -> str:
    try:
        with _open(path, is_gz, encoding, errors) as f:
            sample = f.read(SNIFF_CHARS)
    except UnicodeDecodeError:
        raise _decode_error(path, is_gz, encoding) from None
    except (OSError, EOFError) as e:
        raise ImporterError(f"Couldn't read {path.name}: {e}") from None
    garbled = _utf8_misread(sample, encoding)
    if garbled is not None:
        raise ImporterError(f"{path.name} looks like UTF-8, not {encoding}: read as {encoding}, its accented "
                            f"letters come out garbled, e.g. {_show(garbled, 60)!r}. Leave out --encoding to "
                            "read it as UTF-8 (the default).")
    return sample


def _choose_delimiter(given: Optional[str], sample: str, path: Path) -> Tuple[str, bool, str]:
    """(delimiter, first line is Excel's 'sep=X' hint, how it was chosen)."""
    first_line = sample.lstrip("﻿").split("\n", 1)[0].rstrip("\r")
    sep_hint = re.fullmatch(r'"?sep=(.)"?[,;\t]*', first_line.strip())
    if given:
        return given, bool(sep_hint), "given"
    if sep_hint:
        return sep_hint.group(1), True, "from the file's sep= line"
    if path.name.lower().endswith((".tsv", ".tsv.gz", ".tab", ".tab.gz")):
        return "\t", False, "from the .tsv name"
    # The header row is the best evidence (article HTML is full of ';' from entities such as &nbsp;):
    # when exactly one candidate appears in it, that's the delimiter. Otherwise ask csv.Sniffer.
    counts = {c: first_line.count(c) for c in ",;\t"}
    present = [c for c in counts if counts[c]]
    if len(present) == 1:
        return present[0], False, "detected"
    trimmed = sample[: sample.rfind("\n") + 1] or sample  # whole lines only; a cut record confuses it
    try:
        d = csv.Sniffer().sniff(trimmed, delimiters=",;\t").delimiter
        if d in present:
            return d, False, "detected"
    except csv.Error:
        pass
    if present:
        return max(present, key=lambda c: counts[c]), False, "detected from the first line"
    return ",", False, "default"


def _rows(f, delimiter: str, path: Path, encoding: str, is_gz: bool) -> Iterator[Tuple[int, List[str]]]:
    reader = csv.reader(f, delimiter=delimiter)
    while True:
        try:
            row = next(reader)
        except StopIteration:
            return
        except UnicodeDecodeError:
            raise _decode_error(path, is_gz, encoding) from None
        except csv.Error as e:
            raise ImporterError(f"{path.name}, line {reader.line_num}: {e}. Check --delimiter and that the "
                                "file uses standard CSV quoting.") from None
        except (OSError, EOFError) as e:
            raise ImporterError(f"Couldn't read {path.name} (line {reader.line_num}): {e}") from None
        if row and any(c.strip() for c in row):
            yield reader.line_num, row


def _cell(row: List[str], idx: Optional[int]) -> str:
    if idx is None or idx >= len(row):
        return ""
    v = row[idx]
    return "" if v.strip() in NULL_MARKERS else v


def _show(value: str, n: int = 40) -> str:
    v = re.sub(r"\s+", " ", value.strip())
    return v[:n] + ("..." if len(v) > n else "")


# ---------------------------------------------------------------- the import

@dataclass
class _Stats:
    rows: int = 0
    ragged: int = 0
    first_ragged: Optional[int] = None
    html: int = 0
    text: int = 0
    excel_cut: int = 0
    relative_urls: int = 0
    replaced: int = 0
    encoding: str = DEFAULT_ENCODING
    bad_dates: List[str] = field(default_factory=list)
    authors_dropped: Counter = field(default_factory=Counter)
    dropped_examples: Dict[str, str] = field(default_factory=dict)


def iter_articles(args) -> Iterator[Dict[str, Any]]:
    opts = _options(args)
    old_limit = csv.field_size_limit()
    csv.field_size_limit(FIELD_SIZE_LIMIT)  # article bodies easily pass the 128 KB default
    try:
        yield from _import(opts)
    finally:
        csv.field_size_limit(old_limit)


def _import(opts: _Options) -> Iterator[Dict[str, Any]]:
    path = opts.path
    is_gz, head = _inspect(path)
    encoding, enc_note = opts.encoding or DEFAULT_ENCODING, ""
    if not opts.encoding and head[:2] in (b"\xff\xfe", b"\xfe\xff"):
        encoding, enc_note = "utf-16", " (UTF-16 byte-order mark)"
    sample = _read_sample(path, is_gz, encoding, opts.encoding_errors)
    delimiter, skip_sep_line, how = _choose_delimiter(opts.delimiter, sample, path)

    size = path.stat().st_size
    print(f"Reading {path.name} ({size / 1e6:.1f} MB{', gzipped' if is_gz else ''}): delimiter "
          f"{'tab' if delimiter == chr(9) else repr(delimiter)} ({how}), encoding {encoding}{enc_note}",
          file=sys.stderr)

    with _open(path, is_gz, encoding, opts.encoding_errors) as f:
        rows = _rows(f, delimiter, path, encoding, is_gz)
        if skip_sep_line:
            next(rows, None)
        first = next(rows, None)
        if first is None:
            raise ImporterError(f"{path.name} has no rows.")
        if opts.has_header:
            headers = [h.strip() for h in first[1]]
            headers[0] = headers[0].lstrip("﻿").strip()
            pending = []
        else:
            headers = [f"column {i}" for i in range(1, len(first[1]) + 1)]
            pending = [first]
        mapping = _resolve_columns(headers, opts)

        sample_rows = list(pending)
        chars = 0
        for item in rows:
            sample_rows.append(item)
            chars += sum(len(c) for c in item[1])
            if len(sample_rows) >= SAMPLE_ROWS or chars >= SAMPLE_CHARS:
                break

        column_is_utc = False
        date_idx = mapping.cols["date"]
        if opts.has_header and re.search(r"(gmt|utc)$", _norm(headers[date_idx])):
            column_is_utc = True
        dates = _DateParser(opts.date_formats, opts.tz, column_is_utc)
        where = [(_lookup(col, headers, "--where", opts.has_header, mapping.warnings), val.casefold())
                 for col, val in opts.where]

        _check_sample_columns(sample_rows, mapping, opts)
        _print_mapping(mapping, opts, column_is_utc)
        _precheck(sample_rows, mapping, opts, dates)

        stats = _Stats(encoding=encoding)
        convert = _Converter(opts, mapping, dates, where, stats, len(headers))
        finished = False
        try:
            for line, row in itertools.chain(sample_rows, rows):
                stats.rows += 1
                if stats.rows % PROGRESS_EVERY == 0:
                    print(f"  {stats.rows:,} rows read (line {line:,})", file=sys.stderr)
                yield convert(line, row)
            finished = True
        except GeneratorExit:  # the writer reached --limit
            finished = True
            raise
        finally:
            if finished:
                _print_summary(stats, mapping, opts)


def _check_sample_columns(sample_rows: List[Tuple[int, List[str]]], mapping: _Mapping, opts: _Options) -> None:
    """Second look at auto-detected columns using the first rows' values."""
    idx = mapping.cols.get("status")
    if idx is not None and "status" in mapping.auto and _norm(mapping.headers[idx]) in ("published", "ispublished",
                                                                                         "publishedflag"):
        values = [_cell(row, idx).strip() for _, row in sample_rows]
        if any(re.search(r"\d{4}|\d{1,2}[/.:]\d{1,2}", v) for v in values):
            mapping.cols["status"] = None
            mapping.warnings.append(f"column {mapping.headers[idx]!r} holds dates, not a published yes/no flag, so "
                                    "it isn't used as the publish status (name one with --status-col)")
    if mapping.authors:
        names = [n for _, row in sample_rows for i in mapping.authors
                 for n in _split_authors(_cell(row, i), opts.authors_seps)
                 if not re.fullmatch(r"\d+", n) and not _EMAIL.fullmatch(n)]
        if names and all(re.fullmatch(r"[a-z0-9][a-z0-9._-]*", n) for n in names):
            columns = ", ".join(repr(mapping.headers[i]) for i in mapping.authors)
            mapping.warnings.append(f"the byline values in {columns} look like user names or logins (e.g. "
                                    f"{names[0]!r}), not the names readers see; if another column has the "
                                    "display names, choose it with --authors-col")


def _print_mapping(mapping: _Mapping, opts: _Options, column_is_utc: bool) -> None:
    cols = mapping.cols
    notes = {
        "content": {"auto": "HTML or plain text, detected per row", "html": "HTML",
                    "text": "plain text"}[opts.content_mode],
        "date": ("naive times read as UTC (column name says GMT/UTC)" if column_is_utc
                 else f"times without a zone read as {opts.tz_label}")
                + (f"; formats {', '.join(opts.date_formats)}" if opts.date_formats else ""),
        "authors": "split on " + " or ".join(repr(s) for s in opts.authors_seps),
        "id": f"ids '{opts.id_prefix}-<value>'",
        "status": "kept: " + ", ".join(sorted(opts.published_values)),
        "visibility": "kept: " + ", ".join(sorted(opts.public_values)),
        "password": "rows with a password are skipped",
        "hidden": "rows where one is set (not blank, 0, false, f or no) are skipped",
    }
    out = ["Column mapping (check it; change it with --headline-col, --date-col, etc.):"]
    for role in (_ROLE[k] for k in _DISPLAY_ORDER):
        if role.key in _MULTI:
            chosen = mapping.authors if role.key == "authors" else mapping.hidden
            idx_text = ", ".join(mapping.name(i) for i in chosen) or "(none)"
            found = bool(chosen)
        else:
            idx_text, found = mapping.name(cols.get(role.key)), cols.get(role.key) is not None
        note = notes.get(role.key, "") if found else {
            "authors": "no byline column: articles will have no byline",
            "id": f"ids '{opts.id_prefix}-' + a hash of the URL",
        }.get(role.key, "")
        out.append(f"  {role.key:<10} <- {idx_text}" + (f"  [{note}]" if note else ""))
    used = {i for i in cols.values() if i is not None} | set(mapping.authors) | set(mapping.hidden)
    unused = [h for i, h in enumerate(mapping.headers) if i not in used]
    if unused and opts.has_header:
        shown = ", ".join(repr(h[:30]) for h in unused[:15])
        out.append(f"  not used: {shown}{', ...' if len(unused) > 15 else ''}")
    if opts.has_header and cols.get("status") is None and cols.get("visibility") is None and not mapping.hidden:
        out.append("  No status, visibility or not-public flag column: every row is taken to be published and "
                   "public.")
    for w in mapping.warnings:
        out.append(f"  Note: {w}")
    print("\n".join(out), file=sys.stderr)


def _precheck(sample_rows: List[Tuple[int, List[str]]], mapping: _Mapping, opts: _Options,
              dates: _DateParser) -> None:
    """Fail before importing when the first rows show the mapping or date format can't work."""
    if not sample_rows:
        return
    cols = mapping.cols
    date_col = mapping.headers[cols["date"]]
    values = [(line, _cell(row, cols["date"]).strip()) for line, row in sample_rows]
    values = [(line, v) for line, v in values if v and not v.startswith("0000-00-00")]
    numeric_dm = [v for _, v in values if _numeric_dm(v)]
    parsed = 0
    zones: List[str] = []
    for line, v in values:
        try:
            if dates.parse(v) is not None:
                parsed += 1
        except _AmbiguousDate as e:
            raise _ambiguous_error(date_col, e, f"line {line}", numeric_dm) from None
        except _UnknownZone as e:
            zones.append(e.zone)
    if values and not parsed:
        examples = ", ".join(repr(_show(v, 30)) for _, v in values[:3])
        hint = ""
        if zones:
            hint = (f" They end in a time zone the importer doesn't know ({zones[0]!r}): write it into "
                    f"--date-format literally and give its offset with --timezone-offset.")
        elif all(re.fullmatch(r"\d{5}(?:\.\d+)?", v) for _, v in values[:20]):
            hint = (" They look like Excel serial day numbers: format the column as a date in Excel "
                    "(Format Cells > Date) before saving as CSV.")
        elif all(v.casefold() in ("true", "false", "yes", "no", "0", "1", "y", "n") for _, v in values[:20]):
            hint = (f" Column {date_col!r} looks like a published yes/no flag, not a date: use "
                    f"--status-col {date_col!r} for it, and name the date column with --date-col.")
        tried = (f"--date-format {' / '.join(opts.date_formats)} didn't match them. " if opts.date_formats
                 else "")
        raise ImporterError(f"None of the first {len(values)} dates in column {date_col!r} could be read "
                            f"(e.g. {examples}).{hint} {tried}If this isn't the publish date, choose the right "
                            "column with --date-col; otherwise give its format with --date-format "
                            "(strptime codes, e.g. '%d %B %Y %H:%M').")

    id_idx = cols.get("id")
    if id_idx is not None:
        for line, row in sample_rows:
            v = _cell(row, id_idx).strip()
            if re.fullmatch(r"-?\d(?:[.,]\d+)?[eE]\+?\d+", v):
                raise ImporterError(
                    f"Ids in column {mapping.headers[id_idx]!r} look like {v!r} (line {line}): Excel turned "
                    "long numbers into scientific notation and lost digits, so they can't identify articles. "
                    "Re-export without opening the file in Excel, or use --id-col none for URL-based ids.")

    urls = [_cell(row, cols["url"]).strip() for _, row in sample_rows]
    urls = [u for u in urls if u]
    if urls and not any(u.lower().startswith(("http://", "https://")) for u in urls):
        url_col = mapping.headers[cols["url"]]
        if any(u.startswith("/") for u in urls) and not opts.base_url:
            raise ImporterError(f"URLs in column {url_col!r} are paths such as {_show(urls[0])!r}. Add the "
                                "site address with --base-url https://www.example.com to make them full URLs.")
        if not any(u.startswith("/") for u in urls):
            raise ImporterError(f"Column {url_col!r} doesn't hold web addresses (e.g. {_show(urls[0])!r}). "
                                "Choose the column with full article URLs with --url-col.")


class _Converter:
    def __init__(self, opts: _Options, mapping: _Mapping, dates: _DateParser,
                 where: List[Tuple[int, str]], stats: _Stats, ncols: int):
        self.opts = opts
        self.cols = mapping.cols
        self.mapping = mapping
        self.dates = dates
        self.where = where
        self.stats = stats
        self.ncols = ncols
        self.future_cutoff = datetime.now(timezone.utc) + timedelta(days=1)

    def header(self, key: str) -> str:
        return self.mapping.headers[self.cols[key]]

    def __call__(self, line: int, row: List[str]) -> Dict[str, Any]:
        if len(row) != self.ncols:
            self.stats.ragged += 1
            self.stats.first_ragged = self.stats.first_ragged or line
            if len(row) > self.ncols and any(c.strip() for c in row[self.ncols:]):
                # Extra cells with text: a delimiter inside an unquoted field shifted every column after it,
                # so the byline, text or URL would come from the wrong place.
                return {"_skip": "row has more cells than the header row (an unquoted delimiter inside a "
                                 "field?), so its columns can't be trusted"}
        if self.opts.encoding_errors == "replace" and any("\ufffd" in c for c in row):
            self.stats.replaced += 1
        reason = self._filter(row)
        if reason:
            return {"_skip": reason}
        cols, opts = self.cols, self.opts

        date_text = _cell(row, cols["date"]).strip()
        published = None
        if date_text and not date_text.startswith("0000-00-00"):
            try:
                published = self.dates.parse(date_text)
            except _AmbiguousDate as e:
                raise _ambiguous_error(self.header("date"), e, f"line {line}", [e.value]) from None
            except _UnknownZone as e:
                if len(self.stats.bad_dates) < 3:
                    self.stats.bad_dates.append(f"line {line}: {_show(date_text)!r}")
                return {"_skip": f"time zone {_show(e.zone, 20)!r} in the date isn't one the importer knows "
                                 "(match it in --date-format and set --timezone-offset)"}
            if published is None:
                if len(self.stats.bad_dates) < 3:
                    self.stats.bad_dates.append(f"line {line}: {_show(date_text)!r}")
                return {"_skip": "date doesn't match --date-format (or any standard format)"
                        if opts.date_formats else "unrecognized date format (set --date-format)"}
            if published > self.future_cutoff and not opts.include_future:
                return {"_skip": "dated in the future, probably scheduled (use --include-future to import)"}

        url = _cell(row, cols["url"]).strip()
        if url.startswith("/"):  # a path (or //host/path): resolve it against the site
            if opts.base_url:
                url = urllib.parse.urljoin(opts.base_url, url)
            else:
                self.stats.relative_urls += 1

        id_idx = cols.get("id")
        if id_idx is not None:
            value = _cell(row, id_idx).strip()
            if not value:
                return {"_skip": f"blank id in column {self.header('id')!r} (--id-col none uses URL-based ids)"}
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", value):
                article_id = f"{opts.id_prefix}-{value}"
            else:
                article_id = f"{opts.id_prefix}-{hashlib.sha1(value.encode('utf-8')).hexdigest()[:16]}"
        else:
            article_id = f"{opts.id_prefix}-{hashlib.sha1(url.encode('utf-8')).hexdigest()[:16]}" if url else ""

        authors: List[str] = []
        for idx in self.mapping.authors:
            for name in _split_authors(_cell(row, idx), opts.authors_seps):
                kind = "number" if re.fullmatch(r"\d+", name) else "email" if _EMAIL.fullmatch(name) else ""
                if kind:  # a user id or an e-mail address is not a byline
                    self.stats.authors_dropped[kind] += 1
                    self.stats.dropped_examples.setdefault(kind, name)
                elif name not in authors:
                    authors.append(name)

        raw: Dict[str, Any] = {"id": article_id, "headline": _cell(row, cols["headline"]), "url": url,
                               "authors": authors, "publish_date": published}
        content = _cell(row, cols["content"])
        if len(content) == EXCEL_CELL_LIMIT:
            self.stats.excel_cut += 1
        is_html = opts.content_mode == "html" or (opts.content_mode == "auto" and bool(_HTML_HINT.search(content)))
        if is_html:
            self.stats.html += 1
            raw["content_html"] = content
        else:
            self.stats.text += 1
            raw["content"] = content.replace("\r\n", "\n").replace("\r", "\n")
        return raw

    def _filter(self, row: List[str]) -> Optional[str]:
        """Why this row is left out (not public / not published / not matching --where), or None."""
        if self.where:
            by_col: Dict[int, List[str]] = {}
            for idx, wanted in self.where:
                by_col.setdefault(idx, []).append(wanted)
            for idx, allowed in by_col.items():
                v = _cell(row, idx).strip()
                if v.casefold() not in allowed:
                    name = self.mapping.headers[idx]
                    return f"{name} is {_show(v, 30)!r}, not {' or '.join(repr(a) for a in allowed)} (--where)"
        idx = self.cols.get("status")
        if idx is not None:
            v = _cell(row, idx).strip()
            if not v:
                return (f"blank status in column {self.header('status')!r}, so it can't be confirmed as "
                        "published (--status-col none ignores the column)")
            if v.casefold() not in self.opts.published_values:
                return (f"status {_show(v, 30).lower()!r} is not published (list it in --published-values "
                        "to import it)")
        idx = self.cols.get("visibility")
        if idx is not None:
            v = _cell(row, idx).strip()
            if not v:
                return (f"blank visibility in column {self.header('visibility')!r}, so it can't be confirmed "
                        "as public (--visibility-col none ignores the column)")
            if v.casefold() not in self.opts.public_values:
                return (f"visibility {_show(v, 30).lower()!r}: members-only or not public (list it in "
                        "--public-values to import it)")
        idx = self.cols.get("password")
        if idx is not None:
            v = _cell(row, idx).strip()
            if v and v.casefold() not in ("0", "false", "no"):
                return "password-protected (its text isn't public)"
        for idx in self.mapping.hidden:
            v = _cell(row, idx).strip()
            if v and v.casefold() not in _UNSET_FLAGS and not v.startswith("0000-00-00"):
                return (f"flagged in column {self.mapping.headers[idx]!r} (deleted, draft, private or "
                        "members-only; see --hidden-col)")
        return None


def _print_summary(stats: _Stats, mapping: _Mapping, opts: _Options) -> None:
    lines = [f"Read {stats.rows:,} row(s) from {opts.path.name}"
             + (f" (content: {stats.html:,} HTML, {stats.text:,} plain text)" if stats.html or stats.text else "")
             + "."]
    if stats.ragged:
        lines.append(f"  Warning: {stats.ragged:,} row(s) have a different number of columns than the header "
                     f"(first at line {stats.first_ragged:,}); check --delimiter and the file's quoting.")
    if stats.bad_dates:
        lines.append("  Unreadable dates, e.g. " + "; ".join(stats.bad_dates))
    if stats.replaced:
        lines.append(f"  Warning: {stats.replaced:,} row(s) had bytes that aren't valid {stats.encoding}; "
                     "they were imported with U+FFFD in their place (--encoding-errors replace).")
    if stats.relative_urls:
        lines.append(f"  {stats.relative_urls:,} row(s) have a path instead of a full URL and can't be imported; "
                     "add the site address with --base-url https://www.example.com")
    if stats.excel_cut:
        lines.append(f"  Warning: {stats.excel_cut:,} article(s) are exactly {EXCEL_CELL_LIMIT:,} characters, "
                     "Excel's cell limit, so they were probably cut off. Export straight from the CMS if you can.")
    dropped = stats.authors_dropped
    if dropped.get("number"):
        lines.append(f"  Ignored {dropped['number']:,} byline value(s) that are numbers (user ids, not names), "
                     f"e.g. {stats.dropped_examples.get('number')!r}. Map a column of names with --authors-col.")
    if dropped.get("email"):
        lines.append(f"  Ignored {dropped['email']:,} byline value(s) that are e-mail addresses, not names.")
    print("\n".join(lines), file=sys.stderr)

