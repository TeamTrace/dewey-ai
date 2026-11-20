"""
Drupal JSON:API importer: published article nodes from a Drupal 8.7+ site.

Beta: tested against sample exports only (fixtures shaped like Drupal core's
JSON:API responses), not yet against a live newsroom site.

    python app/import_archive.py drupal --site https://www.example.com --limit 20

Reads <site>/jsonapi/node/<type> (Drupal core's JSON:API module) 50 nodes at a
time, oldest node first. Published content is readable without a login on a
default install. A username and password (Drupal's core HTTP Basic
Authentication module) are only needed when a site closes JSON:API to anonymous
visitors. Run `python app/import_archive.py drupal --help` for where to find
each value in Drupal.

Bylines come from the node's author account ("Authored by", `uid`) by default,
or from a byline field with --byline-field. A node whose byline can't be read,
or whose author is Drupal's anonymous user, gets `authors: []`; no other name
is substituted.

Behaviour checked against Drupal core's JSON:API source (core/modules/jsonapi):
page[limit] defaults to and is capped at 50 (OffsetPage::SIZE_MAX); a `next`
link is present only when more rows exist; rows the current user can't view
come back as `meta.omitted` links instead of resources; users carry a computed
`display_name`, and "view label" access (all a visitor gets) exposes only that.

Standard library only.
"""
import argparse
import base64
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, time, timezone
from typing import Any, Dict, Generator, Iterator, List, Optional, Tuple

from importers.common import ImporterError, clean_text, http_get_json, parse_date

NAME = "drupal"
HELP = ("Beta (tested against sample exports only): published articles from a Drupal 8.7+ site, "
        "read through its core JSON:API module.")

USER_ENV = "DRUPAL_USER"
PASSWORD_ENV = "DRUPAL_PASSWORD"
MAX_PAGE_SIZE = 50  # core JSON:API caps page[limit] at 50 and uses 50 when it is not given
DEFAULT_TYPE = "article"
JSONAPI_MEDIA_TYPE = "application/vnd.api+json"
# Base fields stored as Unix timestamps, which the server can filter by date.
_TIMESTAMP_FIELDS = {"created", "changed"}
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
_NAME_KEYS = ("display_name", "name", "title")  # user, taxonomy term, node (e.g. a "person" node)

_EPILOG = f"""\
where to find each value:
  --site          The address readers use, e.g. https://www.example.com (include a
                  subfolder or language prefix such as /es if articles live under
                  one). To check it, open https://www.example.com/jsonapi in a
                  browser: you should see JSON whose "links" list "node--article".
                  "Page not found" means JSON:API is off: an administrator turns it
                  on at Manage > Extend (tick "JSON:API", Install). Its settings
                  page (Configuration > Web services > JSON:API) can stay on
                  "Accept only JSON:API read operations".
  --api-url       Only if JSON:API isn't at <site>/jsonapi: a headless/decoupled
                  site whose Drupal runs elsewhere (e.g. https://cms.example.com/jsonapi),
                  or a developer changed jsonapi.base_path. Article links still use --site.
  --type          Structure > Content types: the "Machine name" column (default:
                  article). The node--... names listed at <site>/jsonapi work too.
  --body-field    Structure > Content types > (type) > Manage fields: the machine
                  name of the field holding the story text (default: body). If the
                  text is in Paragraphs (an "Entity reference revisions" field),
                  give that field: the formatted text of each paragraph is joined
                  in order (paragraphs nested inside paragraphs are not read).
  --byline-field  Same page: the machine name of the byline field, either text or a
                  reference to users, taxonomy terms (an "Authors" vocabulary) or
                  person nodes. Default: uid, the account in "Authored by", which on
                  many newsroom sites is a web producer rather than the reporter.
                  Use none to import without bylines.
  --date-field    Default: created ("Authored on"). A date field's machine name if
                  the publish date is kept elsewhere.
  --user, --password
                  Only if <site>/jsonapi refuses anonymous visitors. Needs the core
                  "HTTP Basic Authentication" module (Manage > Extend). Create an
                  account at People > Add user (e.g. dewey-reader) with no extra
                  roles: published content that account can see is imported,
                  including members-only content, so give it no more access than
                  the public. Prefer the environment variables {USER_ENV} and
                  {PASSWORD_ENV} to the flags: flags are saved in your shell history.

what is imported:
  Published nodes only: the request asks Drupal for status = published, so drafts,
  unpublished, archived and not-yet-scheduled nodes are never downloaded. Nodes
  Drupal lists but hides from the visitor (members-only or restricted content)
  are skipped and counted in the report. Each article's link is --site plus its
  URL alias (Search and metadata > URL alias), or /node/<id> when it has none.
  Articles are read in the content language of the address you give.

large archives:
  Try --limit 20 first. For a very large site, import a year at a time with
  --after and --before (UTC dates); each date window gets its own output files.

examples:
  python app/import_archive.py drupal --site https://www.example.com --limit 20
  python app/import_archive.py drupal --site https://www.example.com \\
      --type article --type story --byline-field field_authors
  python app/import_archive.py drupal --site https://www.example.com \\
      --api-url https://cms.example.com/jsonapi --body-field field_paragraphs
  export {USER_ENV}=dewey-reader
  read -rs {PASSWORD_ENV} && export {PASSWORD_ENV}   # type it; not shown or saved
  python app/import_archive.py drupal --site https://www.example.com
"""


# ---------------------------------------------------------------- arguments

