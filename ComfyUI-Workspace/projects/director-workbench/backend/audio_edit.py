"""Bounded CPU PCM16 WAV editing; caller supplies trusted paths and ownership."""
from pathlib import Path
from contextlib import contextmanager
import math
import wave

import numpy as np

OUTPUT_ROOT = Path('D:/Comfy-Desktop/ComfyUI-Workspace/runtime')


@contextmanager
def pcm_input(path):
    try:
        with wave.open(str(path), 'rb') as stream:
            if (stream.getsampwidth() != 2 or stream.getcomptype() != 'NONE'
                    or stream.getnchannels() not in (1, 2)
                    or not 8000 <= stream.getframerate() <= 96000):
                raise ValueError('Only PCM16 mono/stereo WAV at 8–96 kHz is supported')
            yield stream
    except (wave.Error, EOFError) as exc:
        raise ValueError(f'Invalid or unsupported WAV: {exc}') from exc


def number(value, name, low=0, high=120):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f'{name} must be finite in [{low}, {high}]')
    return float(value)


def read_segment(path, start, duration, *, expected=None, pad_silence=False):
    with pcm_input(path) as stream:
        rate, channels, total = stream.getframerate(), stream.getnchannels(), stream.getnframes()
        if expected is not None and (rate, channels) != expected:
            raise ValueError('Background sample rate and channels must match the source')
        first = round(start * rate)
        if first >= total:
            raise ValueError('Trim starts beyond the audio')
        count = total - first if duration is None else round(duration * rate)
        if count <= 0 or count > 120 * rate:
            raise ValueError('Selected audio must be nonempty and at most 120 seconds')
        if first + count > total and not pad_silence:
            raise ValueError('Selected source duration exceeds available audio')
        stream.setpos(first)
        available = min(count, total - first)
        data = stream.readframes(available)
        if len(data) != available * channels * 2:
            raise ValueError('Truncated PCM WAV')
        samples = np.frombuffer(data, dtype='<i2').reshape(-1, channels).astype(np.float64)
        samples /= 32768
        if available < count:
            samples = np.pad(samples, ((0, count - available), (0, 0)))
        return samples, rate, channels


def edit_audio(source, output, *, start_seconds=0, duration_seconds=None, gain=1,
               fade_in_seconds=0, fade_out_seconds=0, background=None,
               background_gain=0.25, background_offset_seconds=0,
               output_root=OUTPUT_ROOT, pad_silence=False):
    """Source selection defines exact output frame count. Background starts at offset.

    Short background is padded with silence; long background is trimmed. No resampling,
    channel conversion, looping, normalization or implicit overwrite is performed.
    output_root is trusted server configuration, never request-controlled.
    """
    start = number(start_seconds, 'start_seconds', high=86400)
    if type(pad_silence) is not bool:
        raise ValueError('pad_silence must be boolean')
    duration = None if duration_seconds is None else number(duration_seconds, 'duration_seconds')
    gain = number(gain, 'gain', high=8)
    bg_gain = number(background_gain, 'background_gain', high=8)
    fade_in = number(fade_in_seconds, 'fade_in_seconds')
    fade_out = number(fade_out_seconds, 'fade_out_seconds')
    offset = number(background_offset_seconds, 'background_offset_seconds')
    if background is None and offset:
        raise ValueError('Background offset requires a background track')
    source, output = Path(source).resolve(), Path(output).resolve()
    root = Path(output_root).resolve()
    if not output.is_relative_to(root) or output.suffix.lower() != '.wav':
        raise ValueError('Output must be a WAV within the permitted runtime root')
    if output.exists():
        raise FileExistsError(output)
    samples, rate, channels = read_segment(source, start, duration, pad_silence=pad_silence)
    frames = len(samples)
    actual_duration = frames / rate
    if fade_in + fade_out > actual_duration or offset > actual_duration:
        raise ValueError('Fades/offset exceed output duration')
    mixed = samples
    mixed *= gain
    bg_frames = 0
    if background is not None:
        # Probe length first, read only the useful part; never load an unbounded stem.
        with pcm_input(background) as stream:
            bg_duration = stream.getnframes() / stream.getframerate()
        begin = round(offset * rate)
        needed = frames - begin
        if needed <= 0:
            raise ValueError('Background offset leaves no audible output interval')
        bg, _, _ = read_segment(background, 0, min(bg_duration, needed / rate), expected=(rate, channels))
        bg_frames = min(len(bg), needed)
        bg *= bg_gain
        mixed[begin:begin + bg_frames] += bg[:bg_frames]
        del bg
    fade_in_frames, fade_out_frames = round(fade_in * rate), round(fade_out * rate)
    if fade_in_frames + fade_out_frames > frames:
        raise ValueError('Rounded fade ranges overlap')
    if fade_in_frames:
        mixed[:fade_in_frames] *= np.linspace(0, 1, fade_in_frames)[:, None]
    if fade_out_frames:
        mixed[-fade_out_frames:] *= (np.linspace(1, 0, fade_out_frames)[:, None] if fade_out_frames > 1 else 0)
    peak_before = max(abs(float(mixed.min())), abs(float(mixed.max())))
    scaled = mixed
    scaled *= 32768
    np.rint(scaled, out=scaled)
    clipped = int(np.count_nonzero((scaled < -32768) | (scaled > 32767)))
    np.clip(scaled, -32768, 32767, out=scaled)
    pcm = scaled.astype('<i2')
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('xb') as handle:
        with wave.open(handle, 'wb') as stream:
            stream.setnchannels(channels)
            stream.setsampwidth(2)
            stream.setframerate(rate)
            stream.writeframes(pcm.tobytes())
    return {'status': 'succeeded', 'output': str(output), 'source': str(source),
            'sample_rate': rate, 'channels': channels, 'frames': frames,
            'duration_seconds': actual_duration, 'encoding': 'PCM_16',
            'peak_before_clipping': peak_before,
            'peak_after_clipping': max(abs(int(pcm.min())), abs(int(pcm.max()))) / 32768,
            'clipped_samples': clipped, 'background_mixed_frames': bg_frames,
            'parameters': {'start_seconds': start_seconds, 'duration_seconds': duration_seconds,
                           'pad_silence': pad_silence,
                           'gain': gain, 'fade_in_seconds': fade_in_seconds, 'fade_out_seconds': fade_out_seconds,
                           'background': None if background is None else str(Path(background).resolve()),
                           'background_gain': bg_gain, 'background_offset_seconds': background_offset_seconds}}
