"""Publish a generated register inside a managed section of an existing page.

Cloud uses REST v2; Data Center uses the content REST API.  This module never
creates pages, changes titles, follows redirects, or retries a conflicting PUT.
Credentials are read from environment variables, never stored in configuration.

Official API references:
https://developer.atlassian.com/cloud/confluence/rest/v2/api-group-page/
https://developer.atlassian.com/server/confluence/rest/v1110/api-group-content-resource/

The replacement preserves the *published storage body* outside the markers.
Confluence Cloud may reconcile updates into unpublished drafts; use a dedicated
register page and avoid concurrent manual drafts when publishing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from html import unescape
from html.parser import HTMLParser
import os
import re
from typing import Any, Literal, Mapping
from urllib.parse import urlsplit

import httpx


START_MARKER = "FRONTIER_REGISTER_MANAGED_START"
END_MARKER = "FRONTIER_REGISTER_MANAGED_END"
_PARAGRAPH = re.compile(r"<p\b[^>]*>(.*?)</p\s*>", re.IGNORECASE | re.DOTALL)
_TAG = re.compile(r"<[^>]+>")
_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


class ConfluenceError(RuntimeError):
    """A sanitized operational error, safe for ordinary application logs."""


class ManagedSectionError(ConfluenceError):
    """The target page does not contain one unambiguous managed section."""


class VersionConflict(ConfluenceError):
    """The page changed after retrieval; fetch and review again before retrying."""


class _StorageStructure(HTMLParser):
    """Locate real root paragraphs without modifying the original storage text."""

    def __init__(self, body: str) -> None:
        super().__init__(convert_charrefs=True)
        self.line_offsets = [0] + [match.end() for match in re.finditer("\n", body)]
        self.stack: list[str] = []
        self.root_paragraphs: set[int] = set()
        self.valid = True
        self.feed(body)
        self.close()
        if self.stack:
            self.valid = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "p" and not self.stack:
            line, column = self.getpos()
            self.root_paragraphs.add(self.line_offsets[line - 1] + column)
        if tag not in _VOID_TAGS:
            self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID_TAGS:
            return
        if not self.stack or self.stack[-1] != tag:
            self.valid = False
            return
        self.stack.pop()

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # Empty XHTML and Confluence macro elements do not alter nesting.
        pass


class _BearerAuth(httpx.Auth):
    """Explicit bearer authentication without credentials in configuration."""

    def __init__(self, token: str) -> None:
        self._token = token

    def auth_flow(self, request: httpx.Request):
        request.headers["Authorization"] = f"Bearer {self._token}"
        yield request


@dataclass(frozen=True)
class ConfluenceConfig:
    mode: Literal["cloud", "data_center"]
    base_url: str
    page_id: str
    token_env: str = "CONFLUENCE_API_TOKEN"
    username_env: str = "CONFLUENCE_EMAIL"
    auth_kind: Literal["auto", "basic", "bearer"] = "auto"
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.mode not in {"cloud", "data_center"}:
            raise ValueError("Confluence mode must be cloud or data_center.")
        if self.auth_kind not in {"auto", "basic", "bearer"}:
            raise ValueError("Confluence auth_kind must be auto, basic, or bearer.")
        if not isinstance(self.page_id, str) or not re.fullmatch(r"[1-9][0-9]*", self.page_id):
            raise ValueError("Confluence page_id must be a positive numeric ID string.")
        if not isinstance(self.base_url, str) or any(c.isspace() for c in self.base_url):
            raise ValueError("Confluence base_url must be an HTTPS URL without whitespace.")
        parsed = urlsplit(self.base_url)
        if (
            parsed.scheme.lower() != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Confluence base_url requires HTTPS and no credentials, query, or fragment.")
        # A context path is permitted for Data Center or the Atlassian API gateway.
        if any(segment in {".", ".."} for segment in parsed.path.split("/")):
            raise ValueError("Confluence base_url cannot contain dot path segments.")
        if not 0 < self.timeout_seconds <= 120:
            raise ValueError("Confluence timeout_seconds must be between 0 and 120.")
        for name in (self.token_env, self.username_env):
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise ValueError("Credential environment variable names are invalid.")

    @property
    def page_url(self) -> str:
        """Return the API endpoint; base_url includes any deployment context path."""
        base = self.base_url.rstrip("/")
        if self.mode == "cloud":
            # Accept both https://site.atlassian.net and .../wiki, as well as
            # https://api.atlassian.com/ex/confluence/{cloudId}/wiki.
            if not urlsplit(base).path.endswith("/wiki"):
                base += "/wiki"
            return f"{base}/api/v2/pages/{self.page_id}"
        return f"{base}/rest/api/content/{self.page_id}"


@dataclass(frozen=True)
class PageSnapshot:
    page_id: str
    title: str
    version: int
    storage_body: str = field(repr=False)


@dataclass(frozen=True)
class PublishResult:
    status: Literal["dry_run", "unchanged", "published"]
    page_id: str
    old_version: int
    new_version: int | None
    changed: bool
    proposed_body_sha256: str
    # Available to save a local preview. It is excluded from ordinary repr/logs.
    proposed_body: str = field(repr=False)


def _validate_fragment(managed_html: str) -> str:
    if not isinstance(managed_html, str) or not managed_html.strip():
        raise ManagedSectionError("Generated Confluence content must be a nonempty HTML fragment.")
    decoded = unescape(managed_html)
    if START_MARKER in decoded or END_MARKER in decoded:
        raise ManagedSectionError("Generated content must not contain managed-section markers.")
    return managed_html.strip()


def managed_section(managed_html: str) -> str:
    """Return initial storage markup for a page's managed region.

    In the editor, ordinary paragraphs containing START_MARKER and END_MARKER
    can also be entered manually. Do not place them in a code block or table.
    HTML comments are deliberately not used as persistence markers.
    """
    fragment = _validate_fragment(managed_html)
    return f"<p>{START_MARKER}</p>\n{fragment}\n<p>{END_MARKER}</p>"


def replace_managed_section(storage_body: str, managed_html: str) -> tuple[str, bool]:
    """Replace only the content between unique marker paragraphs.

    Original marker tags, attributes and all surrounding storage bytes survive.
    Marker paragraphs must be top-level, outside tables and macros. Harmless
    inline formatting inside a marker paragraph is accepted. Missing,
    duplicated, inverted, or non-paragraph markers cause a refusal.
    """
    fragment = _validate_fragment(managed_html)
    decoded = unescape(storage_body)
    if decoded.count(START_MARKER) != 1 or decoded.count(END_MARKER) != 1:
        raise ManagedSectionError("Page must contain exactly one start and one end marker.")
    try:
        structure = _StorageStructure(storage_body)
    except (ValueError, AssertionError):
        raise ManagedSectionError("Page storage markup cannot be safely parsed.") from None
    if not structure.valid:
        raise ManagedSectionError("Page storage markup is unbalanced; no replacement was prepared.")
    found: dict[str, re.Match[str]] = {}
    for paragraph in _PARAGRAPH.finditer(storage_body):
        visible = unescape(_TAG.sub("", paragraph.group(1))).strip()
        if visible in {START_MARKER, END_MARKER}:
            if paragraph.start() not in structure.root_paragraphs:
                raise ManagedSectionError("Managed markers must be top-level paragraphs outside tables and macros.")
            found[visible] = paragraph
    if set(found) != {START_MARKER, END_MARKER}:
        raise ManagedSectionError("Each managed marker must occupy its own ordinary paragraph.")
    start, end = found[START_MARKER], found[END_MARKER]
    if start.end() > end.start():
        raise ManagedSectionError("The end marker must follow the start marker.")
    existing = storage_body[start.end():end.start()]
    if existing.strip() == fragment:
        return storage_body, False
    return storage_body[:start.end()] + "\n" + fragment + "\n" + storage_body[end.start():], True


class ConfluencePublisher:
    """Existing-page publisher with dry-run default and optimistic concurrency.

    ``session`` is injectable for offline tests. A real httpx.Client keeps
    environment proxy / CA configuration, validates TLS, and receives an explicit
    auth object. No response bodies or credentials are included in exceptions.
    """

    def __init__(
        self,
        config: ConfluenceConfig,
        session: httpx.Client | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.config = config
        self._session = session if session is not None else httpx.Client()
        self._environ = os.environ if environ is None else environ

    def _auth(self) -> httpx.Auth:
        token = self._environ.get(self.config.token_env, "")
        if not token or "\r" in token or "\n" in token:
            raise ConfluenceError(f"A valid credential is required in {self.config.token_env}.")
        kind = self.config.auth_kind
        if kind == "auto":
            kind = "basic" if self.config.mode == "cloud" else "bearer"
        if kind == "bearer":
            return _BearerAuth(token)
        username = self._environ.get(self.config.username_env, "")
        if not username or any(c in username for c in ("\r", "\n", ":")):
            raise ConfluenceError(f"A valid username is required in {self.config.username_env}.")
        return httpx.BasicAuth(username, token)

    def _request(self, method: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self._session.request(
                method,
                self.config.page_url,
                auth=self._auth(),
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                timeout=self.config.timeout_seconds,
                follow_redirects=False,
                **kwargs,
            )
        except httpx.HTTPError:
            # Raw exceptions can include URLs, proxy information or request data.
            raise ConfluenceError(f"Confluence {method} failed. Check connectivity and TLS configuration.") from None
        if response.status_code == 409:
            raise VersionConflict("Confluence version conflict. Fetch and review the page again; no retry was attempted.")
        if 300 <= response.status_code < 400:
            raise ConfluenceError("Confluence redirected the request. Configure the canonical HTTPS endpoint; redirect not followed.")
        if response.status_code != 200:
            raise ConfluenceError(f"Confluence {method} returned HTTP {response.status_code}; no retry was attempted.")
        return response

    @staticmethod
    def _object(response: httpx.Response) -> dict[str, Any]:
        try:
            result = response.json()
        except ValueError:
            raise ConfluenceError("Confluence returned an invalid JSON response.") from None
        if not isinstance(result, dict):
            raise ConfluenceError("Confluence returned an unexpected JSON structure.")
        return result

    def get_page(self) -> PageSnapshot:
        params = {"body-format": "storage"} if self.config.mode == "cloud" else {"expand": "body.storage,version"}
        data = self._object(self._request("GET", params=params))
        try:
            page_id = str(data["id"])
            title = data["title"]
            version = data["version"]["number"]
            storage_body = data["body"]["storage"]["value"]
        except (KeyError, TypeError):
            raise ConfluenceError("Confluence response lacks page identity, version, or storage content.") from None
        if page_id != self.config.page_id:
            raise ConfluenceError("Confluence returned a different page ID than configured.")
        if data.get("status") != "current":
            raise ConfluenceError("Only an existing published/current Confluence page can be updated.")
        if self.config.mode == "data_center" and data.get("type") != "page":
            raise ConfluenceError("Configured Data Center content is not a page.")
        if not isinstance(title, str) or not isinstance(storage_body, str):
            raise ConfluenceError("Confluence title or storage content has an unexpected type.")
        if type(version) is not int or version < 1:
            raise ConfluenceError("Confluence returned an invalid page version.")
        return PageSnapshot(page_id, title, version, storage_body)

    def publish(
        self,
        managed_html: str,
        *,
        dry_run: bool = True,
        version_message: str = "Refresh frontier model register",
    ) -> PublishResult:
        """Fetch live content, prepare a scoped replacement, and optionally PUT it.

        Dry run performs a GET but no write. ``new_version`` is set only after
        a successful PUT. The generated fragment must already be escaped by the
        renderer; this module accepts trusted storage markup, not raw vendor HTML.
        """
        _validate_fragment(managed_html)
        if type(dry_run) is not bool:
            raise ValueError("dry_run must be a boolean.")
        if not isinstance(version_message, str) or len(version_message) > 255:
            raise ValueError("version_message must be at most 255 characters.")
        page = self.get_page()
        proposed_body, changed = replace_managed_section(page.storage_body, managed_html)
        digest = sha256(proposed_body.encode("utf-8")).hexdigest()
        if not changed:
            return PublishResult("unchanged", page.page_id, page.version, None, False, digest, proposed_body)
        if dry_run:
            return PublishResult("dry_run", page.page_id, page.version, None, True, digest, proposed_body)
        version = {"number": page.version + 1, "message": version_message}
        payload: dict[str, Any] = {
            "id": page.page_id,
            "status": "current",
            "title": page.title,
            "version": version,
        }
        if self.config.mode == "cloud":
            payload["body"] = {"representation": "storage", "value": proposed_body}
        else:
            payload["type"] = "page"
            payload["body"] = {"storage": {"representation": "storage", "value": proposed_body}}
        # A conflict or transport failure is returned to the caller, never retried.
        self._request("PUT", json=payload)
        return PublishResult("published", page.page_id, page.version, page.version + 1, True, digest, proposed_body)
