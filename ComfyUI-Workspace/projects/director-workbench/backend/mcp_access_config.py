"""Trusted server allowlists for the SDK's DNS rebinding checks.

Never infer permitted entries from a request, Host or proxy forwarding header.
Only loopback defaults allow wildcard ports; configured entries are exact.
"""
import ipaddress
import os
import re
from urllib.parse import urlsplit

from mcp.server.transport_security import TransportSecuritySettings

LOCAL_HOSTS = ('127.0.0.1:*', 'localhost:*', '[::1]:*')
LOCAL_ORIGINS = ('http://127.0.0.1:*', 'http://localhost:*', 'http://[::1]:*')


def _authority(value):
    if any(char.isspace() for char in value) or any(char in value for char in '*%@\\'):
        raise ValueError()
    parsed = urlsplit('http://' + value)
    if not parsed.hostname or parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError()
    port = parsed.port  # Reject malformed/out-of-range ports.
    if port == 0:
        raise ValueError()
    hostname = parsed.hostname
    if ':' in hostname:
        ipaddress.IPv6Address(hostname)
        canonical = '[' + hostname + ']'
    else:
        if not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', hostname):
            raise ValueError()
        if len(hostname) > 253 or any(not label or len(label) > 63 or label.startswith('-') or label.endswith('-')
                                     for label in hostname.split('.')):
            raise ValueError()
        canonical = hostname
    if port is not None:
        canonical += ':' + str(port)
    if value.lower() != canonical:
        raise ValueError()
    return canonical


def _configured(environ, name, *, origin=False):
    raw = environ.get(name, '')
    values = []
    try:
        for value in raw.split(','):
            value = value.strip()
            if not value:
                continue
            if origin:
                parsed = urlsplit(value)
                if parsed.scheme not in ('http', 'https') or value != parsed.scheme + '://' + parsed.netloc:
                    raise ValueError()
                value = parsed.scheme + '://' + _authority(parsed.netloc)
            else:
                value = _authority(value)
            if value not in values:
                values.append(value)
    except (AttributeError, TypeError, ValueError):
        # Configuration may contain private machine names; do not echo values.
        raise ValueError(f'{name} requires exact comma-separated ' +
                         ('HTTP(S) origins without paths or wildcards' if origin else 'hosts with optional ports, without wildcards')) from None
    return values


def transport_security(environ=None):
    environ = os.environ if environ is None else environ
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(LOCAL_HOSTS) + _configured(environ, 'DIRECTOR_MCP_ALLOWED_HOSTS'),
        allowed_origins=list(LOCAL_ORIGINS) + _configured(environ, 'DIRECTOR_MCP_ALLOWED_ORIGINS', origin=True),
    )
