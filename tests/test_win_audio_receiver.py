import importlib.util
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock


RECEIVER_PATH = Path(__file__).parents[1] / "scripts" / "win-audio-receiver.py"
SPEC = importlib.util.spec_from_file_location("win_audio_receiver", RECEIVER_PATH)
RECEIVER = importlib.util.module_from_spec(SPEC)
with mock.patch.dict(sys.modules, {"winsound": mock.Mock()}):
    SPEC.loader.exec_module(RECEIVER)


class WindowsReceiverTests(unittest.TestCase):
    def setUp(self):
        self.ledger_dir = tempfile.TemporaryDirectory()
        self.original_ledger_path = RECEIVER.DELIVERY_LEDGER_PATH
        RECEIVER.DELIVERY_LEDGER_PATH = str(
            Path(self.ledger_dir.name) / "deliveries.json"
        )
        with RECEIVER._delivery_lock:
            RECEIVER._deliveries.clear()
        with RECEIVER._audio_lock:
            RECEIVER._audio_cache.update(ts=time.time(), ready=True)

    def tearDown(self):
        RECEIVER.DELIVERY_LEDGER_PATH = self.original_ledger_path
        self.ledger_dir.cleanup()

    def test_delivery_id_is_registered_only_once(self):
        first, first_status = RECEIVER.register_delivery("delivery-1")
        second, second_status = RECEIVER.register_delivery("delivery-1")
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual("accepted", first_status["status"])
        self.assertEqual("accepted", second_status["status"])

    def test_stale_network_snapshot_is_not_location_ready(self):
        with RECEIVER._profile_lock:
            RECEIVER._profile_cache.update(
                ts=time.time() - 31,
                profiles=[{
                    "name": "Aragorn's Pixel 32",
                    "interface_alias": "Wi-Fi",
                    "hardware_interface": True,
                    "virtual": False,
                }],
                ready=True,
            )
        payload = RECEIVER.health_payload()
        self.assertFalse(payload["location_ready"])
        self.assertGreater(payload["location_age_seconds"], 30)

    def test_home_profile_sets_home_lan(self):
        with RECEIVER._profile_lock:
            RECEIVER._profile_cache.update(
                ts=time.time(),
                profiles=[{
                    "name": "AragornHQ",
                    "interface_alias": "Wi-Fi",
                    "hardware_interface": True,
                    "virtual": False,
                }],
                ready=True,
            )
        payload = RECEIVER.health_payload()
        self.assertTrue(payload["location_ready"])
        self.assertTrue(payload["home_lan"])
        self.assertEqual("home", payload["location_state"])

    def test_physical_non_home_profile_is_verified_away(self):
        with RECEIVER._profile_lock:
            RECEIVER._profile_cache.update(
                ts=time.time(),
                profiles=[{
                    "name": "Aragorn's Pixel 32",
                    "interface_alias": "Wi-Fi",
                    "hardware_interface": True,
                    "virtual": False,
                }, {
                    "name": "Tailscale",
                    "interface_alias": "Tailscale",
                    "hardware_interface": False,
                    "virtual": True,
                }],
                ready=True,
            )
        payload = RECEIVER.health_payload()
        self.assertTrue(payload["location_ready"])
        self.assertFalse(payload["home_lan"])
        self.assertEqual("away", payload["location_state"])
        self.assertEqual(["Aragorn's Pixel 32"], payload["physical_network_profiles"])

    def test_vpn_only_profile_is_unknown_not_away(self):
        with RECEIVER._profile_lock:
            RECEIVER._profile_cache.update(
                ts=time.time(),
                profiles=[{
                    "name": "Tailscale",
                    "interface_alias": "Tailscale",
                    "hardware_interface": False,
                    "virtual": True,
                }],
                ready=True,
            )
        payload = RECEIVER.health_payload()
        self.assertFalse(payload["location_ready"])
        self.assertEqual("unknown", payload["location_state"])

    def test_virtual_home_name_does_not_override_physical_away_profile(self):
        with RECEIVER._profile_lock:
            RECEIVER._profile_cache.update(
                ts=time.time(),
                profiles=[{
                    "name": "AragornHQ",
                    "interface_alias": "Tailscale",
                    "hardware_interface": False,
                    "virtual": True,
                }, {
                    "name": "Aragorn's Pixel 32",
                    "interface_alias": "WiFi",
                    "hardware_interface": True,
                    "virtual": False,
                }],
                ready=True,
            )
        payload = RECEIVER.health_payload()
        self.assertFalse(payload["home_lan"])
        self.assertEqual("away", payload["location_state"])

    def test_cancelled_reservation_cannot_be_accepted_later(self):
        created, reserved = RECEIVER.reserve_delivery("delivery-cancel")
        cancelled, cancelled_status = RECEIVER.cancel_delivery("delivery-cancel")
        accepted, final_status = RECEIVER.accept_delivery("delivery-cancel")
        self.assertTrue(created)
        self.assertEqual("reserved", reserved["status"])
        self.assertTrue(cancelled)
        self.assertEqual("cancelled", cancelled_status["status"])
        self.assertFalse(accepted)
        self.assertEqual("cancelled", final_status["status"])

    def test_accepted_reservation_cannot_be_cancelled(self):
        RECEIVER.reserve_delivery("delivery-accepted")
        accepted, _ = RECEIVER.accept_delivery("delivery-accepted")
        cancelled, status = RECEIVER.cancel_delivery("delivery-accepted")
        self.assertTrue(accepted)
        self.assertFalse(cancelled)
        self.assertEqual("accepted", status["status"])

    def test_cancelled_tombstone_survives_receiver_restart(self):
        RECEIVER.reserve_delivery("delivery-persistent-cancel")
        RECEIVER.cancel_delivery("delivery-persistent-cancel")
        with RECEIVER._delivery_lock:
            RECEIVER._deliveries.clear()
        RECEIVER.load_delivery_ledger()
        accepted, status = RECEIVER.accept_delivery("delivery-persistent-cancel")
        self.assertFalse(accepted)
        self.assertEqual("cancelled", status["status"])

    def test_receiver_identity_is_canonicalized_for_fqdn_guard(self):
        self.assertNotIn(".", RECEIVER.RECEIVER_ID)

    def test_invalid_wav_is_rejected_before_acceptance(self):
        self.assertIsNone(RECEIVER.validate_wav(b"not-a-wave"))

    def test_transient_audio_probe_timeout_preserves_fresh_ready_state(self):
        with RECEIVER._audio_lock:
            RECEIVER._audio_cache.update(ts=time.time(), ready=True)
        with mock.patch.object(
            RECEIVER.subprocess,
            "run",
            side_effect=RECEIVER.subprocess.TimeoutExpired("powershell", 3),
        ):
            self.assertTrue(RECEIVER.refresh_audio_ready())
        with RECEIVER._audio_lock:
            self.assertTrue(RECEIVER._audio_cache["ready"])

    def test_explicit_audio_probe_failure_clears_ready_state(self):
        result = mock.Mock(returncode=0, stdout="false\r\n")
        with mock.patch.object(RECEIVER.subprocess, "run", return_value=result):
            self.assertFalse(RECEIVER.refresh_audio_ready())
        with RECEIVER._audio_lock:
            self.assertFalse(RECEIVER._audio_cache["ready"])


if __name__ == "__main__":
    unittest.main()
