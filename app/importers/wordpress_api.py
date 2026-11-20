"""
WordPress REST API importer: published posts from a live WordPress site.

    python app/import_archive.py wordpress-api --site https://www.example.com --limit 20

Reads <site>/wp-json/wp/v2/posts page by page (oldest post first). Published,
public posts need no login. An Application Password (WordPress 5.6+) is only
needed for --include-private, or for sites that close the REST API to
anonymous readers. Run `python app/import_archive.py wordpress-api --help` for
where to find each value in WordPress.

Bylines come from Co-Authors Plus when the site exposes its coauthors/v1 API
(one extra request per post), otherwise from the WordPress post author. A post
whose byline can't be read gets `authors: []`; no other name is substituted.

Standard library only.
"""
import argparse
import base64
import html
import os
import re
import sys
import urllib.parse
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Any, Dict, Iterator, List, Optional, Tuple
from zoneinfo import ZoneInfo

from importers.common import ImporterError, clean_text, http_get, http_get_json, parse_date

NAME = "wordpress-api"
HELP = "Published posts from a live WordPress site, read through its REST API (no login needed)."

USER_ENV = "WORDPRESS_USER"
PASSWORD_ENV = "WORDPRESS_APP_PASSWORD"
MAX_PER_PAGE = 100
FALLBACK_PER_PAGE = 10  # WordPress's own default page size, accepted everywhere
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
_INDEX_FIELDS = "name,url,home,gmt_offset,timezone_string,namespaces"
_POST_FIELDS = ["id", "date", "date_gmt", "link", "title", "content", "status", "author",
                "coauthors", "_links", "_embedded"]

_EPILOG = f"""\
where to find each value:
  --site          The address readers use, e.g. https://www.example.com. To check
                  it, open https://www.example.com/wp-json/ in a browser: you should
                  see JSON starting with {{"name": ...}}. If WordPress lives in a
                  subfolder (https://example.com/news), include the subfolder.
  --api-root      Only if that check shows an error page. View the source of your
                  home page and copy the href of <link rel="https://api.w.org/" ...>
                  (often https://www.example.com/?rest_route=/).
  --post-type     Open <site>/wp-json/wp/v2/types and use the "rest_base" of each
                  type you want. The default is posts; a custom "Stories" type might
                  be story or stories.
  --user          Your WordPress username: wp-admin > Users > Profile, "Username".
  --app-password  wp-admin > Users > Profile > Application Passwords: type a name
                  such as Dewey, click "Add Application Password" ("Add New
                  Application Password" before WordPress 6.8) and copy the
                  code (it is shown only once). It is not your login password. Needs
                  WordPress 5.6+ and an https:// site; if the section is missing, a
                  plugin or your host has turned Application Passwords off.
  Prefer the environment variables {USER_ENV} and {PASSWORD_ENV}
  to the flags: flags are saved in your shell history.

what is imported:
  Published, public posts only: drafts, scheduled and trashed posts are never
  requested, and password-protected posts are skipped and counted in the report.
  Private posts are imported only with --include-private and an Application
  Password for an account that can read them. Text hidden by a paywall plugin is not visible to the API: what an
  anonymous reader gets is what is imported. Posts WordPress counts but a plugin keeps
  out of the API are listed in the report as not returned.
  Bylines come from Co-Authors Plus when the site uses it (one extra request per
  post), otherwise from the WordPress post author, or from --byline-field. A post
  whose byline can't be read gets no author rather than a guessed one.

large archives:
  Try --limit 20 first. For a very large site, import a year at a time with
  --after and --before; each date window gets its own output files.

examples:
  python app/import_archive.py wordpress-api --site https://www.example.com --limit 20
  python app/import_archive.py wordpress-api --site https://www.example.com \\
      --after 2023-01-01 --before 2024-01-01
  export {USER_ENV}=editor
  read -rs {PASSWORD_ENV} && export {PASSWORD_ENV}   # paste it; not shown or saved
  python app/import_archive.py wordpress-api --site https://www.example.com --include-private
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


def _rest_base_arg(value: str) -> str:
    value = value.strip().strip("/")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise argparse.ArgumentTypeError(f"expected a REST name such as posts or story, got {value!r}")
    return value


def _field_path_arg(value: str) -> str:
    value = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)*", value):
        raise argparse.ArgumentTypeError(f"expected a field name such as acf.byline, got {value!r}")
    return value


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.formatter_class = argparse.RawDescriptionHelpFormatter
    parser.epilog = _EPILOG
    parser.add_argument("--site", required=True, metavar="URL",
                        help="your site's public address, e.g. https://www.example.com (see below)")
    parser.add_argument("--api-root", metavar="URL",
                        help="the REST API root, only if it isn't <site>/wp-json/ and isn't found automatically")
    parser.add_argument("--post-type", dest="post_types", action="append", type=_rest_base_arg,
                        metavar="REST_BASE",
                        help="post type to import, by its REST name (default: posts); repeat for several")
    parser.add_argument("--after", type=_date_arg, metavar="YYYY-MM-DD",
                        help="only posts published on or after this date (the site's local time)")
    parser.add_argument("--before", type=_date_arg, metavar="YYYY-MM-DD",
                        help="only posts published before this date (the site's local time)")
    parser.add_argument("--byline-field", type=_field_path_arg, metavar="FIELD",
                        help="read bylines from this field of each post first, e.g. acf.byline, for sites "
                             "that keep bylines in a custom field exposed to the REST API")
    parser.add_argument("--user", metavar="USERNAME",
                        help=f"WordPress username for the Application Password (or set {USER_ENV})")
    parser.add_argument("--app-password", metavar="PASSWORD",
                        help=f"Application Password (better: set {PASSWORD_ENV}, since flags are "
                             "saved in shell history)")
    parser.add_argument("--include-private", action="store_true",
                        help="also import private posts (needs an Application Password for an account "
                             "that can read them)")
    parser.add_argument("--per-page", type=_per_page_arg, default=MAX_PER_PAGE, metavar="N",
                        help=f"posts per API request, 1-{MAX_PER_PAGE} (default: {MAX_PER_PAGE}; "
                             "lower it if the site times out)")


def default_prefix(args) -> str:
    """wordpress-<site>[-<types>][-after-YYYYMMDD][-before-YYYYMMDD]; stable for the same settings."""
    parts = ["wordpress", _site_key(_lenient_url(getattr(args, "site", "") or "")) or "site"]
    types = getattr(args, "post_types", None) or ["posts"]
    if types != ["posts"]:
        parts.extend(types)
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
    """'https://www.example.com/news/' -> 'example-com-news'. Used in ids and file names."""
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


def _origin(url: str) -> Tuple[str, str]:
    parts = urllib.parse.urlsplit(url)
    return parts.scheme.lower(), parts.netloc.lower()


def _hide_userinfo(raw: str) -> str:
    """The address for messages, with any 'user:password@' part masked so a password is never echoed."""
    return re.sub(r"(^|//)[^/@\s]*@", r"\1***@", raw or "", count=1)


def _refuse_userinfo(raw: str, flag: str) -> None:
    """A user:password@ part would be printed in progress and error messages, and urllib can't use it."""
    netloc = urllib.parse.urlsplit(_lenient_url(raw)).netloc
    if "@" in netloc:
        raise ImporterError(f"{flag} should be a web address without a username or password (got "
                            f"{_hide_userinfo(raw)!r}). WordPress logins go in {USER_ENV} and {PASSWORD_ENV} "
                            "(see --help). A site behind an extra server password, as staging sites often "
                            "are, can't be read through the API: use the wordpress-export importer.")


