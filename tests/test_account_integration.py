"""Office-account contract from browser capture; credentials here are synthetic."""
import httpx
import pytest
import respx

from app.aniimo.client import AniimoClient
from app.aniimo.providers import SessionRoleProvider
from app.config import Settings
from app.errors import AccountAuthenticationError, InvalidUidError, UpstreamBusinessError, UpstreamUnavailableError

URL = 'https://worldx-office-api.aniimo.com/api/player/roles'
UID = '412001104638'


def settings():
    return Settings(role_provider='session', account_bearer_token='synthetic-test-token', _env_file=None)


def role_response(**changes):
    role = dict(roleId=UID, roleName='Miwx', serverArea=4, openid='test-openid', level='0', avatarUrl='https://example.test/avatar.png')
    role.update(changes)
    return {'code': 200, 'msg': None, 'data': {'roles': [role]}}


@pytest.mark.asyncio
@respx.mock
async def test_office_bearer_contract_and_role_mapping():
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=role_response()))
    role = await SessionRoleProvider(settings()).resolve(UID, '4')
    assert role.role_name == 'Miwx' and role.openid == 'test-openid'
    assert role.level == 0 and role.avatar_url == 'https://example.test/avatar.png'
    req = route.calls[0].request
    assert req.headers['Authorization'] == 'Bearer synthetic-test-token'
    assert 'cookie' not in req.headers and not req.url.query
    assert not role.is_mock


@pytest.mark.asyncio
@respx.mock
async def test_session_does_not_resolve_other_uid():
    respx.get(URL).mock(return_value=httpx.Response(200, json=role_response()))
    with pytest.raises(InvalidUidError):
        await SessionRoleProvider(settings()).resolve('999999999', '4')


@pytest.mark.asyncio
@respx.mock
async def test_session_wrong_server_is_not_found():
    respx.get(URL).mock(return_value=httpx.Response(200, json=role_response()))
    with pytest.raises(InvalidUidError):
        await SessionRoleProvider(settings()).resolve(UID, '5')


@pytest.mark.asyncio
@respx.mock
async def test_unnamed_role_is_not_given_fake_name():
    respx.get(URL).mock(return_value=httpx.Response(200, json=role_response(roleName='', nickname='')))
    role = await SessionRoleProvider(settings()).resolve(UID, '4')
    assert role.role_name == ''


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize('status,code', [(401,401), (403,403), (200,401)])
async def test_expired_credential_is_actionable(status, code):
    respx.get(URL).mock(return_value=httpx.Response(status, json={'code':code,'data':None}))
    with pytest.raises(AccountAuthenticationError) as exc:
        await SessionRoleProvider(settings()).resolve(UID, '4')
    assert 'synthetic-test-token' not in str(exc.value)


@pytest.mark.asyncio
@respx.mock
async def test_invalid_account_shape_is_not_uid_not_found():
    respx.get(URL).mock(return_value=httpx.Response(200, json={'code':200,'data':{}}))
    with pytest.raises(UpstreamUnavailableError):
        await SessionRoleProvider(settings()).resolve(UID, '4')


def test_account_code_200_does_not_relax_store_contract():
    client = AniimoClient(settings())
    with pytest.raises(UpstreamBusinessError):
        client._unwrap(httpx.Response(200, json={'code':200,'data':{}}))


def test_token_is_redacted_from_settings():
    assert 'synthetic-test-token' not in repr(settings())
