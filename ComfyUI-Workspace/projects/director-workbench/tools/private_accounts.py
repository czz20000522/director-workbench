"""Trusted local administrator command. Never expose this command as an API."""
import argparse
from getpass import getpass
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.private_sessions import AccountStore
from backend.user_context import PERMITTED_ROOT


def main(argv=None, password_prompt=getpass):
    parser = argparse.ArgumentParser(description='本地私人工作台账号管理，不读取旧明文账号文件')
    parser.add_argument('operation', choices=('create', 'password'))
    parser.add_argument('username')
    parser.add_argument('--accounts-file', type=Path, default=PERMITTED_ROOT / 'runtime/director-private/accounts-hashed.json')
    args = parser.parse_args(argv)
    password = password_prompt('密码（不回显）: ')
    if password != password_prompt('再次输入密码: '):
        parser.error('两次输入不一致，未修改账号')
    store = AccountStore(args.accounts_file)
    try:
        if args.operation == 'create':
            store.create_account(args.username, password)
        else:
            store.set_password(args.username, password)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print('账号已创建' if args.operation == 'create' else '密码已更新，原会话将在下次请求时失效')


if __name__ == '__main__':
    main()