def _parse_site(raw: str) -> Tuple[str, Optional[str]]:
    """Validate --site. Returns (site base URL without trailing slash, API root if the URL named one)."""
    _refuse_userinfo(raw, "--site")
    url = _lenient_url(raw)
    try:
        parts = urllib.parse.urlsplit(url)
        parts.port  # noqa: B018  (raises ValueError on a bad port)
    except ValueError:
        parts = None
    if not parts or parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise ImporterError(f"--site should be your site's web address, like https://www.example.com "
                            f"(got {_hide_userinfo(raw)!r}).")
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    path = parts.path
    api_root = None
    if any(k == "rest_route" for k, _ in query):
        path = re.sub(r"/index\.php$", "", path.rstrip("/"))
        api_root = urllib.parse.urlunsplit((parts.scheme, parts.netloc, (path or "") + "/", "rest_route=/", ""))
    else:
        m = re.search(r"/wp-json(?:/|$)", path)
        if m:
            path = path[:m.start()]
            api_root = urllib.parse.urlunsplit((parts.scheme, parts.netloc, path + "/wp-json/", "", ""))
    site = urllib.parse.urlunsplit((parts.scheme.lower(), parts.netloc, path.rstrip("/"), "", ""))
    return site, api_root


def _parse_api_root(raw: str) -> str:
    _refuse_userinfo(raw, "--api-root")
    url = _lenient_url(raw)
    parts = urllib.parse.urlsplit(url)
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        raise ImporterError(f"--api-root should be a web address like https://www.example.com/wp-json/ "
                            f"(got {_hide_userinfo(raw)!r}).")
    if "rest_route=" in parts.query:
        query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True) if k != "rest_route"]
        query.append(("rest_route", "/"))
        return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query, safe="/"), fragment=""))
    return urllib.parse.urlunsplit(parts._replace(path=parts.path.rstrip("/") + "/", query="", fragment=""))


