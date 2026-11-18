"""
Ghost Content API importer: published posts from a Ghost site.

    export GHOST_CONTENT_API_KEY=...      # Ghost Admin > Integrations > custom integration
    python app/import_archive.py ghost --url https://www.example.com --limit 20

Reads <url>/ghost/api/content/posts/ page by page, oldest post first, with
`include=authors`. The Content API key is read-only and only ever sees what the
site already shows an anonymous visitor. Run
`python app/import_archive.py ghost --help` for where to find each value in
Ghost Admin.

Members-only, paid and tier-restricted posts come back from the Content API cut
at the paywall (or empty), so they are skipped and counted by default;
--include-member-previews imports the public preview text. Bylines come from
the post's Ghost staff authors. A post with no author gets `authors: []`; no
other name is substituted.

Behaviour checked against Ghost's own source and e2e snapshots
(TryGhost/Ghost, ghost/core/server and ghost/test/e2e-api/content):
- the public context enforces `status:published`, so drafts, scheduled and
  email-only posts are never returned (models/post.js enforcedFilters);
- a post whose `visibility` isn't "public" is gated for a request without a
  member: html is cut at `<!--members-only-->` or emptied and `access` is set
  to false (post-gating.js, members/content-gating.js);
- `limit` is capped at 100 from Ghost 6 ('all' included) (shared/max-limit-cap.ts);
  without `limit` the page size is 15;
- list responses carry meta.pagination {page, limit, pages, total, next, prev};
- `order` takes comma-separated "field dir" pairs and ignores fields it can't
  sort by; `filter` takes NQL such as published_at:<'2024-01-01 00:00:00';
- /ghost/api/v2|v3|v4|canary/content/ paths are still accepted and rewritten,
  and Accept-Version is only echoed (Content-Version), not required;
- a post that no route serves gets the URL <site>/404/ (url service);
- settings/ returns {"settings": {title, url, timezone, version, ...}}.

Standard library only.
"""
import argparse
import hashlib
import html
import http.client
import json
import os
import re
import sys
import urllib.parse
from datetime import date, datetime, time, timezone, tzinfo
from html.parser import HTMLParser
from typing import Any, Dict, Iterator, List, Optional, Tuple

from importers.common import ImporterError, clean_text, http_get_json, parse_date

NAME = "ghost"
HELP = "Published posts from a Ghost site, read through its Content API (needs a Content API key)."

KEY_ENV = "GHOST_CONTENT_API_KEY"
MAX_PER_PAGE = 100  # Ghost 6 caps limit at 100 (and turns limit=all into 100)
# Unversioned (Ghost 5+) first; older Ghost only serves the versioned paths.
_API_PATHS = ("ghost/api/content/", "ghost/api/v4/content/", "ghost/api/v3/content/", "ghost/api/v2/content/")
_ORDER = "published_at asc,id asc"  # oldest first, so posts published mid-import land on later pages
_KEY_RE = re.compile(r"[0-9a-fA-F]{26}")
_ADMIN_KEY_RE = re.compile(r"[0-9a-fA-F]{24}:[0-9a-fA-F]{64}")
_GATED = "members-only"

_EPILOG = f"""\
where to find each value:
  --url       In Ghost Admin (your site address followed by /ghost), open
              Settings and find Integrations (under "Advanced" in recent versions).
              Click "Add custom integration", name it Dewey and create it. Copy
              the "API URL" shown there, e.g. https://example.ghost.io. Your
              public address (https://www.example.com) works too; include a
              subfolder if Ghost lives in one (https://example.com/blog).
  --key       On the same integration page, copy the "Content API key" (26
              letters and digits). It is read-only and sees only what your site
              shows the public. Don't use the "Admin API key" (the long one with
              a colon): Dewey doesn't need it.
              Prefer the environment variable {KEY_ENV} to the flag:
              flags are saved in your shell history.
  --filter    Optional Ghost filter (NQL), e.g. "tag:news" or "author:jane-doe".
              A tag's or staff member's slug is the last part of its page
              address (example.com/tag/news/ -> news).

what is imported:
  Published posts that anyone can read. Drafts, scheduled posts and email-only
  newsletters aren't available through the Content API and are never imported.
  Members-only, paid and tier-only posts: the Content API returns only their
  public preview (the part above the paywall divider), or nothing, so they are
  skipped and counted in the report. --include-member-previews imports the
  preview text where there is one; the rest of those articles stays out of
  Dewey. Pages (About, Contact, ...) are left out unless --include-pages.
  Bylines are the post's Ghost staff authors, in order. A post without one gets
  no author rather than a guessed one.
  Signup forms, buttons, audio/video player controls, file sizes and link
  preview (bookmark) cards are left out of the text; captions are kept.

large archives:
  Try --limit 20 first. For a very large site, import a year at a time with
  --after and --before (dates are in the site's time zone, from Ghost's
  settings); each date window gets its own output files.

examples:
  read -rs {KEY_ENV} && export {KEY_ENV}   # paste it; not shown or saved
  python app/import_archive.py ghost --url https://www.example.com --limit 20
  python app/import_archive.py ghost --url https://example.ghost.io \\
      --after 2023-01-01 --before 2024-01-01
"""


