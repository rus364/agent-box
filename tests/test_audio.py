"""Host audio in workspace boxes.

What these tests pin down:

* **Off is really off.** A default configuration must not hand a box the
  host's sound server, even when the socket is right there: it carries the
  microphone.
* **On means the socket directory and a client that finds it.** The directory,
  not the socket file, so a restarted sound server stays reachable from a
  running box.
* **A missing sound server degrades, it does not fail.** Audio is an extra
  capability; a headless host still gets a working box and is told why it has
  no sound.
"""
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_box import runtime
from agent_box.config import AudioConfig, Config


def audio_config(enabled: bool = True) -> Config:
    return Config(audio=AudioConfig(enabled=enabled))


class AudioPassthroughTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.pulse = Path(self._tmp.name) / "pulse"
        self.pulse.mkdir()
        patch = mock.patch.object(runtime, "PULSE_DIR", self.pulse)
        patch.start()
        self.addCleanup(patch.stop)

    def _serve(self):
        server = socket.socket(socket.AF_UNIX)
        server.bind(str(self.pulse / "native"))
        self.addCleanup(server.close)

    def test_disabled_exposes_nothing_even_with_a_sound_server(self):
        self._serve()
        self.assertEqual(runtime.audio_args(audio_config(False)), [])
        self.assertEqual(runtime.audio_warnings(audio_config(False)), [])

    def test_enabled_mounts_the_socket_directory_and_points_clients_at_it(self):
        self._serve()
        args = runtime.audio_args(audio_config())
        self.assertIn(f"type=bind,src={self.pulse},dst={self.pulse}", args)
        self.assertIn(f"--env=PULSE_SERVER=unix:{self.pulse / 'native'}", args)
        self.assertEqual(runtime.audio_warnings(audio_config()), [])

    def test_enabled_without_a_sound_server_warns_and_mounts_nothing(self):
        self.assertEqual(runtime.audio_args(audio_config()), [])
        warnings = runtime.audio_warnings(audio_config())
        self.assertEqual(len(warnings), 1)
        self.assertIn(str(self.pulse / "native"), warnings[0])

    def test_a_regular_file_is_not_mistaken_for_the_socket(self):
        """Mounting a stale file would give the agent a box that silently
        records nothing, instead of the warning that explains it."""
        (self.pulse / "native").write_text("")
        self.assertEqual(runtime.audio_args(audio_config()), [])
        self.assertEqual(len(runtime.audio_warnings(audio_config())), 1)


if __name__ == "__main__":
    unittest.main()
