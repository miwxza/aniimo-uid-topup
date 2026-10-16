"""Start a local account-session preview; the credential stays in process memory."""
import argparse
import getpass
import os
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description='Local session preview for an operator-owned Aniimo test account.')
    parser.add_argument('--port', type=int, default=8002)
    parser.add_argument('--proxy', action='store_true', help='Prompt for an existing Taiwan HTTP proxy URL without saving it')
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('port must be between 1024 and 65535')
    token = getpass.getpass('Fresh operator Bearer token (hidden; not saved): ').strip()
    if token.lower().startswith('bearer '):
        token = token[7:].strip()
    if not token:
        parser.error('A fresh operator-owned token is required. Do not reuse a screenshot credential.')
    env = os.environ.copy()
    env.update(ANIIMO_ENV='dev', ANIIMO_ROLE_PROVIDER='session',
               ANIIMO_ACCOUNT_BEARER_TOKEN=token,
               ANIIMO_IDEMPOTENCY_BACKEND='memory', PYTHONDONTWRITEBYTECODE='1')
    if args.proxy:
        from urllib.parse import urlsplit
        proxy = getpass.getpass('Taiwan HTTP proxy URL (hidden; not saved): ').strip()
        parsed = urlsplit(proxy)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname:
            parser.error('Proxy must be an existing http:// or https:// URL.')
        env['ANIIMO_OUTBOUND_PROXY'] = proxy
    print(f'Local session preview: http://127.0.0.1:{args.port}/')
    print('Only the connected account roles can be verified. This is not arbitrary UID lookup.')
    print('This mode calls live services. Do not create an order unless you intend to test a live order.')
    try:
        subprocess.run([sys.executable, '-B', '-m', 'uvicorn', 'app.main:app',
                        '--host', '127.0.0.1', '--port', str(args.port)],
                       cwd=Path(__file__).resolve().parents[1], env=env, check=True)
    except KeyboardInterrupt:
        pass
    finally:
        env.pop('ANIIMO_ACCOUNT_BEARER_TOKEN', None)


if __name__ == '__main__':
    main()