class _Api:
    """A REST API root: either path style (/wp-json/) or query style (?rest_route=/)."""

    def __init__(self, root: str):
        self.root = root
        parts = urllib.parse.urlsplit(root)
        query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        self.query_style = any(k == "rest_route" for k, _ in query)
        self._parts = parts._replace(fragment="")
        self._extra = [(k, v) for k, v in query if k != "rest_route"]
        self.origin = _origin(root)

    def url(self, route: str) -> str:
        if self.query_style:
            query = urllib.parse.urlencode(self._extra + [("rest_route", route)], safe="/")
            return urllib.parse.urlunsplit(self._parts._replace(query=query))
        return urllib.parse.urlunsplit(self._parts._replace(path=self._parts.path.rstrip("/") + route,
                                                            query=""))


# ---------------------------------------------------------------- HTTP

class _WPError(ImporterError):
    """ImporterError plus what could be read from it: HTTP status and WordPress error code."""

    def __init__(self, message: str, status: Optional[int] = None, code: Optional[str] = None,
                 kind: str = "http"):
        super().__init__(message)
        self.status, self.code, self.kind = status, code, kind

    def short(self) -> str:
        if self.status:
            return f"HTTP {self.status}" + (f" {self.code}" if self.code else "")
        return {"network": "site unreachable", "not_json": "response was not JSON"}.get(self.kind, self.kind)


_STATUS_RE = re.compile(r"^HTTP (\d{3}) ")
_CODE_RE = re.compile(r'"code"\s*:\s*"([A-Za-z0-9_.-]+)"')


class _Client:
    """http_get_json with the Authorization header sent only to the one origin the user named."""

    def __init__(self, auth_header: Optional[str], trusted_origin: Tuple[str, str], user: Optional[str] = None):
        self.auth = auth_header
        self.trusted_origin = trusted_origin
        self.user = user

    def get(self, url: str, params: Optional[Dict[str, Any]] = None, retries: int = 4,
            use_auth: bool = True) -> Tuple[Any, Dict[str, str]]:
        headers = {}
        if use_auth and self.auth and _origin(url) == self.trusted_origin:
            headers["Authorization"] = self.auth
        try:
            return http_get_json(url, params, headers, retries=retries)
        except ImporterError as e:
            msg = str(e)
            m = _STATUS_RE.match(msg)
            code = _CODE_RE.search(msg)
            kind = "http" if m else ("not_json" if "did not return JSON" in msg else "network")
            raise _WPError(msg, int(m.group(1)) if m else None, code.group(1) if code else None, kind) from None


# ---------------------------------------------------------------- discovery and login

def _probe(client: _Client, api: _Api) -> Dict[str, Any]:
    """
    The API index if `api` is a WordPress REST root, else raise _WPError.
    Asks without credentials first (nothing secret is needed to find the API),
    and again with them only if the site closes the API to anonymous readers.
    """
    try:
        data, _ = client.get(api.url("/"), {"_fields": _INDEX_FIELDS}, retries=1, use_auth=False)
    except _WPError as e:
        if not (client.auth and e.status in (401, 403) and api.origin == client.trusted_origin):
            raise
        try:
            data, _ = client.get(api.url("/"), {"_fields": _INDEX_FIELDS}, retries=1)
        except _WPError as e2:
            _raise_login_problem(e2, client.user)
            raise
    if not isinstance(data, dict):
        raise _WPError(f"{api.root} is not a WordPress REST API index", kind="not_wp")
    namespaces = data.get("namespaces")
    if isinstance(namespaces, list):
        if "wp/v2" not in namespaces:
            raise ImporterError(f"{api.root} is a WordPress REST API, but its wp/v2 routes (where posts are) "
                                "are turned off. Ask your site administrator, or export the archive from "
                                "wp-admin > Tools > Export and use the wordpress-export importer.")
        return data
    if any(k in data for k in ("home", "url", "name", "routes")):
        return data  # an index trimmed by a plugin; posts may still work
    raise _WPError(f"{api.root} is not a WordPress REST API index", kind="not_wp")


def _api_link_from_home(site: str) -> Optional[str]:
    """WordPress advertises its API root on every page: Link: <...>; rel="https://api.w.org/"."""
    try:
        body, headers = http_get(site + "/", retries=1)
    except ImporterError:
        return None
    m = re.search(r'<([^>]+)>\s*;\s*rel="?https://api\.w\.org/"?', headers.get("link", ""))
    href = m.group(1) if m else None
    if not href:
        text = body[:500_000].decode("utf-8", "replace")
        for tag in re.findall(r"<link\b[^>]*>", text, flags=re.I):
            if re.search(r"""rel\s*=\s*["']https://api\.w\.org/["']""", tag, flags=re.I):
                h = re.search(r"""href\s*=\s*["']([^"']+)["']""", tag, flags=re.I)
                href = h.group(1) if h else None
                break
    if not href:
        return None
    root = urllib.parse.urljoin(site + "/", html.unescape(href).strip())
    return root if root.lower().startswith(("http://", "https://")) else None


