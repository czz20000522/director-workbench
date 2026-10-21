from backend import dialogue_shot_recipe


def test_dialogue_graph_keeps_first_frame_and_locks_supplied_audio():
    graph = dialogue_shot_recipe.build_graph(
        first_frame_name='first.png',
        audio_name='line.wav',
        prompt='人物开口说话，无字幕，嘴部无遮挡。',
        duration_seconds=5.0,
        seed=7,
        output_prefix='导演工作台工作目录/p/S/dialogue-t',
        reference_names=['ref-a.png'],
    )
    conditioning = graph['conditioning']['inputs']
    assert conditioning['first_frame'] == ['first_frame', 0]
    assert conditioning['drive_audio'] == ['window', 0]
    assert conditioning['final_audio'] == ['window', 0]
    assert conditioning['audio_mode'] == 'lock_source'
    assert conditioning['task_type'] == 'auto'
    assert conditioning['ref_images.ref_image_0'] == ['ref_0', 0]
    assert graph['first_frame']['inputs']['image'] == 'first.png'
    assert graph['audio']['inputs']['audio'] == 'line.wav'
    assert graph['window']['inputs']['scene_duration_seconds'] == 5.0
    assert graph['sampler']['inputs']['steps'] == 8
    assert graph['save']['inputs']['filename_prefix'] == '导演工作台工作目录/p/S/dialogue-t'
    assert graph['trim']['inputs']['audio'] == ['conditioning', 2]


def test_dialogue_graph_rejects_invalid_duration():
    try:
        dialogue_shot_recipe.build_graph(
            first_frame_name='first.png', audio_name='line.wav', prompt='x',
            duration_seconds=1.0, seed=1, output_prefix='x',
        )
    except ValueError as exc:
        assert 'duration' in str(exc)
    else:
        raise AssertionError('duration should be rejected')