def _date_arg(value: str) -> date:
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a date like 2024-01-31, got {value!r}") from None


def _page_size_arg(value: str) -> int:
    try:
        n = int(value)
    except ValueError:
        n = 0
    if not 1 <= n <= MAX_PAGE_SIZE:
        raise argparse.ArgumentTypeError(f"must be a whole number from 1 to {MAX_PAGE_SIZE} (Drupal's "
                                         f"maximum), got {value!r}")
    return n


def _field_arg(value: str) -> str:
    value = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise argparse.ArgumentTypeError(f"expected a field machine name such as field_byline, got {value!r}")
    return value


def _type_arg(value: str) -> str:
    value = value.strip().strip("/")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise argparse.ArgumentTypeError(f"expected a content type machine name such as article, or a "
                                         f"resource type such as node--article, got {value!r}")
    return value


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.formatter_class = argparse.RawDescriptionHelpFormatter
    parser.epilog = _EPILOG
    parser.add_argument("--site", required=True, metavar="URL",
                        help="your site's public address, e.g. https://www.example.com (see below)")
    parser.add_argument("--api-url", metavar="URL",
                        help="the JSON:API address, only if it isn't <site>/jsonapi (headless sites)")
    parser.add_argument("--type", dest="types", action="append", type=_type_arg, metavar="TYPE",
                        help=f"content type machine name (default: {DEFAULT_TYPE}); repeat for several")
    parser.add_argument("--body-field", type=_field_arg, default="body", metavar="FIELD",
                        help="field with the story text: formatted text, or Paragraphs (default: body)")
    parser.add_argument("--byline-field", type=_field_arg, default="uid", metavar="FIELD",
                        help="field with the byline: text, or a reference to users, terms or nodes "
                             "(default: uid, the node's author account; none for no bylines)")
    parser.add_argument("--date-field", type=_field_arg, default="created", metavar="FIELD",
                        help="field with the publish date (default: created, \"Authored on\")")
    parser.add_argument("--after", type=_date_arg, metavar="YYYY-MM-DD",
                        help="only articles published on or after this date (UTC)")
    parser.add_argument("--before", type=_date_arg, metavar="YYYY-MM-DD",
                        help="only articles published before this date (UTC)")
    parser.add_argument("--user", metavar="USERNAME",
                        help=f"Drupal username, only if JSON:API refuses anonymous visitors (or set {USER_ENV})")
    parser.add_argument("--password", metavar="PASSWORD",
                        help=f"that account's password (better: set {PASSWORD_ENV}, since flags are saved "
                             "in shell history)")
    parser.add_argument("--page-size", type=_page_size_arg, default=MAX_PAGE_SIZE, metavar="N",
                        help=f"nodes per request, 1-{MAX_PAGE_SIZE} (default: {MAX_PAGE_SIZE}; lower it if "
                             "the site times out)")


def default_prefix(args) -> str:
    """drupal-<site>[-<types>][-after-YYYYMMDD][-before-YYYYMMDD]; stable for the same settings."""
    parts = ["drupal", _site_key(_lenient_url(getattr(args, "site", "") or "")) or "site"]
    types = getattr(args, "types", None) or [DEFAULT_TYPE]
    if types != [DEFAULT_TYPE]:
        parts.extend(re.sub(r"^node--", "", t) for t in types)
    after, before = getattr(args, "after", None), getattr(args, "before", None)
    if after:
        parts.append(f"after-{after:%Y%m%d}")
    if before:
        parts.append(f"before-{before:%Y%m%d}")
    return "-".join(parts)


# ---------------------------------------------------------------- URLs

def _lenient_url(raw: str) -> str:
    raw = (raw or "").strip()
    if raw and not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", raw):
        raw = "https://" + raw
    return raw


def _site_key(url: str) -> str:
    """'https://www.example.com/es/' -> 'example-com-es'. Used in ids and file names."""
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
    path = re.split(r"/jsonapi(?:/|$)", parts.path, maxsplit=1)[0]
    return re.sub(r"[^a-z0-9]+", "-", (host + path).lower()).strip("-")


def _origin(url: str) -> Tuple[str, str]:
    parts = urllib.parse.urlsplit(url)
    return parts.scheme.lower(), parts.netloc.lower()


def _split_url(raw: str, option: str, example: str) -> urllib.parse.SplitResult:
    url = _lenient_url(raw)
    if "@" in urllib.parse.urlsplit(url).netloc:
        # Not echoed: the part before @ is usually a username and password, which would end up in error messages,
        # progress output and every article link.
        raise ImporterError(f"{option} should not contain a username or password. Give the address alone, like "
                            f"{example}, and the login with {USER_ENV} and {PASSWORD_ENV} (or --user and "
                            "--password; see --help).")
    try:
        parts = urllib.parse.urlsplit(url)
        parts.port  # noqa: B018  (raises ValueError on a bad port)
    except ValueError:
        parts = None
    if not parts or parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise ImporterError(f"{option} should be a web address like {example} (got {raw!r}).")
    return parts


def _parse_site(raw: str) -> Tuple[str, Optional[str]]:
    """Validate --site. Returns (public base URL without trailing slash, API URL if --site named one)."""
    parts = _split_url(raw, "--site", "https://www.example.com")
    path = parts.path
    api = None
    m = re.search(r"/jsonapi(?:/|$)", path)
    if m:
        api = urllib.parse.urlunsplit((parts.scheme.lower(), parts.netloc, path[:m.start()] + "/jsonapi", "", ""))
        path = path[:m.start()]
    site = urllib.parse.urlunsplit((parts.scheme.lower(), parts.netloc, path.rstrip("/"), "", ""))
    return site, api


