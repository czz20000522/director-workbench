import pytest

from backend.mcp_access_config import transport_security


def test_default_protection_and_explicit_exact_entries():
    default = transport_security({})
    assert default.enable_dns_rebinding_protection
    assert all(value.startswith(('127.0.0.1:', 'localhost:', '[::1]:')) for value in default.allowed_hosts)
    configured = transport_security({
        'DIRECTOR_MCP_ALLOWED_HOSTS': ' workbench.example.invalid:4100, [fd00::1]:4100, workbench.example.invalid:4100 ',
        'DIRECTOR_MCP_ALLOWED_ORIGINS': 'https://workbench.example.invalid,http://[fd00::1]:4100',
    })
    assert configured.enable_dns_rebinding_protection
    assert configured.allowed_hosts[-2:] == ['workbench.example.invalid:4100', '[fd00::1]:4100']
    assert configured.allowed_origins[-2:] == ['https://workbench.example.invalid', 'http://[fd00::1]:4100']
    assert set(default.allowed_hosts) <= set(configured.allowed_hosts)


@pytest.mark.parametrize('value', [
    '*', '*.example.invalid', 'workbench.example.invalid:*', 'https://workbench.example.invalid',
    'workbench.example.invalid/path', 'user:private@workbench.example.invalid',
    'workbench.example.invalid:0', 'workbench.example.invalid:70000',
    'workbench.example.invalid:', 'workbench.example.invalid:004100',
    'workbench.example.invalid?token=private', 'workbench.example.invalid#private',
    'workbench.example.invalid\\private', 'workbench.example.invalid\n:4100',
    'workbench..example.invalid', '-workbench.example.invalid',
])
def test_invalid_hosts_fail_closed_without_echoing_values(value):
    with pytest.raises(ValueError) as error:
        transport_security({'DIRECTOR_MCP_ALLOWED_HOSTS': value})
    assert 'DIRECTOR_MCP_ALLOWED_HOSTS' in str(error.value)
    assert value not in str(error.value)


@pytest.mark.parametrize('value', [
    '*', 'https://*.example.invalid', 'http://workbench.example.invalid:*',
    'https://workbench.example.invalid/', 'https://workbench.example.invalid/private',
    'https://private@workbench.example.invalid', 'https://workbench.example.invalid?key=private',
    'https://workbench.example.invalid#private', 'file://workbench.example.invalid', 'null',
])
def test_invalid_origins_fail_closed_without_echoing_values(value):
    with pytest.raises(ValueError) as error:
        transport_security({'DIRECTOR_MCP_ALLOWED_ORIGINS': value})
    assert 'DIRECTOR_MCP_ALLOWED_ORIGINS' in str(error.value)
    assert value not in str(error.value)
