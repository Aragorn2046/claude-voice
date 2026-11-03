import contextlib
import io
import http.client
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

from test_voice_session_gate import VOICE_HOOK as hook, gate, WAV, SID, _co_entry, _write_entry
import test_voice_gate_loop7 as loop7


class FramingTests(unittest.TestCase):
    setUp = loop7.PocketLoopTests.setUp
    start_server = loop7.PocketLoopTests.start_server
    wav = staticmethod(loop7.PocketLoopTests.wav)
    run_lane = loop7.PocketLoopTests.run_lane
    marker = loop7.PocketLoopTests.marker

    def test_bad_framing_and_slow_retry_fall_back(self):
        for kind in ('trailers', 'chunk_size', 'truncated', 'malformed', 'non_wav', 'slow_retry'):
            with self.subTest(kind=kind):
                if self.marker().exists():
                    self.marker().unlink()
                stop = threading.Event()
                self.addCleanup(stop.set)

                def bad(h, voice):
                    try:
                        if kind == 'slow_retry':
                            time.sleep(0.8)
                            if voice == 'case-o':
                                h.send_error(404)
                            else:
                                self.wav(h)
                            return
                        h.send_response(200)
                        if kind in ('trailers', 'chunk_size', 'malformed'):
                            h.send_header('Transfer-Encoding', 'chunked')
                        else:
                            h.send_header('Content-Length', '50000' if kind == 'truncated' else '2000')
                        h.end_headers()
                        if kind == 'trailers':
                            h.wfile.write(b'4\r\ndata\r\n0\r\n')
                            h.wfile.flush()
                            while not stop.wait(0.2):
                                h.wfile.write(b'X-Trailer: more\r\n')
                                h.wfile.flush()
                        elif kind == 'chunk_size':
                            while not stop.wait(0.2):
                                h.wfile.write(b'1')
                                h.wfile.flush()
                        elif kind == 'truncated':
                            h.wfile.write((WAV * 7)[:20000])
                        elif kind == 'malformed':
                            h.wfile.write(b'not-hex\r\n')
                        else:
                            h.wfile.write(b'x' * 2000)
                    except OSError:
                        pass

                light, light_url = self.start_server(bad)
                heavy, heavy_url = self.start_server(lambda h, v: self.wav(h))
                elapsed, played = self.run_lane(light_url, heavy_url)
                stop.set()
                self.assertLessEqual(elapsed, 1.75)
                self.assertEqual(['case-o'], heavy.voices)
                self.assertEqual([WAV], played)
                self.assertTrue(self.marker().is_file())
                self.assertEqual(light_url, json.loads(self.marker().read_text())['url'])
                if kind == 'slow_retry':
                    self.assertEqual(['case-o', 'jarvis'], light.voices)

    def test_stalled_heavy_has_own_deadline(self):
        stop = threading.Event()
        self.addCleanup(stop.set)
        def trickle(h, voice):
            try:
                h.send_response(200)
                h.send_header('Content-Length', '50000')
                h.end_headers()
                while not stop.wait(0.2):
                    h.wfile.write(b'x')
                    h.wfile.flush()
            except OSError:
                pass
        heavy, heavy_url = self.start_server(trickle)
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        dead_url = 'http://127.0.0.1:%d' % sock.getsockname()[1]
        sock.close()
        with mock.patch.object(hook, 'log'):
            started = time.monotonic()
            result = hook.speak_pocket('hello', 'case-o', base_url=dead_url,
                                       fallback_base_url=heavy_url, first_timeout_s=1,
                                       fallback_timeout_s=1, tail_ms=0)
        stop.set()
        self.assertFalse(result)
        self.assertLessEqual(time.monotonic() - started, 1.75)
        self.assertEqual(['case-o'], heavy.voices)

    def test_hard_link_marker_does_not_change_target(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        target = Path(outside.name) / 'target'
        target.write_bytes(b'keep hard link')
        target.chmod(0o640)
        before = (target.read_bytes(), target.stat().st_mode)
        os.link(target, self.marker())
        light, light_url = self.start_server(lambda h, v: time.sleep(2))
        heavy, heavy_url = self.start_server(lambda h, v: self.wav(h))
        elapsed, played = self.run_lane(light_url, heavy_url)
        self.assertLessEqual(elapsed, 1.75)
        self.assertEqual(['case-o'], heavy.voices)
        self.assertEqual([WAV], played)
        self.assertEqual(before, (target.read_bytes(), target.stat().st_mode))
        self.assertNotEqual(target.stat().st_ino, self.marker().stat().st_ino)

    def test_exception_falls_through_distinct_endpoints(self):
        marker = mock.Mock()
        with mock.patch.object(hook, '_pocket_fetch', side_effect=[RuntimeError('bad'), (WAV, True)]), \
             mock.patch.object(hook, 'play_audio_file'), mock.patch.object(hook, 'log') as log:
            self.assertTrue(hook.speak_pocket('hello', 'case-o', base_url='light',
                            fallback_base_url='heavy', on_primary_failure=marker, tail_ms=0))
        marker.assert_called_once_with()
        self.assertTrue(any('RuntimeError' in str(c) for c in log.call_args_list))

    def test_deadline_maps_http_and_framing_exceptions(self):
        errors = [urllib.error.HTTPError('http://127.0.0.1', 404, 'missing', {}, None),
                  http.client.IncompleteRead(b'partial'), ValueError('invalid'), RuntimeError('bad')]
        for error in errors:
            with self.subTest(error=type(error).__name__), \
                 mock.patch.object(urllib.request, 'urlopen', side_effect=error), \
                 mock.patch.object(hook, 'log') as logs:
                result = hook._pocket_fetch('hello', 'case-o', 'http://127.0.0.1', 1,
                                            deadline=time.monotonic() + 1)
                self.assertEqual((None, isinstance(error, urllib.error.HTTPError)), result)
                self.assertTrue(any(type(error).__name__ in str(c) for c in logs.call_args_list))


class GateReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sessions = self.root / '.claude' / 'sessions'
        old = signal.getsignal(signal.SIGTERM)
        self.addCleanup(signal.signal, signal.SIGTERM, old)

    def row(self, **changes):
        return _co_entry(pid=os.getpid(), **changes)

    def run_main(self, cfg, message, extra=None, call_speak=False):
        state = self.root / 'voxtype-state'
        state.write_text('idle')
        env = {'HOME': str(self.root), 'PATH': '/usr/bin:/bin', 'CLAUDE_CODE_CHILD_SESSION': '1',
               'SHELBY_VOXTYPE_STATE_FILE': str(state),
               'SHELBY_VOICE_QUEUE_DIR': str(self.root / 'queue')}
        env.update(extra or {})
        payload = json.dumps({'session_id': SID, 'last_assistant_message': message})
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, env, clear=True))
            stack.enter_context(mock.patch.object(hook.sys, 'stdin', io.StringIO(payload)))
            stack.enter_context(mock.patch.object(hook.sys, 'argv', ['hook', 'CO co-shelby-w']))
            stack.enter_context(mock.patch.object(hook, 'load_config', return_value=cfg))
            mocks = {name: stack.enter_context(mock.patch.object(hook, name)) for name in
                     ('acquire_lock', 'release_lock', '_wait_for_voxtype_idle_status',
                      '_record_missing_voice_block', '_record_lang_drift', '_record_voice_defect', 'log')}
            mocks['acquire_lock'].return_value = object()
            mocks['_wait_for_voxtype_idle_status'].return_value = (0, False)
            if not call_speak:
                mocks['speak'] = stack.enter_context(mock.patch.object(hook, 'speak'))
            stack.enter_context(mock.patch.object(hook, 'IS_MACOS', False))
            hook.main()
        return mocks

    def test_real_environment_table(self):
        cases = [({}, {}, True, 'registry_name'),
                 ({'CLAUDE_CODE_ENTRYPOINT': 'sdk-cli'}, {}, False, 'entrypoint'),
                 ({'CLAUDE_CODE_SESSION_ATTENDED': '0'}, {}, False, 'unattended'),
                 ({'CLAUDE_CODE_SHELBY_WORKER': '1'}, {}, False, 'worker'),
                 ({'SHELBY_DELEGATED': '1'}, {}, False, 'delegated'),
                 ({'SHELBY_CODE_WORKER_DEPTH': '1'}, {}, False, 'worker_depth'),
                 ({}, {'nameSource': 'derived'}, False, 'registry_not_co'),
                 ({}, {'name': 'Day · gauntlet-EBR'}, False, 'registry_not_co')]
        for env, changes, allowed, reason in cases:
            with self.subTest(env=env, changes=changes):
                _write_entry(self.sessions, f'{os.getpid()}.json', self.row(**changes))
                environment = {'HOME': str(self.root), 'CLAUDE_CODE_CHILD_SESSION': '1', **env}
                decision = gate.decide(SID, env=environment, home=str(self.root))
                self.assertEqual((allowed, reason), (decision.allow, decision.reason))
                for message in (['CO co-shelby-w <voice>Ready.</voice>'] if allowed else
                                ['CO co-shelby-w <voice>Ready.</voice>', 'CO co-shelby-w no voice block']):
                    m = self.run_main({'speak_sessions': 'co_only'}, message, env)
                    self.assertEqual(int(allowed), m['speak'].call_count)
                    self.assertEqual(int(allowed), m['_wait_for_voxtype_idle_status'].call_count)
                    if not allowed:
                        for name in ('acquire_lock', '_wait_for_voxtype_idle_status',
                                     '_record_missing_voice_block', '_record_lang_drift', '_record_voice_defect'):
                            m[name].assert_not_called()
                        self.assertTrue(any('reason=' + reason in str(c) for c in m['log'].call_args_list))
                self.assertEqual([], list(self.root.rglob('*nag*')))
                self.assertFalse((self.root / '.shelby' / 'voice-lang-drift.json').exists())

    def test_registry_schema_and_corrupt_live_sibling(self):
        for key in ('kind', 'entrypoint', 'nameSource'):
            for value in (None, 'unexpected', 42):
                with self.subTest(key=key, value=value):
                    row = self.row()
                    if value is None:
                        row.pop(key)
                    else:
                        row[key] = value
                    _write_entry(self.sessions, f'{os.getpid()}.json', row)
                    decision = gate.decide(SID, env={'HOME': str(self.root)}, home=str(self.root))
                    self.assertFalse(decision.allow)
                    result = self.run_main({'speak_sessions': 'co_only'}, 'CO co-shelby-w <voice>Ready.</voice>')
                    result['speak'].assert_not_called()
        _write_entry(self.sessions, f'{os.getpid()}.json', self.row())
        (self.sessions / f'{os.getppid()}.json').write_text('{bad')
        decision = gate.decide(SID, env={'HOME': str(self.root)}, home=str(self.root))
        self.assertEqual('registry_unreadable', decision.reason)
        self.run_main({'speak_sessions': 'co_only'}, 'CO co-shelby-w <voice>Ready.</voice>')['speak'].assert_not_called()

    def test_default_policy_does_not_import_gate_and_speaks(self):
        _write_entry(self.sessions, f'{os.getpid()}.json', self.row(name='Day · gauntlet-EBR'))
        for policy in (None, 'all'):
            with self.subTest(policy=policy):
                saved = sys.modules.pop('voice_session_gate', None)
                try:
                    cfg = {} if policy is None else {'speak_sessions': policy}
                    result = self.run_main(cfg, '<voice>Ready.</voice>')
                    result['speak'].assert_called_once()
                    self.assertNotIn('voice_session_gate', sys.modules)
                finally:
                    if saved is not None:
                        sys.modules['voice_session_gate'] = saved

    def test_rollback_same_process_uses_heavy_only(self):
        cfg = {'speak_sessions': 'co_only', 'tts_engine': 'pocket', 'source_machine': 'day',
               'pocket_tts_url': 'http://heavy', 'pocket_tts_url_speech': 'http://light',
               'pocket_tail_ms': 0, 'dutch_speech': {'enabled': False}}
        entry = self.row(name='Day · gauntlet-EBR')
        _write_entry(self.sessions, f'{os.getpid()}.json', entry)
        calls = []
        def fetch(text, voice, url, timeout, **kwargs):
            calls.append((url, voice, timeout, kwargs))
            return WAV, True
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(hook, '_pocket_fetch', side_effect=fetch))
            stack.enter_context(mock.patch.object(hook, 'play_audio_file'))
            stack.enter_context(mock.patch.object(hook, 'find_audible_path', return_value=(None, True)))
            self.run_main(cfg, '<voice>Ready.</voice>', call_speak=True)
            self.assertEqual([], calls)
            entry['name'] = 'Day · co-shelby-y'
            _write_entry(self.sessions, f'{os.getpid()}.json', entry)
            self.run_main(cfg, '<voice>Ready.</voice>', call_speak=True)
            self.assertEqual('http://light', calls[0][0])
            self.assertIn('deadline', calls[0][3])
            entry['name'] = 'Day · gauntlet-EBR'
            _write_entry(self.sessions, f'{os.getpid()}.json', entry)
            rollback = {k: v for k, v in cfg.items() if k not in ('speak_sessions', 'pocket_tts_url_speech')}
            self.run_main(rollback, '<voice>Ready.</voice>', call_speak=True)
        self.assertEqual([('http://heavy', 'case-o', 27, {})], calls[1:])

    def test_denied_retry_ticket_clears_and_allowed_peer_speaks(self):
        denied_sid = '123456781234123412341234567890ab'
        _write_entry(self.sessions, f'{os.getpid()}.json', self.row())
        _write_entry(self.sessions, 'denied.json', _co_entry(sid=denied_sid, pid=os.getpid(),
                                                           name='Day · gauntlet-EBR'))
        corrupt = self.sessions / 'corrupt.json'
        corrupt.write_text('{bad')
        state = self.root / 'state'
        state.write_text('idle')
        script = r'''
import importlib.util, json, os, sys, time
from pathlib import Path
root = Path(sys.argv[1]); role = sys.argv[2]; sid = sys.argv[3]
spec = importlib.util.spec_from_file_location('hook', sys.argv[4])
hook = importlib.util.module_from_spec(spec); spec.loader.exec_module(hook)
import voice_session_gate as gate
hook.IS_MACOS = False
hook.LOCKFILE_PATH = str(root / 'lock')
hook._is_our_hook = lambda pid: True
hook.log = lambda *args: None
hook.load_config = lambda: {'speak_sessions': 'co_only'}
hook.speak = lambda *args, **kwargs: (root / 'spoken').write_text(str(time.monotonic()))
register, clear = hook._register_ticket, hook._clear_ticket
times = {}
def record_register(stamp):
    path = register(stamp)
    times['registered'] = time.monotonic()
    times['path'] = path
    return path
def record_clear(path=None):
    clear(path)
    if 'registered' in times and 'cleared' not in times:
        times['cleared'] = time.monotonic()
hook._register_ticket, hook._clear_ticket = record_register, record_clear
if role == 'denied':
    def retry(delay):
        (root / 'retry').write_text(str(time.monotonic()))
        time.sleep(delay)
    gate._RETRY_SLEEP = retry
hook.main()
(root / (role + '.json')).write_text(json.dumps(times))
'''
        env = {'HOME': str(self.root), 'PATH': '/usr/bin:/bin',
               'PYTHONPATH': str(Path(hook.__file__).parent),
               'SHELBY_VOXTYPE_STATE_FILE': str(state),
               'SHELBY_VOICE_QUEUE_DIR': str(self.root / 'queue')}
        payload = lambda sid: json.dumps({'session_id': sid, 'last_assistant_message': '<voice>Ready.</voice>'})
        def start(role, sid):
            child = subprocess.Popen([sys.executable, '-c', script, str(self.root), role, sid, hook.__file__],
                                     env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True)
            child.stdin.write(payload(sid))
            child.stdin.close()
            return child
        children = []
        try:
            denied = start('denied', denied_sid)
            children.append(denied)
            end = time.monotonic() + 2
            while not (self.root / 'retry').exists() and time.monotonic() < end:
                time.sleep(0.001)
            self.assertTrue((self.root / 'retry').exists())
            self.assertEqual(1, len(list((self.root / 'queue').iterdir())))
            allowed_start = time.monotonic()
            allowed = start('allowed', SID)
            children.append(allowed)
            corrupt.unlink()
            for child in children:
                child.wait(timeout=3)
                self.assertEqual(0, child.returncode, child.stderr.read())
            times = json.loads((self.root / 'denied.json').read_text())
            self.assertLess(times['cleared'] - times['registered'], 0.5)
            self.assertFalse(Path(times['path']).exists())
            self.assertLessEqual(float((self.root / 'spoken').read_text()) - allowed_start, 1.5)
            self.assertLess(allowed_start - float((self.root / 'retry').read_text()), 0.05)
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait()
                child.stdout.close()
                child.stderr.close()