def _parse_api_url(raw: str) -> str:
    parts = _split_url(raw, "--api-url", "https://cms.example.com/jsonapi")
    return urllib.parse.urlunsplit((parts.scheme.lower(), parts.netloc, parts.path.rstrip("/"), "", ""))


def _rebase(href: str, entry_self: Optional[str], api_url: str) -> Optional[str]:
    """
    A URL Drupal generated, moved onto the API address the user gave. Drupal builds
    absolute links from the request it sees, which behind a proxy can carry an
    internal host or http://, so only the part below the entry point is kept.
    """
    try:
        path = urllib.parse.urlsplit(href).path
        base_path = urllib.parse.urlsplit(entry_self).path.rstrip("/") if entry_self else ""
    except ValueError:
        return None
    if base_path and path.startswith(base_path + "/"):
        return api_url.rstrip("/") + path[len(base_path):]
    if _origin(href) == _origin(api_url):
        return urllib.parse.urlunsplit(urllib.parse.urlsplit(href)._replace(query="", fragment=""))
    return None


def _article_url(attrs: Dict[str, Any], nid: Optional[int], public_base: str) -> str:
    path = attrs.get("path")
    alias = path.get("alias") if isinstance(path, dict) else None
    if isinstance(alias, str) and alias.strip().startswith("/") and not alias.strip().startswith("//"):
        # Aliases are stored without the base path or language prefix; keep %-escapes already present.
        return public_base + urllib.parse.quote(alias.strip(), safe="/%:@!$&'()*+,;=-._~")
    if nid:
        return f"{public_base}/node/{nid}"
    return ""


# ---------------------------------------------------------------- HTTP

class _ApiError(ImporterError):
    """ImporterError plus what could be read from it: HTTP status and the JSON:API error detail."""

    def __init__(self, message: str, status: Optional[int] = None, detail: str = "", kind: str = "http"):
        super().__init__(message)
        self.status, self.detail, self.kind = status, detail, kind

    def short(self) -> str:
        if self.status:
            return f"HTTP {self.status}" + (f": {self.detail}" if self.detail else "")
        return {"network": "site unreachable", "not_json": "response was not JSON"}.get(self.kind, self.kind)


_STATUS_RE = re.compile(r"^HTTP (\d{3}) ")
# common.http_get keeps the first 200 characters of an error body, and Drupal puts its "jsonapi" header
# before "errors", so the detail is often cut short: take what is there.
_DETAIL_RE = re.compile(r'"detail"\s*:\s*"((?:[^"\\]|\\.)*)("?)')


def _error_detail(message: str) -> str:
    m = _DETAIL_RE.search(message)
    if not m or not m.group(1).strip():
        return ""
    text = m.group(1)
    try:
        text = json.loads(f'"{text}"')
    except ValueError:
        text = text.replace("\\/", "/")
    return clean_text(text)[:200] + ("" if m.group(2) else "...")


class _RedirectRefused(ImporterError):
    """A signed-in request was redirected off the API's origin; nothing was sent there."""


