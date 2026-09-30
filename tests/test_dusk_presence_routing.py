import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


HOOK_PATH = Path(__file__).parents[1] / "scripts" / "voice-stop-hook.py"
SPEC = importlib.util.spec_from_file_location("voice_stop_hook", HOOK_PATH)
VOICE_HOOK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VOICE_HOOK)


POLICY = {
    "enabled": True,
    "origin_target": "",
    "off_lan_target": "http://100.99.87.61:9876/tts",
    "probe_timeout_seconds": 0.5,
}


def config(source="day", **updates):
    cfg = {
        "source_machine": source,
        "remote_audio": True,
        "dusk_presence_routing": dict(POLICY),
    }
    cfg.update(updates)
    return cfg


def health(home_lan=False, receiver_id="dusk", audio_ready=True, location_ready=True):
    profile = "AragornHQ" if home_lan else "Aragorn's Pixel 32"
    return {
        "schema_version": "shelby.voice-receiver.health.v1",
        "receiver_id": receiver_id,
        "audio_ready": audio_ready,
        "location_ready": location_ready,
        "location_state": "home" if home_lan else "away",
        "location_age_seconds": 0.25,
        "network_profiles": [profile, "Tailscale"],
        "physical_network_profiles": [profile],
        "home_lan": home_lan,
    }


