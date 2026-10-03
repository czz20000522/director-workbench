import json
from pathlib import Path
import sys
import tempfile
import unittest
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import audio_edit_tasks as tasks


class AudioTaskTests(unittest.TestCase):
    def setUp(self):
        root = tasks.audio_edit.OUTPUT_ROOT / 'audio-edit-task-tests'
        root.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(dir=root))
        self.source = self.root / 'input.wav'
        with wave.open(str(self.source), 'wb') as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(8000)
            stream.writeframes(b'\x00\x10' * 8000)
        self.payload = tasks.prepare('task-1', self.source, self.root / 'tasks', parameters={'duration_seconds': .5})

    def receipt_path(self):
        return Path(self.payload['output_dir']) / 'receipt.json'

    def test_roundtrip_and_original_change(self):
        original = Path(self.payload['request']['source']).read_bytes()
        self.source.write_bytes(b'changed-original')
        receipt = tasks.execute(self.payload)
        self.assertEqual(receipt['frames'], 4000)
        self.assertEqual(tasks.read_receipt(self.payload), receipt)
        self.assertEqual(Path(self.payload['request']['source']).read_bytes(), original)
        self.assertFalse(self.receipt_path().with_suffix('.tmp').exists())

    def test_duplicate_task_and_execution_refused(self):
        snapshot = Path(self.payload['request_path']).read_bytes()
        with self.assertRaises(FileExistsError):
            tasks.prepare('task-1', self.source, self.root / 'tasks', parameters={})
        self.assertEqual(Path(self.payload['request_path']).read_bytes(), snapshot)
        first = tasks.execute(self.payload)
        with self.assertRaises(FileExistsError):
            tasks.execute(self.payload)
        self.assertEqual(tasks.read_receipt(self.payload), first)

    def test_cancel_before_start_creates_no_output(self):
        with self.assertRaises(tasks.AudioEditStoppedBeforeStart):
            tasks.execute(self.payload, lambda: True)
        self.assertFalse(Path(self.payload['output_dir']).exists())

    def test_bad_selection_has_no_success_receipt(self):
        payload = tasks.prepare('bad-selection', self.source, self.root / 'tasks', parameters={'duration_seconds': 2})
        with self.assertRaises(ValueError):
            tasks.execute(payload)
        self.assertFalse((Path(payload['output_dir']) / 'receipt.json').exists())
        self.assertFalse((Path(payload['output_dir']) / 'speech.wav').exists())

    def test_request_snapshot_mismatch(self):
        Path(self.payload['request_path']).write_text('{}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'snapshot differs'):
            tasks.execute(self.payload)
        self.assertFalse(Path(self.payload['output_dir']).exists())

    def test_receipt_mismatch_nonfinite_and_wrong_path(self):
        receipt = tasks.execute(self.payload)
        for key, value in [('task_id', 'other'), ('parameters', {}), ('duration_seconds', float('nan')),
                           ('duration_seconds', float('inf')), ('output', str(self.source)), ('frames', True)]:
            with self.subTest(key=key, value=value):
                self.receipt_path().write_text(json.dumps({**receipt, key: value}), encoding='utf-8')
                with self.assertRaises(ValueError):
                    tasks.read_receipt(self.payload)

    def test_missing_output_rejected(self):
        receipt = tasks.execute(self.payload)
        output = Path(receipt['output'])
        output.rename(output.with_suffix('.retained'))
        with self.assertRaises(FileNotFoundError):
            tasks.read_receipt(self.payload)

    def test_self_consistent_but_wrong_selection_or_format_rejected(self):
        receipt = tasks.execute(self.payload)
        output = Path(receipt['output'])
        for rate, channels, frames in ((8000, 1, 2000), (16000, 1, 8000), (8000, 2, 4000)):
            with self.subTest(rate=rate, channels=channels, frames=frames):
                with wave.open(str(output), 'wb') as stream:
                    stream.setnchannels(channels)
                    stream.setsampwidth(2)
                    stream.setframerate(rate)
                    stream.writeframes(bytes(frames * channels * 2))
                forged = json.loads(json.dumps(receipt))
                metadata = dict(sample_rate=rate, channels=channels, frames=frames, duration_seconds=frames / rate)
                forged.update(metadata)
                forged['report'].update(metadata)
                self.receipt_path().write_text(json.dumps(forged), encoding='utf-8')
                with self.assertRaisesRegex(ValueError, 'requested selection and format'):
                    tasks.read_receipt(self.payload)

    def test_truncated_output_rejected(self):
        receipt = tasks.execute(self.payload)
        output = Path(receipt['output'])
        output.write_bytes(output.read_bytes()[:-10])
        with self.assertRaisesRegex(ValueError, 'Truncated'):
            tasks.read_receipt(self.payload)

    def test_invalid_prepare_does_not_reserve_task(self):
        with self.assertRaises(ValueError):
            tasks.prepare('invalid', self.source, self.root / 'tasks', parameters={'gain': float('nan')})
        self.assertFalse((self.root / 'tasks/invalid').exists())


class RealTaskProof(unittest.TestCase):
    source = None

    def test_real_frozen_task_receipt(self):
        if self.source is None:
            self.skipTest('Pass --real-source to verify the complete adapter using real speech')
        source = Path(self.source)
        original = source.read_bytes()
        root = Path(tempfile.mkdtemp(prefix='audio-task-real-', dir=tasks.audio_edit.OUTPUT_ROOT))
        payload = tasks.prepare('real-speech', source, root, parameters={
            'start_seconds': .25, 'duration_seconds': 3.5, 'gain': .8,
            'fade_in_seconds': .1, 'fade_out_seconds': .2})
        receipt = tasks.execute(payload)
        self.assertEqual(tasks.read_receipt(payload), receipt)
        self.assertEqual(receipt['frames'], 84000)
        self.assertEqual(receipt['sample_rate'], 24000)
        self.assertEqual(receipt['channels'], 1)
        self.assertEqual(source.read_bytes(), original)
        with self.assertRaises(FileExistsError):
            tasks.execute(payload)
        self.assertEqual(tasks.read_receipt(payload), receipt)
        print('REAL_TASK_PROOF=' + str(root))


if __name__ == '__main__':
    if '--real-source' in sys.argv:
        index = sys.argv.index('--real-source')
        RealTaskProof.source = sys.argv[index + 1]
        del sys.argv[index:index + 2]
    unittest.main()