class _SameOriginRedirects(urllib.request.HTTPRedirectHandler):
    """
    urllib copies every request header, Authorization included, onto a redirect to any host and to plain
    http://. While credentials are in use, follow redirects only within the API's own scheme, host and port.
    """

    def __init__(self, trusted_origin: Tuple[str, str]):
        super().__init__()
        self.trusted_origin = trusted_origin

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if _origin(newurl) != self.trusted_origin:
            fp.close()
            src, dst = urllib.parse.urlsplit(req.full_url), urllib.parse.urlsplit(newurl)
            raise _RedirectRefused(
                f"Drupal redirected {src.scheme}://{src.netloc}{src.path} to {dst.scheme}://{dst.netloc}{dst.path}. "
                f"Your Drupal login is only sent to {src.scheme}://{src.netloc}, so nothing was sent there. If "
                f"{dst.scheme}://{dst.netloc} is the right address, use it in --site (or --api-url) and run again.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _Client:
    """http_get_json with the JSON:API media type, and credentials sent only to the API's own origin."""

    def __init__(self, auth_header: Optional[str], trusted_origin: Tuple[str, str]):
        self.auth = auth_header
        self.trusted_origin = trusted_origin

    def get(self, url: str, params: Optional[Dict[str, Any]] = None, retries: int = 4) -> Tuple[Any, Dict[str, str]]:
        headers = {"Accept": JSONAPI_MEDIA_TYPE}
        if self.auth and _origin(url) == self.trusted_origin:
            headers["Authorization"] = self.auth
        # With a login, use an opener that refuses to follow the API off its own origin (common.http_get
        # would strip the credentials, but a clear error is better than an anonymous 401 elsewhere).
        guarded = "Authorization" in headers
        opener = urllib.request.build_opener(_SameOriginRedirects(self.trusted_origin)) if guarded else None
        try:
            return http_get_json(url, params, headers, retries=retries, opener=opener)
        except _RedirectRefused:
            raise
        except ImporterError as e:
            msg = str(e)
            m = _STATUS_RE.match(msg)
            kind = "http" if m else ("not_json" if "did not return JSON" in msg else "network")
            raise _ApiError(msg, int(m.group(1)) if m else None, _error_detail(msg), kind) from None


def _auth_header(args, api_url: str) -> Optional[str]:
    user = (getattr(args, "user", None) or os.environ.get(USER_ENV) or "").strip()
    password = getattr(args, "password", None) or os.environ.get(PASSWORD_ENV) or ""
    if bool(user) != bool(password):
        missing = f"the password ({PASSWORD_ENV} or --password)" if user else f"the username ({USER_ENV} or --user)"
        raise ImporterError(f"Give both a Drupal username and password; {missing} is missing.")
    if not user:
        return None
    parts = urllib.parse.urlsplit(api_url)
    if parts.scheme.lower() != "https" and (parts.hostname or "").lower() not in _LOCAL_HOSTS:
        raise ImporterError(f"Refusing to send your Drupal password over plain http to {parts.netloc}. "
                            "Use the https:// address of the site.")
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return "Basic " + token


# ---------------------------------------------------------------- discovery

def _blocked_message(e: _ApiError, url: str, have_auth: bool) -> str:
    if have_auth:
        return (f"Drupal refused {url} ({e.short()}) for this account. Check its roles can view this "
                "content, or ask your site administrator.")
    return (f"Drupal refused {url} to anonymous visitors ({e.short()}). Ask an administrator to let anonymous "
            "visitors read published content through JSON:API, or pass --user and --password (see --help).")


def _entry_point(client: _Client, api_url: str, explicit_api: bool) -> Dict[str, Any]:
    where = "--api-url" if explicit_api else "--site"
    try:
        doc, _ = client.get(api_url, retries=1)
    except _ApiError as e:
        if e.kind == "network":
            raise ImporterError(f"Couldn't reach {api_url}. Check {where} and your internet connection. ({e})") from None
        if e.status == 404:
            raise ImporterError(f"No JSON:API at {api_url} (HTTP 404). If {api_url} shows \"Page not found\" in a "
                                "browser, an administrator needs to turn on the JSON:API module (Manage > Extend). "
                                "For a headless site, or a changed JSON:API base path, pass --api-url.") from None
        if e.status == 401 and client.auth:
            raise ImporterError(_LOGIN_FAILED) from None  # a failed login is handled as anonymous, then refused
        if e.status in (401, 403):
            raise ImporterError(_blocked_message(e, api_url, bool(client.auth))) from None
        if e.kind == "not_json":
            raise ImporterError(f"{api_url} returned a web page, not JSON:API data. Check {where}: opening it in a "
                                "browser should show JSON whose \"links\" list \"node--article\".") from None
        raise
    if not (isinstance(doc, dict) and isinstance(doc.get("links"), dict) and ("jsonapi" in doc or "data" in doc)):
        raise ImporterError(f"{api_url} returned JSON, but not a Drupal JSON:API entry point (no \"links\" to "
                            f"resources). Check {where}.")
    return doc


def _href(link: Any) -> Optional[str]:
    if isinstance(link, dict):
        link = link.get("href")
    return link.strip() if isinstance(link, str) and link.strip() else None


_LOGIN_FAILED = ("Drupal didn't accept the login: the username or password is wrong, the HTTP Basic "
                 "Authentication module is off (Manage > Extend), or the web server drops the Authorization "
                 "header before Drupal sees it. Drupal blocks Basic Auth for an account after 5 failed attempts "
                 "in 6 hours by default, so check the details before retrying.")


def _check_login(entry: Dict[str, Any]) -> None:
    meta = entry.get("meta")
    links = meta.get("links") if isinstance(meta, dict) else None
    if isinstance(links, dict) and links.get("me"):
        print("Signed in to Drupal. Published content this account can see is imported, including content "
              "anonymous visitors can't see.", file=sys.stderr)
        return
    # Drupal treats a failed Basic Auth login as an anonymous request rather than an error.
    raise ImporterError(_LOGIN_FAILED)


def _resolve_type(entry: Dict[str, Any], api_url: str, wanted: str) -> Tuple[str, str]:
    """(resource type name, collection URL) for --type, from the entry point's links."""
    links = entry["links"]
    entry_self = _href(links.get("self"))
    names = [wanted] if "--" in wanted else [wanted, f"node--{wanted}"]
    for name in names:
        href = _href(links.get(name))
        if href and name != "self":
            # Core's collection path is <api>/<entity type>/<bundle> when the link can't be moved over.
            return name, _rebase(href, entry_self, api_url) or f"{api_url.rstrip('/')}/{name.replace('--', '/', 1)}"
    offered = sorted(k[len("node--"):] for k in links if isinstance(k, str) and k.startswith("node--"))
    listed = f" Content types it offers: {', '.join(offered)}." if offered else ""
    raise ImporterError(f"This site's JSON:API has no content type {wanted!r}.{listed} Use the machine name from "
                        "Structure > Content types. If the type is missing from <site>/jsonapi, it may be "
                        "disabled in JSON:API settings.")


# ---------------------------------------------------------------- fields

class _Plan:
    """Which node fields to read, and whether each is an attribute or a relationship."""

    def __init__(self, type_name: str, body: str, byline: Optional[str], date_field: str):
        self.type_name = type_name
        self.body, self.byline, self.date_field = body, byline, date_field
        self.body_is_rel = False
        self.byline_is_rel = byline == "uid"

    def classify(self, samples: List[Dict[str, Any]]) -> None:
        """
        Decide from nodes the visitor can see. Several are used because a field the visitor may not view is left
        out of a node (field permissions, a paywall), so one node may lack a field that the type has.
        """
        attrs: set = set()
        rels: set = set()
        for sample in samples:
            if isinstance(sample.get("attributes"), dict):
                attrs.update(sample["attributes"])
            if isinstance(sample.get("relationships"), dict):
                rels.update(sample["relationships"])

        def where(field: str, option: str) -> bool:
            if field in rels:
                return True
            if field in attrs:
                return False
            fields = ", ".join(sorted(k for k in attrs | rels if not k.startswith("drupal_internal__")))
            raise ImporterError(f"{self.type_name} has no field {field!r} ({option}), or it is hidden from this "
                                f"visitor in the nodes checked. Its fields: {fields}. Check the machine name at "
                                "Structure > Content types > Manage fields.")

        self.body_is_rel = where(self.body, "--body-field")
        if self.byline:
            self.byline_is_rel = where(self.byline, "--byline-field")
        if where(self.date_field, "--date-field"):
            raise ImporterError(f"--date-field {self.date_field} is a reference field, not a date.")

    def includes(self) -> List[str]:
        out = []
        if self.byline and self.byline_is_rel:
            out.append(self.byline)
        if self.body_is_rel and self.body not in out:
            out.append(self.body)
        return out

    def node_fields(self) -> List[str]:
        fields = ["drupal_internal__nid", "status", "title", "path", self.date_field, self.body]
        if self.byline:
            fields.append(self.byline)
        return list(dict.fromkeys(fields))


def _date_value(value: Any) -> Any:
    """A date attribute: a string, a Unix time, a date range {value, end_value}, or a list of those."""
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, dict):
        value = value.get("value")
    return value