# ---------------------------------------------------------------- arguments

def _date_arg(value: str) -> date:
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a date like 2024-01-31, got {value!r}") from None


def _per_page_arg(value: str) -> int:
    try:
        n = int(value)
    except ValueError:
        n = 0
    if not 1 <= n <= MAX_PER_PAGE:
        raise argparse.ArgumentTypeError(f"must be a whole number from 1 to {MAX_PER_PAGE}, got {value!r}")
    return n


def _filter_arg(value: str) -> str:
    value = value.strip()
    if not value:
        raise argparse.ArgumentTypeError("the filter is empty")
    return value


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.formatter_class = argparse.RawDescriptionHelpFormatter
    parser.epilog = _EPILOG
    parser.add_argument("--url", required=True, metavar="URL",
                        help="the integration's API URL or your site's address, e.g. https://www.example.com "
                             "(see below)")
    parser.add_argument("--key", "--content-api-key", dest="key", metavar="KEY",
                        help=f"Content API key (better: set {KEY_ENV}, since flags are saved in shell history)")
    parser.add_argument("--after", type=_date_arg, metavar="YYYY-MM-DD",
                        help="only posts published on or after this date (the site's time zone)")
    parser.add_argument("--before", type=_date_arg, metavar="YYYY-MM-DD",
                        help="only posts published before this date (the site's time zone)")
    parser.add_argument("--filter", type=_filter_arg, metavar="NQL",
                        help='extra Ghost filter, e.g. "tag:news" (advanced; gets its own output files)')
    parser.add_argument("--include-member-previews", action="store_true",
                        help="import the public preview of members-only and paid posts (only the text above "
                             "the paywall; the rest isn't available to the Content API)")
    parser.add_argument("--include-pages", action="store_true",
                        help="also import Ghost pages (About, Contact, ...), not just posts")
    parser.add_argument("--per-page", type=_per_page_arg, default=MAX_PER_PAGE, metavar="N",
                        help=f"posts per API request, 1-{MAX_PER_PAGE} (default: {MAX_PER_PAGE}; "
                             "lower it if the site times out)")


def default_prefix(args) -> str:
    """ghost-<site>[-after-YYYYMMDD][-before-YYYYMMDD][-filter-<hash>]; stable for the same settings."""
    try:
        base, _ = _parse_url(getattr(args, "url", "") or "")
        key = _site_key(base)
    except ImporterError:
        key = ""
    parts = ["ghost", key or "site"]
    after, before = getattr(args, "after", None), getattr(args, "before", None)
    if after:
        parts.append(f"after-{after:%Y%m%d}")
    if before:
        parts.append(f"before-{before:%Y%m%d}")
    nql = getattr(args, "filter", None)
    if nql:
        parts.append("filter-" + hashlib.sha1(nql.encode("utf-8")).hexdigest()[:8])
    return "-".join(parts)


# ---------------------------------------------------------------- URL + key

