"""Single-process local accounts and opaque sessions; no routes or default users.

The account file is a NEW hashed schema, not the legacy plaintext accounts.json.
Only trusted administrator code may call account mutation methods or choose paths.
"""
from dataclasses import dataclass
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import threading
import time

from .user_context import PERMITTED_ROOT, SessionIdentity, no_links

ITERATIONS = 600_000


class AuthenticationError(ValueError):
    def __init__(self):
        super().__init__('用户名或密码错误')


def username_valid(value):
    return isinstance(value, str) and re.fullmatch(r'user[0-9]{3}', value) is not None


def password_bytes(value):
    if not isinstance(value, str) or not value or len(value.encode('utf-8')) > 1024:
        raise ValueError('Password must contain 1–1024 UTF-8 bytes')
    return value.encode('utf-8')


def derive(password, salt):
    return hashlib.pbkdf2_hmac('sha256', password, salt, ITERATIONS, dklen=32)


class AccountStore:
    def __init__(self, path: Path, *, permitted_root: Path = PERMITTED_ROOT):
        self.path = no_links(Path(path))
        self.permitted_root = no_links(Path(permitted_root))
        if not self.path.is_relative_to(self.permitted_root):
            raise ValueError('Account storage must be within the permitted server root')
        self._lock = threading.RLock()
        self._dummy_salt = secrets.token_bytes(16)
        self._dummy_hash = secrets.token_bytes(32)

    def _read(self):
        no_links(self.path)
        if not self.path.exists():
            return {'schema_version': 1, 'users': {}}
        if self.path.stat().st_size > 1024 * 1024:
            raise ValueError('Invalid account store')
        document = json.loads(self.path.read_text(encoding='utf-8'))
        if not isinstance(document, dict) or document.get('schema_version') != 1 or not isinstance(document.get('users'), dict):
            raise ValueError('Unsupported account schema; automatic plaintext migration is disabled')
        for name, record in document['users'].items():
            if not username_valid(name) or not isinstance(record, dict):
                raise ValueError('Invalid account record')
            if record.get('scheme') != 'pbkdf2_sha256' or type(record.get('iterations')) is not int or record['iterations'] != ITERATIONS:
                raise ValueError('Unsupported password scheme')
            for key, length in (('salt', 32), ('digest', 64), ('version', 32)):
                if not isinstance(record.get(key), str) or not re.fullmatch('[0-9a-f]{' + str(length) + '}', record[key]):
                    raise ValueError('Invalid account record')
        return document

    def _write(self, document):
        no_links(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + '.' + secrets.token_hex(8) + '.tmp')
        with temporary.open('x', encoding='utf-8') as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(self.path)

    def _set(self, username, password, create):
        if not username_valid(username):
            raise ValueError('Invalid account name')
        encoded = password_bytes(password)
        with self._lock:
            document = self._read()
            exists = username in document['users']
            if create == exists:
                raise ValueError('Account already exists' if create else 'Account does not exist')
            salt = secrets.token_bytes(16)
            document['users'][username] = {'scheme': 'pbkdf2_sha256', 'iterations': ITERATIONS,
                'salt': salt.hex(), 'digest': derive(encoded, salt).hex(), 'version': secrets.token_hex(16)}
            self._write(document)

    def create_account(self, username: str, password: str):
        self._set(username, password, True)

    def set_password(self, username: str, password: str):
        self._set(username, password, False)

    def authenticate(self, username, password):
        """Returns an internal credential version, never a session token."""
        try:
            encoded = password_bytes(password)
        except (ValueError, UnicodeError):
            encoded = b''
        with self._lock:
            try:
                record = self._read()['users'].get(username) if username_valid(username) else None
            except (OSError, ValueError):
                record = None
            salt = bytes.fromhex(record['salt']) if record else self._dummy_salt
            expected = bytes.fromhex(record['digest']) if record else self._dummy_hash
            matched = hmac.compare_digest(derive(encoded, salt), expected)
            return record['version'] if record and matched and encoded else None

    def account_version(self, username):
        with self._lock:
            try:
                record = self._read()['users'].get(username) if username_valid(username) else None
            except (OSError, ValueError):
                return None
            return record['version'] if record else None


@dataclass(frozen=True)
class _Session:
    identity: SessionIdentity
    account_version: str


class SessionManager:
    def __init__(self, accounts: AccountStore, *, ttl_seconds=43200, clock=time.time):
        if type(ttl_seconds) not in (int, float) or not math.isfinite(ttl_seconds) or not 0 < ttl_seconds <= 604800:
            raise ValueError('Session lifetime must be finite, positive and at most seven days')
        self.accounts, self.ttl_seconds, self.clock = accounts, ttl_seconds, clock
        self._sessions = {}
        self._lock = threading.RLock()

    @staticmethod
    def _key(token):
        if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
            return None
        return hashlib.sha256(token.encode('ascii')).digest()

    def login(self, username: str, password: str) -> str:
        version = self.accounts.authenticate(username, password)
        if version is None:
            raise AuthenticationError()
        now = self.clock()
        token = secrets.token_urlsafe(32)
        identity = SessionIdentity(username, now + self.ttl_seconds)
        with self._lock:
            # Bound stale memory without storing or logging raw tokens.
            self._sessions = {k: s for k, s in self._sessions.items() if s.identity.expires_at > now}
            self._sessions[self._key(token)] = _Session(identity, version)
        return token

    def lookup(self, token: str) -> SessionIdentity | None:
        key = self._key(token)
        if key is None:
            return None
        with self._lock:
            session = self._sessions.get(key)
            if session is None:
                return None
            if session.identity.expires_at <= self.clock() or self.accounts.account_version(session.identity.username) != session.account_version:
                self._sessions.pop(key, None)
                return None
            return session.identity

    def logout(self, token: str):
        with self._lock:
            self._sessions.pop(self._key(token), None)

    def revoke_user(self, username: str):
        """Trusted administrative action, not a client-selected identity switch."""
        with self._lock:
            self._sessions = {key: value for key, value in self._sessions.items() if value.identity.username != username}