def _formatted_html(value: Any) -> List[str]:
    """HTML from formatted text values ({value, format, processed}); plain strings are not included."""
    if isinstance(value, list):
        return [h for item in value for h in _formatted_html(item)]
    if isinstance(value, dict) and ("format" in value or "processed" in value):
        text = value.get("processed") if isinstance(value.get("processed"), str) else value.get("value")
        return [text] if isinstance(text, str) and text.strip() else []
    return []


def _names_from_value(value: Any) -> List[str]:
    """Names from a text byline field: text, formatted text, or a list of either."""
    if isinstance(value, str):
        name = re.sub(r"^by\s+", "", clean_text(value), flags=re.I).strip()
        return [name] if name else []
    if isinstance(value, dict):
        html = _formatted_html(value)
        return _names_from_value(html[0]) if html else []
    if isinstance(value, list):
        names: List[str] = []
        for item in value:
            for name in _names_from_value(item):
                if name not in names:
                    names.append(name)
        return names
    return []


def _int_id(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip()) or None
    return None


def _identifiers(rel: Any) -> List[Dict[str, Any]]:
    data = rel.get("data") if isinstance(rel, dict) else None
    if isinstance(data, dict):
        data = [data]
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []


def _target_id(ident: Dict[str, Any]) -> Any:
    meta = ident.get("meta")
    return meta.get("drupal_internal__target_id") if isinstance(meta, dict) else None


def _is_anonymous_user(ident: Dict[str, Any]) -> bool:
    target = _target_id(ident)
    return ident.get("type") == "user--user" and target is not None and str(target).strip() == "0"


def _is_unpublished(attrs: Dict[str, Any]) -> bool:
    status = attrs.get("status")
    return status is False or status in (0, "0", "false")


# The anonymous user's label on a default install. Only used when nothing else identifies the account: older
# releases have no drupal_internal__target_id on relationships, and visitors get a label-only user object.
_ANONYMOUS_LABEL = "Anonymous"


# ---------------------------------------------------------------- articles

_MALFORMED = "malformed entry in the API response (not a node)"


class _Context:
    def __init__(self, site_key: str, public_base: str, plan: _Plan, have_auth: bool,
                 after: Optional[datetime], before: Optional[datetime], collection_url: str):
        self.site_key, self.public_base, self.plan = site_key, public_base, plan
        self.have_auth, self.after, self.before = have_auth, after, before
        self.collection_tail = _collection_tail(collection_url)
        self.anonymous_author = 0
        self.hidden_bylines = 0
        self.hidden_body_parts = 0
        self.unpublished_bylines = 0
        self.unpublished_body_parts = 0


def _authors(resource: Dict[str, Any], ctx: _Context, included: Dict[Tuple[str, str], Dict[str, Any]]) -> List[str]:
    plan = ctx.plan
    if not plan.byline:
        return []
    attrs = resource.get("attributes") or {}
    if not plan.byline_is_rel:
        return _names_from_value(attrs.get(plan.byline))
    rels = resource.get("relationships") if isinstance(resource.get("relationships"), dict) else {}
    names: List[str] = []
    for ident in _identifiers(rels.get(plan.byline)):
        if _is_anonymous_user(ident):
            ctx.anonymous_author += 1  # Drupal's anonymous user is not a byline
            continue
        if ident.get("id") in ("missing", "virtual"):
            continue  # a reference to a deleted entity
        obj = included.get((str(ident.get("type")), str(ident.get("id"))))
        if obj is None:
            ctx.hidden_bylines += 1
            continue
        obj_attrs = obj.get("attributes") if isinstance(obj.get("attributes"), dict) else {}
        if obj.get("type") == "user--user":
            uid = obj_attrs.get("drupal_internal__uid")
            if str(uid if uid is not None else "").strip() == "0" or (
                    uid is None and _target_id(ident) is None and obj_attrs.get("display_name") == _ANONYMOUS_LABEL):
                ctx.anonymous_author += 1  # same check from the user itself, for sites without relationship meta
                continue
        elif _is_unpublished(obj_attrs):
            # An unpublished author term or person node: readers don't see it in the byline. (A signed-in
            # editor's account can.) Blocked user accounts still show as "Authored by", so users are kept.
            ctx.unpublished_bylines += 1
            continue
        for key in _NAME_KEYS:
            name = clean_text(obj_attrs.get(key)) if isinstance(obj_attrs.get(key), str) else ""
            if name:
                if name not in names:
                    names.append(name)
                break
    return names


