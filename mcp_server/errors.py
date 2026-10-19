"""Canonical error contract shared by every director-workbench MCP tool.

A tool call has exactly two observable shapes:

* success: ``{ok: true, data: ...}`` as ordinary structured content;
* failure: ``isError=true`` plus structured ``{ok: false, error: {...}}``.

The ``error`` object always contains ``code``, ``message``, ``kind`` and
``retryable``.  Business HTTP failures add ``http_status`` and ``detail``;
a write whose receipt was lost adds ``outcome_unknown``.  ``kind`` is one of
``business`` (authenticated HTTP business route refused the request),
``argument`` (the adapter rejected an input the schema cannot express),
``transport`` (no usable workbench response; read-only retry is safe) or
``outcome_unknown`` (a write may have been applied, so it must be reconciled,
never blindly re-sent).

The remaining failure channel is the SDK ``tool_error``: missing, mistyped or
out-of-range arguments fail the advertised input schema before the adapter
runs, so MCP returns a tool error without structured content.  Tools never
return a bare JSON error as a successful call; this module is the single place
that decides which channel an in-body failure uses.
"""
from __future__ import annotations

import functools
import json
from typing import Any, Callable

from mcp.types import CallToolResult, TextContent, ToolAnnotations

ERROR_KINDS = ('business', 'argument', 'transport', 'outcome_unknown')

# Paginated list tools advertise their offset contract on the tool metadata.
# MCP annotations have no pagination field, so callers discover it from `_meta`.
PAGINATION_META = {
    'pagination': {
        'style': 'offset',
        'limit_parameter': 'limit',
        'offset_parameter': 'offset',
        'next_offset_field': 'next_offset',
    },
}


class ToolInputError(ValueError):
    """An in-body argument failure with a stable, documented code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def error(code: str, message: str, *, kind: str = 'business', retryable: bool = False,
          http_status: int | None = None, detail: Any = None,
          outcome_unknown: bool = False, **extra: Any) -> dict[str, Any]:
    """Build the canonical ``{ok: false, error: ...}`` payload."""
    if not isinstance(code, str) or not code.strip():
        raise ValueError('error code must be a non-empty string')
    if kind not in ERROR_KINDS:
        raise ValueError(f'unknown error kind: {kind}')
    value: dict[str, Any] = {'code': code, 'message': str(message or code), 'kind': kind,
                             'retryable': bool(retryable)}
    if outcome_unknown:
        value['outcome_unknown'] = True
        value['retryable'] = False
    if http_status is not None:
        value['http_status'] = int(http_status)
    if detail is not None:
        value['detail'] = detail
    value.update(extra)
    return {'ok': False, 'error': value}


def http_error(status: int, data: Any) -> dict[str, Any]:
    """Turn a non-2xx business response into the canonical business error."""
    detail = data.get('detail', data) if isinstance(data, dict) else data
    code = 'workbench_http_error'
    if isinstance(detail, dict) and isinstance(detail.get('code'), str) and detail['code'].strip():
        code = detail['code']
    return error(code, _business_message(detail, status), kind='business',
                 http_status=status, detail=detail)


def _business_message(detail: Any, status: int) -> str:
    if isinstance(detail, str) and detail.strip():
        return detail.strip()
    if isinstance(detail, dict):
        for key in ('message', 'detail', 'error'):
            value = detail.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return f'工作台返回 HTTP {status}'


def normalize(value: Any) -> Any:
    """Give any legacy-shaped ``{ok: false}`` result the canonical error keys."""
    if not (isinstance(value, dict) and value.get('ok') is False):
        return value
    current = value.get('error')
    if isinstance(current, dict):
        current = dict(current)
    else:
        current = {'message': str(current) if current is not None else '未知错误'}
    current.setdefault('code', 'unknown_error')
    current.setdefault('message', str(current['code']))
    current.setdefault('kind', 'business')
    current['retryable'] = bool(current.get('retryable', False))
    if current.get('outcome_unknown'):
        current['retryable'] = False
    return {'ok': False, 'error': current}


def failure(payload: dict[str, Any]) -> CallToolResult:
    """Publish a failure as ``isError=true`` plus structured content."""
    payload = normalize(payload)
    assert isinstance(payload, dict) and payload.get('ok') is False
    return CallToolResult(
        content=[TextContent(type='text', text=json.dumps(payload, ensure_ascii=False, sort_keys=True))],
        structured_content=payload,
        is_error=True,
    )


def finalize(value: Any) -> Any:
    """Convert a tool return value into the one canonical failure shape."""
    if isinstance(value, CallToolResult):
        return value
    normalized = normalize(value)
    if isinstance(normalized, dict) and normalized.get('ok') is False:
        return failure(normalized)
    return normalized


def tool(server: Any, *, annotations: ToolAnnotations, meta: dict[str, Any] | None = None,
         ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register a tool behind the canonical in-body error boundary."""

    def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        async def guarded(*args: Any, **kwargs: Any) -> Any:
            try:
                value = await fn(*args, **kwargs)
            except ToolInputError as exc:
                return failure(error(exc.code, str(exc), kind='argument'))
            except ValueError as exc:
                return failure(error('invalid_argument', str(exc), kind='argument'))
            return finalize(value)

        return server.tool(annotations=annotations, meta=meta)(guarded)

    return decorate


class ToolServer:
    """Decorator proxy so registrars cannot bypass the error boundary."""

    def __init__(self, server: Any):
        self._server = server

    def tool(self, *, annotations: ToolAnnotations, meta: dict[str, Any] | None = None,
             ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return tool(self._server, annotations=annotations, meta=meta)