def _blocked_message(e: _WPError, root: str, have_auth: bool) -> str:
    login = ("Check the account has access." if have_auth else
             "If a security plugin limits the API to logged-in users, pass --user and --app-password.")
    return (f"The WordPress REST API at {root} refused the request ({e.short()}). {login} Otherwise export "
            "the archive from wp-admin > Tools > Export and use the wordpress-export importer.")


def _discover(client: _Client, site: str, explicit_root: Optional[str]) -> Tuple[_Api, Dict[str, Any]]:
    have_auth = bool(client.auth)
    if explicit_root:
        api = _Api(explicit_root)
        try:
            return api, _probe(client, api)
        except _WPError as e:
            if e.status in (401, 403):
                raise ImporterError(_blocked_message(e, api.root, have_auth)) from None
            raise ImporterError(f"No WordPress REST API at {api.root} ({e.short()}). Open that address in a "
                                "browser: it should show JSON starting with {\"name\": ...}.") from None
    tried: List[str] = []
    candidates = [site + "/wp-json/"]
    while candidates:
        api = _Api(candidates.pop(0))
        if api.root in tried:
            continue
        tried.append(api.root)
        try:
            return api, _probe(client, api)
        except _WPError as e:
            if e.kind == "network":
                raise ImporterError(f"Couldn't reach {site}. Check the address and your internet "
                                    f"connection. ({e})") from None
            if e.status in (401, 403) and e.code:
                raise ImporterError(_blocked_message(e, api.root, have_auth)) from None
        if len(tried) == 1:
            found = _api_link_from_home(site)
            if found:
                candidates.append(found)
            candidates.append(site + "/?rest_route=/")
    raise ImporterError(f"Couldn't find a WordPress REST API for {site} (tried {', '.join(tried)}). Check "
                        "--site is the address readers use. If the site's REST API is turned off, export the "
                        "archive from wp-admin > Tools > Export and use the wordpress-export importer.")


def _raise_login_problem(e: _WPError, user: Optional[str]) -> None:
    """Turn WordPress's Application Password error codes into advice; return if it isn't one."""
    if e.code == "invalid_username":
        why = (f"WordPress doesn't know the username {user!r}. Use the Username shown at "
               "wp-admin > Users > Profile.")
    elif e.code in ("incorrect_password", "invalid_email"):
        why = (f"WordPress rejected the Application Password for {user!r}. Create a new one at wp-admin > "
               "Users > Profile > Application Passwords and copy it exactly (it is shown once); your "
               "normal login password doesn't work here.")
    elif e.code == "application_passwords_disabled":
        why = "Application Passwords are turned off on this site; ask your site administrator."
    else:
        return
    raise ImporterError(why) from None


def _check_login(client: _Client, api: _Api, user: str) -> None:
    try:
        me, _ = client.get(api.url("/wp/v2/users/me"), retries=1)
    except _WPError as e:
        _raise_login_problem(e, user)
        if e.status not in (401, 403):
            raise
        raise ImporterError(f"The site ignored the login ({e.short()}). Either Application Passwords are "
                            "turned off (some security plugins do this), or the web server drops the "
                            "Authorization header before WordPress sees it (common with Apache and PHP-CGI; "
                            "your host can fix it).") from None
    name = clean_text(me.get("name")) if isinstance(me, dict) else ""
    print(f"Signed in to WordPress as {name or user}.", file=sys.stderr)


def _auth_header(args, trusted_url: str) -> Tuple[Optional[str], Optional[str]]:
    user = (getattr(args, "user", None) or os.environ.get(USER_ENV) or "").strip()
    password = getattr(args, "app_password", None) or os.environ.get(PASSWORD_ENV) or ""
    # Application Passwords are shown in groups of four; the spaces are display only.
    password = re.sub(r"\s+", "", password)
    if getattr(args, "include_private", False) and not (user and password):
        raise ImporterError(f"--include-private needs an Application Password: set {USER_ENV} and "
                            f"{PASSWORD_ENV} (or pass --user and --app-password). See --help for where "
                            "to create one.")
    if bool(user) != bool(password):
        missing = f"the Application Password ({PASSWORD_ENV} or --app-password)" if user else \
            f"the username ({USER_ENV} or --user)"
        raise ImporterError(f"Give both a username and an Application Password; {missing} is missing.")
    if not user:
        return None, None
    parts = urllib.parse.urlsplit(trusted_url)
    if parts.scheme.lower() != "https" and (parts.hostname or "").lower() not in _LOCAL_HOSTS:
        raise ImporterError(f"Refusing to send your Application Password over plain http to {parts.netloc}. "
                            "Use the https:// address of your site.")
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return "Basic " + token, user


# ---------------------------------------------------------------- dates