def _body(resource: Dict[str, Any], ctx: _Context,
          included: Dict[Tuple[str, str], Dict[str, Any]]) -> Tuple[str, Optional[str]]:
    """(HTML, plain text or None). Plain text is used for plain long-text fields."""
    plan = ctx.plan
    attrs = resource.get("attributes") or {}
    if not plan.body_is_rel:
        value = attrs.get(plan.body)
        if isinstance(value, str):
            return "", value
        return "\n".join(f"<div>{h}</div>" for h in _formatted_html(value)), None
    rels = resource.get("relationships") if isinstance(resource.get("relationships"), dict) else {}
    parts: List[str] = []
    for ident in _identifiers(rels.get(plan.body)):
        if ident.get("id") in ("missing", "virtual"):
            continue
        obj = included.get((str(ident.get("type")), str(ident.get("id"))))
        if obj is None:
            ctx.hidden_body_parts += 1
            continue
        obj_attrs = obj.get("attributes") if isinstance(obj.get("attributes"), dict) else {}
        if _is_unpublished(obj_attrs):
            ctx.unpublished_body_parts += 1  # draft text, visible only to a signed-in editor's account
            continue
        for value in obj_attrs.values():
            parts.extend(_formatted_html(value))
    return "\n".join(f"<div>{h}</div>" for h in parts), None


