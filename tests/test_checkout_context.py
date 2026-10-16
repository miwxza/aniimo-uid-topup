import json
import httpx
import respx
from tests.test_api import live_mode, mock_mode, STORE, PARTNER, _products, _store_init, _partner_role


def test_mock_role_catalogue_is_ready(mock_mode):
    r=mock_mode.get('/api/v1/aniimo/packages',params={'uid':'412000126173','server':'4'})
    assert r.status_code==200
    assert r.json()['checkout_ready'] is True
    assert r.json()['country']=='TW'


@respx.mock
def test_catalogue_uses_server_resolved_identity(live_mode):
    respx.post(PARTNER).mock(return_value=_partner_role())
    init=_store_init();init['data']['get_ip_currency']['data']['country']='TW'
    ctx=respx.post(STORE+'/api/store/init').mock(return_value=httpx.Response(200,json=init))
    products=respx.post(STORE+'/api/game_products').mock(return_value=httpx.Response(200,json=_products()))
    r=live_mode.get('/api/v1/aniimo/packages',params={'uid':'412000126173','server':'4'})
    assert r.status_code==200 and r.json()['checkout_ready'] is True
    for route in [ctx,products]:
        payload=json.loads(route.calls[0].request.content)
        assert payload['openid']=='oid_real_123' and payload['server_id']==4


@respx.mock
def test_th_catalogue_reports_not_ready(live_mode):
    respx.post(PARTNER).mock(return_value=_partner_role())
    init=_store_init(currency='THB',channels=());init['data']['get_ip_currency']['data']['country']='TH'
    respx.post(STORE+'/api/store/init').mock(return_value=httpx.Response(200,json=init))
    respx.post(STORE+'/api/game_products').mock(return_value=httpx.Response(200,json=_products(currency='THB')))
    r=live_mode.get('/api/v1/aniimo/packages',params={'uid':'412000126173','server':'4'})
    assert r.status_code==200
    assert r.json()['country']=='TH' and r.json()['currency']=='THB'
    assert r.json()['mycard_available'] is False and r.json()['checkout_ready'] is False
