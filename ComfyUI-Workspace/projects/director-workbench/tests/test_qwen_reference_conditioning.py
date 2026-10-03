from pathlib import Path

from tools.qwen_image_edit import graph


def test_both_cfg_branches_encode_the_same_reference_image():
    recipe = graph(Path('reference.png'), 'wave', 'candidate', 340925)
    sampler = recipe['15']['inputs']
    encoders = []
    for branch in ('positive', 'negative'):
        conditioning = recipe[sampler[branch][0]]['inputs']['conditioning']
        encoders.append(recipe[conditioning[0]]['inputs'])
    for encoder in encoders:
        assert encoder['image1'] == ['16', 0]
        assert encoder['vae'] == ['3', 0]
    assert encoders[0]['prompt'] != encoders[1]['prompt']
