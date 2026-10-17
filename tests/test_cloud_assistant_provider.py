import asyncio
import json
import threading

import httpx
import pytest

from backend.cloud_assistant_provider import CloudAssistantError, CloudAssistantProvider, load_config


MESSAGES = [{'role': 'user', 'content': '合成纸船故事'}]
TOOL = {'type': 'function', 'function': {'name': 'read_current_text',
        'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False}}}


def write_config(path, **changes):
    data = {'schema_version': 1, 'provider': 'synthetic', 'model': 'model-A',
            'base_url': 'https://fake.invalid/v1', 'api_mode': 'chat_completions',
            'api_key': 'private-key-A', 'timeout_seconds': 1,
            'request_headers': {'User-Agent': 'test-provider', 'Accept': 'application/json'}}
    data.update(changes)
    path.write_text(json.dumps(data), encoding='utf-8')


def response(content='合成草稿', **changes):
    data = {'model': 'synthetic-model', 'choices': [{'finish_reason': 'stop',
            'message': {'role': 'assistant', 'content': content}}],
            'usage': {'prompt_tokens': 4, 'completion_tokens': 3, 'total_tokens': 7}}
    data.update(changes)
    return httpx.Response(200, json=data)


def run(provider, **kwargs):
    return asyncio.run(provider.complete(MESSAGES, **kwargs))


def test_reloads_config_and_forwards_tools_without_executing(tmp_path):
    path = tmp_path / 'config.json'
    write_config(path)
    seen = []
    def handle(request):
        seen.append(request)
        return response()
    provider = CloudAssistantProvider(path, transport=httpx.MockTransport(handle))
    first = run(provider, tools=[TOOL])
    write_config(path, api_key='private-key-B', model='model-B', base_url='https://other.invalid/api',
                 timeout_seconds=.5, request_headers={'User-Agent': 'changed', 'Accept': 'application/json',
                                                     'Authorization': 'ignored-header-secret'})
    second = run(provider)
    assert first['message'] == {'role': 'assistant', 'content': '合成草稿', 'tool_calls': []}
    assert first['usage'] == {'prompt_tokens': 4, 'completion_tokens': 3, 'total_tokens': 7}
    assert [r.headers['authorization'] for r in seen] == ['Bearer private-key-A', 'Bearer private-key-B']
    assert [json.loads(r.content)['model'] for r in seen] == ['model-A', 'model-B']
    assert json.loads(seen[0].content)['tools'] == [TOOL]
    assert json.loads(seen[0].content)['tool_choice'] == 'auto'
    assert 'tools' not in json.loads(seen[1].content)
    assert str(seen[1].url) == 'https://other.invalid/api/chat/completions'
    assert seen[1].headers['user-agent'] == 'changed'
    assert seen[1].extensions['timeout']['read'] == .5
    assert 'private-key' not in repr(first) + repr(second)


def test_function_call_result_and_usage_are_projected(tmp_path):
    path = tmp_path / 'config.json'
    write_config(path)
    calls = [{'id': 'call-1', 'type': 'function', 'function': {'name': 'read_current_text', 'arguments': '{}'},
              'upstream_debug': 'private-key-A'}]
    reply = response(None, choices=[{'finish_reason': 'tool_calls', 'message': {
        'role': 'assistant', 'content': None, 'tool_calls': calls, 'headers': {'authorization': 'private-key-A'}}}],
        usage={'prompt_tokens': 4, 'total_tokens': 7, 'completion_tokens': -1, 'upstream_key': 'private-key-A'},
        upstream_headers={'authorization': 'private-key-A'})
    result = run(CloudAssistantProvider(path, transport=httpx.MockTransport(lambda _: reply)), tools=[TOOL])
    assert result['message']['tool_calls'] == [{'id': 'call-1', 'type': 'function',
                                              'function': {'name': 'read_current_text', 'arguments': '{}'}}]
    assert result['usage'] == {'prompt_tokens': 4, 'total_tokens': 7}
    assert 'private-key-A' not in json.dumps(result)


@pytest.mark.parametrize('arguments', ['not-json', '[]', '{"x":NaN}'])
def test_invalid_tool_arguments_fail_without_leaking(tmp_path, arguments):
    path = tmp_path / 'config.json'
    write_config(path)
    reply = response(None, choices=[{'message': {'role': 'assistant', 'tool_calls': [
        {'id': 'call-1', 'type': 'function', 'function': {'name': 'read_current_text', 'arguments': arguments}}]}}])
    with pytest.raises(CloudAssistantError) as caught:
        run(CloudAssistantProvider(path, transport=httpx.MockTransport(lambda _: reply)))
    assert caught.value.code == 'invalid_response'


@pytest.mark.parametrize('status', [302, 401, 403, 429, 500])
def test_http_errors_are_sanitized_and_not_retried(tmp_path, status, caplog):
    path = tmp_path / 'config.json'
    write_config(path)
    seen = []
    def handle(request):
        seen.append(request)
        return httpx.Response(status, text='private-key-A upstream request dump',
                              headers={'Location': 'https://elsewhere.invalid/private-key-A'})
    with pytest.raises(CloudAssistantError) as caught:
        run(CloudAssistantProvider(path, transport=httpx.MockTransport(handle)))
    error = caught.value
    assert len(seen) == 1 and error.http_status == status
    assert error.retryable == (status == 429 or status >= 500)
    assert 'private-key-A' not in str(error) + repr(error) + repr(error.as_dict()) + caplog.text
    assert error.__context__ is None and error.__cause__ is None


@pytest.mark.parametrize('kind', ['network', 'timeout'])
def test_transport_exception_drops_sensitive_context(tmp_path, kind):
    path = tmp_path / 'config.json'
    write_config(path)
    def handle(request):
        exception = httpx.ReadTimeout if kind == 'timeout' else httpx.ConnectError
        raise exception('private-key-A Authorization upstream exception', request=request)
    with pytest.raises(CloudAssistantError) as caught:
        run(CloudAssistantProvider(path, transport=httpx.MockTransport(handle)))
    assert caught.value.code == ('provider_timeout' if kind == 'timeout' else 'provider_unavailable')
    assert 'private-key-A' not in str(caught.value) + repr(caught.value.as_dict())
    assert caught.value.__context__ is None


def test_total_deadline_and_event_cancel_reap_waiting_request(tmp_path):
    path = tmp_path / 'config.json'
    write_config(path, timeout_seconds=.08)
    async def verify(mode):
        entered, released = asyncio.Event(), asyncio.Event()
        cancel = threading.Event()
        async def handle(_):
            entered.set()
            try:
                await asyncio.sleep(10)
                return response('late reply')
            finally:
                released.set()
        provider = CloudAssistantProvider(path, transport=httpx.MockTransport(handle))
        task = asyncio.create_task(provider.complete(MESSAGES, cancel_event=cancel))
        await entered.wait()
        if mode == 'cancel':
            cancel.set()
        with pytest.raises(CloudAssistantError) as caught:
            await asyncio.wait_for(task, timeout=1)
        assert caught.value.code == ('cancelled' if mode == 'cancel' else 'provider_timeout')
        assert released.is_set()
    asyncio.run(verify('timeout'))
    asyncio.run(verify('cancel'))


def test_cancel_before_dispatch_and_external_task_cancel(tmp_path):
    path = tmp_path / 'config.json'
    write_config(path)
    cancel = threading.Event()
    cancel.set()
    def never(_):
        pytest.fail('Cancelled run must not send a request')
    with pytest.raises(CloudAssistantError) as caught:
        run(CloudAssistantProvider(path, transport=httpx.MockTransport(never)), cancel_event=cancel)
    assert caught.value.code == 'cancelled'
    async def verify():
        entered, released = asyncio.Event(), asyncio.Event()
        async def handle(_):
            entered.set()
            try:
                await asyncio.sleep(10)
            finally:
                released.set()
        task = asyncio.create_task(CloudAssistantProvider(path, transport=httpx.MockTransport(handle)).complete(MESSAGES))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert released.is_set()
    asyncio.run(verify())


def test_cancel_at_response_boundary_discards_late_result(tmp_path):
    path = tmp_path / 'config.json'
    write_config(path)
    cancel = threading.Event()
    def handle(_):
        cancel.set()
        return response('late result')
    with pytest.raises(CloudAssistantError) as caught:
        run(CloudAssistantProvider(path, transport=httpx.MockTransport(handle)), cancel_event=cancel)
    assert caught.value.code == 'cancelled'


@pytest.mark.parametrize('changes', [dict(api_key=''), dict(api_mode='responses'),
    dict(base_url='https://user:private-key-A@fake.invalid/v1'), dict(timeout_seconds=float('inf')),
    dict(timeout_seconds=True), dict(request_headers={'User-Agent': 'bad\r\nprivate-key-A'})])
def test_invalid_config_private_repr_and_missing(tmp_path, changes):
    path = tmp_path / 'config.json'
    write_config(path, **changes)
    with pytest.raises(CloudAssistantError) as caught:
        load_config(path)
    assert caught.value.code == 'config_invalid'
    assert 'private-key-A' not in str(caught.value)
    assert caught.value.__context__ is None


def test_missing_bad_json_and_configuration_repr(tmp_path):
    path = tmp_path / 'config.json'
    with pytest.raises(CloudAssistantError, match='配置缺失') as caught:
        load_config(path)
    assert caught.value.code == 'config_missing'
    path.write_text('{bad-json-private-key-A', encoding='utf-8')
    with pytest.raises(CloudAssistantError) as caught:
        load_config(path)
    assert caught.value.__context__ is None and 'private-key-A' not in str(caught.value)
    write_config(path)
    assert 'private-key-A' not in repr(load_config(path))


def test_parallel_threads_keep_per_request_configuration_snapshot(tmp_path):
    path = tmp_path / 'config.json'
    write_config(path)
    entered = threading.Event()
    release = threading.Event()
    seen, results = [], []
    async def handle(request):
        payload = json.loads(request.content)
        seen.append((payload['model'], request.headers['authorization']))
        if payload['model'] == 'model-A':
            entered.set()
            await asyncio.to_thread(release.wait, 2)
        return response()
    provider = CloudAssistantProvider(path, transport=httpx.MockTransport(handle))
    thread = threading.Thread(target=lambda: results.append(run(provider)))
    thread.start()
    try:
        assert entered.wait(2)
        write_config(path, api_key='private-key-B', model='model-B')
        results.append(run(provider))
    finally:
        release.set()
        thread.join(2)
    assert not thread.is_alive() and len(results) == 2
    assert seen == [('model-A', 'Bearer private-key-A'), ('model-B', 'Bearer private-key-B')]


@pytest.mark.parametrize('content', ['private-key-A', 'Bearer custom-header-secret'])
def test_credential_echo_is_blocked(tmp_path, content):
    path = tmp_path / 'config.json'
    write_config(path, request_headers={'X-Api-Key': 'Bearer custom-header-secret'})
    with pytest.raises(CloudAssistantError) as caught:
        run(CloudAssistantProvider(path, transport=httpx.MockTransport(lambda _: response(content))))
    assert caught.value.code == 'sensitive_response'
    assert content not in str(caught.value)


def test_output_and_input_limits_and_incomplete_response(tmp_path):
    path = tmp_path / 'config.json'
    write_config(path)
    with pytest.raises(CloudAssistantError) as caught:
        run(CloudAssistantProvider(path, max_response_bytes=10, transport=httpx.MockTransport(lambda _: response())))
    assert caught.value.code == 'response_too_large'
    with pytest.raises(CloudAssistantError) as caught:
        run(CloudAssistantProvider(path, max_input_bytes=10, transport=httpx.MockTransport(lambda _: pytest.fail('no request'))))
    assert caught.value.code == 'input_limit'
    truncated = response(choices=[{'finish_reason': 'length', 'message': {'role': 'assistant', 'content': '{'}}])
    with pytest.raises(CloudAssistantError) as caught:
        run(CloudAssistantProvider(path, transport=httpx.MockTransport(lambda _: truncated)))
    assert caught.value.code == 'response_truncated'


def test_secret_with_json_escaping_and_provider_metadata_cannot_escape(tmp_path):
    path = tmp_path / 'config.json'
    key = 'private-key-"quoted"'
    write_config(path, api_key=key)
    for reply in (response(key), response(model=key)):
        with pytest.raises(CloudAssistantError) as caught:
            run(CloudAssistantProvider(path, transport=httpx.MockTransport(lambda _: reply)))
        assert caught.value.code == 'sensitive_response'
        assert key not in repr(caught.value.as_dict())


@pytest.mark.parametrize('reply', [httpx.Response(200, text='private-key-A invalid-json'),
    response(choices=[{'message': {'role': 'assistant', 'refusal': 'private-key-A'}}]),
    response(choices=[{'message': {'role': 'assistant', 'content': None}}]),
    response(choices=[{'message': {'role': 'assistant', 'content': [], 'tool_calls': []}}])])
def test_invalid_and_refused_responses_preserve_no_provider_text(tmp_path, reply):
    path = tmp_path / 'config.json'
    write_config(path)
    with pytest.raises(CloudAssistantError) as caught:
        run(CloudAssistantProvider(path, transport=httpx.MockTransport(lambda _: reply)))
    assert caught.value.code in {'invalid_response', 'provider_refusal'}
    assert 'private-key-A' not in str(caught.value)
    assert caught.value.__context__ is None


def test_timeout_in_body_read_closes_stream(tmp_path):
    path = tmp_path / 'config.json'
    write_config(path, timeout_seconds=.08)
    class SlowBody(httpx.AsyncByteStream):
        closed = False
        async def __aiter__(self):
            yield b'{'
            await asyncio.sleep(10)
            yield b'}'
        async def aclose(self):
            self.closed = True
    stream = SlowBody()
    with pytest.raises(CloudAssistantError) as caught:
        run(CloudAssistantProvider(path, transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream))))
    assert caught.value.code == 'provider_timeout' and stream.closed


