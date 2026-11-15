"""Offline tests: never call a provider or read real credentials."""
import json
from pathlib import Path
import tempfile
import unittest

import httpx

from frontier_register.collectors import (
    CollectionError, DiscoveryError, discover_models, fetch_sources, validate_source_url,
)


def no_dns(_host):
    return None


class Response:
    def __init__(self, status=200, body=b"", headers=None):
        self.status_code = status
        self.body = body
        self.headers = headers or {"Content-Type": "text/html"}
        self.closed = False

    def iter_bytes(self, chunk_size):
        yield self.body

    def close(self):
        self.closed = True


class Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, {**kwargs, "headers": dict(kwargs.get("headers", {})), "params": dict(kwargs.get("params", {}))}))
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def api_response(payload, status=200):
    return Response(status, json.dumps(payload).encode(), {"Content-Type": "application/json"})


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        self.source = [{"source_id": "official", "url": "https://docs.example.com/report"}]

    def tearDown(self):
        self.tmp.cleanup()

    def fetch(self, session, **kwargs):
        return fetch_sources(self.source, self.path, ["docs.example.com"], session=session, resolver=no_dns, **kwargs)[0]

    def test_new_changed_and_failure_preserve_last_success(self):
        first = self.fetch(Session(Response(body=b"<main>First finding</main>", headers={"Content-Type": "text/html", "ETag": '"v1"'})))
        self.assertEqual(first["status"], "new")
        self.assertEqual(Path(first["text_path"]).read_text().strip(), "First finding")
        second = self.fetch(Session(Response(body=b"<main>Changed finding</main>")))
        self.assertEqual(second["status"], "changed")
        self.assertNotEqual(first["raw_sha256"], second["raw_sha256"])
        self.assertTrue(Path(first["raw_path"]).exists())
        failed = self.fetch(Session(httpx.ReadTimeout("secret API key must not appear")))
        self.assertEqual(failed["status"], "unavailable")
        self.assertEqual(failed["raw_sha256"], second["raw_sha256"])
        self.assertEqual(failed["last_success_at"], second["last_success_at"])
        self.assertNotIn("secret", failed["error"])
        manifest = json.loads((self.path / "manifest.json").read_text())
        self.assertEqual(manifest["sources"]["official"]["last_success"]["raw_sha256"], second["raw_sha256"])
        self.assertEqual(manifest["sources"]["official"]["latest_attempt"]["status"], "unavailable")

    def test_conditional_304_keeps_hashes_and_content(self):
        first = self.fetch(Session(Response(body=b"hello", headers={"Content-Type": "text/plain", "ETag": '"tag"'})))
        client = Session(Response(304))
        second = self.fetch(client)
        self.assertEqual(second["status"], "unchanged")
        self.assertEqual(second["raw_sha256"], first["raw_sha256"])
        self.assertEqual(second["last_success_at"], first["last_success_at"])
        self.assertEqual(client.calls[0][1]["headers"]["If-None-Match"], '"tag"')

    def test_304_without_previous_content_is_unavailable(self):
        result = self.fetch(Session(Response(304)))
        self.assertEqual(result["status"], "unavailable")
        self.assertNotIn("raw_sha256", result)

    def test_304_cannot_confirm_corrupt_cached_evidence(self):
        first = self.fetch(Session(Response(body=b"<main>original</main>")))
        Path(first["raw_path"]).write_bytes(b"tampered")
        result = self.fetch(Session(Response(304)))
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("hash mismatch", result["error"])

    def test_304_cannot_confirm_corrupt_extracted_text(self):
        first = self.fetch(Session(Response(body=b"<main>original</main>")))
        Path(first["text_path"]).write_text("tampered\n")
        result = self.fetch(Session(Response(304)))
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("text hash mismatch", result["error"])

    def test_httpx_adapter_streams_without_inheriting_client_credentials(self):
        observed = []
        def handler(request):
            observed.append(request)
            return httpx.Response(200, content=b"<main>HTTPX evidence</main>", headers={"Content-Type": "text/html"})
        with httpx.Client(transport=httpx.MockTransport(handler), headers={"Authorization": "Bearer inherited-secret"}, cookies={"session": "secret"}) as client:
            result = self.fetch(client)
        self.assertEqual(result["status"], "new")
        self.assertNotIn("Authorization", observed[0].headers)
        self.assertEqual(observed[0].headers.get("cookie", ""), "")

    def test_httpx_lowercase_headers_support_text_and_conditional_validation(self):
        observed = []
        def handler(request):
            observed.append(request)
            if len(observed) == 1:
                return httpx.Response(200, content=b"plain text evidence", headers={"content-type": "text/plain", "etag": '"revision-1"', "last-modified": "Sat, 14 Nov 2026 10:00:00 GMT"})
            return httpx.Response(304)
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            first = self.fetch(client)
            second = self.fetch(client)
        self.assertEqual(Path(first["text_path"]).read_text().strip(), "plain text evidence")
        self.assertEqual(second["status"], "unchanged")
        self.assertEqual(observed[1].headers["if-none-match"], '"revision-1"')
        self.assertEqual(observed[1].headers["if-modified-since"], "Sat, 14 Nov 2026 10:00:00 GMT")

    def test_identical_200_is_unchanged_and_same_text_raw_change_is_visible(self):
        self.fetch(Session(Response(body=b"<main>same</main>")))
        same = self.fetch(Session(Response(body=b"<main>same</main>")))
        self.assertEqual(same["status"], "unchanged")
        cosmetic = self.fetch(Session(Response(body=b'<main class="new">same</main>')))
        self.assertEqual(cosmetic["status"], "changed")
        self.assertFalse(cosmetic["text_changed"])

    def test_redirect_cannot_escape_allowlist_or_downgrade(self):
        for target in ("https://evil.example/report", "http://docs.example.com/report", "https://127.0.0.1/report"):
            with self.subTest(target=target):
                client = Session(Response(302, headers={"Location": target}))
                result = self.fetch(client)
                self.assertEqual(result["status"], "unavailable")
                self.assertEqual(len(client.calls), 1)

    def test_allowed_redirect_and_credentials_are_not_inherited(self):
        client = Session(Response(302, headers={"Location": "/new"}), Response(body=b"<main>ok</main>"))
        result = self.fetch(client)
        self.assertEqual(result["final_url"], "https://docs.example.com/new")
        self.assertFalse(client.calls[0][1]["allow_redirects"])
        self.assertIsNone(client.calls[0][1]["headers"]["Authorization"])
        self.assertIsNone(client.calls[0][1]["headers"]["X-Api-Key"])

    def test_content_length_and_stream_are_bounded(self):
        a = self.fetch(Session(Response(body=b"abc", headers={"Content-Length": "500"})), max_bytes=4)
        self.assertEqual(a["status"], "unavailable")
        b = self.fetch(Session(Response(body=b"12345")), max_bytes=4)
        self.assertEqual(b["status"], "unavailable")

    def test_bad_urls_rejected_before_io(self):
        for url in ("http://docs.example.com", "https://docs.example.com.evil.com", "https://user:secret@docs.example.com", "https://10.1.1.1", "https://localhost", "https://docs.example.com:444", "https://docs.example.com?api_key=secret"):
            with self.subTest(url=url), self.assertRaises(CollectionError):
                validate_source_url(url, ["docs.example.com", "10.1.1.1", "localhost"], resolver=no_dns)

    def test_private_dns_is_rejected(self):
        from unittest.mock import patch
        with patch("frontier_register.collectors.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("192.168.1.1", 443))]):
            with self.assertRaises(CollectionError):
                validate_source_url("https://docs.example.com", ["docs.example.com"])

    def test_manifest_corruption_is_not_overwritten(self):
        (self.path / "manifest.json").write_text("{bad")
        with self.assertRaises(CollectionError):
            self.fetch(Session())
        self.assertEqual((self.path / "manifest.json").read_text(), "{bad")

    def test_source_id_cannot_change_url_or_escape_directory(self):
        self.fetch(Session(Response(body=b"<main>ok</main>")))
        with self.assertRaises(CollectionError):
            fetch_sources([{"source_id": "official", "url": "https://docs.example.com/changed"}], self.path, ["docs.example.com"], session=Session(), resolver=no_dns)
        with self.assertRaises(ValueError):
            fetch_sources([{"source_id": "../bad", "url": "https://docs.example.com"}], self.path, ["docs.example.com"], session=Session(), resolver=no_dns)


class DiscoveryTests(unittest.TestCase):
    def test_httpx_adapter_sends_only_explicit_discovery_credentials(self):
        observed = []
        def handler(request):
            observed.append(request)
            return httpx.Response(200, json={"data": [{"id": "gpt-example", "owned_by": "openai"}]})
        with httpx.Client(transport=httpx.MockTransport(handler), headers={"Authorization": "Bearer wrong"}) as client:
            rows = discover_models("OpenAI", environ={"OPENAI_API_KEY": "expected"}, session=client)
        self.assertEqual(len(rows), 1)
        self.assertEqual(observed[0].headers["Authorization"], "Bearer expected")

    def test_openai_excludes_customer_fine_tunes(self):
        client = Session(api_response({"data": [
            {"id": "gpt-example", "owned_by": "openai"},
            {"id": "ft:gpt-example:org:custom", "owned_by": "org-123"},
            {"id": "custom", "owned_by": "org-123"},
        ]}))
        rows = discover_models("openai", environ={"OPENAI_API_KEY": "fake"}, session=client)
        self.assertEqual([r["model_id"] for r in rows], ["gpt-example"])
        self.assertEqual(rows[0]["provider"], "OpenAI")
        self.assertEqual(client.calls[0][0], "https://api.openai.com/v1/models")
        self.assertEqual(client.calls[0][1]["params"], {})

    def test_anthropic_pagination_uses_last_id(self):
        client = Session(
            api_response({"data": [{"id": "claude-one"}], "has_more": True, "last_id": "claude-one"}),
            api_response({"data": [{"id": "claude-two"}], "has_more": False, "last_id": "claude-two"}),
        )
        rows = discover_models("Anthropic", environ={"ANTHROPIC_API_KEY": "fake"}, session=client)
        self.assertEqual(len(rows), 2)
        self.assertEqual(client.calls[1][1]["params"]["after_id"], "claude-one")
        self.assertEqual(client.calls[0][1]["headers"]["anthropic-version"], "2026-10-21")

    def test_google_key_is_header_and_pagination_does_not_use_returned_urls(self):
        client = Session(
            api_response({"models": [{"name": "models/gemini-one", "displayName": "Gemini One"}], "nextPageToken": "opaque-token"}),
            api_response({"models": [{"name": "models/gemini-two"}, {"name": "tunedModels/private"}]}),
        )
        rows = discover_models("Google", environ={"GOOGLE_API_KEY": "fake"}, session=client)
        self.assertEqual([r["model_id"] for r in rows], ["gemini-one", "gemini-two"])
        self.assertEqual(client.calls[1][1]["params"]["pageToken"], "opaque-token")
        self.assertEqual(client.calls[0][1]["headers"]["x-goog-api-key"], "fake")
        self.assertNotIn("fake", client.calls[0][0])
        self.assertFalse(client.calls[0][1]["allow_redirects"])

    def test_failure_on_second_page_returns_no_partial_list(self):
        client = Session(
            api_response({"models": [{"name": "models/gemini-one"}], "nextPageToken": "next"}),
            api_response({"error": "bad"}, 503),
        )
        with self.assertRaises(DiscoveryError):
            discover_models("Google", environ={"GOOGLE_API_KEY": "fake"}, session=client)

    def test_redirect_rejected_without_following_credentials(self):
        client = Session(Response(302, headers={"Location": "https://evil.example"}))
        with self.assertRaises(DiscoveryError):
            discover_models("OpenAI", environ={"OPENAI_API_KEY": "fake"}, session=client)
        self.assertEqual(len(client.calls), 1)

    def test_pagination_loops_and_page_cap_fail_closed(self):
        payload = {"models": [{"name": "models/gemini-one"}], "nextPageToken": "same"}
        with self.assertRaises(DiscoveryError):
            discover_models("Google", environ={"GOOGLE_API_KEY": "fake"}, session=Session(api_response(payload), api_response(payload)))
        with self.assertRaises(DiscoveryError):
            discover_models("Google", environ={"GOOGLE_API_KEY": "fake"}, session=Session(api_response(payload)), max_pages=1)

    def test_invalid_or_missing_model_arrays_are_not_empty_success(self):
        for payload in ({}, {"data": "bad"}, {"data": [{}]}):
            with self.subTest(payload=payload), self.assertRaises(DiscoveryError):
                discover_models("OpenAI", environ={"OPENAI_API_KEY": "fake"}, session=Session(api_response(payload)))

    def test_missing_credentials_and_invalid_provider_fail_before_io(self):
        with self.assertRaises(DiscoveryError):
            discover_models("OpenAI", environ={}, session=Session())
        with self.assertRaises(ValueError):
            discover_models("UnapprovedVendor", environ={}, session=Session())


if __name__ == "__main__":
    unittest.main()