def _to_raw(resource: Any, ctx: _Context, included: Dict[Tuple[str, str], Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(resource, dict) or not isinstance(resource.get("attributes"), dict):
        return {"_skip": _MALFORMED}
    attrs = resource["attributes"]
    if _is_unpublished(attrs):
        return {"_skip": "unpublished (draft, archived or not yet published)"}
    when = parse_date(_date_value(attrs.get(ctx.plan.date_field)))
    if when is not None and ((ctx.after and when < ctx.after) or (ctx.before and when >= ctx.before)):
        return {"_skip": "published outside the --after/--before dates"}
    # An empty field is null; a field the visitor may not view is left out of the node altogether.
    body_holder = resource.get("relationships") if ctx.plan.body_is_rel else attrs
    if not isinstance(body_holder, dict) or ctx.plan.body not in body_holder:
        return {"_skip": "article text hidden from this visitor (field permissions, e.g. a paywall)"}
    nid = _int_id(attrs.get("drupal_internal__nid"))
    uuid = resource.get("id") if isinstance(resource.get("id"), str) else ""
    key = str(nid) if nid else re.sub(r"[^A-Za-z0-9-]+", "", uuid)
    title = attrs.get("title")
    html, plain = _body(resource, ctx, included)
    raw: Dict[str, Any] = {
        "id": f"drupal-{ctx.site_key}-{key}" if key else "",
        "headline": title if isinstance(title, str) else "",
        "url": _article_url(attrs, nid, ctx.public_base),
        "authors": _authors(resource, ctx, included),
        "publish_date": when,
    }
    if plain is not None:
        raw["content"] = plain
    else:
        raw["content_html"] = html
    return raw


def _included_index(doc: Dict[str, Any]) -> Dict[Tuple[str, str], Dict[str, Any]]:
    out = {}
    for obj in doc.get("included") or []:
        if isinstance(obj, dict) and isinstance(obj.get("type"), str) and isinstance(obj.get("id"), str):
            out[(obj["type"], obj["id"])] = obj
    return out


def _omitted_hrefs(doc: Dict[str, Any]) -> List[str]:
    meta = doc.get("meta")
    omitted = meta.get("omitted") if isinstance(meta, dict) else None
    links = omitted.get("links") if isinstance(omitted, dict) else None
    if not isinstance(links, dict):
        return []
    hrefs = []
    for link in links.values():
        if isinstance(link, dict) and isinstance(link.get("meta"), dict) and link["meta"].get("rel") == "item":
            href = _href(link)
            if href:
                hrefs.append(href)
    return hrefs


def _collection_tail(collection_url: str) -> str:
    return "/" + "/".join(urllib.parse.urlsplit(collection_url).path.rstrip("/").split("/")[-2:])


def _is_primary(href: str, collection_tail: str) -> bool:
    """True for an omitted node of this collection (<api>/node/article/<uuid>), not an included entity."""
    try:
        parent = urllib.parse.urlsplit(href).path.rstrip("/").rsplit("/", 1)[0]
    except ValueError:
        return False
    return parent.endswith(collection_tail)


# ---------------------------------------------------------------- paging

def _next_offset(link: Any, offset: int, size: int) -> int:
    href = _href(link)
    if href:
        try:
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(href).query)
            value = query.get("page[offset]", [None])[0]
            if value is not None and str(value).strip().isdigit():
                return int(value)
        except ValueError:
            pass
    return offset + size


def _collection_error(e: _ApiError, url: str, ctx_type: str, have_auth: bool) -> ImporterError:
    if e.status == 400:
        return ImporterError(f"Drupal rejected the request for {ctx_type} ({e.short()}). If it names a field, "
                             "check --body-field, --byline-field and --date-field against Structure > Content "
                             "types > Manage fields.")
    if e.status in (401, 403):
        return ImporterError(_blocked_message(e, url, have_auth))
    if e.status == 404:
        return ImporterError(f"{url} was not found ({e.short()}). The content type may be disabled in JSON:API "
                             "settings.")
    if e.status and e.status >= 500:
        return ImporterError(f"{e} Large pages can time out; try a smaller --page-size, e.g. 10.")
    return e


def _get_page(client: _Client, url: str, query: Dict[str, Any], label: str,
              have_auth: bool) -> Tuple[Dict[str, Any], List[Any]]:
    try:
        doc, _ = client.get(url, query)
    except _ApiError as e:
        raise _collection_error(e, url, label, have_auth) from None
    data = doc.get("data") if isinstance(doc, dict) else None
    if not isinstance(data, list):
        raise ImporterError(f"{url} returned {type(data).__name__} instead of a list of nodes. Check --type "
                            "names a content type.")
    return doc, data


class _RepeatGuard:
    """Stops when two pages in a row hold the same rows: a cache is ignoring page[offset]."""

    def __init__(self, label: str):
        self.label, self.previous = label, None

    def check(self, doc: Dict[str, Any], data: List[Any]) -> None:
        # Hidden rows count too: a page of nothing but meta.omitted links can repeat forever.
        rows = [r.get("id") for r in data if isinstance(r, dict)] + _omitted_hrefs(doc)
        if self.previous is not None and rows and rows == self.previous:
            raise ImporterError(f"The site returned the same {self.label} nodes for two different pages: something "
                                "(often a page cache, CDN or proxy) is ignoring the page[offset] part of JSON:API "
                                "addresses. Exclude /jsonapi from that cache, then re-run.")
        if rows:
            self.previous = rows


def _following_offset(doc: Dict[str, Any], data: List[Any], offset: int, size: int, label: str) -> Optional[int]:
    """The next page's offset, or None after the last page. Never stops on a short page while a next link exists."""
    links = doc.get("links")
    if isinstance(links, dict):
        if not links.get("next"):
            return None
        new_offset = _next_offset(links["next"], offset, size)
    else:
        # No links at all (not core's behaviour): stop on a short page.
        if len(data) + len(_omitted_hrefs(doc)) < size:
            return None
        new_offset = offset + size
    if new_offset <= offset:
        raise ImporterError(f"The site's next-page link for {label} doesn't move forward (offset {offset}); "
                            "stopping rather than reading the same page again.")
    return new_offset


def _pages(client: _Client, url: str, params: Dict[str, Any], size: int, label: str,
           have_auth: bool, start: int = 0) -> Iterator[Dict[str, Any]]:
    offset, page, read = start, 1, 0
    guard = _RepeatGuard(label)
    while True:
        query = dict(params)
        query["page[offset]"] = offset
        query["page[limit]"] = size
        doc, data = _get_page(client, url, query, label, have_auth)
        guard.check(doc, data)
        read += len(data)
        meta = doc.get("meta") if isinstance(doc.get("meta"), dict) else {}
        total = meta.get("count")
        total_text = f" of {total}" if isinstance(total, int) and not isinstance(total, bool) else ""
        print(f"  {label}: page {page} ({read}{total_text} read)", file=sys.stderr)
        yield doc
        new_offset = _following_offset(doc, data, offset, size, label)
        if new_offset is None:
            return
        offset, page = new_offset, page + 1


def _probe(client: _Client, url: str, params: Dict[str, Any], size: int, label: str, have_auth: bool,
           hidden_reason: str) -> Generator[Dict[str, Any], None, Tuple[List[Dict[str, Any]], Optional[int], bool]]:
    """
    Find the first page holding nodes the visitor can see, to read the type's fields from (attribute or
    relationship), before the real requests ask for the right includes. Old nodes are often members-only, so
    this reads past pages of hidden nodes, yielding a skip for each; the main read then starts at the page
    where visible nodes were found, so nothing is read twice.

    Returns (visible nodes on that page, its offset or None when there are none, whether the collection has rows).
    """
    query = {k: v for k, v in params.items() if not k.startswith(("fields[", "include"))}
    tail = _collection_tail(url)
    offset, limit, any_rows, hidden = 0, 5, False, 0
    guard = _RepeatGuard(label)
    while True:
        query["page[offset]"] = offset
        query["page[limit]"] = limit
        doc, data = _get_page(client, url, query, label, have_auth)
        visible = [r for r in data if isinstance(r, dict) and isinstance(r.get("attributes"), dict)]
        if visible:
            return visible, offset, True
        guard.check(doc, data)
        omitted = _omitted_hrefs(doc)
        any_rows = any_rows or bool(data) or bool(omitted)
        for _ in data:
            yield {"_skip": _MALFORMED}
        for href in omitted:
            if _is_primary(href, tail):
                hidden += 1
                yield {"_skip": hidden_reason}
        new_offset = _following_offset(doc, data, offset, limit, label)
        if new_offset is None:
            return [], None, any_rows
        print(f"  {label}: the first {hidden} node(s) are hidden from this visitor; reading on to find one it "
              "can see.", file=sys.stderr)
        offset, limit = new_offset, size


def _utc_midnight(d: Optional[date]) -> Optional[datetime]:
    return datetime.combine(d, time(), tzinfo=timezone.utc) if d else None


def iter_articles(args) -> Iterator[Dict[str, Any]]:
    public_base, api_from_site = _parse_site(args.site)
    explicit_api = bool(getattr(args, "api_url", None))
    api_url = _parse_api_url(args.api_url) if explicit_api else (api_from_site or public_base + "/jsonapi")
    auth = _auth_header(args, api_url)
    after, before = getattr(args, "after", None), getattr(args, "before", None)
    if after and before and after >= before:
        raise ImporterError("--after must be an earlier date than --before.")
    after_dt, before_dt = _utc_midnight(after), _utc_midnight(before)
    page_size = getattr(args, "page_size", None) or MAX_PAGE_SIZE
    size = page_size
    limit = getattr(args, "limit", None)
    if limit and 0 < limit < size:
        size = limit
    byline = getattr(args, "byline_field", None) or "uid"
    byline = None if byline.lower() == "none" else byline
    body = getattr(args, "body_field", None) or "body"
    date_field = getattr(args, "date_field", None) or "created"

    client = _Client(auth, _origin(api_url))
    entry = _entry_point(client, api_url, explicit_api)
    print(f"Reading Drupal JSON:API at {api_url}", file=sys.stderr)
    if auth:
        _check_login(entry)
    site_key = _site_key(public_base)
    if byline == "uid":
        print("Bylines: the Drupal account in each node's \"Authored by\". If your site keeps bylines in a field, "
              "use --byline-field (see --help).", file=sys.stderr)
    elif not byline:
        print("Bylines: none (--byline-field none).", file=sys.stderr)
    if (after or before) and date_field not in _TIMESTAMP_FIELDS:
        print(f"  --after/--before are checked against {date_field} as nodes are read, so every node of the type "
              "is downloaded.", file=sys.stderr)

    contexts: List[_Context] = []
    try:
        for wanted in getattr(args, "types", None) or [DEFAULT_TYPE]:
            type_name, url = _resolve_type(entry, api_url, wanted)
            plan = _Plan(type_name, body, byline, date_field)
            params: Dict[str, Any] = {
                # Published only, asked of the server: drafts are never downloaded.
                "filter[status]": "1",
                # nid is unique and only grows: nodes created mid-import land after the current offset.
                "sort": "drupal_internal__nid",
            }
            if date_field in _TIMESTAMP_FIELDS:
                if after_dt:
                    params["filter[dewey_after][condition][path]"] = date_field
                    params["filter[dewey_after][condition][operator]"] = ">="
                    params["filter[dewey_after][condition][value]"] = str(int(after_dt.timestamp()))
                if before_dt:
                    params["filter[dewey_before][condition][path]"] = date_field
                    params["filter[dewey_before][condition][operator]"] = "<"
                    params["filter[dewey_before][condition][value]"] = str(int(before_dt.timestamp()))
            hidden_reason = ("not visible to this account (restricted content)" if auth else
                             "not visible to anonymous visitors (members-only or restricted content)")
            # page_size, not the --limit-sized page: hidden rows don't count toward --limit.
            samples, start, any_rows = yield from _probe(client, url, params, page_size, type_name, bool(auth),
                                                         hidden_reason)
            if not any_rows:
                print(f"  {type_name}: no published nodes with these settings.", file=sys.stderr)
                continue
            if start is None:
                print(f"  {type_name}: none of its published nodes are visible to this visitor.", file=sys.stderr)
                continue
            plan.classify(samples)
            params[f"fields[{type_name}]"] = ",".join(plan.node_fields())
            includes = plan.includes()
            if includes:
                params["include"] = ",".join(includes)
                if plan.byline_is_rel:
                    params["fields[user--user]"] = "display_name,drupal_internal__uid"
            ctx = _Context(site_key, public_base, plan, bool(auth), after_dt, before_dt, url)
            contexts.append(ctx)
            # Rows before `start` were all hidden, and were counted by the probe.
            for doc in _pages(client, url, params, size, type_name, bool(auth), start=start):
                included = _included_index(doc)
                for resource in doc["data"]:
                    yield _to_raw(resource, ctx, included)
                for href in _omitted_hrefs(doc):
                    if _is_primary(href, ctx.collection_tail):  # hidden included entities are counted per article
                        yield {"_skip": hidden_reason}
    finally:
        for note in _notes(contexts):
            print(note, file=sys.stderr)


def _notes(contexts: List[_Context]) -> List[str]:
    anonymous = sum(c.anonymous_author for c in contexts)
    bylines = sum(c.hidden_bylines for c in contexts)
    body_parts = sum(c.hidden_body_parts for c in contexts)
    notes = []
    if anonymous:
        notes.append(f"Note: {anonymous} article(s) are authored by Drupal's anonymous user, so they have no "
                     "byline. No other author was substituted.")
    if bylines:
        notes.append(f"Note: {bylines} byline(s) couldn't be read because Drupal hides the referenced entity from "
                     "this visitor; those articles have fewer or no authors. No other author was substituted.")
    if body_parts:
        notes.append(f"Note: {body_parts} paragraph(s) of article text were hidden from this visitor and are "
                     "missing from the imported text.")
    unpublished_bylines = sum(c.unpublished_bylines for c in contexts)
    unpublished_parts = sum(c.unpublished_body_parts for c in contexts)
    if unpublished_bylines:
        notes.append(f"Note: {unpublished_bylines} byline(s) point to an unpublished author entry, which readers "
                     "don't see, so they were left out.")
    if unpublished_parts:
        notes.append(f"Note: {unpublished_parts} unpublished paragraph(s) (draft text readers don't see) were left "
                     "out of the imported text.")
    return notes
