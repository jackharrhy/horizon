from collections import deque
import io
import os
import re
import struct
import threading
import unittest
from unittest.mock import patch

from tools.bot_stream import BotStream, codec_mime, read_exact, select_frame, write_all
from tools.horizon_bot_dashboard import ASSETS, Dashboard


class BotStreamTests(unittest.TestCase):
    def test_dashboard_controls_match_the_player_script(self):
        ids = re.findall(r'\bid="([^"]+)"', (ASSETS / "index.html").read_text())
        references = set(re.findall(r"\$\('([^']+)'\)", (ASSETS / "app.js").read_text()))
        self.assertEqual(len(ids), len(set(ids)), "duplicate element IDs")
        self.assertFalse(references - set(ids), f"missing controls: {references - set(ids)}")

    def test_truncated_packet_fails_instead_of_spinning(self):
        with self.assertRaises(EOFError):
            read_exact(io.BytesIO(b"short"), 12)

    def test_mp4_initialization_and_fragments_remain_separate(self):
        def box(kind, data):
            return struct.pack(">I4s", len(data) + 8, kind) + data

        initialization = box(b"ftyp", b"mp42") + box(b"moov", box(b"avcC", bytes([1, 66, 192, 32])))
        fragment = box(b"moof", b"header") + box(b"mdat", b"pixels")
        stream = object.__new__(BotStream)
        stream.running = threading.Event()
        stream.running.set()
        stream.cv = threading.Condition()
        stream.init = b""
        stream.fragments = deque(maxlen=8)
        stream.sequence = 0
        stream.audio = False
        stream.fragment_times = deque(maxlen=60)
        stream.encoder = type("Encoder", (), {"stdout": io.BytesIO(initialization + fragment)})()
        with self.assertRaises(EOFError):
            stream.read_fragments()
        self.assertEqual(stream.init, initialization)
        self.assertEqual(list(stream.fragments), [(1, fragment)])
        self.assertEqual(stream.mime, 'video/mp4; codecs="avc1.42C020,mp4a.40.2"')

    def test_sound_toggle_reuses_the_encoder_and_clears_old_samples(self):
        stream = object.__new__(BotStream)
        stream.running = threading.Event()
        stream.running.set()
        stream.stop_lock = threading.Lock()
        stream.cv = threading.Condition()
        stream.audio = False
        stream.audio_data = bytearray(b"stale")
        stream.audio_peak = 1.0
        stream.audio_start = 123
        stream.socket = "unused"
        stream.id = "same-stream"
        with patch("tools.bot_stream.control") as control:
            stream.set_audio(True)
            stream.set_audio(True)
            stream.set_audio(False)
        self.assertEqual([call.args[1] for call in control.call_args_list], ["broadcast audio on", "broadcast audio off"])
        self.assertEqual(stream.id, "same-stream")
        self.assertFalse(stream.audio_data)
        self.assertEqual(stream.audio_peak, 0.0)

    def test_codec_declaration_matches_actual_profile_level_and_audio_tracks(self):
        init = b"avcC" + bytes([1, 100, 0, 42])
        self.assertEqual(codec_mime(init, True), 'video/mp4; codecs="avc1.64002A,mp4a.40.2"')
        with self.assertRaises(ValueError):
            codec_mime(b"not AVC", False)

    def test_evicted_video_is_rejected_instead_of_encoding_future_frames(self):
        frames = deque([(1.0, b"first"), (1.02, b"second")])
        self.assertEqual(select_frame(frames, 1.01), (1.0, b"first"))
        with self.assertRaisesRegex(RuntimeError, "queue"):
            select_frame(frames, .9)

    def test_blocked_encoder_pipe_has_a_write_deadline(self):
        read, write = os.pipe()
        try:
            os.set_blocking(write, False)
            with self.assertRaisesRegex(RuntimeError, "stalled"):
                write_all(write, bytes(1024 * 1024), timeout=.02)
        finally:
            os.close(read)
            os.close(write)

    def test_dashboard_rejects_injection_and_nonfinite_coordinates(self):
        dashboard = object.__new__(Dashboard)
        for action in (
            [], {"action": "follow", "name": "player\nquit"},
            {"action": "goto", "x": float("nan"), "y": 65, "z": 0},
            {"action": "goto", "x": 1000, "y": 65, "z": 0},
            {"action": "stream-start", "fps": True, "audio": False},
        ):
            with self.subTest(action=action), self.assertRaises(ValueError):
                dashboard.action(action)

    def test_missing_ffmpeg_does_not_start_native_capture(self):
        with patch("tools.bot_stream.shutil.which", return_value=None), patch("tools.bot_stream.control") as control:
            with self.assertRaisesRegex(RuntimeError, "FFmpeg"):
                BotStream("unused", ".", [640, 360])
            control.assert_not_called()


if __name__ == "__main__":
    unittest.main()
