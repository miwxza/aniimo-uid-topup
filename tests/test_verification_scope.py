import httpx
import pytest
import respx
from tests.test_api import live_mode, mock_mode, PARTNER
from app.config import Settings
from app.aniimo.providers import SessionRoleProvider
from app.errors import UidNotInAccountError


@pytest.mark.asyncio
@respx.mock
async def test_other_account_uid_is_not_reported_as_globally_missing():
    s=Settings(role_provider='session',account_bearer_token='synthetic-token',_env_file=None)
    respx.get(s.account_api_base+'/api/player/roles').mock(return_value=httpx.Response(200,json={'code':200,'data':{'roles':[]}}))
    with pytest.raises(UidNotInAccountError) as exc:
        await SessionRoleProvider(s).resolve('999999999','4')
    assert exc.value.code=='UID_NOT_IN_ACCOUNT'


@respx.mock
def test_verify_circuit_stops_repeated_upstream_fault(live_mode):
    route=respx.post(PARTNER).mock(return_value=httpx.Response(503))
    data={'uid':'412000126173','server':'4'}
    assert live_mode.post('/api/v1/aniimo/verify',json=data).status_code==502
    assert live_mode.post('/api/v1/aniimo/verify',json=data).status_code==502
    assert route.call_count==1


@pytest.mark.parametrize('uid,server',[('１２３４','4'),('412000126173','0'),('412000126173','abc')])
def test_verify_rejects_invalid_identifiers(mock_mode,uid,server):
    assert mock_mode.post('/api/v1/aniimo/verify',json={'uid':uid,'server':server}).status_code==422


@respx.mock
@pytest.mark.parametrize('response',[{'code':500,'data':{'role_name':'Fake'}},{'data':{'uid':'999999999','role_name':'Wrong role'}}])
def test_partner_errors_or_wrong_identity_never_verify(live_mode,response):
    respx.post(PARTNER).mock(return_value=httpx.Response(200,json=response))
    r=live_mode.post('/api/v1/aniimo/verify',json={'uid':'412000126173','server':'4'})
    assert r.status_code==502
