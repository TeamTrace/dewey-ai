import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "app"))

import http.client  # noqa: E402
import json  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402
import unittest  # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402
from unittest import mock  # noqa: E402

from importers import common  # noqa: E402
from importers.common import (  # noqa: E402
    ArticleWriter, ImporterError, ImportReport, fix_c1, http_get, parse_date, redact_url,
)


class _Recorder(BaseHTTPRequestHandler):
    """Records request headers; /redirect-* answers 302 to the configured target."""

    seen = []
    target = ""

    def log_message(self, *a):
        pass

    def do_GET(self):
        type(self).seen.append((self.server.server_port, self.path, dict(self.headers)))
        if self.path.startswith("/redirect-away"):
            self.send_response(302)
            self.send_header("Location", type(self).target)
            self.end_headers()
            return
        if self.path.startswith("/redirect-same"):
            self.send_response(302)
            self.send_header("Location", "/final")
            self.end_headers()
            return
        if self.path.startswith("/denied"):
            body = b'{"error": "bad key secretkey123456 for you"}'
            self.send_response(401)
        else:
            body = b'{"ok": true}'
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _serve():
    handler = type("H", (_Recorder,), {"seen": [], "target": ""})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, handler


class RedirectCredentialTests(unittest.TestCase):
    def setUp(self):
        self.a, self.ha = _serve()
        self.b, self.hb = _serve()
        self.ha.target = f"http://127.0.0.1:{self.b.server_port}/landing"

    def tearDown(self):
        for srv in (self.a, self.b):
            srv.shutdown()
            srv.server_close()

    def test_cross_origin_redirect_drops_credentials(self):
        http_get(f"http://127.0.0.1:{self.a.server_port}/redirect-away",
                 headers={"Authorization": "Basic c2VjcmV0", "Cookie": "s=1", "X-Api-Key": "k"})
        self.assertEqual(len(self.hb.seen), 1)
        landed = {k.lower(): v for k, v in self.hb.seen[0][2].items()}
        self.assertNotIn("authorization", landed)
        self.assertNotIn("cookie", landed)
        self.assertNotIn("x-api-key", landed)
        self.assertIn("user-agent", landed)

    def test_global_default_opener_does_not_disable_protection(self):
        # urllib.request.urlopen() installs a plain global opener on first use; any library
        # in the process may trigger that. Credentials must still be stripped.
        import urllib.request
        previous = getattr(urllib.request, "_opener", None)
        urllib.request.install_opener(urllib.request.build_opener())
        try:
            http_get(f"http://127.0.0.1:{self.a.server_port}/redirect-away",
                     headers={"Authorization": "Basic c2VjcmV0"})
        finally:
            urllib.request.install_opener(previous)
        landed = {k.lower(): v for k, v in self.hb.seen[-1][2].items()}
        self.assertNotIn("authorization", landed)

    def test_same_origin_redirect_keeps_credentials(self):
        http_get(f"http://127.0.0.1:{self.a.server_port}/redirect-same",
                 headers={"Authorization": "Basic c2VjcmV0"})
        final = [h for _, path, h in self.ha.seen if path == "/final"]
        self.assertEqual(len(final), 1)
        self.assertEqual({k.lower(): v for k, v in final[0].items()}.get("authorization"), "Basic c2VjcmV0")


class ErrorTests(unittest.TestCase):
    def setUp(self):
        self.srv, self.h = _serve()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def test_http_error_has_status_and_scrubbed_body(self):
        with self.assertRaises(ImporterError) as cm:
            http_get(f"http://127.0.0.1:{self.srv.server_port}/denied", params={"key": "secretkey123456"})
        err = cm.exception
        self.assertEqual(err.status, 401)
        self.assertNotIn("secretkey123456", str(err) + err.body)
        self.assertIn("***", err.body)

    def test_invalid_url_fails_fast_without_retrying(self):
        with mock.patch.object(common.time, "sleep") as sleep:
            with self.assertRaises(ImporterError) as cm:
                http_get("http://example.com/a path with spaces")
        self.assertIn("Invalid URL", str(cm.exception))
        sleep.assert_not_called()

    def test_truncated_response_is_retried_then_reported(self):
        calls = {"n": 0}

        def boom(req, timeout, opener=None):
            calls["n"] += 1
            raise http.client.IncompleteRead(b"partial")

        with mock.patch.object(common, "_open", boom), mock.patch.object(common.time, "sleep"):
            with self.assertRaises(ImporterError):
                http_get("http://127.0.0.1:1/x", retries=2)
        self.assertEqual(calls["n"], 3)


class RedactTests(unittest.TestCase):
    def test_userinfo_and_key_params_are_masked(self):
        shown = redact_url("https://bob:hunter2@example.com/api?key=abc&page=2&token=t0k")
        self.assertNotIn("hunter2", shown)
        self.assertNotIn("bob", shown)
        self.assertNotIn("abc", shown)
        self.assertNotIn("t0k", shown)
        self.assertIn("page=2", shown)
        self.assertIn("key=***", shown)


class DateTests(unittest.TestCase):
    def test_rfc822_variants(self):
        want = "2024-05-01T13:00:00+00:00"
        for value in ["Wed, 01 May 2024 18:30:00 +05:30", "Wed, 01 May 2024 14:00:00 GMT+1",
                      "Wed, 01 May 2024 08:00:00 UTC-05:00", "2024-05-01t13:00:00z",
                      "2024-05-01T09:00:00-04:00"]:
            self.assertEqual(parse_date(value).isoformat(), want, value)

    def test_unparseable_and_zero_dates(self):
        for value in ["", "garbage", "0000-00-00 00:00:00", None]:
            self.assertIsNone(parse_date(value))


class TextTests(unittest.TestCase):
    def test_c1_controls_map_to_cp1252_punctuation(self):
        self.assertEqual(fix_c1("It\x92s \x93quoted\x94 \x85"), "It’s “quoted” …")
        self.assertEqual(fix_c1("undefined\x81gone"), "undefinedgone")


class WriterTests(unittest.TestCase):
    def _raw(self, i):
        return {"id": f"t-{i}", "headline": f"H{i}", "content": "text", "url": f"https://x.example/{i}",
                "authors": ["A"], "publish_date": "2024-01-01T00:00:00Z"}

    def test_empty_import_keeps_previous_files(self):
        out = pathlib.Path(tempfile.mkdtemp())
        with ArticleWriter(out, "src", ImportReport("t")) as w:
            w.add_all(self._raw(i) for i in range(3))
        before = (out / "src-0001.json").read_bytes()
        with ArticleWriter(out, "src", ImportReport("t")) as w:
            w.add_all([])
        self.assertEqual((out / "src-0001.json").read_bytes(), before)
        self.assertEqual(len(json.loads(before)), 3)

    def test_nonempty_reimport_replaces(self):
        out = pathlib.Path(tempfile.mkdtemp())
        with ArticleWriter(out, "src", ImportReport("t")) as w:
            w.add_all(self._raw(i) for i in range(3))
        with ArticleWriter(out, "src", ImportReport("t")) as w:
            w.add_all([self._raw(9)])
        self.assertEqual([a["id"] for a in json.loads((out / "src-0001.json").read_text())], ["t-9"])


if __name__ == "__main__":
    unittest.main()
