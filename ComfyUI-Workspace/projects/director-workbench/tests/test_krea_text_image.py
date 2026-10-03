import pytest
import json
from pathlib import Path
from backend.keyframes import prepare_text
from tools.krea_text_image import graph


def test_text_only_graph_keeps_user_description_and_uses_no_reference_files():
    recipe = graph('雨后的小镇，清晨暖光', '系列工作目录/新作/首图', 42)
    assert recipe['4']['inputs']['text'] == '雨后的小镇，清晨暖光'
    assert not any(node['class_type'].startswith('LoadImage') for node in recipe.values())
    assert recipe['18']['inputs']['filename_prefix'] == '系列工作目录/新作/首图'
    for node in recipe.values():
        for value in node['inputs'].values():
            if isinstance(value, list):
                assert value[0] in recipe


@pytest.mark.parametrize('changes', [{'width': 0}, {'height': 513}, {'width': 1536, 'height': 1536},
                                     {'prefix': '../outside'}, {'seed': -1}, {'prompt': ' '}])
def test_invalid_requests_rejected(changes):
    args = {'prompt': 'image', 'prefix': 'project/image', 'seed': 42}
    with pytest.raises(ValueError):
        graph(**{**args, **changes})


def test_text_snapshot_needs_no_reference_and_cannot_be_overwritten(tmp_path):
    payload = prepare_text('text-1', '清晨街道', tmp_path, 'project/first', 42)
    workflow = Path(payload['workflow'])
    before = workflow.read_bytes()
    assert payload['request']['mode'] == 'text'
    assert 'reference_image' not in payload['request']
    assert json.loads(workflow.read_text(encoding='utf-8'))['6']['inputs']['height'] == 1344
    with pytest.raises(FileExistsError):
        prepare_text('text-1', '替换画面', tmp_path, 'project/second', 43)
    assert workflow.read_bytes() == before
