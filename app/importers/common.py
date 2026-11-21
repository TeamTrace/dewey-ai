"""
Shared pieces for archive importers. Standard library only.

Every importer yields raw dicts:
    {"id": str, "headline": str, "content_html" or "content": str, "url": str,
     "authors": [str], "publish_date": datetime | str}
and `normalize()` turns each one into Dewey's article JSON:
    {"id", "headline", "content", "url", "authors", "publish_date"}
with plain-text content and an ISO 8601 UTC date ("2024-05-01T13:00:00Z").

An importer that deliberately leaves an item out (a draft, a private or
members-only post) yields {"_skip": "<reason>"} so the report says why.

Nothing is invented: an article missing a headline, text, URL or date is
skipped and counted, and an article with no byline keeps `authors: []` and is
counted, never given a placeholder author.
"""
import email.utils
import html
import http.client
import json
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

USER_AGENT = "Dewey-archive-importer/1.0 (+https://github.com/phillymedia/dewey-ai)"
ARTICLES_PER_FILE = 1000


class ImporterError(Exception):
    """
    A problem the user can fix (bad URL, missing key, unreadable file).
    For HTTP failures, `status` is the response code and `body` the start of
    the response body (with credentials scrubbed), so importers can react to
    specific errors without parsing the message.
    """

    def __init__(self, message: str, status: Optional[int] = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


# ---------------------------------------------------------------- HTML -> text

_BLOCK_TAGS = {
    "p", "div", "section", "article", "header", "footer", "aside", "main", "nav",
    "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "figure", "figcaption",
    "ul", "ol", "dl", "dt", "dd", "table", "thead", "tbody", "tr", "hr", "address",
}
# Elements whose contents are never article text. Only elements that always
# have a closing tag: a void element here (e.g. <embed>) would never close and
# would swallow the rest of the article.
_SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "iframe"}


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self.skip_depth += 1
        elif self.skip_depth:
            return
        elif tag == "br":
            self.parts.append("\n")
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag in ("td", "th"):
            self.parts.append(" ")
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n\n")

    def handle_startendtag(self, tag, attrs):
        # Self-closing form (<br/>, <hr/>, <svg/>): emit the separator, never open a skip.
        if self.skip_depth or tag in _SKIP_TAGS:
            return
        if tag == "br":
            self.parts.append("\n")
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n\n")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            self.skip_depth = max(0, self.skip_depth - 1)
        elif not self.skip_depth and tag in _BLOCK_TAGS:
            self.parts.append("\n\n")

    def handle_data(self, data):
        if not self.skip_depth:
            self.parts.append(data)


def _c1_table() -> Dict[int, Optional[str]]:
    table: Dict[int, Optional[str]] = {}
    for code in range(0x80, 0xA0):
        try:
            table[code] = bytes([code]).decode("cp1252")
        except UnicodeDecodeError:
            table[code] = None  # undefined in cp1252: drop
    return table


# Text exported through a latin-1 step often carries Windows-1252 punctuation
# as C1 control characters (e.g. U+0092 for a curly apostrophe). Map them back.
_C1_TO_CP1252 = _c1_table()


def fix_c1(text: str) -> str:
    return text.translate(_C1_TO_CP1252) if text else text


