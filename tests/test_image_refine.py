"""文生图的降噪：轻度高斯 + 混回原图，只在勾选时加节点。

曾经试过「低 denoise 二次采样」，实测反而让 Turbo 补细节（高频 +100%），
所以这里的断言固定成图像空间方案，防止以后有人改回去。
"""
import pytest

from tools.qwen_image_t2i import graph


def test_graph_without_refine_keeps_single_pass():
    recipe = graph('测试', 'keyframe', 123, 768, 1344)
    assert '40' not in recipe
    assert recipe['18']['inputs']['images'] == ['8', 0]


def test_refine_blurs_and_blends_back():
    recipe = graph('测试', 'keyframe', 123, 768, 1344, refine=True)
    assert recipe['40']['class_type'] == 'ImageBlur'
    assert recipe['40']['inputs']['image'] == ['8', 0]
    assert recipe['41']['class_type'] == 'ImageBlend'
    assert recipe['41']['inputs']['image1'] == ['8', 0]
    assert recipe['41']['inputs']['image2'] == ['40', 0]
    assert recipe['41']['inputs']['blend_factor'] == 0.55
    assert recipe['18']['inputs']['images'] == ['41', 0]
    # 不额外采样：步数、种子、模型链都和没开时一致。
    assert '31' not in recipe
    assert recipe['7']['inputs']['seed'] == 123


def test_refine_keeps_lora_model_chain_untouched():
    recipe = graph('测试', 'keyframe', 1, 768, 1344, refine=True,
                   lora_names=['qwen-image-2.1-uncensored-lora.safetensors'])
    assert recipe['7']['inputs']['model'] == ['19', 0]


def test_refine_rejects_out_of_range_strength():
    with pytest.raises(ValueError, match='降噪强度'):
        graph('测试', 'keyframe', 1, 768, 1344, refine=True, refine_strength=0.95)
