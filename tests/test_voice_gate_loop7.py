import contextlib
import http.server
import io
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from test_voice_session_gate import VOICE_HOOK as hook, gate, WAV, _write_entry


class GateLoopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = self.temp.name
        self.sessions = Path(self.home) / '.claude' / 'sessions'
        self.sid = 'aabbccdd-1234-4abc-8def-1234567890ab'
        old = signal.getsignal(signal.SIGTERM)
        self.addCleanup(signal.signal, signal.SIGTERM, old)

    def entry(self, sid, name='Day · co-shelby-y'):
        return {'sessionId': sid, 'name': name, 'nameSource': 'user',
                'entrypoint': 'cli', 'kind': 'interactive', 'tmux': 'co-shelby-y:@119.%119'}

    def run_hook(self, sid, message, argv=None):
        data = json.dumps({'session_id': sid, 'last_assistant_message': message})
        env = {'HOME': self.home, 'PATH': '/usr/bin:/bin', 'CLAUDE_SESSION_TYPE': 'main'}
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, env, clear=True))
            stack.enter_context(mock.patch.object(hook.sys, 'stdin', io.StringIO(data)))
            stack.enter_context(mock.patch.object(hook.sys, 'argv', argv or ['hook']))
            stack.enter_context(mock.patch.object(hook, 'load_config', return_value={'speak_sessions': 'co_only'}))
            mocks = {name: stack.enter_context(mock.patch.object(hook, name)) for name in
                     ('speak', 'acquire_lock', 'release_lock', '_register_ticket',
                      '_record_missing_voice_block', '_record_lang_drift', '_record_voice_defect', 'log')}
            stack.enter_context(mock.patch.object(hook.os.path, 'exists', return_value=True))
            hook.main()
            return mocks

    def test_live_co_hyphenated_ids_and_hook(self):
        for sid in (self.sid, self.sid.upper()):
            with self.subTest(sid=sid):
                _write_entry(self.sessions, f'{os.getpid()}.json', self.entry(sid))
                decision = gate.decide(sid, env={'HOME': self.home}, home=self.home, pid_alive=lambda pid: pid == os.getpid())
                self.assertTrue(decision.allow)
                self.assertEqual('registry_name', decision.reason)
                result = self.run_hook(sid, '<voice>Ready.</voice>')
                result['speak'].assert_called_once()
                for name in ('_record_missing_voice_block', '_record_lang_drift', '_record_voice_defect'):
                    result[name].assert_not_called()
                with mock.patch.object(gate, '_SESSION_ID_RE', re.compile('[0-9A-Fa-f]{32}')):
                    self.assertEqual('bad_session_id', gate.decide(sid, env={'HOME': self.home}, home=self.home).reason)

    def test_gauntlet_tokens_do_not_authorize_queue(self):
        _write_entry(self.sessions, f'{os.getpid()}.json', self.entry(self.sid, 'Day · gauntlet-EBR'))
        result = self.run_hook(self.sid, 'CO co-shelby-w <voice>Ready.</voice>', ['hook', 'CO co-shelby-w'])
        for name in ('speak', 'acquire_lock', '_record_missing_voice_block', '_record_lang_drift', '_record_voice_defect'):
            result[name].assert_not_called()
        self.assertTrue(any('reason=registry_not_co' in str(c) for c in result['log'].call_args_list))

    def test_dead_producer_during_retry(self):
        _write_entry(self.sessions, '111.json', self.entry(self.sid))
        (self.sessions / '222.json').write_text('{broken')
        alive = {111, 222}
        with mock.patch.object(gate, '_RETRY_SLEEP', side_effect=lambda _: alive.remove(222)):
            decision = gate.decide(self.sid, env={'HOME': self.home}, home=self.home, pid_alive=alive.__contains__)
        self.assertTrue(decision.allow)
        self.assertEqual('registry_name', decision.reason)

    def test_explain_env_home(self):
        _write_entry(self.sessions, f'{os.getpid()}.json', self.entry(self.sid))
        proc = subprocess.run([sys.executable, str(Path(gate.__file__)), '--explain', '--session-id', self.sid,
                               '--env', f'HOME={self.home}'], capture_output=True, text=True, check=True)
        self.assertEqual('registry_name', json.loads(proc.stdout)['reason'])


class VoxTypeGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.queue = self.root / 'queue'
        self.state = self.root / 'voxtype-state'
        self.state.write_text('idle')
        self.sid = 'aabbccdd-1234-4abc-8def-1234567890ab'
        self.sessions = self.root / '.claude' / 'sessions'
        old_handler = signal.getsignal(signal.SIGTERM)
        self.addCleanup(signal.signal, signal.SIGTERM, old_handler)

    def run_hook(self, session_type='main', message='<voice>Ready.</voice>', inspect_gate=None,
                 inspect_loads=None):
        data = json.dumps({'session_id': self.sid, 'last_assistant_message': message})
        env = {'HOME': str(self.root), 'PATH': '/usr/bin:/bin',
               'CLAUDE_SESSION_TYPE': session_type,
               'SHELBY_VOXTYPE_STATE_FILE': str(self.state),
               'SHELBY_VOICE_QUEUE_DIR': str(self.queue)}
        real_gate = hook._session_may_speak
        real_loads = json.loads
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, env, clear=True))
            stack.enter_context(mock.patch.object(hook, 'IS_MACOS', False))
            stack.enter_context(mock.patch.object(hook.sys, 'stdin', io.StringIO(data)))
            stack.enter_context(mock.patch.object(hook, 'load_config', return_value={'speak_sessions': 'co_only'}))
            stack.enter_context(mock.patch.object(hook, 'log'))
            mocks = {name: stack.enter_context(mock.patch.object(hook, name)) for name in
                     ('speak', 'acquire_lock', 'release_lock', '_wait_for_voxtype_idle_status',
                      '_record_missing_voice_block', '_record_lang_drift', '_record_voice_defect')}
            mocks['_wait_for_voxtype_idle_status'].return_value = (0, False)
            if inspect_gate:
                def observe_gate(cfg):
                    inspect_gate()
                    return real_gate(cfg)
                stack.enter_context(mock.patch.object(hook, '_session_may_speak', side_effect=observe_gate))
            if inspect_loads:
                def observe_loads(raw, *args, **kwargs):
                    inspect_loads()
                    return real_loads(raw, *args, **kwargs)
                stack.enter_context(mock.patch.object(hook.json, 'loads', side_effect=observe_loads))
            started = time.monotonic()
            hook.main()
            elapsed = time.monotonic() - started
        return elapsed, mocks

    def test_denied_session_with_older_live_ticket_does_not_wait(self):
        _write_entry(self.sessions, f'{os.getpid()}.json',
                     {'sessionId': self.sid, 'name': 'gauntlet-EBR', 'nameSource': 'user',
                      'entrypoint': 'cli', 'kind': 'interactive'})
        self.queue.mkdir(mode=0o700)
        peer = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(5)'])
        def reap_peer():
            try:
                peer.kill()
            finally:
                peer.wait()
        self.addCleanup(reap_peer)
        older = self.queue / f'{0:020d}-{peer.pid}'
        older.write_bytes(b'older ticket')
        seen = []
        def inspect_gate():
            seen.append(Path(hook._ticket_path))
            self.assertTrue(seen[-1].is_file())
            self.assertTrue(hook._older_ticket_ahead())
        with mock.patch.object(hook, '_is_our_hook', return_value=True):
            elapsed, mocks = self.run_hook(inspect_gate=inspect_gate)
        self.assertLess(elapsed, 1.0)
        self.assertEqual(len(seen), 1)
        self.assertFalse(seen[0].exists())
        self.assertIsNone(hook._ticket_path)
        self.assertEqual(older.read_bytes(), b'older ticket')
        self.assertEqual(list(self.queue.iterdir()), [older])
        for name in ('speak', 'acquire_lock', '_wait_for_voxtype_idle_status',
                     '_record_missing_voice_block', '_record_lang_drift', '_record_voice_defect'):
            mocks[name].assert_not_called()

    def test_denial_leaves_same_queue_state_as_headless(self):
        _, headless = self.run_hook(session_type='headless')
        self.assertTrue(self.queue.is_dir())
        headless_state = sorted(p.name for p in self.queue.iterdir())
        self.assertEqual(headless_state, [])
        self.queue.rmdir()
        _, denied = self.run_hook(message='No voice block')
        self.assertTrue(self.queue.is_dir())
        self.assertEqual(sorted(p.name for p in self.queue.iterdir()), headless_state)
        for name in ('speak', 'acquire_lock', '_wait_for_voxtype_idle_status',
                     '_record_missing_voice_block', '_record_lang_drift', '_record_voice_defect'):
            headless[name].assert_not_called()
            denied[name].assert_not_called()

    def test_allowed_session_registers_before_stdin_parse_and_speaks(self):
        _write_entry(self.sessions, f'{os.getpid()}.json',
                     {'sessionId': self.sid, 'name': 'Day · co-shelby-y', 'nameSource': 'user',
                      'entrypoint': 'cli', 'kind': 'interactive'})
        seen = []
        def inspect_loads():
            if not seen:
                path = Path(hook._ticket_path)
                seen.append((path, list(self.queue.iterdir())))
        _, mocks = self.run_hook(inspect_loads=inspect_loads)
        self.assertEqual(len(seen), 1)
        path, names = seen[0]
        self.assertRegex(path.name, r'^\d{20}-%d$' % os.getpid())
        self.assertIn(path, names)
        self.assertFalse(path.exists())
        self.assertIsNone(hook._ticket_path)
        mocks['speak'].assert_called_once()


class PocketLoopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.servers = []

    def start_server(self, behavior):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                self.server.voices.append(data['voice'])
                behavior(self, data['voice'])
            def log_message(self, *args):
                pass
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.daemon_threads = True
        server.voices = []
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.02}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 1)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server, f'http://127.0.0.1:{server.server_address[1]}'

    @staticmethod
    def wav(handler):
        try:
            handler.send_response(200)
            handler.send_header('Content-Length', str(len(WAV)))
            handler.end_headers()
            handler.wfile.write(WAV)
        except OSError:
            pass

    def run_lane(self, light, heavy, voice='case-o'):
        cfg = {'tts_engine': 'pocket', 'source_machine': 'day', 'pocket_tts_url': heavy,
               'pocket_tts_url_speech': light, 'pocket_tts_speech_timeout_s': 1,
               'pocket_tail_ms': 0, 'tts_voice_pocket_en': voice, 'dutch_speech': {'enabled': False}}
        played = []
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, {'HOME': str(self.root), 'PATH': '/usr/bin:/bin'}, clear=True))
            stack.enter_context(mock.patch.object(hook.tempfile, 'gettempdir', return_value=str(self.root)))
            stack.enter_context(mock.patch.object(hook, 'find_audible_path', return_value=(None, True)))
            stack.enter_context(mock.patch.object(hook, 'play_audio_file', side_effect=lambda p: played.append(Path(p).read_bytes())))
            stack.enter_context(mock.patch.object(hook, 'log'))
            started = time.monotonic()
            hook.speak('Ready.', cfg)
            elapsed = time.monotonic() - started
        return elapsed, played

    def marker(self):
        return self.root / f'claude-voice-light-down-{os.getuid()}.json'

    def test_persona_404_jarvis_success_uses_light(self):
        def behavior(h, voice):
            if voice == 'case-o':
                h.send_error(404)
            else:
                self.wav(h)
        light, light_url = self.start_server(behavior)
        heavy, heavy_url = self.start_server(lambda h, v: self.wav(h))
        _, played = self.run_lane(light_url, heavy_url)
        self.assertEqual(['case-o', 'jarvis'], light.voices)
        self.assertEqual([], heavy.voices)
        self.assertEqual([WAV], played)
        self.assertFalse(self.marker().exists())

    def test_timeout_trickle_and_retry_share_deadline(self):
        for kind in ('timeout', 'retry', 'trickle'):
            with self.subTest(kind=kind):
                if self.marker().exists():
                    self.marker().unlink()
                def behavior(h, voice):
                    if kind == 'retry' and voice == 'case-o':
                        h.send_error(404)
                    elif kind == 'trickle':
                        try:
                            h.send_response(200)
                            h.send_header('Content-Length', '999999')
                            h.end_headers()
                            while True:
                                h.wfile.write(b'x')
                                h.wfile.flush()
                                time.sleep(0.3)
                        except OSError:
                            pass
                    else:
                        time.sleep(2)
                light, light_url = self.start_server(behavior)
                heavy, heavy_url = self.start_server(lambda h, v: self.wav(h))
                elapsed, played = self.run_lane(light_url, heavy_url)
                self.assertLess(elapsed, 1.5 if kind == 'retry' else 3.0)
                self.assertEqual(['case-o', 'jarvis'] if kind == 'retry' else ['case-o'], light.voices)
                self.assertEqual(['case-o'], heavy.voices)
                self.assertEqual([WAV], played)
                self.assertTrue(self.marker().is_file())

    def test_marker_symlink_target_unchanged(self):
        target = self.root / 'target'
        target.write_bytes(b'keep me')
        target.chmod(0o640)
        before = (target.read_bytes(), target.stat().st_mode)
        self.marker().symlink_to(target)
        light, light_url = self.start_server(lambda h, v: time.sleep(2))
        heavy, heavy_url = self.start_server(lambda h, v: self.wav(h))
        elapsed, played = self.run_lane(light_url, heavy_url)
        self.assertLessEqual(elapsed, 1.75)
        self.assertEqual(['case-o'], heavy.voices)
        self.assertEqual([WAV], played)
        self.assertEqual(before, (target.read_bytes(), target.stat().st_mode))
        self.assertFalse(self.marker().is_symlink())

    def test_explicit_personas_on_both_endpoints(self):
        for voice in ('case-o', 'jarvis'):
            with self.subTest(voice=voice):
                if self.marker().exists():
                    self.marker().unlink()
                light, light_url = self.start_server(lambda h, v: time.sleep(2))
                heavy, heavy_url = self.start_server(lambda h, v: self.wav(h))
                self.run_lane(light_url, heavy_url, voice)
                self.assertEqual([voice], light.voices)
                self.assertEqual([voice], heavy.voices)

