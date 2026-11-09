import io
import json
from pathlib import Path

import pytest
from fastapi import HTTPException
from PIL import Image, ImageDraw

from backend import inpaint_recipe, keyframes
from backend.routers.task_media import decode_mask_image


def paint_mask(size, box=None, colour=(255, 255, 255)):
    image = Image.new('RGB', size, (0, 0, 0))
    if box:
        ImageDraw.Draw(image).rectangle(box, fill=colour)
    buffer = io.BytesIO()
    image.save(buffer, format='PNG')
    return buffer.getvalue()


def frame(size=(768, 1344)):
    return Image.new('RGB', size, (188, 180, 168))


def test_graph_rebuilds_only_the_masked_area(tmp_path):
    source = tmp_path / 'source.png'
    mask = tmp_path / 'mask.png'
    frame().save(source)
    mask.write_bytes(paint_mask((768, 1344), (200, 400, 500, 900)))
    recipe = inpaint_recipe.graph('换成白色高领毛衣', 'project/keyframe/task', 7, source, mask,
                                  768, 1344, steps=8, denoise=0.75)
    # 原图 → latent → 蒙版加噪；采样结果贴回原图，未涂区域不经过 VAE 重建。
    assert recipe['12'] == {'class_type': 'SetLatentNoiseMask',
                            'inputs': {'samples': ['11', 0], 'mask': ['10', 0]}}
    assert recipe['13']['inputs']['latent_image'] == ['12', 0]
    assert recipe['13']['inputs']['denoise'] == 0.75
    assert recipe['15']['inputs']['destination'] == ['6', 0]
    assert recipe['15']['inputs']['source'] == ['14', 0]
    assert recipe['18']['inputs']['images'] == ['15', 0]
    assert recipe['9']['inputs']['expand'] == 8
    assert recipe['4']['inputs']['images'] == {'image_0': ['6', 0]}
    # 参考图必须按自身尺寸编码：传 max(w, h) 会把它放大（1344×768 → 1792×1024），
    # 实测输出逐像素相同，只是白算 44% 的参考 latent。
    assert recipe['4']['inputs']['resolution'] == 0
    # 蒙版外之所以能逐像素保真，靠的是「合成区 == 加噪区」这一条：
    # 两个节点必须吃同一张蒙版，一旦拆开，未涂区域就会被模型输出盖掉。
    assert recipe['15']['inputs']['mask'] == recipe['12']['inputs']['mask']


def test_prepare_inpaint_freezes_source_mask_and_prompt(tmp_path):
    source = tmp_path / 'frame.png'
    frame().save(source)
    mask_bytes = paint_mask((768, 1344), (120, 300, 480, 880))
    payload = keyframes.prepare_inpaint('task-1', '换成白色高领毛衣', source, mask_bytes,
                                        tmp_path / 'snapshots', 'project/keyframe/task-1', 42)
    request = payload['request']
    assert request['mode'] == 'inpaint'
    assert request['denoise'] == 0.75
    assert Path(request['reference_image']).is_file()
    assert Path(request['mask']).read_bytes() == mask_bytes
    assert request['prompt_for_model'].startswith('参考图1是当前画面')
    assert '换成白色高领毛衣' in request['prompt_for_model']
    graph = json.loads(Path(payload['workflow']).read_text(encoding='utf-8'))
    assert graph['18']['class_type'] == 'SaveImage'


def test_prepare_inpaint_rejects_mismatched_mask(tmp_path):
    source = tmp_path / 'frame.png'
    frame((768, 1344)).save(source)
    with pytest.raises(ValueError, match='不一致'):
        keyframes.prepare_inpaint('task-2', '换衣服', source, paint_mask((512, 512)),
                                  tmp_path / 'snapshots', 'project/keyframe/task-2', 42)


def test_prepare_inpaint_rejects_non_png_mask(tmp_path):
    source = tmp_path / 'frame.png'
    frame().save(source)
    with pytest.raises(ValueError, match='PNG'):
        keyframes.prepare_inpaint('task-3', '换衣服', source, b'not-a-png',
                                  tmp_path / 'snapshots', 'project/keyframe/task-3', 42)


def test_decode_mask_requires_png_data_url():
    with pytest.raises(HTTPException) as error:
        decode_mask_image('data:image/jpeg;base64,AAAA')
    assert error.value.status_code == 422
    with pytest.raises(HTTPException):
        decode_mask_image('not-a-data-url')
