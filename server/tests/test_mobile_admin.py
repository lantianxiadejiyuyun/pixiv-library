import json

from app.auth import COOKIE_NAME
from app.main import app
from app.mobile_admin import TITLES, catalog
from test_devices import bind, bearer
from test_library import client


def test_management_catalog_requires_admin_not_pairing_authority(client):
    cookie = client.cookies.get(COOKIE_NAME)
    response = client.get('/api/mobile-admin/catalog')
    assert response.status_code == 200
    serialized = response.text
    assert cookie not in serialized and 'local-library' not in serialized
    device = bind(client)
    assert client.get('/api/mobile-admin/catalog', headers=bearer(device)).status_code == 403
    client.cookies.clear()
    assert client.get('/api/mobile-admin/catalog').status_code == 401
    assert client.get('/api/mobile-admin/catalog', headers=bearer(device)).status_code == 403


def test_all_published_management_contracts_have_real_routes_and_native_types(client):
    result = catalog(app.openapi())
    assert len(result['actions']) == len(TITLES)
    assert len(result['groups']) == 8
    assert len({action['id'] for action in result['actions']}) == len(TITLES)
    assert '$ref' not in json.dumps(result)
    actions = {a['id']: a for a in result['actions']}
    roles = actions['PUT /sources/pixiv/account-roles']['body']['properties']
    assert roles['account_limits']['additionalProperties']['maximum'] == 8
    assert roles['content_account_ids']['type'] == 'array'
    queue = actions['PUT /queue/settings']['body']['properties']
    assert queue['concurrency']['maximum'] == 8
    assert queue['rate_limit_enabled']['type'] == 'boolean'
    assert actions['POST /imports/upload']['media_type'] == 'multipart/form-data'
    assert actions['POST /app-updates/android']['body']['format'] == 'binary'
    assert actions['POST /database/export']['download']
    assert actions['GET /items/{item_id}/export']['download']
    assert actions['DELETE /devices/{device_id}']['confirmation']
    assert actions['POST /translation/batch']['body']['properties']['consent']['const'] is True


def test_existing_administration_routes_are_exposed_without_new_execution_surface(client):
    actions = {a['id'] for a in catalog(app.openapi())['actions']}
    intentionally_existing_reader = {
        'POST /api/auth/login', 'POST /api/auth/logout', 'POST /api/devices/pairing/exchange',
        'POST /api/devices/disconnect', 'PUT /api/items/{item_id}/progress',
        'PUT /api/items/{item_id}/favorite', 'PUT /api/items/{item_id}/score',
        'POST /api/items/{item_id}/visit', 'POST /api/network/speed/upload', 'POST /api/pixiv/direct/credentials',
    }
    for path, operations in app.openapi()['paths'].items():
        for method in operations:
            if method.upper() not in {'POST', 'PUT', 'PATCH', 'DELETE'}:
                continue
            identity = method.upper() + ' ' + path
            assert identity in intentionally_existing_reader or identity.replace(' /api/', ' /') in actions, identity


def test_native_admin_cookie_can_manage_without_bearer_or_origin_change(client):
    # This is the same administrator login as the web; QR sessions stay narrow.
    client.cookies.clear()
    assert client.post('/api/auth/login', json={'password': 'wrong'}).status_code == 401
    assert client.post('/api/auth/login', json={'password': 'local-library'}).status_code == 200
    before = client.get('/api/queue/settings').json()
    changed = client.put('/api/queue/settings', json={'paused': not before['paused']})
    assert changed.status_code == 200
    assert changed.json()['paused'] is not before['paused']
    assert client.get('/api/devices').status_code == 200
    assert client.get('/api/platform-endpoints').status_code == 200
    assert client.post('/api/auth/logout').status_code == 200
    assert client.get('/api/mobile-admin/catalog').status_code == 401