def _parse_url(raw: str) -> Tuple[str, Optional[str]]:
    """
    Validate --url. Returns (site base without trailing slash, key found in the query string).
    Accepts the site address, the integration's API URL, the admin URL (.../ghost/#/...) or a
    full Content API URL (.../ghost/api/content/posts/?key=...).
    """
    raw = (raw or "").strip()
    if not raw:
        raise ImporterError("--url is empty. Use the API URL from Ghost Admin > Integrations, "
                            "e.g. https://www.example.com")
    if re.search(r"[\s\x00-\x1f\x7f]", raw):
        # http.client refuses such a URL with an error that quotes the request, ?key= included.
        raise ImporterError("--url contains a space or an invisible character. Copy the address again, "
                            "e.g. https://www.example.com (the API URL from Ghost Admin > Integrations).")
    if not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", raw):
        raw = "https://" + raw
    try:
        parts = urllib.parse.urlsplit(raw)
        host = parts.hostname
        port = parts.port
    except ValueError:
        host, port, parts = None, None, None
    if parts is None or parts.scheme.lower() not in ("http", "https") or not host:
        raise ImporterError("--url should be a web address like https://www.example.com "
                            "(the API URL from Ghost Admin > Integrations).")
    key = None
    for name, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True):
        if name.lower() == "key" and value.strip():
            key = value.strip()
    # Drop the admin / API part of the path: /blog/ghost/api/content/posts/ -> /blog
    path = re.split(r"/ghost(?:/|$)", parts.path, maxsplit=1)[0].rstrip("/")
    path = urllib.parse.quote(path, safe="/%:@!$&'()*+,;=~")  # /blög -> /bl%C3%B6g; existing %xx kept
    netloc = f"[{host}]" if ":" in host else host  # credentials in the URL are dropped, never sent
    if port:
        netloc += f":{port}"
    return f"{parts.scheme.lower()}://{netloc.lower()}{path}", key


def _site_key(base: str) -> str:
    """'https://www.example.com/blog' -> 'example-com-blog'. Used in output file names."""
    parts = urllib.parse.urlsplit(base)
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if parts.port and parts.port not in (80, 443):
        host += f"-{parts.port}"
    return re.sub(r"[^a-z0-9]+", "-", (host + parts.path).lower()).strip("-")


def _resolve_key(args, key_from_url: Optional[str]) -> str:
    key = (getattr(args, "key", None) or os.environ.get(KEY_ENV) or "").strip()
    if not key and key_from_url:
        print("  Using the key= from --url. Next time, set the environment variable "
              f"{KEY_ENV} instead (and leave it out of the URL).", file=sys.stderr)
        key = key_from_url
    if not key:
        raise ImporterError(
            "No Content API key. In Ghost Admin open Settings > Integrations (under Advanced in recent "
            "versions), click 'Add custom integration', and copy its Content API key. Then set it with "
            f"`read -rs {KEY_ENV} && export {KEY_ENV}` (or pass --key).")
    if _ADMIN_KEY_RE.fullmatch(key) or ":" in key:
        raise ImporterError(
            "That looks like an Admin API key (it contains a colon). Dewey needs the read-only "
            "Content API key, shown just above it on the same integration page in Ghost Admin.")
    if not _KEY_RE.fullmatch(key):
        print("  Note: that key doesn't look like a Ghost Content API key (26 letters and digits); "
              "trying it anyway.", file=sys.stderr)
    return key


# ---------------------------------------------------------------- HTTP

def _status(err: ImporterError) -> Optional[int]:
    """HTTP status of an ImporterError raised by common.http_get, if it was an HTTP error."""
    status = getattr(err, "status", None)
    if isinstance(status, int):
        return status
    match = re.match(r"HTTP (\d{3}) ", str(err))
    return int(match.group(1)) if match else None


def _server_message(err: ImporterError) -> str:
    """Ghost's own error message ({"errors": [{"message": ...}]}) from an ImporterError, if present."""
    match = re.search(r'"message"\s*:\s*"((?:[^"\\]|\\.)*)"', str(err))
    if not match:
        return ""
    try:
        return clean_text(json.loads(f'"{match.group(1)}"'))[:160]
    except ValueError:
        return clean_text(match.group(1))[:160]


def _without_key(text: str, key: Optional[str]) -> str:
    if key:
        for form in {key, urllib.parse.quote(key, safe=""), urllib.parse.quote_plus(key)}:
            text = text.replace(form, "***")
    return text