def html_to_text(markup: str) -> str:
    """Readable plain text from article HTML: paragraphs kept, tags/scripts dropped, entities decoded."""
    if not markup:
        return ""
    parser = _TextExtractor()
    parser.feed(fix_c1(markup))
    parser.close()
    text = "".join(parser.parts)
    lines = [re.sub(r"[ \t\r\f\v ]+", " ", line).strip() for line in text.split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def clean_text(value: Any) -> str:
    """Plain-text field (headline, name): decode entities, drop stray tags, collapse whitespace."""
    if value is None:
        return ""
    text = html_to_text(str(value)) if "<" in str(value) else fix_c1(html.unescape(str(value)))
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------- dates

def parse_date(value: Any, assume_utc: bool = True) -> Optional[datetime]:
    """
    Parse ISO 8601 / RFC 3339, RFC 822 (RSS pubDate) or 'YYYY-MM-DD HH:MM:SS'.
    Naive values are treated as UTC when assume_utc is True, otherwise rejected.
    Returns an aware UTC datetime, or None when the value can't be parsed.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        dt = datetime.fromtimestamp(value, tz=timezone.utc)
    else:
        s = str(value).strip()
        if not s or s.startswith("0000-00-00"):
            return None
        dt = None
        iso = s
        if re.match(r"^\d{4}-\d{2}-\d{2}[Tt ]", s):
            iso = s[:10] + s[10:].replace("t", "T", 1)
            if iso[-1:] in ("Z", "z"):
                iso = iso[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(iso)
        except ValueError:
            # RFC 822 variants seen in feeds: "+05:30" (colon) and "GMT+1" / "UTC-05:00".
            rfc = re.sub(r"\s(?:GMT|UTC)([+-])(\d{1,2})(?::?(\d{2}))?$",
                         lambda m: f" {m.group(1)}{int(m.group(2)):02d}{m.group(3) or '00'}", s)
            rfc = re.sub(r"([+-])(\d{2}):(\d{2})$", r"\1\2\3", rfc)
            try:
                dt = email.utils.parsedate_to_datetime(rfc)
            except (TypeError, ValueError, IndexError):
                return None
        if dt is None:
            return None
    if dt.tzinfo is None:
        if not assume_utc:
            return None
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- HTTP

# Headers that carry credentials. They are dropped when a redirect leaves the
# original origin (scheme, host, port), because urllib otherwise copies every
# header onto the redirected request.
SENSITIVE_HEADERS = {"authorization", "proxy-authorization", "cookie", "x-api-key", "api-key"}
SENSITIVE_PARAMS = {"key", "api_key", "apikey", "token", "access_token", "password", "secret"}


def _origin(url: str) -> Tuple[str, str, Optional[int]]:
    parts = urllib.parse.urlsplit(url)
    default = {"http": 80, "https": 443}.get(parts.scheme.lower())
    try:
        port = parts.port or default
    except ValueError:
        port = default
    return parts.scheme.lower(), (parts.hostname or "").lower(), port


class _CredentialSafeRedirects(urllib.request.HTTPRedirectHandler):
    """Follow redirects, but never carry credential headers to another origin or down to http://."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is None:
            return None
        old_o, new_o = _origin(req.full_url), _origin(new.full_url)
        if old_o != new_o:
            for name in list(new.headers):
                if name.lower() in SENSITIVE_HEADERS:
                    del new.headers[name]
            for name in list(new.unredirected_hdrs):
                if name.lower() in SENSITIVE_HEADERS:
                    del new.unredirected_hdrs[name]
        return new


_SAFE_OPENER = urllib.request.build_opener(_CredentialSafeRedirects)


def _open(req: urllib.request.Request, timeout: float, opener=None):
    # Never use urllib's global opener: urllib.request.urlopen() installs a plain
    # default one the first time anything in the process calls it, which would
    # silently turn off the cross-origin credential stripping. A caller that wants
    # a stricter policy (drupal.py refuses cross-origin redirects) passes its own.
    return (opener or _SAFE_OPENER).open(req, timeout=timeout)


def _secrets_in(url: str, headers: Dict[str, str]) -> List[str]:
    values = [v for k, v in urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query, keep_blank_values=True)
              if k.lower() in SENSITIVE_PARAMS and len(v) >= 4]
    for k, v in headers.items():
        if k.lower() in SENSITIVE_HEADERS and v:
            values.append(v)
            values.extend(part for part in v.split() if len(part) >= 8)  # e.g. the token after "Basic "
    parts = urllib.parse.urlsplit(url)
    if parts.password:
        values.append(parts.password)
    return values


def scrub(text: str, secrets: Iterable[str]) -> str:
    """Replace any secret value that a server echoed back (e.g. in an error page) with ***."""
    for secret in sorted(set(secrets), key=len, reverse=True):
        if secret:
            text = text.replace(secret, "***")
    return text


