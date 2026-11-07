"""Final remote delivery checks and bounded presence storage."""

import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

import numpy as np

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GATE = load('round11_gate', ROOT / 'scripts/voice_presence_gate.py')
HOOK = load('round11_hook', ROOT / 'scripts/voice-stop-hook.py')
CMD = b'python3\0/opt/bin/case-presence\0daemon\0'


@unittest.skipUnless(sys.platform.startswith('linux'), 'Linux only')
class PresenceRound11(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.presence = self.root / 'case-presence'
        self.presence.mkdir()
        self.state_path = self.presence / 'state.json'
        self.queue_path = self.presence / 'held-voice.jsonl'
        self.now = time.time()
        self.cfg = {'case_presence_gate': True, 'case_presence_held_max': 200}
        self.state(False)

    def state(self, hold=True):
        self.state_path.write_text(json.dumps({
            'pid': os.getpid(), 'updated_at': self.now, 'hold_voice': hold,
        }))

    def check(self, cfg=None, text='line', queue=True, cmd=CMD):
        return GATE.should_hold(
            cfg or self.cfg, text, runtime_dir=str(self.root), now=self.now,
            cmdline_reader=lambda _pid: cmd, queue=queue,
        )

    def rows(self):
        if not self.queue_path.exists():
            return []
        return [json.loads(line) for line in self.queue_path.read_text().splitlines()]

    def gate_adapter(self):
        return SimpleNamespace(should_hold=lambda cfg, text, queue=True:
                               self.check(cfg, text, queue))

    def test_remote_paths_hold_after_synthesis(self):
        async def save(_path):
            self.state()

        def synth(*_args, **_kwargs):
            self.state()
            return b'wav'

        response = mock.Mock()
        response.iter_content.return_value = iter([b'pcm'])
        response.raise_for_status.return_value = None
        def post(*_args, **_kwargs):
            self.state()
            return response

        edge_module = SimpleNamespace(Communicate=lambda *_a, **_k: SimpleNamespace(save=save))
        sf_module = SimpleNamespace(read=lambda *_a: (np.zeros(4), 24000))
        requests = SimpleNamespace(post=post)
        for engine in ('edge', 'pocket', 'eleven'):
            with self.subTest(engine=engine):
                self.state(False)
                self.queue_path.unlink(missing_ok=True)
                self.assertFalse(self.check(queue=False))
                with mock.patch.object(HOOK, '_load_presence_gate', return_value=self.gate_adapter()), \
                     mock.patch.object(HOOK, 'send_audio_remote') as send, \
                     mock.patch.object(HOOK, 'play_audio_file') as file_player, \
                     mock.patch.object(HOOK, 'play_raw_pcm') as pcm_player, \
                     mock.patch.object(HOOK, '_pocket_fetch', side_effect=synth), \
                     mock.patch.object(HOOK, '_wav_tempo', side_effect=lambda wav, *_a: wav), \
                     mock.patch.object(HOOK, '_pad_wav_tail', side_effect=lambda wav, **_k: wav), \
                     mock.patch.object(HOOK, 'log') as log, \
                     mock.patch.dict(sys.modules, {'edge_tts': edge_module, 'soundfile': sf_module, 'requests': requests}):
                    if engine == 'edge':
                        asyncio.run(HOOK.speak_edge('engine', 'v', '+0%', remote_target='receiver', cfg=self.cfg, line_text='line'))
                    elif engine == 'pocket':
                        self.assertTrue(HOOK.speak_pocket('engine', 'v', remote_target='receiver', cfg=self.cfg, line_text='line'))
                    else:
                        HOOK.speak_elevenlabs_streaming('engine', 'v', 'm', 'key', remote_target='receiver', cfg=self.cfg, line_text='line')
                send.assert_not_called()
                file_player.assert_not_called()
                pcm_player.assert_not_called()
                log.assert_any_call('presence gate: held before remote send')
                self.assertEqual([r['text'] for r in self.rows()], ['line'])

    def test_remote_success_then_local_hold_does_not_requeue(self):
        self.queue_path.write_text(json.dumps({'ts': self.now, 'text': 'earlier'}) + '\n')
        def send(*_args, **_kwargs):
            self.state()
            return True
        with mock.patch.object(HOOK, '_load_presence_gate', return_value=self.gate_adapter()), \
             mock.patch.object(HOOK, '_pocket_fetch', return_value=b'wav'), \
             mock.patch.object(HOOK, '_wav_tempo', side_effect=lambda wav, *_a: wav), \
             mock.patch.object(HOOK, '_pad_wav_tail', side_effect=lambda wav, **_k: wav), \
             mock.patch.object(HOOK, 'send_audio_remote', side_effect=send) as sender, \
             mock.patch.object(HOOK, 'IS_MACOS', True), \
             mock.patch.object(HOOK.subprocess, 'run') as player, mock.patch.object(HOOK, 'log'):
            self.assertTrue(HOOK.speak_pocket('engine', 'v', remote_target='receiver', play_local=True, cfg=self.cfg, line_text='line'))
        sender.assert_called_once()
        player.assert_not_called()
        self.assertEqual([r['text'] for r in self.rows()], ['earlier'])

    def test_failed_remote_then_local_hold_queues_line(self):
        def send(*_args, **_kwargs):
            self.state()
            return False
        with mock.patch.object(HOOK, '_load_presence_gate', return_value=self.gate_adapter()), \
             mock.patch.object(HOOK, '_pocket_fetch', return_value=b'wav'), \
             mock.patch.object(HOOK, '_wav_tempo', side_effect=lambda wav, *_a: wav), \
             mock.patch.object(HOOK, '_pad_wav_tail', side_effect=lambda wav, **_k: wav), \
             mock.patch.object(HOOK, 'send_audio_remote', side_effect=send), \
             mock.patch.object(HOOK, 'IS_MACOS', True), \
             mock.patch.object(HOOK.subprocess, 'run') as player, mock.patch.object(HOOK, 'log'):
            self.assertTrue(HOOK.speak_pocket('engine', 'v', remote_target='receiver', cfg=self.cfg, line_text='line'))
        player.assert_not_called()
        self.assertEqual([r['text'] for r in self.rows()], ['line'])

    def test_hold_before_remote_fallback_skips_fallback_and_local(self):
        response = SimpleNamespace(status_code=500)
        def reserve(*_args, **_kwargs):
            self.state()
            return response
        requests = SimpleNamespace(post=mock.Mock(side_effect=reserve))
        with mock.patch.object(HOOK, '_load_presence_gate', return_value=self.gate_adapter()), \
             mock.patch.object(HOOK, '_pocket_fetch', return_value=b'wav'), \
             mock.patch.object(HOOK, '_wav_tempo', side_effect=lambda wav, *_a: wav), \
             mock.patch.object(HOOK, '_pad_wav_tail', side_effect=lambda wav, **_k: wav), \
             mock.patch.dict(sys.modules, {'requests': requests}), \
             mock.patch.object(HOOK, 'play_audio_file') as player, mock.patch.object(HOOK, 'log') as log:
            self.assertTrue(HOOK.speak_pocket('engine', 'v', remote_target='http://remote/tts',
                                              remote_fallback_target='http://fallback/tts',
                                              cfg=self.cfg, line_text='line'))
        requests.post.assert_called_once()
        player.assert_not_called()
        self.assertEqual([r['text'] for r in self.rows()], ['line'])
        log.assert_any_call('presence gate: held before remote send')

    def test_dutch_remote_hold_and_remote_then_local_hold(self):
        cfg = dict(self.cfg, tts_engine='edge', source_machine='day',
                   dutch_speech={'enabled': True, 'timeout_s': 14}, pocket_play_both=True)
        audio = HOOK.make_wav(b'\0' * 1200)
        sentence = 'Dit is een Nederlandse zin.'
        for before_remote in (True, False):
            with self.subTest(before_remote=before_remote):
                self.state(False)
                self.queue_path.unlink(missing_ok=True)
                def fetch(*_args):
                    if before_remote:
                        self.state()
                    return audio
                def send(*_args, **_kwargs):
                    self.state()
                    return True
                with mock.patch.object(HOOK, '_load_presence_gate', return_value=self.gate_adapter()), \
                     mock.patch.object(HOOK, 'find_audible_path', return_value=('receiver', True)), \
                     mock.patch.object(HOOK, 'resolve_dutch_speech_engine', return_value='omnivoice:tts'), \
                     mock.patch.object(HOOK, '_resolve_dutch_lane_voice', return_value='v'), \
                     mock.patch.object(HOOK, '_omnivoice_fetch', side_effect=fetch), \
                     mock.patch.object(HOOK, '_wav_tempo', side_effect=lambda wav, *_a: wav), \
                     mock.patch.object(HOOK, '_pad_wav_tail', side_effect=lambda wav, **_k: wav), \
                     mock.patch.object(HOOK, '_precheck_dutch_audio'), \
                     mock.patch.object(HOOK, 'send_audio_remote', side_effect=send) as sender, \
                     mock.patch.object(HOOK, 'IS_MACOS', True), \
                     mock.patch.object(HOOK.subprocess, 'run') as player, \
                     mock.patch.object(HOOK, 'speak_edge') as edge, mock.patch.object(HOOK, 'log') as log:
                    HOOK.speak(sentence, cfg, lang_hint='nl')
                self.assertEqual(sender.call_count, 0 if before_remote else 1)
                player.assert_not_called()
                edge.assert_not_called()
                self.assertEqual([r['text'] for r in self.rows()], [sentence] if before_remote else [])
                if before_remote:
                    log.assert_any_call('presence gate: held before remote send')

    def test_disabled_remote_gate_does_not_load_module(self):
        with mock.patch.object(HOOK, '_load_presence_gate', side_effect=AssertionError) as loader, \
             mock.patch.object(HOOK, '_pocket_fetch', return_value=b'wav'), \
             mock.patch.object(HOOK, '_wav_tempo', side_effect=lambda wav, *_a: wav), \
             mock.patch.object(HOOK, '_pad_wav_tail', side_effect=lambda wav, **_k: wav), \
             mock.patch.object(HOOK, 'send_audio_remote', return_value=True) as sender:
            self.assertTrue(HOOK.speak_pocket('engine', 'v', remote_target='receiver',
                                              play_local=False, cfg={'case_presence_gate': False}))
        loader.assert_not_called()
        sender.assert_called_once()

    def test_oversize_state_is_not_parsed(self):
        self.state()
        obj = self.state_path.read_bytes()
        raw = obj + b' ' * (65536 - len(obj)) + b'x'
        self.assertEqual(len(raw), 65537)
        self.state_path.write_bytes(raw)
        self.assertFalse(self.check())
        self.assertEqual(self.rows(), [])

    def test_maximum_history_retained(self):
        self.state()
        records = [{'ts': float(i), 'text': f'{i:03d}' + '😀' * 297} for i in range(200)]
        self.queue_path.write_text(''.join(json.dumps(r, ensure_ascii=True) + '\n' for r in records))
        self.assertGreater(self.queue_path.stat().st_size, 256 * 1024)
        self.assertTrue(self.check(text='new'))
        self.assertEqual(self.rows(), records[1:] + [{'ts': self.now, 'text': 'new'}])

    def test_oversize_malformed_queue_keeps_valid_suffix(self):
        self.state()
        records = [{'ts': float(i), 'text': str(i)} for i in range(12)]
        self.queue_path.write_bytes(b'x' * (GATE._HELD_READ_MAX + 3) + b'\ninvalid\n' +
                                    b''.join(json.dumps(row).encode() + b'\n' for row in records))
        self.assertTrue(self.check(text='new'))
        self.assertEqual(self.rows(), records + [{'ts': self.now, 'text': 'new'}])

    def test_fresh_invalid_publisher_command_lines(self):
        self.state()
        tail = b'/bin/case-presence'
        prefix = b'python3\0'
        truncated = prefix + b'x' * (GATE._CMDLINE_READ_MAX - len(prefix) - len(tail)) + tail
        self.assertEqual(len(truncated), GATE._CMDLINE_READ_MAX)
        for cmd in (truncated, b'python3\0-c\0print("case-presence")\0', b'python3\0-u\0-B\0'):
            with self.subTest(cmd=cmd[:80]):
                self.assertFalse(self.check(cmd=cmd))
        self.assertEqual(self.rows(), [])

    def test_fresh_incomplete_and_nonboolean_states(self):
        base = {'pid': os.getpid(), 'updated_at': self.now, 'hold_voice': True}
        cases = [{k: v for k, v in base.items() if k != 'pid'},
                 {k: v for k, v in base.items() if k != 'hold_voice'},
                 dict(base, hold_voice=1), dict(base, hold_voice='true')]
        for state in cases:
            with self.subTest(state=state):
                self.state_path.write_text(json.dumps(state))
                self.assertFalse(self.check())
        self.assertEqual(self.rows(), [])


if __name__ == '__main__':
    unittest.main()
