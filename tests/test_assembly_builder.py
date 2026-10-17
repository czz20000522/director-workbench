import pytest
from tools import build_assembly_workflow as builder


def shot(name, path='output/video.mp4', duration=5):
    return {'id': name, 'video': {'path': path}, 'duration_seconds': duration}


def test_full_assembly_rejects_missing_video_but_explicit_subset_is_allowed():
    plan = {'segments': [shot('S01'), shot('S02', '')]}
    with pytest.raises(ValueError, match='S02'):
        builder.build_graph(plan, 'voice.wav', 'result')
    graph = builder.build_graph(plan, 'voice.wav', 'result', {'S01'})
    assert graph['concatenate']['inputs']['videos.video0'] == ['create-S01', 0]
    assert 'load-S02' not in graph
    with pytest.raises(ValueError, match='不存在'):
        builder.build_graph(plan, 'voice.wav', 'result', {'S03'})


@pytest.mark.parametrize('duration', [0, -1, float('nan'), float('inf'), None])
def test_invalid_duration_is_rejected(duration):
    with pytest.raises(ValueError, match='时长'):
        builder.build_graph({'segments': [shot('S01', duration=duration)]}, 'voice.wav', 'result')


def test_duplicate_ids_cannot_overwrite_graph_nodes():
    with pytest.raises(ValueError, match='唯一'):
        builder.build_graph({'segments': [shot('S01'), shot('S01')]}, 'voice.wav', 'result')


def test_assembly_preserves_segment_audio_without_explicit_master():
    plan = {'segments': [shot('S01'), shot('S02')]}
    graph = builder.build_graph(plan, '', 'result')
    assert 'load-complete-audio' not in graph
    assert 'complete_audio' not in graph['concatenate']['inputs']
    for name in ('S01', 'S02'):
        assert graph[f'create-{name}']['inputs']['audio'] == [f'trim-{name}', 1]
        assert graph[f'trim-{name}']['inputs']['audio'] == [f'components-{name}', 1]
        assert f'load-delivery-audio-{name}' not in graph
    overridden = builder.build_graph(plan, 'explicit-master.wav', 'result')
    assert overridden['load-complete-audio']['inputs']['audio'] == 'explicit-master.wav'


def test_segment_delivery_master_replaces_only_its_model_audio():
    first = shot('S01')
    first['audio'] = {'delivery_master': 'output/dialogue.wav', 'guide': 'unused-guide.wav'}
    graph = builder.build_graph({'segments': [first, shot('S02')]}, '', 'result')
    assert graph['load-delivery-audio-S01']['inputs']['audio'] == 'dialogue.wav [output]'
    assert graph['trim-S01']['inputs']['audio'] == ['load-delivery-audio-S01', 0]
    assert graph['trim-S01']['inputs']['frames'] == ['components-S01', 0]
    assert graph['trim-S01']['inputs']['duration_seconds'] == 5
    assert graph['create-S01']['inputs']['audio'] == ['trim-S01', 1]
    assert graph['trim-S02']['inputs']['audio'] == ['components-S02', 1]
    assert 'complete_audio' not in graph['concatenate']['inputs']


def test_full_master_takes_precedence_without_loading_unused_segment_master():
    first = shot('S01')
    first['audio'] = {'delivery_master': 'missing-unused.wav'}
    graph = builder.build_graph({'segments': [first]}, 'full-master.wav', 'result')
    assert 'load-delivery-audio-S01' not in graph
    assert graph['concatenate']['inputs']['complete_audio'] == ['load-complete-audio', 0]
    assert graph['load-complete-audio']['inputs']['audio'] == 'full-master.wav'


@pytest.mark.parametrize('value', [None, '', '   '])
def test_empty_segment_master_keeps_model_audio(value):
    first = shot('S01')
    first['audio'] = {'delivery_master': value}
    graph = builder.build_graph({'segments': [first]}, '', 'result')
    assert graph['trim-S01']['inputs']['audio'] == ['components-S01', 1]
    assert 'load-delivery-audio-S01' not in graph


def test_explicit_output_does_not_use_stale_input_copy(tmp_path, monkeypatch):
    monkeypatch.setattr(builder, 'SHARED', tmp_path)
    (tmp_path / 'input').mkdir()
    (tmp_path / 'input/video.mp4').write_bytes(b'old')
    assert builder.input_reference('output/video.mp4') == 'video.mp4 [output]'
    assert builder.input_reference('input/video.mp4') == str(tmp_path / 'input/video.mp4')