def _get(url: str, params: Dict[str, Any]) -> Any:
    """
    GET JSON. Any failure is an ImporterError whose message never contains the key: common.http_get
    redacts the URL it quotes, but not a server body that echoes the request, and low-level errors
    from http.client (which quote the whole request line) aren't ImporterErrors at all.
    """
    key = params.get("key")
    try:
        data, _ = http_get_json(url, params)
    except ImporterError as e:
        message = _without_key(str(e), key)
        if message == str(e):
            raise
        clean = ImporterError(message)
        for name, value in vars(e).items():  # e.g. a future `status` attribute
            setattr(clean, name, _without_key(value, key) if isinstance(value, str) else value)
        raise clean from None
    except (ValueError, http.client.HTTPException) as e:  # InvalidURL, UnicodeError, IncompleteRead, ...
        raise ImporterError(f"Couldn't fetch {_without_key(url, key)}: "
                            f"{_without_key(f'{type(e).__name__}: {e}', key)}") from None
    return data


def _connect(base: str, key: str) -> Tuple[str, Dict[str, Any]]:
    """Find the Content API root under `base` and check the key. Returns (api root, settings)."""
    tried: List[str] = []
    for path in _API_PATHS:
        root = f"{base}/{path}"
        tried.append(root)
        try:
            data = _get(root + "settings/", {"key": key})
        except ImporterError as e:
            status = _status(e)
            if status in (401, 403):
                said = _server_message(e)
                raise ImporterError(
                    f"Ghost refused the Content API key (HTTP {status}{': ' + said if said else ''}). "
                    "Copy the Content API key again from Ghost Admin > Integrations > your custom "
                    "integration, and check --url is the same site."
                    + ("" if said else " If the key is right, a firewall or CDN in front of the site may "
                       "be blocking API requests; try the integration's API URL as --url.")) from None
            if status in (404, 405, 410) or (status is None and "did not return JSON" in str(e)):
                continue  # not a Content API at this path; try the next (older) one
            raise
        if isinstance(data, dict) and isinstance(data.get("settings"), dict):
            return root, data["settings"]
        if isinstance(data, dict) and "settings" in data:
            return root, {}
    raise ImporterError(
        f"No Ghost Content API found at {tried[0]} (also tried the /v4/, /v3/ and /v2/ paths). "
        "Check --url: use the API URL shown on your custom integration in Ghost Admin > Integrations, "
        "or your site's public address. Ghost versions before 2.x have no Content API.")


# ---------------------------------------------------------------- dates

def _site_zone(settings: Dict[str, Any]) -> Tuple[tzinfo, str]:
    name = str(settings.get("timezone") or "").strip()
    if not name or name.upper() in ("UTC", "ETC/UTC", "GMT", "ETC/GMT"):
        return timezone.utc, "UTC"
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name), name
    except Exception:  # unknown zone, or no time zone database on this computer (Windows without tzdata)
        print(f"  Note: couldn't load the site's time zone ({name[:60]}); reading --after/--before as UTC.",
              file=sys.stderr)
        return timezone.utc, "UTC"


def _window(args, zone: tzinfo) -> Tuple[Optional[datetime], Optional[datetime]]:
    def bound(day: Optional[date]) -> Optional[datetime]:
        if not day:
            return None
        return datetime.combine(day, time(0), tzinfo=zone).astimezone(timezone.utc)
    return bound(getattr(args, "after", None)), bound(getattr(args, "before", None))


def _nql_time(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _date_filter(after: Optional[datetime], before: Optional[datetime]) -> List[str]:
    parts = []
    if after:
        parts.append(f"published_at:>='{_nql_time(after)}'")
    if before:
        parts.append(f"published_at:<'{_nql_time(before)}'")
    return parts


# ---------------------------------------------------------------- HTML clean-up

# Never article text: form controls and media elements (whose only text is
# "your browser doesn't support..." fallback).
_CHROME_TAGS = {"button", "form", "select", "textarea", "audio", "video"}
# Koenig card parts that are interface, not writing. Class names checked against
# Ghost's rendered cards, except kg-signup-card and kg-button-card (assumed).
_CHROME_CLASSES = {
    "kg-video-overlay", "kg-video-player-container",  # play button, 0:00 / 2:00, 1x
    "kg-audio-player", "kg-audio-thumbnail",          # same for audio; the title is kept
    "kg-file-card-metadata",                          # file name and "2 MB"; title/caption kept
    "kg-bookmark-container",                          # another page's title/description; caption kept
    "kg-signup-card", "kg-button-card",               # "Subscribe" boxes and call-to-action buttons
}
_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param",
              "source", "track", "wbr"}


