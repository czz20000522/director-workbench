"""CPU-only worker contract tests; model inference is explicitly replaced."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave

import numpy as np
import soundfile as sf

SPEC = importlib.util.spec_from_file_location('auk_worker', Path(__file__).resolve().parents[1] / 'tools/generate_auk_flash.py')
worker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker)


class DecoderProtection(unittest.TestCase):
    def test_protects_before_clamp_and_removes_hook(self):
        import torch
        from types import SimpleNamespace
        decoder = SimpleNamespace(conv_post=torch.nn.Identity())
        samples = torch.tensor([[[0.0, 0.5, 1.2, -1.5]]])
        diagnostics = {}
        with worker.protect_decoder_peaks(decoder, diagnostics):
            protected = decoder.conv_post(samples).clamp(-1, 1)
        torch.testing.assert_close(protected, samples * (0.99 / 1.5))
        torch.testing.assert_close(decoder.conv_post(samples), samples)
        self.assertEqual(diagnostics['calls'][0]['raw_boundary_samples'], 2)
        self.assertEqual(len(decoder.conv_post._forward_hooks), 0)

    def test_normal_audio_unchanged_and_failure_removes_hook(self):
        import torch
        from types import SimpleNamespace
        decoder = SimpleNamespace(conv_post=torch.nn.Identity())
        samples = torch.tensor([[[0.0, 0.995, -0.5]]])
        with worker.protect_decoder_peaks(decoder, {}):
            self.assertIs(decoder.conv_post(samples), samples)
        with self.assertRaises(ValueError):
            with worker.protect_decoder_peaks(decoder, {}):
                decoder.conv_post(torch.tensor([float('nan')]))
        self.assertEqual(len(decoder.conv_post._forward_hooks), 0)
        with self.assertRaises(ValueError):
            with worker.protect_decoder_peaks(decoder, {}):
                pass
        self.assertEqual(len(decoder.conv_post._forward_hooks), 0)
        with self.assertRaises(ValueError):
            with worker.protect_decoder_peaks(decoder, {}):
                decoder.conv_post(samples)
                decoder.conv_post(samples)
        self.assertEqual(len(decoder.conv_post._forward_hooks), 0)
        with self.assertRaises(ValueError):
            with worker.protect_decoder_peaks(decoder, {}):
                decoder.conv_post(torch.tensor([float('inf')]))
        self.assertEqual(len(decoder.conv_post._forward_hooks), 0)
        silent = torch.zeros(1, 1, 16)
        with worker.protect_decoder_peaks(decoder, {}):
            self.assertIs(decoder.conv_post(silent), silent)


class WorkerContract(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ref = self.root / 'voice.wav'
        sf.write(self.ref, np.zeros(2400), 24000)
        self.request = {'task_id': 'test-1', 'text': '你好。', 'voice_reference': str(self.ref),
                        'engine': 'auk-flash', 'parameters': {'seed': 20260923, 'gen_seconds': 1,
                        'nfe': 4, 'cfg_strength': 0.0, 'dtype': 'bf16', 'cpu_offload': True}}
        self.path = self.root / 'request.json'
        self.out = self.root / 'result'
        self.models = patch.object(worker, 'MODEL_FILES', {'config': self.ref, 'checkpoint': self.ref,
                                                         'vae': self.ref, 'qwen': self.root})
        self.models.start()
        self.addCleanup(self.models.stop)

    def write_request(self):
        self.path.write_text(json.dumps(self.request), encoding='utf-8')

    def test_pcm_and_receipt_preserve_request(self):
        self.write_request()
        samples = np.array([[0, 0.5, -0.5, 1.2, -1.2]], dtype=np.float32)
        with patch.object(worker, 'infer', return_value=(samples, 24000)) as infer:
            receipt = worker.generate(self.path, self.out)
        infer.assert_called_once_with(self.request, {})
        for key, value in self.request.items():
            self.assertEqual(receipt[key], value)
        self.assertEqual(json.loads((self.out / 'receipt.json').read_text(encoding='utf-8')), receipt)
        self.assertFalse((self.out / 'receipt.tmp').exists())
        with wave.open(receipt['output'], 'rb') as audio:
            self.assertEqual((audio.getnframes(), audio.getnchannels(), audio.getsampwidth()), (5, 1, 2))
        self.assertEqual(receipt['duration_seconds'], 5 / 24000)
        self.assertEqual(receipt['pcm_clipped_samples'], 0)
        self.assertEqual(receipt['pcm_conversion']['raw_out_of_range_samples'], 2)
        self.assertEqual(receipt['pcm_conversion']['raw_boundary_samples'], 2)
        self.assertTrue(receipt['pcm_conversion']['source_saturation_possible'])
        self.assertLess(receipt['pcm_conversion']['gain'], 1)
        decoded, _ = sf.read(receipt['output'], always_2d=True)
        np.testing.assert_allclose(decoded[:, 0], samples[0] * receipt['pcm_conversion']['gain'], atol=1 / 32768)
        self.assertLess(float(np.max(np.abs(decoded))), 1)

    def test_quiet_and_silent_audio_are_not_amplified(self):
        self.write_request()
        for index, samples in enumerate((np.zeros((1, 16)), np.array([[0.125, -0.25], [0.5, -0.125]]))):
            with self.subTest(index=index), patch.object(worker, 'infer', return_value=(samples, 24000)):
                receipt = worker.generate(self.path, self.root / f'normal-{index}')
            self.assertEqual(receipt['pcm_conversion']['gain'], 1)
            self.assertEqual(receipt['pcm_clipped_samples'], 0)
            self.assertFalse(receipt['pcm_conversion']['source_saturation_possible'])
            decoded, _ = sf.read(receipt['output'], always_2d=True)
            np.testing.assert_allclose(decoded, samples.T, atol=1 / 32768)

    def test_invalid_parameters_do_not_create_output(self):
        for key, value in [('seed', True), ('seed', -1), ('gen_seconds', float('nan')),
                           ('gen_seconds', float('inf')), ('gen_seconds', 0), ('gen_seconds', 31),
                           ('nfe', 32), ('cfg_strength', 2), ('dtype', 'fp32'), ('cpu_offload', 1)]:
            with self.subTest(key=key, value=value):
                original = self.request['parameters'][key]
                self.request['parameters'][key] = value
                self.write_request()
                with patch.object(worker, 'infer') as infer, self.assertRaises(ValueError):
                    worker.generate(self.path, self.out)
                infer.assert_not_called()
                self.assertFalse(self.out.exists())
                self.request['parameters'][key] = original

    def test_invalid_fields(self):
        for key, value in [('task_id', '../escape'), ('text', ''), ('engine', 'auk-base'),
                           ('voice_reference', 'relative.wav'), ('parameters', {})]:
            with self.subTest(key=key):
                original = self.request[key]
                self.request[key] = value
                self.write_request()
                with self.assertRaises(ValueError):
                    worker.generate(self.path, self.out)
                self.assertFalse(self.out.exists())
                self.request[key] = original

    def test_existing_output_is_untouched(self):
        self.write_request()
        self.out.mkdir()
        marker = self.out / 'receipt.json'
        marker.write_text('original', encoding='utf-8')
        with patch.object(worker, 'infer') as infer, self.assertRaises(FileExistsError):
            worker.generate(self.path, self.out)
        infer.assert_not_called()
        self.assertEqual(marker.read_text(), 'original')

    def test_invalid_audio_has_no_success_receipt(self):
        self.write_request()
        with patch.object(worker, 'infer', return_value=(np.array([float('nan')]), 24000)):
            with self.assertRaises(ValueError):
                worker.generate(self.path, self.out)
        self.assertFalse((self.out / 'receipt.json').exists())

    def test_model_failure_has_no_success_receipt(self):
        self.write_request()
        with patch.object(worker, 'infer', side_effect=RuntimeError('model failure')):
            with self.assertRaises(RuntimeError):
                worker.generate(self.path, self.out)
        self.assertFalse((self.out / 'receipt.json').exists())

    def test_reference_budget_checked_before_inference(self):
        self.request['parameters']['gen_seconds'] = 30
        self.write_request()
        with patch.object(worker, 'infer') as infer, self.assertRaises(ValueError):
            worker.generate(self.path, self.out)
        infer.assert_not_called()
        self.assertFalse(self.out.exists())

class DesignWorkerContract(unittest.TestCase):
    setUp = WorkerContract.setUp
    write_request = WorkerContract.write_request

    def test_design_needs_no_reference_and_preserves_description(self):
        self.request.pop('voice_reference')
        self.request.update(mode='design', voice_description='青年男声，克制自然。')
        self.write_request()
        with patch.object(worker, 'infer', return_value=(np.zeros((1, 2400), dtype=np.float32), 24000)), patch.object(sf, 'read', side_effect=AssertionError('design must not decode reference')):
            receipt = worker.generate(self.path, self.out)
        self.assertEqual(receipt['voice_description'], self.request['voice_description'])
        self.assertEqual(receipt['mode'], 'design')
        self.assertEqual(receipt['actual_instruction'], worker.build_messages(self.request)[0]['content'][0]['text'])
        self.assertEqual(receipt['instruction_template'], 'auk-pe-instruct-tts-zh-v1')
        self.assertNotIn('voice_reference', receipt)

    def test_design_rejects_reference_and_empty_description(self):
        self.request.update(mode='design', voice_description='青年男声')
        self.write_request()
        with self.assertRaises(ValueError):
            worker.load_request(self.path)
        self.request.pop('voice_reference')
        self.request['voice_description'] = ' '
        self.write_request()
        with self.assertRaises(ValueError):
            worker.load_request(self.path)


class MessageContract(unittest.TestCase):
    def test_native_design_template_separates_description_from_line(self):
        messages = worker.build_messages({'mode': 'design', 'voice_description': '苍老男声', 'text': '我哪儿还有什么家呀。'})
        self.assertEqual(messages, [{'role': 'user', 'content': [{'type': 'text', 'text': '请基于下面的描述: "苍老男声",生成语音内容"我哪儿还有什么家呀。".'}]}])

    def test_reference_message_unchanged(self):
        self.assertEqual(worker.build_messages({'text': '你好。', 'voice_reference': 'voice.wav'}),
                         [{'role': 'user', 'content': [{'type': 'text', 'text': 'Say the following with the same voice: "你好。"'}, {'type': 'audio', 'audio': 'voice.wav'}]}])


if __name__ == '__main__':
    unittest.main()
