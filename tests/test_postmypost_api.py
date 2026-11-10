import json
from copy import deepcopy

import httpx
import pytest

from bmauto.postmypost import Postmypost, PostmypostError, UnknownPublication, confirmed_publication, storage_url


@pytest.mark.parametrize('outcome', ['500', 'timeout', 'invalid-json'])
def test_create_unknown_response_is_not_retried(outcome):
    calls = []
    def handler(request):
        calls.append(request)
        if outcome == 'timeout':
            raise httpx.ReadTimeout('private-token', request=request)
        return httpx.Response(500 if outcome == '500' else 200, content=b'not-json')
    api = Postmypost('private-token', http=httpx.Client(transport=httpx.MockTransport(handler)), budget=False)
    with pytest.raises(UnknownPublication) as error:
        api.create({'project_id': 10})
    assert len(calls) == 1 and 'private-token' not in str(error.value)
    api.close()


@pytest.mark.parametrize('status', [401, 403, 422, 429])
def test_errors_do_not_leak_credentials_or_response_bodies(status):
    def handler(request):
        return httpx.Response(status, json={'error': 'private-token https://secret.example/signed-upload'})
    api = Postmypost('private-token', http=httpx.Client(transport=httpx.MockTransport(handler)), budget=False)
    with pytest.raises(PostmypostError) as error:
        api.projects()
    assert str(status) in str(error.value) and 'private-token' not in str(error.value) and 'secret.example' not in str(error.value)
    api.close()


def test_paginated_lists_use_bearer_and_retrieve_all_pages():
    calls = []
    def handler(request):
        calls.append(request)
        assert request.headers['Authorization'] == 'Bearer private-token'
        page = int(request.url.params['page'])
        return httpx.Response(200, json={'data': [{'id': page, 'name': str(page)}], 'pages': {'total_pages': 2}})
    api = Postmypost('private-token', http=httpx.Client(transport=httpx.MockTransport(handler)), budget=False)
    assert [item['id'] for item in api.projects()] == [1, 2]
    assert len(calls) == 2
    api.close()


def test_upload_isolated_storage_auth_uses_key_value_fields_and_waits_for_file(tmp_path):
    path = tmp_path / 'image.png'
    path.write_bytes(b'actual-image-bytes')
    calls, uploaded = [], []
    def storage_handler(request):
        assert 'Authorization' not in request.headers and 'Cookie' not in request.headers
        assert request.url == 'https://storage.example/bucket'
        assert b'name="policy"' in request.content and b'signed-policy' in request.content
        assert b'actual-image-bytes' in request.content
        uploaded.append(request)
        return httpx.Response(204)
    def api_handler(request):
        assert request.headers['Authorization'] == 'Bearer private-token'
        calls.append(request)
        if request.url.path.endswith('/upload/init'):
            assert json.loads(request.content) == {'project_id': 10, 'name': 'image.png', 'size': 18}
            return httpx.Response(200, headers={'Set-Cookie': 'private=value; Domain=.example'}, json={
                'id': 7, 'action': 'https://storage.example/bucket', 'fields': [{'key': 'policy', 'value': 'signed-policy'}]})
        if request.url.path.endswith('/upload/complete'):
            assert request.method == 'POST' and request.url.params['id'] == '7'
            return httpx.Response(200, json={'id': 7, 'status': 3})
        assert request.url.params['id'] == '7'
        return httpx.Response(200, json={'id': 7, 'status': 1, 'file_id': 800})
    api = Postmypost('private-token', http=httpx.Client(transport=httpx.MockTransport(api_handler)),
        storage_factory=lambda **kwargs: httpx.Client(transport=httpx.MockTransport(storage_handler), **kwargs), budget=False)
    assert api.upload(10, path, 'image/png') == 800
    assert len(uploaded) == 1 and len(calls) == 3
    api.close()


@pytest.mark.parametrize('url', ['http://storage.example/', 'https://127.0.0.1/', 'https://10.1.2.3/',
                                  'https://user:password@storage.example/', 'https://storage.local/', 'https://storage.example:8080/'])
def test_invalid_storage_destinations_are_rejected(url):
    assert not storage_url(url)


def test_confirmed_requires_pending_status_exact_time_accounts_text_and_photos():
    payload = {'account_ids': [20, 21], 'post_at': '2099-11-11T10:45:00+00:00', 'details': [
        {'account_id': 20, 'content': 'Текст', 'file_ids': [800]},
        {'account_id': 21, 'content': '', 'file_ids': []}]}
    body = {'id': 90, 'publication_status': 5, 'account_ids': [21, 20], 'post_at': '2099-11-11T13:45:00+03:00', 'details': [
        {'account_id': 20, 'publication_type': 1, 'content': 'Текст', 'files': [{'id': 800}]},
        {'account_id': 21, 'publication_type': 1, 'content': None, 'files': None}]}
    assert confirmed_publication(body, 90, payload)
    for field, wrong in [('publication_status', 4), ('account_ids', [20]), ('post_at', '2099-11-11T14:45:00+03:00'), ('details', [])]:
        changed = {**body, field: wrong}
        assert not confirmed_publication(changed, 90, payload)
    changed = deepcopy(body)
    changed['details'][0]['files'][0]['id'] = 999
    assert not confirmed_publication(changed, 90, payload)