def _site_timezone(index: Dict[str, Any]) -> Optional[tzinfo]:
    """The site's time zone from its settings (index timezone_string, else gmt_offset)."""
    name = index.get("timezone_string")
    if isinstance(name, str) and name.strip():
        try:
            return ZoneInfo(name.strip())
        except (ValueError, KeyError, OSError):
            pass
    offset = index.get("gmt_offset")
    if offset is None or offset == "" or isinstance(offset, bool):
        return None
    try:
        hours = float(offset)
    except (TypeError, ValueError):
        return None
    if not -14 <= hours <= 14:
        return None
    return timezone(timedelta(hours=hours))


def _publish_date(post: Dict[str, Any], site_tz: Optional[tzinfo]) -> Tuple[Optional[datetime], Optional[str]]:
    """(UTC datetime, None), or (None, skip reason), or (None, None) to let normalize() count it."""
    # Only text is a date here: parse_date reads numbers (and JSON false, a bool) as Unix timestamps,
    # and WordPress sends "date_gmt": false when the stored value is empty (mysql_to_rfc3339('')).
    gmt_value = post.get("date_gmt")
    gmt = parse_date(gmt_value, assume_utc=True) if isinstance(gmt_value, str) else None
    if gmt is not None:
        return gmt, None
    local = post.get("date")
    if not isinstance(local, str) or not local.strip():
        return None, None
    aware = parse_date(local, assume_utc=False)
    if aware is not None:
        return aware, None
    try:
        naive = datetime.fromisoformat(str(local).strip())
    except ValueError:
        return None, None
    if site_tz is None:
        return None, "no UTC publish date (date_gmt) and the site's time zone is unknown"
    return naive.replace(tzinfo=site_tz).astimezone(timezone.utc), None


# ---------------------------------------------------------------- bylines

def _int_id(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip()) or None
    return None


def _dig(obj: Any, path: List[str]) -> Any:
    for key in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


_BY_PREFIX = re.compile(r"^\s*(?:by|written by|story by)[:\s]+", re.I)


def _split_byline(text: str) -> List[str]:
    """
    'By Jane Doe and Sam Lee' -> ['Jane Doe', 'Sam Lee']. Commas split only a list with 'and'/'&'.
    Same rules as wordpress_export._split_byline: both importers write the same ids, so importing a
    post one way after the other must not change its authors (author filters match exact names).
    """
    text = _BY_PREFIX.sub("", clean_text(text))
    if not text:
        return []
    has_conjunction = re.search(r"\s(?:and|&)\s", text, flags=re.I)
    parts = re.split(r"\s*(?:;|\||\s&\s|\sand\s" + (r"|," if has_conjunction else "") + r")\s*", text,
                     flags=re.I)
    return [p.strip(" ,") for p in parts if p and p.strip(" ,")]


def _names_from_value(value: Any, split: bool = True) -> List[str]:
    """
    Names from a custom byline field: text ("By Jane Doe and Sam Lee" is two names), a list of text,
    or objects with display_name/name (one person each, so never split).
    """
    if isinstance(value, str):
        if split:
            return _split_byline(value)
        name = _BY_PREFIX.sub("", clean_text(value)).strip()
        return [name] if name else []
    if isinstance(value, dict):
        for key in ("display_name", "name", "rendered", "title"):
            if value.get(key):
                return _names_from_value(value[key], split=False)
        return []
    if isinstance(value, list):
        names: List[str] = []
        for item in value:
            for name in _names_from_value(item, split):
                if name not in names:
                    names.append(name)
        return names
    return []  # numbers (term or user ids) are not names


