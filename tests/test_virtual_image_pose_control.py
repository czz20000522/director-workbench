"""虚图生真图：姿势槽的线稿/深度必须能显式指定。

线稿和深度图在缩略图里几乎分不出来，靠分类猜会猜错；这里锁住三条规则：
显式选线稿 → Lineart，显式选深度 → Depth（哪怕分类名里带「线稿」），
auto 才回到按分类判断。
"""
import json
import uuid
from pathlib import Path

from backend.keyframes import prepare_text


def _write_png(path: Path, size=(64, 96), color=(120, 120, 120)) -> Path:
    from PIL import Image
    Image.new('RGB', size, color).save(path)
    return path


def _prepared(tmp_path: Path, *, control: str | None, category: str) -> dict:
    pose = _write_png(tmp_path / 'pose.png', (90, 120), (10, 10, 10))
    slot = {'role': '姿势', 'path': str(pose), 'label': '坐姿', 'category': category}
    if control is not None:
        slot['control'] = control
    return prepare_text('virtualstill' + uuid.uuid4().hex[:8], '她坐在窗边，光线柔和', tmp_path / 'tasks', 'Still/pose', 7,
                        768, 1344, 'general', 8, [slot])


def test_explicit_lineart_wins_over_a_depth_looking_category(tmp_path):
    prepared = _prepared(tmp_path, control='lineart', category='姿势')
    assert prepared['request']['control']['mode'] == 'Lineart'


def test_explicit_depth_wins_even_when_the_category_says_lineart(tmp_path):
    prepared = _prepared(tmp_path, control='depth', category='姿势线稿')
    assert prepared['request']['control']['mode'] == 'Depth'


def test_auto_falls_back_to_the_category(tmp_path):
    prepared = _prepared(tmp_path, control='auto', category='姿势线稿')
    assert prepared['request']['control']['mode'] == 'Lineart'
    # 老请求没有 control 字段，同样按分类走，行为不变。
    legacy = _prepared(tmp_path, control=None, category='姿势')
    assert legacy['request']['control']['mode'] == 'Depth'


def test_control_image_is_still_wired_to_the_union_patch(tmp_path):
    prepared = _prepared(tmp_path, control='depth', category='姿势')
    graph = json.loads(Path(prepared['workflow']).read_text(encoding='utf-8'))
    # 只有姿势槽时走 DiffsynthControlnet（image 输入）；带脸/服装时走 UnionApply。
    control_nodes = [node for node in graph.values()
                     if node.get('class_type') == 'QwenImageDiffsynthControlnet']
    assert control_nodes, '姿势图没有接到结构控制节点'
    source = graph[control_nodes[0]['inputs']['image'][0]]['inputs']['image']
    assert str(source).endswith('pose-control.png')
    assert '姿势按结构控制图执行' in prepared['request']['prompt_for_model']
