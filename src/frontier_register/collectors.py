"""Read-only official-source collection and account-scoped model discovery.

The collectors never assign a risk class or change a organization decision. A source
check is not an evidence review. API visibility is not availability assurance,
and a model missing from a list is never classified as retired.

Public-source hosts must be explicitly allowed. Host validation also rejects
private DNS results; a organization-controlled egress proxy remains the appropriate
defence against DNS rebinding between validation and the actual connection.
Run one writer per evidence directory (the CLI owns the process lock).
"""
from __future__ import annotations

import hashlib
from html.parser import HTMLParser
import io
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import tempfile
import time
import unicodedata
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import parse_qsl, urljoin, urlsplit, urlunsplit

import httpx


class CollectionError(RuntimeError):
    """A source cannot be fetched or the evidence store is inconsistent."""


class DiscoveryError(RuntimeError):
    """Discovery failed or returned an incomplete/invalid list."""


class _HTTPXSession:
    """Small streaming adapter. Requests never inherit client credentials/cookies."""

    def __init__(self, client: httpx.Client):
        self.client = client

    def get(self, url: str, *, headers: Mapping[str, Any], params: Mapping[str, Any] | None = None,
            timeout: tuple[float, float], stream: bool, allow_redirects: bool) -> httpx.Response:
        settings = httpx.Timeout(timeout[1], connect=timeout[0])
        request = httpx.Request(
            "GET", url, params=params, headers={k: v for k, v in headers.items() if v is not None},
            extensions={"timeout": settings.as_dict()},
        )
        return self.client.send(request, auth=None, stream=True, follow_redirects=False)

    def close(self) -> None:
        self.client.close()


def _client(session: Any) -> Any:
    if session is None:
        return _HTTPXSession(httpx.Client(follow_redirects=False))
    return _HTTPXSession(session) if isinstance(session, httpx.Client) else session