class PocketDeadlineUnitTests(unittest.TestCase):
    def test_persona_retry_completes_before_marker(self):
        calls = []
        marker = mock.Mock()
        def fetch(text, voice, endpoint, timeout, deadline=None):
            calls.append((voice, endpoint, deadline))
            return (None, True) if voice == 'case-o' else (WAV, True)
        with mock.patch.object(hook, '_pocket_fetch', side_effect=fetch), \
                mock.patch.object(hook, 'play_audio_file'), mock.patch.object(hook, 'log'):
            self.assertTrue(hook.speak_pocket('hello', 'case-o', base_url='light',
                                             fallback_base_url='heavy', first_timeout_s=1,
                                             fallback_timeout_s=3, on_primary_failure=marker,
                                             tail_ms=0))
        self.assertEqual([('case-o', 'light'), ('jarvis', 'light')], [(v, u) for v, u, _ in calls])
        self.assertTrue(all(d is not None and math.isfinite(d) for _, _, d in calls))
        self.assertEqual(calls[0][2], calls[1][2])
        marker.assert_not_called()

    def test_primary_exhaustion_then_heavy_marks_once(self):
        calls = []
        marker = mock.Mock()
        def fetch(text, voice, endpoint, timeout, deadline=None):
            calls.append((voice, endpoint))
            return (None, False) if endpoint == 'light' else (WAV, True)
        with mock.patch.object(hook, '_pocket_fetch', side_effect=fetch), \
                mock.patch.object(hook, 'play_audio_file'), mock.patch.object(hook, 'log'):
            self.assertTrue(hook.speak_pocket('hello', 'case-o', base_url='light',
                                             fallback_base_url='heavy', first_timeout_s=1,
                                             fallback_timeout_s=3, on_primary_failure=marker,
                                             tail_ms=0))
        self.assertEqual([('case-o', 'light'), ('case-o', 'heavy')], calls)
        marker.assert_called_once_with()

    def test_open_headers_obey_overall_deadline(self):
        import urllib.request
        release = threading.Event()
        self.addCleanup(release.set)
        def blocked_open(*args, **kwargs):
            release.wait(1)
            raise OSError("closed")
        with mock.patch.object(urllib.request, 'urlopen', side_effect=blocked_open):
            start = time.monotonic()
            result = hook._pocket_fetch('hello', 'case-o', 'http://127.0.0.1', 1, deadline=start + 0.2)
        self.assertEqual((None, False), result)
        self.assertLess(time.monotonic() - start, 0.6)

    def test_chunked_fetch_checks_overall_deadline(self):
        import socket
        import urllib.request
        reader, writer = socket.socketpair()
        self.addCleanup(reader.close)
        self.addCleanup(writer.close)
        stop = threading.Event()
        def drip():
            while not stop.wait(0.03):
                try:
                    writer.sendall(b'x')
                except OSError:
                    break
        thread = threading.Thread(target=drip, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 1)
        self.addCleanup(stop.set)
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self):
                while reader.recv(65536):
                    pass
        with mock.patch.object(urllib.request, 'urlopen', return_value=Response()), \
                mock.patch.object(hook, 'log'):
            start = time.monotonic()
            result = hook._pocket_fetch('hello', 'case-o', 'http://127.0.0.1', 1, deadline=start + 0.2)
        self.assertEqual((None, False), result)
        self.assertLess(time.monotonic() - start, 0.6)
