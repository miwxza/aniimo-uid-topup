import httpx
import pytest
import respx
from app.config import Settings
from app.aniimo.client import AniimoClient
from app.aniimo.store import PaymentContext, assert_region_and_channel
from app.errors import RegionCurrencyMismatchError
from app.models import Package


@pytest.mark.parametrize('currency,country',[('THB','TW'),('TWD','TH')])
def test_twd_product_does_not_override_conflicting_context(currency,country):
    p=Package(package_id='1',product_id='p',coin=60,currency='TWD',price=30,final_price=28)
    with pytest.raises(RegionCurrencyMismatchError):
        assert_region_and_channel(PaymentContext(currency,['mycard'],country),p,Settings(_env_file=None))


@pytest.mark.asyncio
@respx.mock
async def test_nested_business_order_payload():
    settings=Settings(_env_file=None)
    respx.post(settings.store_api_base+'/api/place_order').mock(return_value=httpx.Response(200,json={
        'code':0,'data':{'code':0,'message':'ok','data':{'order_id':'ORD-NESTED','pay_url':'https://example.test/pay'}}}))
    async with AniimoClient(settings) as client:
        result=await client.place_order('4',{'openid':'synthetic-test'},idempotency_key='synthetic-key')
    assert result['order_id']=='ORD-NESTED'


def test_upstream_server_id_is_json_integer():
    assert AniimoClient(Settings(_env_file=None))._envelope("4")["server_id"] == 4
