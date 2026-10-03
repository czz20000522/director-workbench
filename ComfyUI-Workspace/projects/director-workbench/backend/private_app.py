"""Authenticated entry point for the existing single-process workbench."""
import os
from pathlib import Path

from . import app as backend
from .private_mode import install
from .private_sessions import AccountStore, username_valid
from .user_context import Layout, PERMITTED_ROOT
from .storage import storage_roots


def configure(environ=None):
    env = os.environ if environ is None else environ
    account_path = Path(env.get('DIRECTOR_ACCOUNTS_FILE', str(PERMITTED_ROOT / 'runtime/director-private/accounts-hashed.json')))
    accounts = AccountStore(account_path)
    document = accounts._read()
    if not document['users']:
        raise RuntimeError('请先使用 tools/private_accounts.py 创建账号，再启动私人工作台')
    admins = tuple(name.strip() for name in env.get('DIRECTOR_ADMIN_USERS', '').split(',') if name.strip())
    if not admins or any(not username_valid(name) or name not in document['users'] for name in admins):
        raise RuntimeError('DIRECTOR_ADMIN_USERS 必须指定已创建的管理员账号')
    origins = tuple(value.strip() for value in env.get('DIRECTOR_PRIVATE_ORIGINS', 'http://127.0.0.1:4100,http://localhost:4100').split(',') if value.strip())
    layout = Layout(Path(env.get('DIRECTOR_PRIVATE_ROOT', str(storage_roots(backend.ROOT)['private']))))
    install(backend, accounts=accounts, layout=layout, origins=origins,
            administrators=admins, secure_cookie=env.get('DIRECTOR_COOKIE_SECURE') == '1')
    return backend.app


app = configure()
