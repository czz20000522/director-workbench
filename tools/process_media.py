"""Private worker for frozen workbench media requests; no downloads or GPU."""
import argparse
import difflib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.media_operations import FFMPEG, WHISPER_MODEL, _validate_payload


def publish_receipt(output, receipt):
    """Publish only a complete durable JSON receipt; interrupted writes stay private."""
    temporary = output / 'receipt.pending.json'
    with temporary.open('x', encoding='utf-8') as stream:
        json.dump(receipt, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(output / 'receipt.json')


def inspect_video(source, sample_count=0, output=None):
    import av
    import numpy as np
    from PIL import Image, ImageDraw
    count, first, last, end, dimensions = 0, None, None, 0, None
    audio_peak, audio_frames, audio_rate = 0.0, 0, None
    with av.open(str(source)) as container:
        if not container.streams.video:
            raise ValueError('Source contains no video')
        stream = container.streams.video[0]
        if stream.width * stream.height > 16777216:
            raise ValueError('Video resolution exceeds 16 megapixels')
        if container.duration and container.duration / av.time_base > 1200.1:
            raise ValueError('Video exceeds 20 minutes')
        rate = float(stream.average_rate or 0)
        for frame in container.decode(video=0):
            timestamp = float(frame.time) if frame.time is not None else count / (rate or 25)
            if timestamp > 1200 or count > 360000:
                raise ValueError('Video exceeds bounded decoding limits')
            first = timestamp if first is None else first
            last = timestamp
            end = timestamp + float(frame.duration * frame.time_base) if frame.duration else timestamp + 1 / (rate or 25)
            dimensions = [frame.width, frame.height]
            if frame.width * frame.height > 16777216:
                raise ValueError('Video resolution exceeds 16 megapixels')
            count += 1
    if not count:
        raise ValueError('Video has no decodable frames')
    with av.open(str(source)) as container:
        audio_streams = len(container.streams.audio)
        if audio_streams:
            for frame in container.decode(audio=0):
                values = frame.to_ndarray()
                scale = max(abs(np.iinfo(values.dtype).min), np.iinfo(values.dtype).max) if np.issubdtype(values.dtype, np.integer) else 1
                audio_peak = max(audio_peak, float(np.max(np.abs(values.astype(np.float64)))) / scale)
                audio_frames += frame.samples
                audio_rate = frame.sample_rate
                if audio_frames / audio_rate > 1201:
                    raise ValueError('Audio exceeds duration limit')
    report = {'frame_count': count, 'first_pts_seconds': first, 'last_pts_seconds': last,
              'duration_seconds': end - first, 'width': dimensions[0], 'height': dimensions[1],
              'audio_streams': audio_streams, 'audio_peak': audio_peak,
              'audio_sample_frames': audio_frames, 'audio_sample_rate': audio_rate,
              'approval': False, 'note': 'Technical measurements only; human review is required.'}
    artifacts = []
    if sample_count:
        indices = sorted(set(round(i * (count - 1) / (sample_count - 1)) for i in range(sample_count)))
        thumbs = []
        with av.open(str(source)) as container:
            for index, frame in enumerate(container.decode(video=0)):
                if index not in indices:
                    continue
                image = frame.to_image()
                path = output / f'frame-{index:06d}.jpg'
                image.save(path, quality=90)
                artifacts.append(('image', path, f'Frame {index}'))
                image.thumbnail((320, 240))
                tile = Image.new('RGB', (320, 270), '#161616')
                tile.paste(image, ((320 - image.width) // 2, 0))
                ImageDraw.Draw(tile).text((8, 248), f'Frame {index} | {float(frame.time or 0):.3f}s', fill='white')
                thumbs.append(tile)
        sheet = Image.new('RGB', (320 * min(4, len(thumbs)), 270 * math.ceil(len(thumbs) / 4)), '#161616')
        for i, thumb in enumerate(thumbs):
            sheet.paste(thumb, ((i % 4) * 320, (i // 4) * 270))
        path = output / 'contact-sheet.jpg'
        sheet.save(path, quality=90)
        artifacts.append(('image', path, 'Contact sheet'))
        report['sampled_frame_indices'] = indices
    return report, artifacts


def transcribe(source, output, parameters, load_model=None):
    import numpy as np
    import soundfile as sf
    # Probe into bounded PCM; an extra sample detects overlong input instead of silently truncating it.
    pcm = output / 'recognition-input.wav'
    subprocess.run([str(FFMPEG), '-nostdin', '-v', 'error', '-i', str(source), '-vn', '-t', '120.01',
                    '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', str(pcm)], check=True, timeout=180)
    samples, rate = sf.read(pcm, dtype='float32')
    if not 0 < len(samples) <= 120 * rate:
        raise ValueError('Transcription source must be at most 120 seconds')
    if load_model is None:
        import whisper
        load_model = whisper.load_model
    model = load_model(str(WHISPER_MODEL), device='cpu')
    result = model.transcribe(samples, language=None if parameters['language'] == 'auto' else parameters['language'],
                              word_timestamps=parameters['word_timestamps'], fp16=False, verbose=False)
    text = str(result.get('text', '')).strip()
    segments = result.get('segments', [])
    expected = parameters['expected_text']
    report = {'text': text, 'language': result.get('language'), 'segments': segments,
              'words': [word for segment in segments for word in segment.get('words', [])],
              'duration_seconds': len(samples) / rate, 'sample_rate': rate,
              'peak': float(np.max(np.abs(samples))), 'rms': float(np.sqrt(np.mean(samples ** 2))),
              'expected_text': expected,
              'text_similarity': difflib.SequenceMatcher(None, expected, text).ratio() if expected else None,
              'approval': False, 'note': 'ASR is fallible; comparison is not approval. Expected text was not an ASR prompt.'}
    text_path = output / 'transcript.txt'
    text_path.write_text(text + '\n', encoding='utf-8')
    return report, [('audio', pcm, 'Recognition PCM'), ('report', text_path, 'Recognized text')]


def run(request_path):
    request_path = Path(request_path).absolute()
    if request_path.name != 'request.json' or request_path.stat().st_size > 100000:
        raise ValueError('Invalid request file')
    request = json.loads(request_path.read_text(encoding='utf-8'))
    payload = {'kind': 'media_operation', 'operation': request['operation'], 'request': request,
               'request_path': str(request_path), 'output_dir': request['output_dir']}
    _validate_payload(payload)
    output = Path(request['output_dir'])
    if any(output.iterdir()):
        raise ValueError('Output directory is not empty; refusing overwrite')
    operation, parameters = request['operation'], request['parameters']
    source = Path(request['source']['path']) if request['source'] else None
    if operation == 'silence':
        path = output / 'silence.wav'
        frames = round(parameters['sample_rate'] * parameters['duration_seconds'])
        with wave.open(str(path), 'wb') as stream:
            stream.setnchannels(parameters['channels'])
            stream.setsampwidth(2)
            stream.setframerate(parameters['sample_rate'])
            stream.writeframes(bytes(frames * parameters['channels'] * 2))
        report = {**parameters, 'sample_frames': frames, 'duration_seconds': frames / parameters['sample_rate'], 'format': 'PCM16'}
        artifacts = [('audio', path, 'Silence guide')]
    elif operation == 'video_qc':
        report, artifacts = inspect_video(source, parameters['sample_count'], output)
    elif operation == 'silent_video':
        before, _ = inspect_video(source)
        path = output / 'silent-review.mp4'
        subprocess.run([str(FFMPEG), '-nostdin', '-v', 'error', '-i', str(source), '-map', '0:v:0', '-c:v', 'copy',
                        '-an', '-movflags', '+faststart', str(path)], check=True, timeout=300)
        after, _ = inspect_video(path)
        if any(before[k] != after[k] for k in ('frame_count', 'width', 'height')) or abs(before['duration_seconds'] - after['duration_seconds']) > 0.05 or after['audio_streams']:
            raise ValueError('Silent export failed video preservation checks')
        report = {'before': before, 'after': after, 'video_stream_copy': True, 'approval': False}
        artifacts = [('video', path, 'Silent review export')]
    else:
        report, artifacts = transcribe(source, output, parameters)
    report_path = output / 'report.json'
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    artifacts.append(('report', report_path, 'Technical report'))
    receipt = {**request, 'status': 'succeeded', 'report': report,
               'outputs': [{'kind': kind, 'path': str(path), 'title': title, 'bytes': path.stat().st_size} for kind, path, title in artifacts]}
    publish_receipt(output, receipt)
    return receipt


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--request', required=True)
    run(parser.parse_args().request)
