import importlib.util
from pathlib import Path
import unittest
from unittest import mock


HOOK_PATH = Path(__file__).parents[1] / "scripts" / "voice-stop-hook.py"
SPEC = importlib.util.spec_from_file_location("voice_stop_hook", HOOK_PATH)
VOICE_HOOK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VOICE_HOOK)


POLICY = {
    "enabled": True,
    "peer_names": ["dusk"],
    "home_subnets": ["192.168.2.0/24"],
    "home_probe_urls": ["http://192.168.2.27:9876/health"],
    "away_target": "http://100.99.87.61:9876/tts",
    "probe_timeout_seconds": 0.5,
}


class DuskPresenceRoutingTests(unittest.TestCase):
    def test_disabled_policy_does_not_override_normal_route(self):
        self.assertIsNone(
            VOICE_HOOK.resolve_dusk_presence_target(
                {"dusk_presence_routing": {"enabled": False}}
            )
        )

    def test_lan_receiver_marks_dusk_home(self):
        with mock.patch.object(
            VOICE_HOOK, "_probe_url", side_effect=lambda url, timeout=0.5: "192.168.2.27" in url
        ):
            self.assertTrue(VOICE_HOOK._dusk_is_on_home_lan(POLICY))

    def test_tailscale_home_endpoint_marks_dusk_home(self):
        status = {
            "Peer": {
                "node": {
                    "HostName": "DUSK",
                    "DNSName": "dusk.example.ts.net.",
                    "CurAddr": "192.168.2.44:41641",
                }
            }
        }
        with mock.patch.object(VOICE_HOOK, "_probe_url", return_value=False), mock.patch.object(
            VOICE_HOOK, "_tailscale_status_json", return_value=status
        ):
            self.assertTrue(VOICE_HOOK._dusk_is_on_home_lan(POLICY))

    def test_away_and_healthy_routes_to_dusk(self):
        def probe(url, timeout=0.5):
            return url == "http://100.99.87.61:9876/health"

        with mock.patch.object(VOICE_HOOK, "_probe_url", side_effect=probe), mock.patch.object(
            VOICE_HOOK,
            "_tailscale_status_json",
            return_value={
                "Peer": {
                    "node": {"HostName": "DUSK", "CurAddr": "89.200.44.155:58535"}
                }
            },
        ):
            target = VOICE_HOOK.resolve_dusk_presence_target(
                {"dusk_presence_routing": POLICY}
            )
        self.assertEqual("http://100.99.87.61:9876/tts", target)

    def test_offline_dusk_keeps_normal_route(self):
        with mock.patch.object(VOICE_HOOK, "_probe_url", return_value=False), mock.patch.object(
            VOICE_HOOK, "_tailscale_status_json", return_value={}
        ):
            target = VOICE_HOOK.resolve_dusk_presence_target(
                {"dusk_presence_routing": POLICY}
            )
        self.assertIsNone(target)


if __name__ == "__main__":
    unittest.main()
