import importlib.util
import json
from pathlib import Path
import tempfile
import sys
import tracemalloc
import unittest
import wave

import numpy as np

SPEC = importlib.util.spec_from_file_location('audio_edit', Path(__file__).resolve().parents[1] / 'backend/audio_edit.py')
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)


class AudioEditTests(unittest.TestCase):
    def setUp(self):
        runtime = m.OUTPUT_ROOT / 'audio-edit-tests'
        runtime.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix='case-', dir=runtime))
        # Retain diagnostic artifacts; no deletion, no real user assets.
        self.source = self.make('source', 8000, 1, 8000, 8192)
        self.output = self.root / 'edited.wav'

    def make(self, name, rate, channels, frames, value, width=2):
        path = self.root / (name + '.wav')
        with wave.open(str(path), 'wb') as stream:
            stream.setnchannels(channels)
            stream.setsampwidth(width)
            stream.setframerate(rate)
            data = np.full((frames, channels), value, dtype='<i2').tobytes() if width == 2 else bytes(frames * channels * width)
            stream.writeframes(data)
        return path

    def edit(self, **kwargs):
        return m.edit_audio(self.source, self.output, output_root=self.root, **kwargs)

    def decoded(self):
        with wave.open(str(self.output), 'rb') as stream:
            return np.frombuffer(stream.readframes(stream.getnframes()), dtype='<i2')

    def test_trim_fades_preserve_source(self):
        original = self.source.read_bytes()
        report = self.edit(start_seconds=.25, duration_seconds=.5, fade_in_seconds=.1, fade_out_seconds=.1)
        data = self.decoded()
        self.assertEqual((report['frames'], report['duration_seconds']), (4000, .5))
        self.assertEqual((data[0], data[-1], data[2000]), (0, 0, 8192))
        self.assertTrue(np.all(np.diff(data[:800].astype(int)) >= 0))
        self.assertTrue(np.all(np.diff(data[-800:].astype(int)) <= 0))
        self.assertEqual(original, self.source.read_bytes())

    def test_background_short_offset_silence_padding(self):
        bg = self.make('bg', 8000, 1, 2000, 8192)
        original = bg.read_bytes()
        report = self.edit(gain=0, background=bg, background_gain=1, background_offset_seconds=.25)
        data = self.decoded()
        self.assertEqual(report['frames'], 8000)
        self.assertEqual(report['background_mixed_frames'], 2000)
        self.assertTrue(np.all(data[:2000] == 0))
        self.assertTrue(np.all(data[2000:4000] == 8192))
        self.assertTrue(np.all(data[4000:] == 0))
        self.assertEqual(original, bg.read_bytes())

    def test_long_background_clipped_to_source_duration(self):
        bg = self.make('bg', 8000, 1, 16000, 8192)
        report = self.edit(background=bg)
        self.assertEqual(report['frames'], 8000)
        self.assertEqual(report['background_mixed_frames'], 8000)

    def test_mixing_clipping_report(self):
        report = self.edit(gain=4, background=self.source, background_gain=4)
        self.assertEqual(report['peak_before_clipping'], 2)
        self.assertEqual(report['clipped_samples'], 8000)
        self.assertEqual(report['peak_after_clipping'], 32767 / 32768)
        self.assertTrue(np.all(self.decoded() == 32767))

    def test_stereo(self):
        self.source = self.make('stereo', 24000, 2, 24000, -8192)
        report = self.edit(gain=2)
        self.assertEqual((report['sample_rate'], report['channels'], report['frames']), (24000, 2, 24000))
        self.assertTrue(np.all(self.decoded() == -16384))

    def test_existing_output_never_overwritten(self):
        self.output.write_bytes(b'keep')
        with self.assertRaises(FileExistsError):
            self.edit()
        self.assertEqual(self.output.read_bytes(), b'keep')
        with self.assertRaises(FileExistsError):
            m.edit_audio(self.source, self.source, output_root=self.root)

    def test_invalid_parameters_no_output(self):
        for values in ({'gain': float('nan')}, {'gain': True}, {'gain': -1},
                       {'duration_seconds': 0}, {'duration_seconds': 121}, {'start_seconds': 2},
                       {'duration_seconds': 2}, {'fade_in_seconds': .7, 'fade_out_seconds': .7},
                       {'background_offset_seconds': 1}, {'background': self.source, 'background_offset_seconds': 1},
                       {'fade_out_seconds': float('inf')}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.edit(**values)
            self.assertFalse(self.output.exists())

    def test_background_format_mismatch_rejected(self):
        for rate, channels in ((16000, 1), (8000, 2)):
            bg = self.make(f'bg-{rate}-{channels}', rate, channels, rate, 0)
            with self.assertRaisesRegex(ValueError, 'must match'):
                self.edit(background=bg)
            self.assertFalse(self.output.exists())

    def test_unsupported_pcm_width_rejected(self):
        self.source = self.make('pcm8', 8000, 1, 8000, 0, width=1)
        with self.assertRaisesRegex(ValueError, 'PCM16'):
            self.edit()
        self.assertFalse(self.output.exists())

    def test_output_root_boundary(self):
        with self.assertRaises(ValueError):
            m.edit_audio(self.source, self.root.parent / 'escape.wav', output_root=self.root)

    def test_malformed_wav_is_validation_failure(self):
        invalid = self.root / 'invalid.wav'
        for contents in (b'', b'not a WAV file', b'RIFF\x01\x00\x00\x00WAVE'):
            invalid.write_bytes(contents)
            with self.subTest(contents=contents):
                with self.assertRaisesRegex(ValueError, 'Invalid or unsupported WAV'):
                    self.edit(background=invalid)
                with self.assertRaisesRegex(ValueError, 'Invalid or unsupported WAV'):
                    m.edit_audio(invalid, self.output, output_root=self.root)
                self.assertFalse(self.output.exists())

    def test_truncated_pcm_does_not_create_output(self):
        # Header still advertises 8000 frames but payload is cut short.
        damaged = self.root / 'truncated.wav'
        damaged.write_bytes(self.source.read_bytes()[:-100])
        with self.assertRaisesRegex(ValueError, 'Truncated PCM'):
            m.edit_audio(damaged, self.output, output_root=self.root)
        self.assertFalse(self.output.exists())

    def test_invalid_background_headers_and_truncated_data(self):
        damaged = self.root / 'damaged-background.wav'
        valid = self.source.read_bytes()
        for content in (valid[:24] + bytes(4) + valid[28:], valid[:-100],
                        valid[:20] + bytes((3, 0)) + valid[22:]):
            damaged.write_bytes(content)
            with self.assertRaises(ValueError):
                self.edit(background=damaged)
            self.assertFalse(self.output.exists())
        pcm8 = self.make('background8', 8000, 1, 8000, 0, width=1)
        with self.assertRaises(ValueError):
            self.edit(background=pcm8)
        self.assertFalse(self.output.exists())

    def test_maximum_stereo_allocation(self):
        self.source = self.make('maximum', 96000, 2, 96000 * 120, 100)
        tracemalloc.start()
        try:
            report = self.edit(background=self.source, fade_in_seconds=60, fade_out_seconds=60)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(report['frames'], 11520000)
        self.assertEqual(report['channels'], 2)
        self.assertEqual(report['clipped_samples'], 0)
        self.assertLess(peak, 650 * 1024 * 1024)
        (self.root / 'allocation.json').write_text(json.dumps({
            'rate': 96000, 'channels': 2, 'seconds': 120, 'background': True,
            'tracemalloc_peak_bytes': peak, 'metric': 'tracked Python/NumPy allocations; not total process RSS'
        }, indent=2), encoding='utf-8')
        print('MAXIMUM_STEREO_PROOF=' + str(self.root) + ' peak_bytes=' + str(peak))


class RealSpeechProof(unittest.TestCase):
    source = None

    def test_real_speech_edit_and_mix(self):
        if self.source is None:
            self.skipTest('Pass --real-source with an existing PCM16 speech WAV')
        source = Path(self.source)
        original = source.read_bytes()
        with wave.open(str(source), 'rb') as audio:
            rate, channels = audio.getframerate(), audio.getnchannels()
            self.assertGreaterEqual(audio.getnframes() / rate, 4)
        root = Path(tempfile.mkdtemp(prefix='audio-edit-real-', dir=m.OUTPUT_ROOT))
        background = root / 'diagnostic-tone.wav'
        # Diagnostic tone, not production music or a subjective listening test.
        tone = (np.sin(np.arange(rate) * 2 * np.pi * 220 / rate) * 1000).astype('<i2')
        with wave.open(str(background), 'wb') as audio:
            audio.setnchannels(channels)
            audio.setsampwidth(2)
            audio.setframerate(rate)
            audio.writeframes(np.repeat(tone[:, None], channels, axis=1).tobytes())
        report = m.edit_audio(source, root / 'edited-speech.wav', start_seconds=.25,
                              duration_seconds=3.5, gain=.8, fade_in_seconds=.1,
                              fade_out_seconds=.2, background=background,
                              background_gain=.25, background_offset_seconds=1)
        with wave.open(report['output'], 'rb') as audio:
            data = np.frombuffer(audio.readframes(audio.getnframes()), dtype='<i2').reshape(-1, channels)
            self.assertEqual(audio.getnframes(), round(rate * 3.5))
            self.assertEqual((audio.getframerate(), audio.getnchannels(), audio.getsampwidth()), (rate, channels, 2))
        self.assertTrue(np.all(data[0] == 0) and np.all(data[-1] == 0))
        self.assertEqual(report['background_mixed_frames'], rate)
        self.assertEqual(report['clipped_samples'], 0)
        self.assertEqual(source.read_bytes(), original)
        report['verification'] = {'source_bytes_unchanged': True, 'decoded_frames_checked': True,
                                  'fade_endpoints_zero': True, 'background': 'synthetic diagnostic tone',
                                  'human_listening_approved': False}
        (root / 'verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print('REAL_SPEECH_PROOF=' + str(root))


if __name__ == '__main__':
    if '--real-source' in sys.argv:
        index = sys.argv.index('--real-source')
        RealSpeechProof.source = sys.argv[index + 1]
        del sys.argv[index:index + 2]
    unittest.main()