def http_get(url: str, params: Optional[Dict[str, Any]] = None, headers: Optional[Dict[str, str]] = None,
             retries: int = 4, timeout: float = 30.0, opener=None) -> Tuple[bytes, Dict[str, str]]:
    """
    GET with retries on 429/5xx/network errors (honoring Retry-After). Returns (body, headers).
    Credential headers are not forwarded across origins on redirect, and every
    failure becomes an ImporterError with secrets redacted.
    """
    if params:
        sep = "&" if urllib.parse.urlparse(url).query else "?"
        url = url + sep + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}, doseq=True)
    req_headers = {"User-Agent": USER_AGENT, "Accept": "application/json, application/xml, text/xml, */*"}
    req_headers.update(headers or {})
    secrets = _secrets_in(url, req_headers)
    shown = redact_url(url)
    delay = 2.0
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=req_headers)
            with _open(req, timeout, opener) as resp:
                return resp.read(), {k.lower(): v for k, v in resp.headers.items()}
        except urllib.error.HTTPError as e:
            try:
                raw = e.read()[:4000]
            except (OSError, http.client.HTTPException):
                raw = b""
            body = scrub(raw.decode("utf-8", "replace"), secrets)
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                wait = _retry_after(e.headers.get("Retry-After")) or delay
                print(f"  HTTP {e.code} from {shown}; retrying in {wait:.0f}s", file=sys.stderr)
                time.sleep(wait)
                delay *= 2
                continue
            hint = {401: "check the API key / credentials", 403: "access denied; check credentials or "
                    "whether the site blocks API access", 404: "not found; check the URL"}.get(e.code, "")
            raise ImporterError(f"HTTP {e.code} from {shown}{': ' + hint if hint else ''}. "
                                f"Server said: {body.strip()[:200]}", status=e.code, body=body) from None
        except (ValueError, http.client.InvalidURL) as e:  # malformed URL: retrying can't help
            raise ImporterError(f"Invalid URL {shown}: {scrub(str(e), secrets)}") from None
        except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
            # URLError, timeouts, connection resets, TLS errors (ssl.SSLError is an OSError),
            # and truncated responses (IncompleteRead) are all retried.
            reason = scrub(str(getattr(e, "reason", e)), secrets)
            if attempt < retries:
                print(f"  Network error ({reason}); retrying in {delay:.0f}s", file=sys.stderr)
                time.sleep(delay)
                delay *= 2
                continue
            raise ImporterError(f"Couldn't reach {shown}: {reason}") from None
    raise ImporterError(f"Giving up on {shown}")  # pragma: no cover


def http_get_json(url: str, params=None, headers=None, **kw) -> Tuple[Any, Dict[str, str]]:
    body, resp_headers = http_get(url, params, headers, **kw)
    try:
        return json.loads(body.decode("utf-8-sig")), resp_headers
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ImporterError(f"{redact_url(url)} did not return JSON (got: {body[:120]!r}...). "
                            "Check the URL points at the API, not a web page.") from None


def _retry_after(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return min(float(value), 120.0)
    except ValueError:
        return None


def redact_url(url: str) -> str:
    """Hide credentials in a URL for messages: user:password@ and key/token-like query parameters."""
    parts = urllib.parse.urlsplit(url)
    netloc = parts.netloc
    if "@" in netloc:
        netloc = "***@" + netloc.rsplit("@", 1)[1]
    query = [(k, "***" if k.lower() in SENSITIVE_PARAMS else v)
             for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)]
    return urllib.parse.urlunsplit(parts._replace(netloc=netloc, query=urllib.parse.urlencode(query, safe="*")))


_redact_url = redact_url  # backwards-compatible name


# ---------------------------------------------------------------- normalize + write

SAME_BYLINE_MIN = 3  # this many articles sharing the only byline seen is worth a line in the report


@dataclass
class ImportReport:
    source: str
    written: int = 0
    duplicates: int = 0
    skipped: Counter = field(default_factory=Counter)
    no_byline: int = 0
    bylined: int = 0
    first_byline: Optional[Tuple[str, ...]] = None
    bylines_vary: bool = False
    first_date: Optional[str] = None
    last_date: Optional[str] = None
    files: List[str] = field(default_factory=list)

    def note_date(self, iso: str) -> None:
        self.first_date = min(self.first_date or iso, iso)
        self.last_date = max(self.last_date or iso, iso)

    def note_byline(self, authors: List[str]) -> None:
        """Count one written article's byline, so summary() can say when they are all the same."""
        if not authors:
            self.no_byline += 1
            return
        self.bylined += 1
        if self.first_byline is None:
            self.first_byline = tuple(authors)
        elif tuple(authors) != self.first_byline:
            self.bylines_vary = True

    def summary(self) -> str:
        lines = [f"Imported {self.written} article(s) from {self.source}."]
        if self.first_date:
            lines.append(f"  Publish dates: {self.first_date[:10]} to {self.last_date[:10]}")
        if self.bylined >= SAME_BYLINE_MIN and not self.bylines_vary:
            # A byline setting mapped to the wrong thing imports cleanly and credits one name with
            # everything; the only sign is that the bylines never vary.
            lines.append(f"  All {self.bylined} articles with a byline have the same one: "
                         f"{' and '.join(self.first_byline)}. If the same people didn't write all of them, "
                         "the byline is being read from the wrong place: check the byline settings and "
                         "import again before indexing.")
        if self.no_byline:
            lines.append(f"  {self.no_byline} have no byline: they are searchable, but author filters "
                         "won't find them. No placeholder author was added.")
        if self.duplicates:
            lines.append(f"  {self.duplicates} duplicate(s) skipped (same id or URL seen earlier).")
        for reason, n in sorted(self.skipped.items()):
            lines.append(f"  {n} skipped: {reason}")
        if self.files:
            lines.append(f"  Wrote {len(self.files)} file(s): {self.files[0]}" + (" ..." if len(self.files) > 1 else ""))
        return "\n".join(lines)