class FakeResponse:
    status = 200

    def __init__(self, body, content_type="application/json"):
        self.body = body
        self.headers = {"Content-Type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, _limit=-1):
        return self.body


class DuskPresenceRoutingTests(unittest.TestCase):
    def test_disabled_policy_keeps_legacy_route(self):
        cfg = config()
        cfg["dusk_presence_routing"]["enabled"] = False
        self.assertEqual(
            (VOICE_HOOK.ROUTE_LEGACY, None),
            VOICE_HOOK.resolve_dusk_presence_route(cfg),
        )

    def test_home_receiver_keeps_origin(self):
        with mock.patch.object(VOICE_HOOK, "_receiver_health", return_value=health(home_lan=True)):
            self.assertEqual(
                (VOICE_HOOK.ROUTE_ORIGIN, None),
                VOICE_HOOK.resolve_dusk_presence_route(config()),
            )

    def test_off_lan_receiver_routes_day_and_dawn_to_dusk(self):
        with mock.patch.object(VOICE_HOOK, "_receiver_health", return_value=health(False)):
            for source in ("day", "dawn"):
                with self.subTest(source=source):
                    self.assertEqual(
                        (VOICE_HOOK.ROUTE_DUSK, POLICY["off_lan_target"]),
                        VOICE_HOOK.resolve_dusk_presence_route(config(source)),
                    )

    def test_offline_or_malformed_receiver_keeps_origin(self):
        with mock.patch.object(VOICE_HOOK, "_receiver_health", return_value=None):
            self.assertEqual(
                (VOICE_HOOK.ROUTE_ORIGIN, None),
                VOICE_HOOK.resolve_dusk_presence_route(config()),
            )

    def test_dusk_itself_never_redirects(self):
        dusk_cfg = config("dusk")
        dusk_cfg["dusk_presence_routing"]["origin_target"] = "http://127.0.0.1:9876/tts"
        with mock.patch.object(VOICE_HOOK, "_receiver_health") as probe:
            self.assertEqual(
                (VOICE_HOOK.ROUTE_ORIGIN, "http://127.0.0.1:9876/tts"),
                VOICE_HOOK.resolve_dusk_presence_route(dusk_cfg),
            )
            probe.assert_not_called()

    def test_receiver_health_requires_version_identity_and_readiness(self):
        for payload, expected in (
            (health(False), health(False)),
            (health(False, receiver_id="dawn"), None),
            (health(False, audio_ready=False), None),
            (health(False, location_ready=False), None),
            ({**health(False), "location_age_seconds": 31}, None),
            ({**health(False), "home_lan": True}, None),
            ({**health(False), "network_profiles": []}, None),
            ({**health(False), "physical_network_profiles": []}, None),
            ({**health(False), "location_state": "unknown"}, None),
            ({"status": "ok"}, None),
        ):
            body = json.dumps(payload).encode()
            with self.subTest(payload=payload), mock.patch(
                "urllib.request.urlopen", return_value=FakeResponse(body)
            ):
                self.assertEqual(
                    expected,
                    VOICE_HOOK._receiver_health(
                        "http://100.99.87.61:9876/health", "dusk"
                    ),
                )

    def test_receiver_health_retries_one_transient_timeout(self):
        import urllib.error

        body = json.dumps(health(False)).encode()
        with mock.patch(
            "urllib.request.urlopen",
            side_effect=[urllib.error.URLError("cold path"), FakeResponse(body)],
        ) as request:
            self.assertEqual(
                health(False),
                VOICE_HOOK._receiver_health(
                    "http://100.99.87.61:9876/health", "dusk",
                    timeout=1.0, attempts=2,
                ),
            )
            self.assertEqual(2, request.call_count)

    def test_policy_bypasses_stale_audible_cache_and_legacy_receivers(self):
        cfg = config(
            remote_audio_target="http://100.77.19.108:9876/tts",
            remote_audio_receivers=[
                {"name": "Dawn", "ip": "100.77.19.108", "port": 9876}
            ],
        )
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as cache:
            json.dump({"ts": 9999999999, "remote_url": "http://stale/tts", "play_local": False}, cache)
            cache_path = cache.name
        try:
            with mock.patch.object(VOICE_HOOK, "AUDIBLE_CACHE_PATH", cache_path), mock.patch.object(
                VOICE_HOOK, "resolve_dusk_presence_route", return_value=(VOICE_HOOK.ROUTE_ORIGIN, None)
            ), mock.patch.object(VOICE_HOOK, "is_local_output_real", return_value=True), mock.patch.object(
                VOICE_HOOK, "_probe_url"
            ) as probe:
                self.assertEqual((None, True), VOICE_HOOK.find_audible_path(cfg))
                probe.assert_not_called()
        finally:
            Path(cache_path).unlink(missing_ok=True)

    def test_origin_receiver_uses_audio_health_without_location_dependency(self):
        cfg = config("dawn")
        cfg["dusk_presence_routing"]["origin_target"] = "http://127.0.0.1:9876/tts"
        with mock.patch.object(
            VOICE_HOOK, "resolve_dusk_presence_route",
            return_value=(VOICE_HOOK.ROUTE_ORIGIN, "http://127.0.0.1:9876/tts"),
        ), mock.patch.object(
            VOICE_HOOK, "_origin_receiver_health",
            return_value={"receiver_id": "dawn", "audio_ready": True},
        ), mock.patch.object(
            VOICE_HOOK, "is_local_output_real", return_value=True
        ):
            self.assertEqual(
                ("http://127.0.0.1:9876/tts", True),
                VOICE_HOOK.find_audible_path(cfg),
            )

    def test_origin_receiver_allows_legacy_health_only_on_loopback(self):
        cfg = config("dawn")
        cfg["dusk_presence_routing"]["origin_target"] = "http://127.0.0.1:9876/tts"
        with mock.patch.object(
            VOICE_HOOK, "resolve_dusk_presence_route",
            return_value=(VOICE_HOOK.ROUTE_ORIGIN, "http://127.0.0.1:9876/tts"),
        ), mock.patch.object(VOICE_HOOK, "_origin_receiver_health", return_value="legacy"), mock.patch.object(
            VOICE_HOOK, "is_local_output_real", return_value=False
        ):
            self.assertEqual(
                ("http://127.0.0.1:9876/tts", False),
                VOICE_HOOK.find_audible_path(cfg),
            )

    def test_invalid_versioned_origin_health_never_downgrades_to_legacy(self):
        payload = health(False, receiver_id="dusk", audio_ready=False)
        with mock.patch(
            "urllib.request.urlopen", return_value=FakeResponse(json.dumps(payload).encode())
        ):
            self.assertIs(
                False,
                VOICE_HOOK._origin_receiver_health(
                    "http://127.0.0.1:9876/health", "dawn"
                ),
            )

    def test_exact_plaintext_origin_health_is_legacy_compatible(self):
        with mock.patch(
            "urllib.request.urlopen", return_value=FakeResponse(b"OK", "text/plain")
        ):
            self.assertEqual(
                "legacy",
                VOICE_HOOK._origin_receiver_health(
                    "http://127.0.0.1:9876/health", "dawn"
                ),
            )

    def test_pocket_remote_failure_plays_generated_audio_locally(self):
        wav = b"RIFF" + (b"0" * 2000)
        with mock.patch(
            "urllib.request.urlopen", return_value=FakeResponse(wav)
        ), mock.patch.object(VOICE_HOOK, "send_audio_remote", return_value=False), mock.patch.object(
            VOICE_HOOK, "play_audio_file"
        ) as play:
            self.assertTrue(
                VOICE_HOOK.speak_pocket(
                    "Test.", "jarvis", remote_target="http://dusk/tts",
                    play_local=False, fallback_local=True,
                )
            )
            play.assert_called_once()

    def test_pocket_tail_padding_adds_one_second(self):
        import io
        import wave

        source = io.BytesIO()
        with wave.open(source, "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(24000)
            writer.writeframes(b"\x01\x00" * 2400)
        padded = VOICE_HOOK._pad_wav_tail(source.getvalue(), tail_ms=1000)
        with wave.open(io.BytesIO(padded), "rb") as reader:
            self.assertEqual(26400, reader.getnframes())

    def test_delivery_reconciles_lost_response_without_duplicate_fallback(self):
        import requests

        reserved = mock.Mock(status_code=201)
        accepted = mock.Mock(status_code=200)
        accepted.raise_for_status.return_value = None
        accepted.json.return_value = {"status": "playing"}
        with mock.patch(
            "requests.post", side_effect=[reserved, requests.exceptions.ReadTimeout("lost response")]
        ), mock.patch("requests.get", return_value=accepted) as status_get:
            self.assertTrue(VOICE_HOOK.send_audio_remote(b"wav", "http://dusk:9876/tts"))
            status_get.assert_called_once()

    def test_delivery_cancelled_before_acceptance_allows_local_fallback(self):
        import requests

        reserved = mock.Mock(status_code=201)
        missing = mock.Mock(status_code=404)
        cancelled = mock.Mock(status_code=200, content=b"json")
        cancelled.json.return_value = {"status": "cancelled"}
        with mock.patch(
            "requests.post", side_effect=[reserved, requests.exceptions.ReadTimeout("lost response")]
        ), mock.patch("requests.get", return_value=missing), mock.patch(
            "requests.delete", return_value=cancelled
        ), mock.patch.object(VOICE_HOOK.time, "sleep"):
            self.assertFalse(VOICE_HOOK.send_audio_remote(b"wav", "http://dusk:9876/tts"))

    def test_explicit_rejection_falls_back_even_when_status_is_unavailable(self):
        reserved = mock.Mock(status_code=201)
        rejected = mock.Mock(status_code=409)
        with mock.patch("requests.post", side_effect=[reserved, rejected]), mock.patch(
            "requests.delete", side_effect=OSError("status unavailable")
        ):
            self.assertFalse(
                VOICE_HOOK.send_audio_remote(b"wav", "http://dusk:9876/tts")
            )

    def test_connection_reset_after_acceptance_does_not_duplicate(self):
        import requests

        reserved = mock.Mock(status_code=201)
        accepted = mock.Mock(status_code=200)
        accepted.raise_for_status.return_value = None
        accepted.json.return_value = {"status": "playing"}
        with mock.patch(
            "requests.post",
            side_effect=[reserved, requests.exceptions.ConnectionError("reset after send")],
        ), mock.patch("requests.get", return_value=accepted):
            self.assertTrue(
                VOICE_HOOK.send_audio_remote(b"wav", "http://dusk:9876/tts")
            )

    def test_definitive_dusk_rejection_delivers_once_to_origin_receiver(self):
        primary_reserved = mock.Mock(status_code=201)
        primary_rejected = mock.Mock(status_code=409)
        cancelled = mock.Mock(status_code=200, content=b"json")
        cancelled.json.return_value = {"status": "cancelled"}
        origin_reserved = mock.Mock(status_code=201)
        origin_accepted = mock.Mock(status_code=202)
        playing = mock.Mock(status_code=200)
        playing.raise_for_status.return_value = None
        playing.json.return_value = {"status": "playing"}
        with mock.patch(
            "requests.post",
            side_effect=[primary_reserved, primary_rejected, origin_reserved, origin_accepted],
        ) as post, mock.patch("requests.delete", return_value=cancelled), mock.patch(
            "requests.get", return_value=playing
        ):
            self.assertTrue(
                VOICE_HOOK.send_audio_remote(
                    b"wav", "http://dusk:9876/tts", require_off_lan=True,
                    fallback_target="http://127.0.0.1:9876/tts",
                )
            )
        self.assertEqual("http://dusk:9876/tts", post.call_args_list[1].args[0])
        self.assertEqual("http://127.0.0.1:9876/tts", post.call_args_list[3].args[0])

    def test_preconnect_upload_failure_delivers_once_to_origin_receiver(self):
        import requests

        primary_reserved = mock.Mock(status_code=201)
        cancelled = mock.Mock(status_code=200, content=b"json")
        cancelled.json.return_value = {"status": "cancelled"}
        origin_reserved = mock.Mock(status_code=201)
        origin_accepted = mock.Mock(status_code=202)
        playing = mock.Mock(status_code=200)
        playing.raise_for_status.return_value = None
        playing.json.return_value = {"status": "playing"}
        with mock.patch(
            "requests.post",
            side_effect=[
                primary_reserved,
                requests.exceptions.ConnectTimeout("never connected"),
                origin_reserved,
                origin_accepted,
            ],
        ) as post, mock.patch("requests.delete", return_value=cancelled), mock.patch(
            "requests.get", return_value=playing
        ):
            self.assertTrue(
                VOICE_HOOK.send_audio_remote(
                    b"wav", "http://dusk:9876/tts", require_off_lan=True,
                    fallback_target="http://127.0.0.1:9876/tts",
                )
            )
        self.assertEqual("http://127.0.0.1:9876/tts", post.call_args_list[3].args[0])

    def test_pocket_environment_override_cannot_change_machine_persona(self):
        cfg = config("day", tts_engine="pocket", tts_voice_pocket_en="jarvis")
        with mock.patch.dict(
            VOICE_HOOK.os.environ, {"SHELBY_TTS_POCKET_VOICE": "jarvis-studio-v2"}
        ), mock.patch.object(
            VOICE_HOOK, "find_audible_path", return_value=(None, True)
        ), mock.patch.object(VOICE_HOOK, "speak_pocket", return_value=True) as pocket:
            VOICE_HOOK.speak("Test.", cfg, lang_hint="en")
        self.assertEqual("jarvis", pocket.call_args.args[1])

    def test_lockfile_is_permanent_and_cleared_on_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = str(Path(tmp) / "voice.lock")
            with mock.patch.object(VOICE_HOOK, "LOCKFILE_PATH", lock_path):
                lock = VOICE_HOOK.acquire_lock()
                self.assertIsNotNone(lock)
                VOICE_HOOK.release_lock(lock)
                self.assertTrue(Path(lock_path).exists())
                self.assertEqual("", Path(lock_path).read_text())

    def test_edge_fallback_personas_remain_machine_specific(self):
        for source, expected in (
            ("day", "en-GB-RyanNeural"),
            ("dawn", "en-US-AriaNeural"),
            ("dusk", "en-GB-SoniaNeural"),
        ):
            cfg = config(source, tts_engine="edge", tts_speed="+0%")
            with self.subTest(source=source), mock.patch.object(
                VOICE_HOOK, "find_audible_path", return_value=(None, True)
            ), mock.patch.object(VOICE_HOOK, "speak_edge", new=mock.AsyncMock()) as edge:
                VOICE_HOOK.speak("Test.", cfg, lang_hint="en")
                self.assertEqual(expected, edge.await_args.args[1])


if __name__ == "__main__":
    unittest.main()