def test_unexpected_injected_transport_error_is_also_safe(tmp_path):
    path = tmp_path / 'config.json'
    write_config(path)
    def handle(_):
        raise RuntimeError('private-key-A fake transport failure')
    with pytest.raises(CloudAssistantError) as caught:
        run(CloudAssistantProvider(path, transport=httpx.MockTransport(handle)))
    assert caught.value.code == 'provider_unavailable'
    assert caught.value.__context__ is None and 'private-key-A' not in str(caught.value)


@pytest.mark.parametrize('changes', [dict(base_url='https://fake.invalid/private-key-A'),
    dict(request_headers={'bad header': 'value'}), dict(schema_version=True)])
def test_config_rejects_credential_urls_and_invalid_header_names(tmp_path, changes):
    path = tmp_path / 'config.json'
    write_config(path, **changes)
    with pytest.raises(CloudAssistantError) as caught:
        load_config(path)
    assert caught.value.code == 'config_invalid'


def test_json_escaped_credential_in_tool_arguments_is_blocked(tmp_path):
    path = tmp_path / 'config.json'
    write_config(path)
    arguments = '{"value":"private\\u002dkey-A"}'
    reply = response(None, choices=[{'message': {'role': 'assistant', 'tool_calls': [
        {'id': 'call-1', 'type': 'function', 'function': {'name': 'read_current_text', 'arguments': arguments}}]}}])
    with pytest.raises(CloudAssistantError) as caught:
        run(CloudAssistantProvider(path, transport=httpx.MockTransport(lambda _: reply)))
    assert caught.value.code == 'sensitive_response'
