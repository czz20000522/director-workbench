"""进度板：从 ComfyUI 的 progress_state 里取步级进度，并守住提交身份不漂移。"""
import json
import time
from pathlib import Path

from backend import comfy_progress


def _nodes(entries):
    return {node_id: {'state': state, 'value': value, 'max': maximum, 'node_id': node_id, 'prompt_id': 'p-1'}
            for node_id, (state, value, maximum) in entries.items()}


def _running(node_id, value, maximum):
    return _nodes({node_id: ('running', value, maximum)})


def _board():
    return comfy_progress.ProgressBoard()


def test_the_sampler_node_wins_over_one_step_nodes():
    nodes = _nodes({'4': ('running', 0.0, 1.0), '7': ('running', 3.0, 8.0), '2': ('pending', 0.0, 1.0)})
    best = comfy_progress._active_node(nodes)
    assert best['node_id'] == '7' and best['max'] == 8.0
    assert comfy_progress._active_node(_nodes({'4': ('pending', 0.0, 1.0)})) is None


def test_record_reports_percent_and_an_eta_from_measured_steps():
    board = _board()
    started = time.time()
    board.record('p-1', _running('7', 1.0, 8.0), 'c', started)
    board.record('p-1', _running('7', 5.0, 8.0), 'c', started + 40.0)

    progress = board.for_prompt('p-1')
    assert progress['value'] == 5.0 and progress['max'] == 8.0
    assert progress['percent'] == 62, '进度向下取整：没跑完就不许显示到 100%'
    assert progress['seconds_per_step'] == 10.0
    assert progress['eta_seconds'] == 30
    assert progress['node_id'] == '7'


def test_a_new_node_restares_the_clock_instead_of_inheriting_the_sampler():
    board = _board()
    now = time.time()
    board.record('p-1', _running('7', 8.0, 8.0), 'c', now)
    board.record('p-1', _running('8', 1.0, 1.0), 'c', now + 5.0)

    progress = board.for_prompt('p-1')
    assert progress['node_id'] == '8' and progress['percent'] == 100


def test_stale_and_unknown_prompts_read_as_no_progress():
    board = _board()
    assert board.for_prompt('missing') is None
    assert board.for_prompt(None) is None
    board.record('p-1', _running('7', 2.0, 8.0), 'c', time.time())
    board._entries['p-1']['updated_at'] -= comfy_progress.STALE_SECONDS + 5
    assert board.for_prompt('p-1') is None


def test_only_progress_state_frames_are_accepted():
    # _handle 落在模块级 BOARD 上，这里就按那个真实入口断言。
    comfy_progress._handle(b'\x00\x01binary-preview', 'c')
    comfy_progress._handle('not json', 'c')
    comfy_progress._handle('{"type": "execution_cached", "data": {}}', 'c')
    comfy_progress._handle('{"type": "progress_state", "data": {"prompt_id": "", "nodes": {}}}', 'c')
    assert comfy_progress.BOARD.for_prompt('p-9') is None

    frame = {'type': 'progress_state', 'data': {'prompt_id': 'p-9', 'nodes': _running('7', 4.0, 8.0)}}
    comfy_progress._handle(json.dumps(frame), 'c')
    assert comfy_progress.BOARD.for_prompt('p-9')['percent'] == 50


def test_tracked_prompts_stay_bounded():
    board = comfy_progress.ProgressBoard()
    now = time.time()
    for index in range(comfy_progress.MAX_TRACKED + 20):
        board.record(f'p-{index}', _running('7', 1.0, 8.0), 'c', now + index)
    assert board.snapshot()['tracked'] == comfy_progress.MAX_TRACKED


def test_listener_identities_still_match_whoever_submits():
    # ComfyUI 只把 progress_state 回给提交方那个 client_id，身份一漂移进度就静默消失。
    renderer = Path('.agents/skills/aigc-video-production/scripts/h3_render.py')
    assert comfy_progress.CLIENT_RENDERER in renderer.read_text(encoding='utf-8')
    app_source = Path('backend/app.py').read_text(encoding='utf-8')
    assert 'comfy_progress.CLIENT_WORKBENCH' in app_source
    assert '"client_id": "director' not in app_source, '提交身份必须来自 comfy_progress，别再写第二份字面量'


def test_a_finished_task_never_shows_a_frozen_progress_bar():
    # 完成后最后一条采样常停在解码那一步，继续显示就像还在跑。
    comfy_progress.BOARD.record('p-terminal', _running('8', 0.0, 1.0), 'c', time.time())
    assert comfy_progress.visible_progress({'status': 'running', 'prompt_id': 'p-terminal'}) is not None
    for status in ('succeeded', 'failed', 'stopped'):
        assert comfy_progress.visible_progress({'status': status, 'prompt_id': 'p-terminal'}) is None
    assert comfy_progress.visible_progress({'status': 'running', 'prompt_id': None}) is None


def test_the_engine_origin_is_translated_to_the_websocket_scheme():
    # COMFY_URL 是 http://，直接拿去连 /ws 会静默连不上，进度就永远是空。
    assert comfy_progress.socket_url('http://127.0.0.1:8188/') == 'ws://127.0.0.1:8188'
    assert comfy_progress.socket_url('https://engine.invalid:8188') == 'wss://engine.invalid:8188'
    assert comfy_progress.socket_url('ws://127.0.0.1:8188') == 'ws://127.0.0.1:8188'


def test_the_workbench_and_renderer_ids_are_distinct():
    assert set(comfy_progress.CLIENT_IDS) == {comfy_progress.CLIENT_WORKBENCH, comfy_progress.CLIENT_RENDERER}