def normalize(raw: Dict[str, Any], report: ImportReport) -> Optional[Dict[str, Any]]:
    """Validate and convert one raw article; returns None (and counts why) when it can't be used."""
    if raw.get("_skip"):
        report.skipped[str(raw["_skip"])] += 1
        return None
    headline = clean_text(raw.get("headline"))
    if "content" in raw and raw.get("content") is not None:
        content = fix_c1(str(raw["content"])).strip()
    else:
        content = html_to_text(raw.get("content_html") or "")
    url = str(raw.get("url") or "").strip()
    date = parse_date(raw.get("publish_date"))
    authors: List[str] = []
    for name in raw.get("authors") or []:
        name = clean_text(name)
        if name and name not in authors:
            authors.append(name)

    for reason, missing in (("no headline", not headline), ("no article text", not content),
                            ("no valid http(s) URL", not url.lower().startswith(("http://", "https://"))),
                            ("no parseable publish date", date is None)):
        if missing:
            report.skipped[reason] += 1
            return None

    article_id = str(raw.get("id") or "").strip()
    if not article_id:
        report.skipped["no id"] += 1
        return None
    return {"id": article_id, "headline": headline, "content": content, "url": url,
            "authors": authors, "publish_date": to_iso_utc(date)}


class ArticleWriter:
    """
    Writes articles to <out_dir>/<prefix>-0001.json, -0002.json, ... (1000 per file).
    Files are written to a temporary folder first and only replace earlier
    <prefix>-*.json files once the whole import succeeds, so a failed import
    never leaves a half-replaced archive.
    """

    def __init__(self, out_dir: Path, prefix: str, report: ImportReport, limit: Optional[int] = None):
        self.out_dir = Path(out_dir)
        self.prefix = re.sub(r"[^A-Za-z0-9_-]+", "-", prefix).strip("-") or "import"
        self.report = report
        self.limit = limit
        self.tmp = self.out_dir / f".import-tmp-{self.prefix}"
        self.batch: List[Dict[str, Any]] = []
        self.seen_ids: set = set()
        self.seen_urls: set = set()
        self.file_count = 0

    def __enter__(self):
        self.out_dir.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.tmp.mkdir()
        return self

    @property
    def full(self) -> bool:
        return self.limit is not None and self.report.written >= self.limit

    def add(self, raw: Dict[str, Any]) -> None:
        if self.full:
            return
        article = normalize(raw, self.report)
        if article is None:
            return
        url_key = article["url"].rstrip("/").lower()
        if article["id"] in self.seen_ids or url_key in self.seen_urls:
            self.report.duplicates += 1
            return
        self.seen_ids.add(article["id"])
        self.seen_urls.add(url_key)
        self.report.note_byline(article["authors"])
        self.report.note_date(article["publish_date"])
        self.batch.append(article)
        self.report.written += 1
        if len(self.batch) >= ARTICLES_PER_FILE:
            self._flush()

    def add_all(self, raws: Iterable[Dict[str, Any]]) -> None:
        for raw in raws:
            self.add(raw)
            if self.full:
                break

    def _flush(self) -> None:
        if not self.batch:
            return
        self.file_count += 1
        name = f"{self.prefix}-{self.file_count:04d}.json"
        (self.tmp / name).write_text(json.dumps(self.batch, ensure_ascii=False, indent=1), encoding="utf-8")
        self.batch = []

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            shutil.rmtree(self.tmp, ignore_errors=True)
            return False
        self._flush()
        old_files = sorted(self.out_dir.glob(f"{self.prefix}-[0-9][0-9][0-9][0-9].json"))
        if self.report.written == 0:
            # An import that found nothing must not wipe out an earlier, good import.
            shutil.rmtree(self.tmp, ignore_errors=True)
            if old_files:
                print(f"  Nothing imported; kept the {len(old_files)} existing {self.prefix}-*.json file(s).",
                      file=sys.stderr)
            return False
        if old_files:
            print(f"  Replacing {len(old_files)} file(s) from an earlier {self.prefix} import.", file=sys.stderr)
        for old in old_files:
            old.unlink()
        for new in sorted(self.tmp.glob("*.json")):
            target = self.out_dir / new.name
            new.replace(target)
            self.report.files.append(str(target))
        shutil.rmtree(self.tmp, ignore_errors=True)
        return False