class _TextParser(HTMLParser):
    """Text-only HTML parser: no scripts, resources, or browser execution."""

    _ignored = {"script", "style", "noscript", "nav", "footer"}
    _void = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.pieces: list[str] = []
        self.skip: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in self._void and (self.skip or tag in self._ignored):
            self.skip.append(tag)
        elif not self.skip and tag in {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4"}:
            self.pieces.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.skip:
            index = len(self.skip) - 1 - self.skip[::-1].index(tag)
            del self.skip[index:]
        elif not self.skip:
            self.pieces.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.pieces.append(data)


_REDIRECTS = {301, 302, 303, 307, 308}
_SECRET_QUERY_KEYS = {"key", "api_key", "apikey", "access_token", "token", "sig", "signature"}
_SOURCE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}\Z")
_API_ENDPOINTS = {
    "OpenAI": "https://api.openai.com/v1/models",
    "Anthropic": "https://api.anthropic.com/v1/models",
    "Google": "https://generativelanguage.googleapis.com/v1beta/models",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _json_write(path: Path, value: Any) -> None:
    _atomic_write(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def _public_dns(host: str) -> None:
    try:
        answers = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise CollectionError("DNS lookup failed") from exc
    addresses = {item[4][0] for item in answers}
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise CollectionError("DNS result is not a public address")


def validate_source_url(
    url: str, allowed_hosts: Iterable[str], *, resolver: Callable[[str], None] | None = None
) -> str:
    """Return a fragment-free approved HTTPS URL, or reject it before I/O.

    The allowlist contains exact hostnames, not suffixes or wildcards. The
    injectable resolver is solely for testing/controlled network integration.
    """
    if not isinstance(url, str) or any(ord(c) < 32 for c in url) or "\\" in url:
        raise CollectionError("Invalid source URL")
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError as exc:
        raise CollectionError("Invalid source URL") from exc
    if parsed.scheme.lower() != "https" or port not in (None, 443):
        raise CollectionError("Only HTTPS on port 443 is permitted")
    if parsed.username is not None or parsed.password is not None:
        raise CollectionError("Credentials in source URLs are forbidden")
    if not host or host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise CollectionError("Local source hosts are forbidden")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise CollectionError("IP-literal source hosts are forbidden")
    allowed = {str(h).lower().rstrip(".") for h in allowed_hosts}
    if host not in allowed:
        raise CollectionError("Source host is not on the exact-host allowlist")
    if any(k.lower() in _SECRET_QUERY_KEYS for k, _ in parse_qsl(parsed.query, keep_blank_values=True)):
        raise CollectionError("Credential-like source query parameters are forbidden")
    (resolver or _public_dns)(host)
    return urlunsplit(("https", host, parsed.path or "/", parsed.query, ""))


def _bounded_bytes(response: Any, max_bytes: int, deadline: float) -> bytes:
    length = response.headers.get("Content-Length")
    if length:
        try:
            if int(length) > max_bytes:
                raise CollectionError("Response exceeds configured byte limit")
        except ValueError as exc:
            raise CollectionError("Invalid Content-Length") from exc
    chunks: list[bytes] = []
    count = 0
    for chunk in response.iter_bytes(chunk_size=64 * 1024):
        if time.monotonic() > deadline:
            raise CollectionError("Response exceeded total read deadline")
        if not chunk:
            continue
        count += len(chunk)
        if count > max_bytes:
            raise CollectionError("Response exceeds configured byte limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _extract_text(raw: bytes, content_type: str, *, max_chars: int = 2_000_000) -> tuple[str, str, str | None]:
    """Return normalized text, extension and an extraction warning.

    No browser or JavaScript is executed. Unsupported/empty/scanned content is
    retained as raw evidence and explicitly marked for manual inspection.
    """
    warning = None
    if raw.startswith(b"%PDF-") or "application/pdf" in content_type:
        extension = ".pdf"
        try:
            from pypdf import PdfReader
        except ImportError:
            return "", extension, "PDF text not extracted: install the optional pypdf dependency"
        try:
            reader = PdfReader(io.BytesIO(raw))
            if reader.is_encrypted:
                return "", extension, "Encrypted PDF retained for manual inspection"
            if len(reader.pages) > 500:
                return "", extension, "PDF exceeds the 500-page extraction limit"
            pieces: list[str] = []
            size = 0
            for page in reader.pages:
                piece = page.extract_text() or ""
                size += len(piece)
                if size > max_chars:
                    return "", extension, "PDF text exceeds the extraction character limit"
                pieces.append(piece)
            text = "\n".join(pieces)
        except Exception:
            # Deliberately do not expose source bodies or parser exception text.
            return "", extension, "PDF parsing failed; inspect retained raw evidence"
    elif any(t in content_type for t in ("text/html", "application/xhtml+xml")) or raw.lstrip().lower().startswith((b"<!doctype html", b"<html")):
        extension = ".html"
        parser = _TextParser()
        parser.feed(raw.decode("utf-8", errors="replace"))
        parser.close()
        text = "".join(parser.pieces)
    elif content_type.startswith("text/") or "json" in content_type or "xml" in content_type:
        extension = ".json" if "json" in content_type else ".txt"
        text = raw.decode("utf-8", errors="replace")
    else:
        return "", ".bin", "Unsupported content type; inspect retained raw evidence"
    if len(text) > max_chars:
        return "", extension, "Text exceeds the extraction character limit"
    text = unicodedata.normalize("NFKC", text)
    text = "\n".join(" ".join(line.split()) for line in text.splitlines() if line.strip()).strip()
    if not text:
        warning = "No text extracted; scanned PDFs and JavaScript-only pages need manual inspection"
    return text, extension, warning


def _public_get(
    session: Any, url: str, allowed_hosts: Iterable[str], previous: Mapping[str, Any],
    *, timeout: float, max_bytes: int, max_redirects: int,
    resolver: Callable[[str], None] | None,
) -> tuple[int, bytes, Mapping[str, str], str]:
    current = validate_source_url(url, allowed_hosts, resolver=resolver)
    deadline = time.monotonic() + max(1.0, timeout) * 2
    headers: dict[str, Any] = {
        "User-Agent": "OrganizationFrontierRegister/1.0 (public evidence review)",
        "Accept": "text/html, text/plain, application/pdf, application/json;q=0.8, */*;q=0.1",
        # Do not inherit credentials/cookies from an injected client.
        "Authorization": None, "X-Api-Key": None, "x-goog-api-key": None, "Cookie": "",
    }
    if previous.get("etag"):
        headers["If-None-Match"] = previous["etag"]
    if previous.get("last_modified"):
        headers["If-Modified-Since"] = previous["last_modified"]
    for redirect in range(max_redirects + 1):
        response = session.get(
            current, headers=headers, timeout=(min(timeout, 10), timeout),
            stream=True, allow_redirects=False,
        )
        try:
            status = response.status_code
            if status in _REDIRECTS:
                if redirect >= max_redirects:
                    raise CollectionError("Redirect limit exceeded")
                location = response.headers.get("Location")
                if not location:
                    raise CollectionError("Redirect omitted its Location")
                current = validate_source_url(urljoin(current, location), allowed_hosts, resolver=resolver)
                # A different representation must not receive stale validators.
                headers.pop("If-None-Match", None)
                headers.pop("If-Modified-Since", None)
                continue
            if status == 304:
                return status, b"", {k.lower(): v for k, v in response.headers.items()}, current
            if status != 200:
                raise CollectionError(f"Source returned HTTP {status}")
            body = _bounded_bytes(response, max_bytes, deadline)
            if not body:
                raise CollectionError("Source returned an empty body")
            return status, body, {k.lower(): v for k, v in response.headers.items()}, current
        finally:
            response.close()
    raise CollectionError("Redirect limit exceeded")


def fetch_sources(
    sources: list[dict[str, Any]], evidence_dir: str | Path, allowed_hosts: Iterable[str],
    *, session: Any = None, timeout: float = 30, max_bytes: int = 10_000_000,
    max_redirects: int = 5, resolver: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:
    """Fetch explicit source URLs and write content-addressed evidence.

    Sources require ``url`` and may provide a safe ``source_id``. Results always
    have source_id/url/status/checked_at. With a last successful fetch they also
    carry raw_sha256/text_sha256/raw_path/text_path/last_success_at. On unavailable,
    these are historical values, never newly checked content. A 304 retains the
    content timestamp and hashes while recording last_confirmed_at. No failed
    fetch discards earlier evidence. A change in bytes is queued even if extracted
    text stayed the same (e.g. a link or other non-text content changed).

    Manifest writes use atomic replacement; callers must serialize runs.
    """
    if timeout <= 0 or max_bytes <= 0 or max_redirects < 0:
        raise ValueError("Timeout/byte limit must be positive and redirects non-negative")
    directory = Path(evidence_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    manifest_path = directory / "manifest.json"
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("schema_version") != 1 or not isinstance(manifest.get("sources"), dict):
                raise ValueError("Unexpected schema")
        except (OSError, ValueError, TypeError) as exc:
            raise CollectionError("Evidence manifest cannot be read; refusing to replace history") from exc
    else:
        manifest = {"schema_version": 1, "sources": {}}
    prepared = []
    seen: set[str] = set()
    for source in sources:
        if not isinstance(source, dict) or not isinstance(source.get("url"), str):
            raise ValueError("Each source requires a URL string")
        source_id = source.get("source_id") or hashlib.sha256(source["url"].encode()).hexdigest()[:24]
        if not isinstance(source_id, str) or not _SOURCE_ID.fullmatch(source_id) or source_id in seen:
            raise ValueError("Invalid or duplicate source_id")
        seen.add(source_id)
        prepared.append((source_id, source["url"]))
    allowed = tuple(allowed_hosts)
    own_session = session is None
    client = _client(session)
    results = []
    try:
        for source_id, url in prepared:
            checked_at = _now()
            record = manifest["sources"].get(source_id, {})
            if record.get("url") not in (None, url):
                raise CollectionError("source_id URL changed; use a new source_id to preserve provenance")
            previous = dict(record.get("last_success") or {})
            result: dict[str, Any] = {
                "source_id": source_id, "url": url, "checked_at": checked_at,
                "status": "unavailable", **{k: previous[k] for k in (
                    "raw_sha256", "text_sha256", "raw_path", "text_path", "last_success_at", "last_confirmed_at",
                    "content_observed_at", "content_type", "extraction_warning", "final_url"
                ) if k in previous},
            }
            try:
                status, raw, response_headers, final_url = _public_get(
                    client, url, allowed, previous, timeout=timeout, max_bytes=max_bytes,
                    max_redirects=max_redirects, resolver=resolver,
                )
                if status == 304:
                    cached_raw = Path(previous.get("raw_path", ""))
                    cached_text = Path(previous.get("text_path", ""))
                    if not previous or not cached_raw.is_file() or not cached_text.is_file():
                        raise CollectionError("304 received without retained successful evidence")
                    if hashlib.sha256(cached_raw.read_bytes()).hexdigest() != previous.get("raw_sha256"):
                        raise CollectionError("Retained evidence hash mismatch; fetch cannot confirm corrupt evidence")
                    if previous.get("text_sha256") and hashlib.sha256(cached_text.read_bytes().rstrip(b"\n")).hexdigest() != previous["text_sha256"]:
                        raise CollectionError("Retained text hash mismatch; fetch cannot confirm corrupt evidence")
                    previous["last_confirmed_at"] = checked_at
                    result.update(status="unchanged", http_status=304, final_url=final_url,
                                  raw_changed=False, text_changed=False, last_confirmed_at=checked_at)
                    record = {"url": url, "last_success": previous}
                else:
                    text, extension, warning = _extract_text(raw, response_headers.get("content-type", "").lower())
                    raw_hash = hashlib.sha256(raw).hexdigest()
                    text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest() if text else None
                    source_path = directory / "sources" / source_id
                    raw_path = source_path / f"{raw_hash}{extension}"
                    text_path = source_path / f"{raw_hash}.{text_hash or 'unextracted'}.txt"
                    if not raw_path.exists():
                        _atomic_write(raw_path, raw)
                    elif hashlib.sha256(raw_path.read_bytes()).hexdigest() != raw_hash:
                        raise CollectionError("Retained evidence hash mismatch; refusing to replace history")
                    if not text_path.exists():
                        _atomic_write(text_path, (text + "\n").encode("utf-8"))
                    elif text_hash and hashlib.sha256(text_path.read_bytes().rstrip(b"\n")).hexdigest() != text_hash:
                        raise CollectionError("Retained text hash mismatch; refusing to replace history")
                    raw_changed = previous.get("raw_sha256") != raw_hash
                    text_changed = previous.get("text_sha256") != text_hash
                    new_status = "new" if not previous else "changed" if raw_changed or text_changed else "unchanged"
                    success = {
                        "raw_sha256": raw_hash, "text_sha256": text_hash,
                        "raw_path": str(raw_path), "text_path": str(text_path),
                        "last_success_at": checked_at, "last_confirmed_at": checked_at,
                        "content_observed_at": previous.get("content_observed_at", checked_at) if not raw_changed else checked_at,
                        "etag": response_headers.get("etag"), "last_modified": response_headers.get("last-modified"),
                        "content_type": response_headers.get("content-type", ""),
                        "final_url": final_url, "extraction_warning": warning,
                    }
                    result.update(success, status=new_status, http_status=200,
                                  raw_changed=raw_changed, text_changed=text_changed)
                    record = {"url": url, "last_success": success}
            except (httpx.HTTPError, CollectionError, OSError) as exc:
                # HTTP/client error strings may contain URLs, bodies or secrets.
                error = str(exc) if isinstance(exc, CollectionError) else type(exc).__name__
                result["error"] = error
                record = {"url": url, "last_success": previous or None}
            record["latest_attempt"] = {
                "checked_at": checked_at, "status": result["status"],
                "error": result.get("error"), "http_status": result.get("http_status"),
            }
            manifest["sources"][source_id] = record
            _json_write(manifest_path, manifest)
            results.append(result)
    finally:
        if own_session:
            client.close()
    return results


def _api_json(session: Any, endpoint: str, headers: dict[str, str], params: dict[str, Any], timeout: float) -> dict[str, Any]:
    # Endpoint comes exclusively from the fixed provider map; redirects are never followed.
    try:
        response = session.get(endpoint, headers=headers, params=params,
                               timeout=(min(timeout, 10), timeout), stream=True, allow_redirects=False)
        try:
            if response.status_code != 200:
                raise DiscoveryError(f"Model discovery returned HTTP {response.status_code}; no list accepted")
            raw = _bounded_bytes(response, 10_000_000, time.monotonic() + timeout * 2)
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise DiscoveryError("Model discovery response was not an object")
            return result
        finally:
            response.close()
    except DiscoveryError:
        raise
    except (httpx.HTTPError, CollectionError, ValueError) as exc:
        raise DiscoveryError(f"Model discovery failed ({type(exc).__name__}); no list accepted") from exc


def discover_models(
    provider: str, *, environ: Mapping[str, str] | None = None, session: Any = None,
    timeout: float = 30, max_pages: int = 100,
) -> list[dict[str, Any]]:
    """Return vendor model metadata visible to the supplied account.

    Keys are read only from the passed environment or process environment:
    OPENAI_API_KEY, ANTHROPIC_API_KEY, GOOGLE_API_KEY (GEMINI_API_KEY fallback).
    ANTHROPIC_API_VERSION defaults to the documented 2026-10-21 version.
    No request generates model output. Pagination failure raises DiscoveryError
    and returns no partial list. Returned items are candidates for human review.
    OpenAI fine-tunes/custom-owner records are excluded; Google returns only
    Gemini base-model resource names. No absence is a retirement determination.
    """
    canonical = {"openai": "OpenAI", "anthropic": "Anthropic", "google": "Google"}.get(provider.lower())
    if canonical is None or timeout <= 0 or max_pages < 1:
        raise ValueError("Unsupported provider or invalid discovery limits")
    env = os.environ if environ is None else environ
    key_name = {"OpenAI": "OPENAI_API_KEY", "Anthropic": "ANTHROPIC_API_KEY", "Google": "GOOGLE_API_KEY"}[canonical]
    key = env.get(key_name) or (env.get("GEMINI_API_KEY") if canonical == "Google" else None)
    if not key:
        raise DiscoveryError(f"{key_name} is not set")
    headers = {"Accept": "application/json", "User-Agent": "OrganizationFrontierRegister/1.0", "Cookie": ""}
    params: dict[str, Any] = {}
    if canonical in ("OpenAI", "Anthropic"):
        headers["Authorization"] = f"Bearer {key}"
    else:
        headers["x-goog-api-key"] = key
    if canonical == "Anthropic":
        headers["anthropic-version"] = env.get("ANTHROPIC_API_VERSION", "2026-10-21")
        params["limit"] = 1000
    elif canonical == "Google":
        params["pageSize"] = 1000
    client = _client(session)
    own_session = session is None
    found: dict[str, dict[str, Any]] = {}
    cursors: set[str] = set()
    complete = False
    try:
        for _ in range(max_pages):
            payload = _api_json(client, _API_ENDPOINTS[canonical], headers, params, timeout)
            data_key = "models" if canonical == "Google" else "data"
            items = payload.get(data_key)
            if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                raise DiscoveryError("Model discovery response omitted a valid model array")
            for item in items:
                raw_id = item.get("name") if canonical == "Google" else item.get("id")
                if not isinstance(raw_id, str) or not raw_id.strip():
                    raise DiscoveryError("Model discovery returned an item without an identifier")
                model_id = raw_id.removeprefix("models/") if canonical == "Google" else raw_id
                if canonical == "OpenAI":
                    # Official API visibility also includes customer-owned fine-tunes.
                    if model_id.startswith(("ft:", "ft-")) or ":ft-" in model_id:
                        continue
                    owner = item.get("owned_by", "")
                    if owner and owner not in {"openai", "system", "openai-internal"}:
                        continue
                if canonical == "Anthropic" and not model_id.startswith("claude-"):
                    continue
                if canonical == "Google" and not raw_id.startswith("models/gemini-"):
                    continue
                metadata_fields = {
                    "OpenAI": ("created", "owned_by", "shutdown_date"),
                    "Anthropic": ("created_at", "lifecycle", "model_family", "retirement_date"),
                    "Google": ("version", "baseModelId", "supportedGenerationMethods", "inputTokenLimit", "outputTokenLimit"),
                }[canonical]
                found[model_id] = {
                    "provider": canonical, "model_id": model_id,
                    "display_name": item.get("display_name") or item.get("displayName") or model_id,
                    "description": item.get("description") or "",
                    "api_metadata": {field: item[field] for field in metadata_fields if field in item},
                    "source_url": _API_ENDPOINTS[canonical], "checked_at": _now(),
                    "scope_note": "Account-scoped API observation only. Not organization approval or assurance of deployment availability.",
                }
            if canonical == "OpenAI":
                if payload.get("has_more") or payload.get("nextPageToken"):
                    raise DiscoveryError("Unexpected pagination in OpenAI model list; no partial list accepted")
                complete = True
                break
            cursor = payload.get("nextPageToken") if canonical == "Google" else payload.get("last_id") if payload.get("has_more") else None
            if canonical == "Anthropic" and not isinstance(payload.get("has_more"), bool):
                raise DiscoveryError("Anthropic response omitted has_more pagination indicator")
            if cursor is None or cursor == "":
                if canonical == "Anthropic" and payload.get("has_more"):
                    raise DiscoveryError("Anthropic response needs another page but omitted its cursor")
                complete = True
                break
            if not isinstance(cursor, str) or cursor in cursors:
                raise DiscoveryError("Model discovery returned an invalid or repeated pagination cursor")
            cursors.add(cursor)
            params["pageToken" if canonical == "Google" else "after_id"] = cursor
        if not complete:
            raise DiscoveryError("Model discovery exceeded page limit; no partial list accepted")
        return [found[key] for key in sorted(found)]
    finally:
        if own_session:
            client.close()