class _Bylines:
    def __init__(self, client: _Client, api: _Api, coauthors_api: bool, byline_field: Optional[str]):
        self.client, self.api = client, api
        self.coauthors_api = coauthors_api
        self.field_path = byline_field.split(".") if byline_field else []
        self.users: Dict[int, List[str]] = {}
        self.user_ok = 0
        self.user_failures = 0
        self.users_blocked = False
        self.cap_ok = 0
        self.cap_consecutive_failures = 0
        self.cap_disabled = False
        self.cap_error = ""
        self.cap_missing = 0
        self.hidden = 0
        self.announced = False

    def resolve(self, post: Dict[str, Any]) -> List[str]:
        if self.field_path:
            names = _names_from_value(_dig(post, self.field_path))
            if names:
                return names
        terms = post.get("coauthors")
        if isinstance(terms, list):
            uses_coauthors = len(terms) > 0  # [] means no Co-Authors Plus byline: the post author is shown
        else:
            uses_coauthors = self.coauthors_api
        if uses_coauthors:
            names = self._coauthors(post.get("id"))
            if names is None:
                self.cap_missing += 1
                return []
            if names:
                return names
        return self._core_author(post)

    def _coauthors(self, post_id: Any) -> Optional[List[str]]:
        pid = _int_id(post_id)
        if not self.coauthors_api:
            self.cap_error = self.cap_error or "the site doesn't offer the coauthors/v1 API"
            return None
        if self.cap_disabled or pid is None:
            return None
        if not self.announced:
            self.announced = True
            print("Co-Authors Plus found: reading bylines with one extra request per post.", file=sys.stderr)
        try:
            data, _ = self.client.get(self.api.url("/coauthors/v1/coauthors"), {"post_id": pid}, retries=2)
            if not isinstance(data, list):
                raise _WPError("coauthors/v1 did not return a list", kind="unexpected response")
        except _WPError as e:
            self.cap_error = e.short()
            self.cap_consecutive_failures += 1
            if not self.cap_ok and self.cap_consecutive_failures >= 3:
                self.cap_disabled = True
                print(f"  Co-Authors Plus bylines can't be read ({self.cap_error}); posts that use them will "
                      "have no authors.", file=sys.stderr)
            return None
        self.cap_ok += 1
        self.cap_consecutive_failures = 0
        names: List[str] = []
        for entry in data:
            if isinstance(entry, dict):
                name = clean_text(entry.get("display_name") or entry.get("name"))
                if name and name not in names:
                    names.append(name)
        return names

    def _core_author(self, post: Dict[str, Any]) -> List[str]:
        embedded = post.get("_embedded")
        entries = embedded.get("author") if isinstance(embedded, dict) else None
        if isinstance(entries, list) and entries:
            names = [clean_text(a.get("name")) for a in entries if isinstance(a, dict) and "code" not in a]
            names = [n for n in names if n]
            if names:
                return names
            if any(isinstance(a, dict) and "code" in a for a in entries):
                self.hidden += 1  # embedding was refused: the users route is blocked
                return []
        uid = _int_id(post.get("author"))
        if uid is None:
            return []
        return self._user(uid)

    def _user(self, uid: int) -> List[str]:
        if uid in self.users:
            if not self.users[uid]:
                self.hidden += 1
            return list(self.users[uid])
        if self.users_blocked:
            self.hidden += 1
            return []
        try:
            data, _ = self.client.get(self.api.url(f"/wp/v2/users/{uid}"), retries=2)
        except _WPError as e:
            self.hidden += 1
            if e.status and 400 <= e.status < 500:
                self.users[uid] = []
                self.user_failures += 1
                if not self.user_ok and self.user_failures >= 3:
                    self.users_blocked = True
            return []
        name = clean_text(data.get("name")) if isinstance(data, dict) else ""
        self.users[uid] = [name] if name else []
        self.user_ok += 1
        return list(self.users[uid])

    def notes(self) -> List[str]:
        notes = []
        if self.cap_missing:
            notes.append(f"Note: {self.cap_missing} post(s) have no authors because their Co-Authors Plus "
                         f"bylines couldn't be read ({self.cap_error}). No other author was substituted. If "
                         "the site blocks /wp-json/coauthors/v1/ for anonymous readers, allow it or pass "
                         "--user and --app-password, then re-run.")
        if self.hidden:
            notes.append(f"Note: {self.hidden} post(s) have no authors because the site hides author details "
                         "from the REST API (often a security plugin blocking /wp-json/wp/v2/users). Allow "
                         "author names in the API, use --byline-field if bylines are in a custom field, or "
                         "use the wordpress-export importer.")
        return notes


# ---------------------------------------------------------------- posts

def _status_reason(status: str) -> str:
    s = clean_text(status)[:40].lower()
    if s == "private":
        return "private post (use --include-private with an Application Password to import)"
    if s in ("draft", "auto-draft", "pending"):
        return "draft or pending review (not published)"
    if s == "future":
        return "scheduled, not yet published"
    if s == "trash":
        return "in the trash"
    return f"status '{s}' (only published posts are imported)"


class _Context:
    def __init__(self, site_key: str, site_tz: Optional[tzinfo], bylines: _Bylines, include_private: bool):
        self.site_key, self.site_tz, self.bylines, self.include_private = site_key, site_tz, bylines, include_private


def _to_raw(post: Any, ctx: _Context) -> Dict[str, Any]:
    if not isinstance(post, dict):
        return {"_skip": "malformed entry in the API response (not a post)"}
    status = post.get("status")
    status = "publish" if status in (None, "") else str(status)
    if status != "publish" and not (status == "private" and ctx.include_private):
        return {"_skip": _status_reason(status)}
    content = post.get("content")
    if isinstance(content, dict):
        if content.get("protected"):
            return {"_skip": "password-protected (its text isn't public)"}
        body = content.get("rendered")
    else:
        body = content
    title = post.get("title")
    headline = title.get("rendered") if isinstance(title, dict) else title
    if status == "private" and isinstance(headline, str):
        # WordPress displays private titles as "Private: <title>" (English sites); that's not the headline.
        headline = re.sub(r"^Private:\s+", "", headline)
    when, reason = _publish_date(post, ctx.site_tz)
    if reason:
        return {"_skip": reason}
    pid = _int_id(post.get("id"))
    link = post.get("link")
    raw = {
        "id": f"wp-{ctx.site_key}-{pid}" if pid else "",
        "headline": headline if isinstance(headline, str) else "",
        "content_html": body if isinstance(body, str) else "",
        "url": link.strip() if isinstance(link, str) else "",
        "authors": [],
        "publish_date": when,
    }
    # Only spend byline requests on posts normalize() will keep.
    if pid and raw["headline"].strip() and raw["url"] and raw["content_html"].strip():
        raw["authors"] = ctx.bylines.resolve(post)
    return raw