class _ChromeStripper(HTMLParser):
    """Re-emits HTML without the subtrees listed above (comments dropped)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: List[str] = []
        self.drop_tag: Optional[str] = None
        self.drop_depth = 0

    @staticmethod
    def _is_chrome(tag: str, attrs) -> bool:
        if tag in _CHROME_TAGS:
            return True
        for name, value in attrs:
            if name == "class" and value and _CHROME_CLASSES.intersection(value.split()):
                return True
        return False

    def handle_starttag(self, tag, attrs):
        if self.drop_depth:
            if tag == self.drop_tag:
                self.drop_depth += 1
            return
        if self._is_chrome(tag, attrs):
            if tag not in _VOID_TAGS:
                self.drop_tag, self.drop_depth = tag, 1
            return
        self.out.append(self.get_starttag_text() or "")

    def handle_startendtag(self, tag, attrs):
        if self.drop_depth or self._is_chrome(tag, attrs):
            return
        self.out.append(self.get_starttag_text() or "")

    def handle_endtag(self, tag):
        if self.drop_depth:
            if tag == self.drop_tag:
                self.drop_depth -= 1
            return
        self.out.append(f"</{tag}>")

    def handle_data(self, data):
        if not self.drop_depth:
            self.out.append(html.escape(data, quote=False))


def _strip_chrome(markup: str) -> str:
    """Article HTML without player controls, signup forms and link previews. Unchanged if malformed."""
    if not markup or "<" not in markup:
        return markup or ""
    parser = _ChromeStripper()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:  # HTMLParser is lenient; never lose an article over the clean-up
        return markup
    if parser.drop_depth:  # an element we were dropping never closed: keep everything rather than cut text
        return markup
    return "".join(parser.out)


# ---------------------------------------------------------------- mapping

def _short(value: Any) -> str:
    return re.sub(r"[^A-Za-z0-9 _.-]+", "", str(value))[:40] or "unknown"


def _authors(post: Dict[str, Any]) -> List[str]:
    names: List[str] = []
    people = post.get("authors")
    if not isinstance(people, list):
        primary = post.get("primary_author")
        people = [primary] if isinstance(primary, dict) else []
    for person in people:
        name = person.get("name") if isinstance(person, dict) else None
        if isinstance(name, str) and name.strip():
            names.append(name)
    return names


def _post_url(post: Dict[str, Any], site_url: str) -> Tuple[str, Optional[str]]:
    """(absolute URL or "", skip reason or None)."""
    url = post.get("url")
    if not isinstance(url, str) or not url.strip():
        return "", None  # normalize() counts "no valid http(s) URL"
    url = url.strip()
    if url.startswith("/") and site_url:
        url = urllib.parse.urljoin(site_url, url)
    path = urllib.parse.urlsplit(url).path.rstrip("/")
    site_path = urllib.parse.urlsplit(site_url).path.rstrip("/") if site_url else ""
    if path in ("/404", site_path + "/404"):
        return url, "no public URL (Ghost reports /404/: no route in the theme's routes.yaml serves it)"
    return url, None


def _convert(post: Any, resource: str, site_url: str, previews: bool,
             window: Tuple[Optional[datetime], Optional[datetime]]) -> Dict[str, Any]:
    if not isinstance(post, dict):
        return {"_skip": f"unreadable item in the {resource} response"}
    status = post.get("status")
    if status is not None and status != "published":
        return {"_skip": f"not published (status: {_short(status)})"}

    visibility = post.get("visibility")
    gated = (visibility is not None and visibility != "public") or post.get("access") is False
    content_html = post.get("html") if isinstance(post.get("html"), str) else ""
    if gated:
        label = _short(visibility) if visibility is not None else "access: false"
        if not previews:
            return {"_skip": f"{_GATED} ({label}): only a preview is public "
                             "(--include-member-previews imports it)"}
        if not html.unescape(re.sub(r"<[^>]*>", "", content_html)).strip():
            return {"_skip": f"{_GATED} ({label}) with no public preview"}

    url, url_problem = _post_url(post, site_url)
    if url_problem:
        return {"_skip": url_problem}

    published = post.get("published_at")
    after, before = window
    when = parse_date(published)
    if when is not None and ((after and when < after) or (before and when >= before)):
        return {"_skip": "published outside the --after/--before dates"}

    post_id = post.get("id")
    post_id = str(post_id).strip() if isinstance(post_id, (str, int)) else ""
    return {
        "id": f"ghost-{post_id}" if post_id else "",
        "headline": post.get("title") if isinstance(post.get("title"), str) else "",
        "content_html": _strip_chrome(content_html),
        "url": url,
        "authors": _authors(post),
        "publish_date": published,
    }


# ---------------------------------------------------------------- reading

def _int(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _browse(root: str, resource: str, key: str, per_page: int, filters: List[str],
            date_filters: List[str]) -> Iterator[Any]:
    """
    Yield every item of a Content API collection, page by page, each item once.

    Pages are offsets into a list that can change during a long import. A post published meanwhile
    lands on a later page (oldest first), but one unpublished or deleted on a page already read
    moves every later post back a place, so the first post of the next page would never be read.
    Ghost recounts `total` for every page: when it falls from one page to the next, the pages those
    posts may have moved onto are read again and only posts not seen yet are passed on. (A removal
    and a new post between the same two requests cancel out in `total` and can't be noticed.)
    """
    url = f"{root}{resource}/"
    page, read, seen = 1, 0, set()
    use_dates = bool(date_filters)
    announced = False
    last_total: Optional[int] = None  # total reported with the previous page
    reread_until = 0                  # pages before this one are being read again after a fall
    rewound: set = set()              # (page, total) pairs that already sent the import back
    rewinds_left = 50                 # a server whose total jumps about at random can't loop forever
    biggest = 0                       # largest page so far, for replies without pagination meta
    short_first = False               # page 1 came back smaller than asked for, and without meta
    while True:
        params: Dict[str, Any] = {"key": key, "limit": per_page, "page": page, "include": "authors",
                                  "order": _ORDER}
        nql = "+".join((date_filters if use_dates else []) + filters)
        if nql:
            params["filter"] = nql
        try:
            data = _get(url, params)
        except ImporterError as e:
            if _status(e) in (400, 422) and page == 1 and nql:
                said = _server_message(e) or f"HTTP {_status(e)}"
                if use_dates:  # the date syntax is the part older or proxied sites may not take
                    print(f"  Ghost didn't accept the filter ({said}); retrying without the date part and "
                          "checking --after/--before here instead (this reads every post).", file=sys.stderr)
                    use_dates = False
                    continue
                if filters:
                    raise ImporterError(f"Ghost rejected the filter ({said}). Check the --filter syntax "
                                        "(e.g. tag:news or author:jane-doe).") from None
            raise
        if not isinstance(data, dict) or resource not in data:
            raise ImporterError(f"{url} didn't return a list of {resource} (got keys: "
                                f"{', '.join(map(str, list(data)[:6])) if isinstance(data, dict) else type(data).__name__}). "
                                "Check --url points at a Ghost site.")
        items = data[resource]
        if isinstance(items, dict):  # a lone object instead of a list
            items = [items]
        if not isinstance(items, list):
            raise ImporterError(f"{url} returned {resource} in an unexpected shape.")
        meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
        pagination = meta.get("pagination") if isinstance(meta.get("pagination"), dict) else {}
        total = _int(pagination.get("total"))
        pages = _int(pagination.get("pages"))
        if not announced:
            announced = True
            print(f"  {resource}: {total if total is not None else 'reading'}"
                  f"{' published to read' if total is not None else ''} from {url}", file=sys.stderr)

        # Re-read pages are checked too: posts can go while they are being read again. Each
        # (page, total) sends the import back once, so a cache that keeps serving a page stale (with
        # its old, higher total) can't send it back and forth forever.
        grew = total is not None and last_total is not None and total > last_total
        if total is not None:
            gone = last_total - total if last_total is not None and page > 1 else 0
            last_total = total
            if gone > 0 and (page, total) not in rewound and rewinds_left > 0:
                rewound.add((page, total))
                rewinds_left -= 1
                size = _int(pagination.get("limit")) or per_page
                back = max(1, page - -(-gone // size))  # later posts moved up by `gone` places
                print(f"  {resource}: {gone} removed from the site during the import; reading again from "
                      f"page {back} so the posts that moved up aren't missed.", file=sys.stderr)
                reread_until, page = max(reread_until, page), back
                continue

        ids = {str(i.get("id")) for i in items if isinstance(i, dict) and i.get("id")}
        # Only seen posts: a cache ignoring `page`, unless the total grew (posts dated before the ones
        # already read were added, pushing a page we've read along by one page).
        if page > 1 and page >= reread_until and ids and ids <= seen and not grew:
            if short_first and page == 2:
                return  # page 1 was short and the server repeats it: it was the only page
            raise ImporterError(
                f"Ghost returned the same {resource} for page {page} as for an earlier page, so the import "
                "would never finish. A cache or proxy in front of the site may be ignoring the page "
                "parameter; try the integration's API URL (e.g. https://example.ghost.io) as --url.")
        fresh = [i for i in items if not (isinstance(i, dict) and i.get("id") and str(i.get("id")) in seen)]
        if page < reread_until:  # an item without an id can't be matched; it was passed on the first time
            fresh = [i for i in fresh if isinstance(i, dict) and i.get("id")]
        seen |= ids
        read += len(fresh)
        print(f"  {resource}: page {page}{'/' + str(pages) if pages else ''} ({read} read)", file=sys.stderr)
        yield from fresh

        if "next" in pagination:
            nxt = pagination.get("next")
            if not isinstance(nxt, int) or nxt <= page:
                return
            page = nxt
        elif pages is not None:
            if page >= pages:
                return
            page += 1
        else:
            # No pagination info. A page shorter than an earlier one is the last; but a first page
            # shorter than asked for may be the server's own cap, so one more page is read to tell.
            biggest = max(biggest, len(items))
            if not items or len(items) < biggest:
                return
            if page == 1 and len(items) < per_page:
                short_first = True
            page += 1


def iter_articles(args) -> Iterator[Dict[str, Any]]:
    base, key_from_url = _parse_url(getattr(args, "url", "") or "")
    after_day, before_day = getattr(args, "after", None), getattr(args, "before", None)
    if after_day and before_day and after_day >= before_day:
        raise ImporterError(f"--after ({after_day}) must be earlier than --before ({before_day}).")
    key = _resolve_key(args, key_from_url)

    root, settings = _connect(base, key)
    site_url = settings.get("url") if isinstance(settings.get("url"), str) else ""
    if not site_url.lower().startswith(("http://", "https://")):
        site_url = base + "/"
    title = clean_text(settings.get("title")) or site_url
    version = clean_text(settings.get("version"))
    print(f"Connected to {title} ({site_url}){', Ghost ' + version if version else ''}.", file=sys.stderr)

    zone, zone_name = _site_zone(settings) if (after_day or before_day) else (timezone.utc, "UTC")
    window = _window(args, zone)
    if after_day or before_day:
        print(f"  Only posts published {'from ' + str(after_day) + ' ' if after_day else ''}"
              f"{'up to (not including) ' + str(before_day) + ' ' if before_day else ''}"
              f"in the site's time zone ({zone_name}).", file=sys.stderr)
    date_filters = _date_filter(*window)
    user_filter = getattr(args, "filter", None)
    filters = [f"({user_filter})"] if user_filter else []

    per_page = getattr(args, "per_page", None) or MAX_PER_PAGE
    limit = getattr(args, "limit", None)
    if isinstance(limit, int) and 0 < limit < per_page:
        per_page = limit
    previews = bool(getattr(args, "include_member_previews", False))

    resources = ["posts"] + (["pages"] if getattr(args, "include_pages", False) else [])
    for resource in resources:
        for post in _browse(root, resource, key, per_page, filters, date_filters):
            yield _convert(post, resource, site_url, previews, window)
