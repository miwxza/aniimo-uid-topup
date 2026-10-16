r"""Read-only live checks. No order creation and no credentials printed.

Run from repository: .venv\Scripts\python.exe -m tools.check_live
Reads ANIIMO_* settings/.env. Credentials must be configured locally.
"""
import argparse
import asyncio
import json
from app.config import Settings
from app.aniimo.client import AniimoClient
from app.aniimo.store import LiveStore
from app.errors import AniimoError


async def inspect(server):
    s=Settings()
    report={'server':server,'mode':'read_only','orders_created':0}
    try:
        ctx=await LiveStore(s).payment_context(server)
        report['store']={'country':ctx.country or None,'currency':ctx.currency or None,
                         'mycard_advertised':s.payment_channel_id.lower() in [x.lower() for x in ctx.channels],
                         'channel_count':len(ctx.channels)}
    except AniimoError as e:report['store']={'error':e.code}
    except Exception:report['store']={'error':'CHECK_FAILED'}
    if s.account_bearer_token or s.account_session_cookie:
        try:
            async with AniimoClient(s) as client:
                data=await client.player_roles(s.account_session_cookie)
            report['account']={'authenticated':True,'role_count':len(data['roles']),
                               'scope':'operator_account_only'}
        except AniimoError as e:report['account']={'authenticated':False,'error':e.code}
        except Exception:report['account']={'authenticated':False,'error':'CHECK_FAILED'}
    else:report['account']={'authenticated':False,'error':'CREDENTIAL_NOT_CONFIGURED'}
    report['arbitrary_uid_lookup']='not_verified'
    store=report['store']
    report['tw_checkout_ready']=store.get('country')=='TW' and store.get('currency')=='TWD' and store.get('mycard_advertised') is True
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description='Read-only Aniimo connection check; never creates orders.')
    parser.add_argument('--server',default='4')
    args=parser.parse_args()
    if not args.server.isascii() or not args.server.isdigit() or not 1<=len(args.server)<=6:
        parser.error('server must contain 1-6 ASCII digits')
    print(json.dumps(asyncio.run(inspect(args.server)),ensure_ascii=False,indent=2))