def _header_int(headers: Dict[str, str], name: str) -> Optional[int]:
    try:
        return int(str(headers.get(name, "")).strip())
    except ValueError:
        return None


def _unknown_type(client: _Client, api: _Api, rest_base: str) -> ImporterError:
    offered = ""
    try:
        types, _ = client.get(api.url("/wp/v2/types"), retries=1)
        if isinstance(types, dict):
            bases = sorted({str(t.get("rest_base")) for t in types.values()
                            if isinstance(t, dict) and t.get("rest_base")})
            offered = " Types this site offers: " + ", ".join(bases) + "." if bases else ""
    except _WPError:
        pass
    return ImporterError(f"This site has no REST route for post type {rest_base!r}.{offered} Pass the type's "
                         "rest_base to --post-type (see <site>/wp-json/wp/v2/types).")


_WITHHELD_REASON = "counted by WordPress but not returned by its API (often a membership or paywall plugin)"


def _paginate(client: _Client, api: _Api, rest_base: str, base_params: Dict[str, Any],
              per_page: int, label: str, stats: Optional[Dict[str, int]] = None) -> Iterator[Any]:
    """
    Every item of every page. While the site's counts (X-WP-Total, X-WP-TotalPages) agree with what
    it returns, they decide where the list ends: a page that comes back short or empty inside that
    range means a plugin dropped posts after WordPress counted them, and later pages still hold
    posts. How many never arrived goes in stats["withheld"]. Without usable counts, a short or
    empty page is the end.
    """
    route = f"/wp/v2/{rest_base}"
    page, fetched, previous_ids = 1, 0, None
    total_pages: Optional[int] = None
    total: Optional[int] = None
    counts_usable = True
    while True:
        params = dict(base_params, page=page, per_page=per_page)
        try:
            data, headers = client.get(api.url(route), params)
        except _WPError as e:
            if page > 1 and e.code == "rest_post_invalid_page_number":
                return  # asked one page past the end (no X-WP-TotalPages to tell us)
            if page == 1 and (e.code == "rest_no_route" or e.status == 404):
                raise _unknown_type(client, api, rest_base) from None
            if page == 1 and e.code == "rest_invalid_param" and "per_page" in str(e) and per_page > FALLBACK_PER_PAGE:
                print(f"  The site doesn't accept --per-page {per_page}; using {FALLBACK_PER_PAGE}.", file=sys.stderr)
                per_page = FALLBACK_PER_PAGE
                continue
            if e.code == "rest_forbidden_status":
                raise ImporterError("This WordPress account can't list private posts. Use an account whose "
                                    "role can read them (such as Editor or Administrator), or drop "
                                    "--include-private.") from None
            if e.status in (401, 403):
                _raise_login_problem(e, client.user)
                raise ImporterError(_blocked_message(e, api.root, bool(client.auth))) from None
            if e.status and e.status >= 500:
                raise ImporterError(f"{e} Large pages can time out; try a smaller --per-page, e.g. 20.") from None
            raise
        if not isinstance(data, list):
            raise ImporterError(f"{api.url(route)} returned {type(data).__name__} instead of a list of posts. "
                                "A plugin may be changing the REST API; try the wordpress-export importer.")
        latest_total = _header_int(headers, "x-wp-total")
        latest_pages = _header_int(headers, "x-wp-totalpages")
        if latest_pages is None and latest_total is not None:
            latest_pages = -(-latest_total // per_page)
        if counts_usable and data and latest_pages is not None and latest_pages < page:
            # Posts on a page the site's own count says doesn't exist (e.g. a plugin turning off
            # WordPress's row count sends 0 and 0): the counts can't say where the list ends.
            counts_usable, total, total_pages = False, None, None
            print(f"  The site's post counts (X-WP-Total) don't match the {rest_base} it returns; reading "
                  "until a page comes back short instead.", file=sys.stderr)
        elif counts_usable:
            # Re-read each page: posts published mid-import extend the run.
            total = latest_total if latest_total is not None else total
            total_pages = latest_pages if latest_pages is not None else total_pages
        if page == 1:
            if not data and not total:
                print(f"  The API lists no {label} {rest_base} with these settings.", file=sys.stderr)
            elif total:
                print(f"  {total} {label} {rest_base} to read.", file=sys.stderr)
        ids = [p.get("id") for p in data if isinstance(p, dict)]
        if previous_ids is not None and ids and ids == previous_ids:
            raise ImporterError(f"The site returned the same {rest_base} for pages {page - 1} and {page}: "
                                "something (often a page cache or CDN) is ignoring the page number in API "
                                "requests. Exclude /wp-json/ from the cache, or use the wordpress-export importer.")
        previous_ids = ids
        fetched += len(data)
        print(f"  {rest_base}: page {page}/{total_pages or '?'} ({fetched} read)", file=sys.stderr)
        for post in data:
            yield post
        if total_pages is not None:
            if page >= total_pages:
                break
            # Short or empty but inside the counted range: posts were dropped after counting. Go on.
        elif not data or len(data) < per_page:
            break
        page += 1
    withheld = total - fetched if total is not None else 0
    if withheld > 0:
        print(f"  {withheld} of the {total} {label} {rest_base} WordPress counted were not returned by its API, "
              "usually because a membership or paywall plugin hides them from API readers. They are not in "
              "this import; the wordpress-export importer can read them from an export file.", file=sys.stderr)
        if stats is not None:
            stats["withheld"] = stats.get("withheld", 0) + withheld


def iter_articles(args) -> Iterator[Dict[str, Any]]:
    site, root_from_site = _parse_site(args.site)
    explicit_root = _parse_api_root(args.api_root) if getattr(args, "api_root", None) else root_from_site
    trusted_url = explicit_root or site
    auth, user = _auth_header(args, trusted_url)
    after, before = getattr(args, "after", None), getattr(args, "before", None)
    if after and before and after >= before:
        raise ImporterError("--after must be an earlier date than --before.")
    include_private = bool(getattr(args, "include_private", False))
    per_page = getattr(args, "per_page", None) or MAX_PER_PAGE
    limit = getattr(args, "limit", None)
    if limit and 0 < limit < per_page:
        per_page = limit

    client = _Client(auth, _origin(trusted_url), user)
    api, index = _discover(client, site, explicit_root)
    if auth and api.origin != client.trusted_origin:
        raise ImporterError(f"The REST API for {site} is at {api.root}, a different address. If you trust it "
                            f"with your Application Password, pass --api-root {api.root}")
    site_name = clean_text(index.get("name"))
    print(f"Reading the WordPress REST API at {api.root}" + (f" ({site_name})" if site_name else ""),
          file=sys.stderr)
    if auth:
        _check_login(client, api, user)

    home = index.get("home") or index.get("url")
    site_key = (_site_key(home) if isinstance(home, str) else "") or _site_key(site)
    namespaces = index.get("namespaces") if isinstance(index.get("namespaces"), list) else []
    byline_field = getattr(args, "byline_field", None)
    bylines = _Bylines(client, api, "coauthors/v1" in namespaces, byline_field)
    ctx = _Context(site_key, _site_timezone(index), bylines, include_private)

    fields = list(_POST_FIELDS)
    if byline_field and byline_field.split(".")[0] not in fields:
        fields.append(byline_field.split(".")[0])
    params: Dict[str, Any] = {
        "status": "publish,private" if include_private else "publish",
        "orderby": "id", "order": "asc",  # unique and stable: posts published mid-import land at the end
        "_embed": "author",
        "_fields": ",".join(fields),
    }
    if after:
        # WordPress's `after` is exclusive; one second earlier makes --after inclusive.
        params["after"] = (datetime.combine(after, time()) - timedelta(seconds=1)).isoformat()
    if before:
        params["before"] = datetime.combine(before, time()).isoformat()
    label = "published or private" if include_private else "published"

    post_types = getattr(args, "post_types", None) or ["posts"]
    stats: Dict[str, int] = {}
    listed = withheld_total = 0
    try:
        for rest_base in post_types:
            for post in _paginate(client, api, rest_base, params, per_page, label, stats):
                listed += 1
                yield _to_raw(post, ctx)
            # So the report counts posts the site counted but never sent, instead of losing them quietly.
            withheld = stats.pop("withheld", 0)
            withheld_total += withheld
            for _ in range(withheld):
                yield {"_skip": _WITHHELD_REASON}
        if not listed:
            # Raise rather than finish empty: a finished import replaces the earlier files under the
            # same prefix, so a site that answers [] for a while would wipe a good archive.
            why = (f" WordPress counted {withheld_total}, but its API returned none of them (a membership or "
                   "paywall plugin may hide them from API readers; the wordpress-export importer can read "
                   "them from an export file)." if withheld_total else
                   " Check --site, --post-type, --after and --before.")
            raise ImporterError(f"The API listed no {label} {', '.join(post_types)} with these settings.{why}")
    finally:
        for note in bylines.notes():
            print(note, file=sys.stderr)
