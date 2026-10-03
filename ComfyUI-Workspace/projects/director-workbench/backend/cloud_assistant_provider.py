"""Configurable text provider. No business identity, tools, storage or execution.

Callers supply projected context and implement authorization/tool dispatch. Each
completion reads a private configuration snapshot; no credentials are persisted.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import math
from pathlib import Path
import re
import time
from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import urlsplit

import httpx

DEFAULT_CONFIG_PATH = Path('D:/Comfy-Desktop/.config/director-workbench/cloud-agent.json')
MAX_TIMEOUT_SECONDS = 120


class CloudAssistantError(RuntimeError):
    """Only controlled text/status, never an upstream exception or HTTP request."""

    def __init__(self, code: str, message: str, *, http_status: int | None = None,
                 retryable: bool = False):
        super().__init__(message)
        self.code, self.message = code, message
        self.http_status, self.retryable = http_status, retryable

    def as_dict(self) -> dict[str, Any]:
        return {'code': self.code, 'message': self.message,
                'http_status': self.http_status, 'retryable': self.retryable}


@dataclass(frozen=True)
class ProviderConfig:
    provider: str
    model: str
    timeout_seconds: float
    base_url: str = field(repr=False)
    api_key: str = field(repr=False)
    request_headers: Mapping[str, str] = field(repr=False)
    api_mode: str = 'chat_completions'

    def __repr__(self) -> str:
        # Even provider/model are user-configurable and may contain private text.
        return 'ProviderConfig(<private snapshot>)'


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> ProviderConfig:
    """Read one bounded snapshot; errors never carry configuration contents."""
    document = None
    error = None
    try:
        with Path(path).open('rb') as handle:
            raw = handle.read(65537)
        if len(raw) > 65536:
            error = 'config_invalid'
        else:
            document = json.loads(raw.decode('utf-8-sig'))
    except FileNotFoundError:
        error = 'config_missing'
    except (OSError, UnicodeError, ValueError, TypeError):
        error = 'config_invalid'
    if error:
        raise CloudAssistantError(error, '云端助手配置缺失，请检查配置文件。' if error == 'config_missing'
                                  else '云端助手配置无法读取，请检查格式与权限。')
    valid = False
    try:
        if not isinstance(document, dict) or type(document.get('schema_version', 1)) is not int or document.get('schema_version', 1) != 1:
            raise ValueError()
        key, model, base = (document[name] for name in ('api_key', 'model', 'base_url'))
        provider = document.get('provider', 'openai-compatible')
        mode = document.get('api_mode', 'chat_completions')
        headers = document.get('request_headers', {})
        timeout = document.get('timeout_seconds', 35)
        if not all(isinstance(value, str) and value.strip() and not any(c in value for c in '\r\n')
                   for value in (key, model, base, provider)):
            raise ValueError()
        parsed = urlsplit(base)
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError()
        if mode != 'chat_completions' or type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= MAX_TIMEOUT_SECONDS:
            raise ValueError()
        if not isinstance(headers, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in headers.items()):
            raise ValueError()
        # httpx validates names/ASCII wire values, without sending any request.
        if any(not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", k)
               or any(ord(c) < 32 or ord(c) == 127 for c in v) for k, v in headers.items()):
            raise ValueError()
        httpx.Headers(headers)
        ('Bearer ' + key).encode('ascii')
        if any(ord(c) < 33 or ord(c) == 127 for c in key):
            raise ValueError()
        if key in base or key in provider or key in model:
            raise ValueError()
        valid = True
    except (KeyError, TypeError, ValueError, UnicodeError):
        pass
    if not valid:
        raise CloudAssistantError('config_invalid', '云端助手配置字段无效，请检查模型、地址、请求头和超时。')
    return ProviderConfig(provider=provider, model=model, timeout_seconds=float(timeout),
                          base_url=base.rstrip('/'), api_key=key,
                          request_headers=MappingProxyType(dict(headers)), api_mode=mode)


def _cancelled(event) -> bool:
    return event is not None and event.is_set()


def _check_cancel(event) -> None:
    if _cancelled(event):
        raise CloudAssistantError('cancelled', '云端助手请求已取消。')


def _private_values(config: ProviderConfig) -> tuple[str, ...]:
    values = [config.api_key]
    for name, value in config.request_headers.items():
        if any(part in name.lower() for part in ('authorization', 'key', 'token', 'secret', 'cookie')) and value:
            values.append(value)
            if value.lower().startswith('bearer '):
                values.append(value[7:])
    return tuple(values)


def _reject_constant(value):
    raise ValueError('Non-finite JSON')


def _has_private(value, secrets) -> bool:
    if isinstance(value, str):
        return any(secret in value for secret in secrets)
    if isinstance(value, dict):
        return any(_has_private(key, secrets) or _has_private(item, secrets) for key, item in value.items())
    if isinstance(value, list):
        return any(_has_private(item, secrets) for item in value)
    return False


def _parse_response(raw: bytes, config: ProviderConfig) -> dict[str, Any]:
    result = None
    error = 'invalid_response'
    try:
        data = json.loads(raw, parse_constant=_reject_constant)
        choices = data['choices']
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ValueError()
        choice = choices[0]
        if choice.get('finish_reason') == 'length':
            error = 'response_truncated'
            raise ValueError()
        message = choice['message']
        if not isinstance(message, dict) or message.get('role') != 'assistant':
            raise ValueError()
        if message.get('refusal') or choice.get('finish_reason') == 'content_filter':
            error = 'provider_refusal'
            raise ValueError()
        content = message.get('content')
        calls = message.get('tool_calls')
        if calls is None:
            calls = []
        if content is not None and not isinstance(content, str):
            raise ValueError()
        if not isinstance(calls, list) or len(calls) > 16 or (not content and not calls):
            raise ValueError()
        safe_calls, ids = [], set()
        for call in calls:
            if not isinstance(call, dict) or call.get('type') != 'function':
                raise ValueError()
            call_id, function = call['id'], call['function']
            if not isinstance(call_id, str) or not call_id or len(call_id) > 512 or call_id in ids:
                raise ValueError()
            name, arguments = function['name'], function['arguments']
            if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', name) or not isinstance(arguments, str):
                raise ValueError()
            parsed_arguments = json.loads(arguments, parse_constant=_reject_constant)
            if not isinstance(parsed_arguments, dict):
                raise ValueError()
            if _has_private(parsed_arguments, _private_values(config)):
                error = 'sensitive_response'
                raise ValueError()
            ids.add(call_id)
            safe_calls.append({'id': call_id, 'type': 'function',
                               'function': {'name': name, 'arguments': arguments}})
        usage = data.get('usage')
        safe_usage = None
        if isinstance(usage, dict):
            safe_usage = {key: value for key in ('prompt_tokens', 'completion_tokens', 'total_tokens')
                          if type(value := usage.get(key)) is int and value >= 0}
        model = data.get('model', config.model)
        if not isinstance(model, str):
            raise ValueError()
        result = {'message': {'role': 'assistant', 'content': content, 'tool_calls': safe_calls},
                  'usage': safe_usage, 'model': model, 'provider': config.provider}
        # Reject a credential echo instead of rewriting tool argument semantics.
        if _has_private(result, _private_values(config)):
            result, error = None, 'sensitive_response'
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        result = None
    if result is None:
        messages = {'response_truncated': '云端回复未完成，请缩短请求或分段整理。',
                    'provider_refusal': '云端助手未提供可用提案，请修改描述或手工编辑。',
                    'sensitive_response': '云端回复含敏感配置，已阻止返回。'}
        raise CloudAssistantError(error, messages.get(error, '云端回复结构无效，未执行任何工具。'))
    return result


class CloudAssistantProvider:
    def __init__(self, config_path: str | Path = DEFAULT_CONFIG_PATH, *,
                 transport: httpx.AsyncBaseTransport | None = None,
                 max_output_tokens: int = 2048, max_response_bytes: int = 262144,
                 max_input_bytes: int = 131072):
        if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 8192:
            raise ValueError('Invalid output token limit')
        if any(type(value) is not int or not 1 <= value <= 1048576 for value in (max_response_bytes, max_input_bytes)):
            raise ValueError('Invalid byte limit')
        self.config_path, self.transport = Path(config_path), transport
        self.max_output_tokens = max_output_tokens
        self.max_response_bytes, self.max_input_bytes = max_response_bytes, max_input_bytes

    async def _request(self, config, payload):
        # Fresh client/headers per request: no shared mutable auth or redirects.
        headers = {k: v for k, v in config.request_headers.items() if k.lower() not in {'authorization', 'content-type'}}
        headers.update({'Authorization': 'Bearer ' + config.api_key, 'Content-Type': 'application/json'})
        error, raw = None, None
        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=config.timeout_seconds,
                                         trust_env=False, follow_redirects=False, headers=headers) as client:
                async with client.stream('POST', config.base_url + '/chat/completions', content=payload) as response:
                    if response.status_code != 200:
                        status = response.status_code
                        text = ('云端请求被拒绝，请核对请求头、网关和凭据。' if status in (401, 403)
                                else '云端请求暂未成功，请稍后重新发起或手工编辑。')
                        error = CloudAssistantError('provider_http_error', text, http_status=status,
                                                    retryable=status == 429 or status >= 500)
                    else:
                        chunks, size = [], 0
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > self.max_response_bytes:
                                error = CloudAssistantError('response_too_large', '云端回复超出大小限制。')
                                break
                            chunks.append(chunk)
                        if error is None:
                            raw = b''.join(chunks)
        except httpx.TimeoutException:
            error = CloudAssistantError('provider_timeout', '云端请求超时，请保留输入或手工编辑。', retryable=True)
        except Exception:
            # Includes injected/future transports: never expose their raw errors.
            # asyncio.CancelledError is BaseException and still propagates.
            error = CloudAssistantError('provider_unavailable', '云端请求未取得回执，请稍后重新发起。', retryable=True)
        # Raise outside except so even exception context cannot contain a request.
        if error is not None:
            raise error
        return _parse_response(raw, config)

    async def complete(self, messages, tools=None, *, cancel_event=None) -> dict[str, Any]:
        """Return {message, usage, model, provider}; never execute returned calls.

        `cancel_event` may be threading.Event or asyncio.Event. External asyncio
        task cancellation propagates; event cancellation raises code=cancelled.
        The configured timeout also bounds total request/body waiting time.
        """
        _check_cancel(cancel_event)
        config = load_config(self.config_path)
        if not isinstance(messages, list) or not messages or (tools is not None and not isinstance(tools, list)):
            raise CloudAssistantError('input_invalid', '云端助手输入格式无效。')
        body = {'model': config.model, 'messages': messages, 'max_tokens': self.max_output_tokens, 'stream': False}
        if tools:
            body.update(tools=tools, tool_choice='auto')
        payload = None
        try:
            payload = json.dumps(body, ensure_ascii=False, allow_nan=False).encode('utf-8')
        except (ValueError, TypeError, UnicodeError, RecursionError):
            pass
        if payload is None or len(payload) > self.max_input_bytes:
            raise CloudAssistantError('input_limit', '云端助手输入无效或超过长度限制，请分段整理。')
        _check_cancel(cancel_event)
        deadline = time.monotonic() + config.timeout_seconds
        task = asyncio.create_task(self._request(config, payload))
        try:
            while True:
                _check_cancel(cancel_event)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CloudAssistantError('provider_timeout', '云端请求超时，请保留输入或手工编辑。', retryable=True)
                done, _ = await asyncio.wait({task}, timeout=min(.05, remaining))
                if done:
                    _check_cancel(cancel_event)
                    return await task
        finally:
            if not task.done():
                task.cancel()
            # Always reap HTTP cancellation; no late result can reach caller.
            await asyncio.gather(task, return_exceptions=True)
