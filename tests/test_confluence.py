"""Offline tests for publication scope and concurrency; no real API calls."""

from __future__ import annotations

import unittest

import httpx

from frontier_register.confluence import (
    ConfluenceConfig,
    ConfluenceError,
    ConfluencePublisher,
    END_MARKER,
    ManagedSectionError,
    START_MARKER,
    VersionConflict,
    managed_section,
    replace_managed_section,
)


class FakeResponse:
    def __init__(self, status_code=200, data=None):
        self.status_code = status_code
        self._data = {} if data is None else data

    def json(self):
        return self._data


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def page_response(body=None, version=8):
    return FakeResponse(data={
        "id": "12345", "title": "Governance decisions", "type": "page",
        "status": "current", "version": {"number": version},
        "body": {"storage": {"value": body if body is not None else managed_section("<p>Old evidence</p>")}},
    })


class ManagedSectionTests(unittest.TestCase):
    def test_preserves_surrounding_content_and_marker_markup(self):
        prefix = '<h2>Approved exceptions</h2><p ac:local-id="42">Do not alter me.</p>'
        start = f'<p data-id="7"><strong>{START_MARKER}</strong></p>'
        end = f'<p><em>{END_MARKER}</em></p>'
        suffix = '<ac:structured-macro ac:name="attachments" /><p>Manual footer &amp; owner.</p>'
        original = prefix + start + '<table><tr><td>Old</td></tr></table>' + end + suffix
        fragment = '<table><tr><td>Reviewed &amp; escaped</td></tr></table>'
        updated, changed = replace_managed_section(original, fragment)
        self.assertTrue(changed)
        self.assertEqual(updated, prefix + start + '\n' + fragment + '\n' + end + suffix)

    def test_unchanged_content_keeps_original_bytes(self):
        body = f'<p>{START_MARKER}</p>  \n<p>Same</p>\n  <p>{END_MARKER}</p>'
        self.assertEqual(replace_managed_section(body, '<p>Same</p>'), (body, False))

    def test_missing_duplicate_or_inverted_markers_refused(self):
        candidates = [
            '<p>No markers</p>',
            f'<p>{START_MARKER}</p><p>{START_MARKER}</p><p>{END_MARKER}</p>',
            f'<p>{END_MARKER}</p><p>{START_MARKER}</p>',
            f'<!--{START_MARKER}--><p>{END_MARKER}</p>',
            f'<p>Instructions {START_MARKER}</p><p>{END_MARKER}</p>',
        ]
        for body in candidates:
            with self.subTest(body=body), self.assertRaises(ManagedSectionError):
                replace_managed_section(body, '<p>New</p>')

    def test_empty_or_marker_containing_fragment_refused(self):
        for fragment in ['', '   ', f'<p>{START_MARKER}</p>', f'<p>{END_MARKER}</p>']:
            with self.subTest(fragment=fragment), self.assertRaises(ManagedSectionError):
                managed_section(fragment)

    def test_nested_markers_cannot_cross_container_boundaries(self):
        candidates = [
            f'<table><tr><td><p>{START_MARKER}</p></td></tr></table><p>{END_MARKER}</p>',
            f'<div><p>{START_MARKER}</p><p>{END_MARKER}</p></div>',
            f'<ac:structured-macro ac:name="panel"><ac:rich-text-body><p>{START_MARKER}</p></ac:rich-text-body></ac:structured-macro><p>{END_MARKER}</p>',
            f'<ac:plain-text-body><![CDATA[<p>{START_MARKER}</p>]]></ac:plain-text-body><p>{END_MARKER}</p>',
        ]
        for body in candidates:
            with self.subTest(body=body), self.assertRaises(ManagedSectionError):
                replace_managed_section(body, '<p>New</p>')


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.env = {'CONFLUENCE_API_TOKEN': 'test-token-not-a-secret', 'CONFLUENCE_EMAIL': 'service@example.test'}

    def make_publisher(self, responses, mode='cloud', base_url='https://example.atlassian.net/wiki'):
        session = FakeSession(responses)
        publisher = ConfluencePublisher(ConfluenceConfig(mode, base_url, '12345'), session, self.env)
        return publisher, session

    def test_default_is_read_only_dry_run(self):
        publisher, session = self.make_publisher([page_response()])
        result = publisher.publish('<p>New evidence</p>')
        self.assertEqual(result.status, 'dry_run')
        self.assertTrue(result.changed)
        self.assertIsNone(result.new_version)
        self.assertEqual([c[0] for c in session.calls], ['GET'])
        self.assertEqual(session.calls[0][2]['params'], {'body-format': 'storage'})
        self.assertFalse(session.calls[0][2]['follow_redirects'])
        self.assertEqual(session.calls[0][2]['timeout'], 30.0)

    def test_cloud_update_uses_live_version_and_preserves_title(self):
        body = '<h1>Human section</h1>' + managed_section('<p>Old</p>') + '<p>Human footer</p>'
        publisher, session = self.make_publisher([page_response(body, 12), FakeResponse()])
        result = publisher.publish('<p>New</p>', dry_run=False)
        self.assertEqual(result.status, 'published')
        self.assertEqual((result.old_version, result.new_version), (12, 13))
        payload = session.calls[1][2]['json']
        self.assertEqual(payload['version']['number'], 13)
        self.assertEqual(payload['title'], 'Governance decisions')
        self.assertEqual(payload['status'], 'current')
        self.assertEqual(payload['body']['representation'], 'storage')
        self.assertTrue(payload['body']['value'].startswith('<h1>Human section</h1>'))
        self.assertTrue(payload['body']['value'].endswith('<p>Human footer</p>'))
        self.assertFalse(session.calls[1][2]['follow_redirects'])

    def test_unchanged_body_never_puts_even_when_publish_enabled(self):
        publisher, session = self.make_publisher([page_response(managed_section('<p>Same</p>'))])
        result = publisher.publish('<p>Same</p>', dry_run=False)
        self.assertEqual(result.status, 'unchanged')
        self.assertFalse(result.changed)
        self.assertEqual(len(session.calls), 1)

    def test_data_center_endpoint_and_body_shape(self):
        publisher, session = self.make_publisher([page_response(), FakeResponse()], 'data_center', 'https://confluence.example.test/confluence')
        publisher.publish('<p>New</p>', dry_run=False)
        self.assertEqual(session.calls[0][1], 'https://confluence.example.test/confluence/rest/api/content/12345')
        self.assertEqual(session.calls[0][2]['params'], {'expand': 'body.storage,version'})
        payload = session.calls[1][2]['json']
        self.assertEqual(payload['type'], 'page')
        self.assertEqual(payload['body']['storage']['representation'], 'storage')
        request = httpx.Request('GET', 'https://example.test')
        request = next(session.calls[0][2]['auth'].auth_flow(request))
        self.assertEqual(request.headers['Authorization'], 'Bearer test-token-not-a-secret')

    def test_conflict_does_not_retry(self):
        publisher, session = self.make_publisher([page_response(), FakeResponse(409)])
        with self.assertRaises(VersionConflict):
            publisher.publish('<p>New</p>', dry_run=False)
        self.assertEqual([c[0] for c in session.calls], ['GET', 'PUT'])

    def test_redirect_is_not_followed(self):
        publisher, session = self.make_publisher([FakeResponse(302)])
        with self.assertRaisesRegex(ConfluenceError, 'redirect'):
            publisher.publish('<p>New</p>', dry_run=False)
        self.assertEqual(len(session.calls), 1)
        self.assertFalse(session.calls[0][2]['follow_redirects'])

    def test_transport_error_does_not_expose_raw_detail(self):
        publisher, session = self.make_publisher([httpx.ConnectError('secret-token-in-raw-detail')])
        with self.assertRaises(ConfluenceError) as caught:
            publisher.publish('<p>New</p>')
        self.assertNotIn('secret-token', str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    def test_missing_markers_prevent_write(self):
        publisher, session = self.make_publisher([page_response('<p>Human content only</p>')])
        with self.assertRaises(ManagedSectionError):
            publisher.publish('<p>New</p>', dry_run=False)
        self.assertEqual([c[0] for c in session.calls], ['GET'])

    def test_invalid_identity_or_noncurrent_page_prevent_write(self):
        for patch in ({'id': '67890'}, {'status': 'draft'}, {'version': {'number': True}}):
            response = page_response()
            response._data.update(patch)
            publisher, session = self.make_publisher([response])
            with self.subTest(patch=patch), self.assertRaises(ConfluenceError):
                publisher.publish('<p>New</p>', dry_run=False)
            self.assertEqual(len(session.calls), 1)

    def test_credentials_are_environment_only(self):
        session = FakeSession([])
        publisher = ConfluencePublisher(ConfluenceConfig('cloud', 'https://example.test', '12345'), session, {})
        with self.assertRaisesRegex(ConfluenceError, 'CONFLUENCE_API_TOKEN'):
            publisher.publish('<p>New</p>')
        self.assertEqual(session.calls, [])


class ConfigTests(unittest.TestCase):
    def test_cloud_root_and_gateway_paths(self):
        root = ConfluenceConfig('cloud', 'https://example.atlassian.net', '12345')
        gateway = ConfluenceConfig('cloud', 'https://api.atlassian.com/ex/confluence/cloud-id/wiki/', '12345')
        self.assertEqual(root.page_url, 'https://example.atlassian.net/wiki/api/v2/pages/12345')
        self.assertEqual(gateway.page_url, 'https://api.atlassian.com/ex/confluence/cloud-id/wiki/api/v2/pages/12345')

    def test_unsafe_or_ambiguous_target_refused(self):
        for url in ['http://example.test', 'https://user:pass@example.test', 'https://example.test?token=secret', 'https://example.test#x', 'https://example.test/../wiki']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                ConfluenceConfig('cloud', url, '12345')
        for page_id in ['', '123/other', '0', 12345]:
            with self.subTest(page_id=page_id), self.assertRaises(ValueError):
                ConfluenceConfig('cloud', 'https://example.test', page_id)


if __name__ == '__main__':
    unittest.main()
