import unittest
from unittest.mock import patch

from tools import horizon_bot_lab as lab


class BotLabTests(unittest.TestCase):
    def test_isolation_drops_production_session_and_fixture_hooks(self):
        with patch.dict(lab.os.environ, {
            "HORIZON_SESSION_FILE": "/not-a-real-session",
            "HORIZON_PLAYER_NAME": "ProductionBot",
            "HORIZON_COMMAND_SCRIPT": "/not-a-real-script",
        }), patch.object(lab.tempfile, "mkdtemp", side_effect=["/a", "/b", "/c", "/d"]):
            env = lab.isolated_env()
        self.assertEqual(env["HORIZON_NO_AUDIO"], "1")
        self.assertEqual(env["HORIZON_LAUNCHER_NO_SELF_UPDATE"], "1")
        self.assertEqual([env[key] for key in (
            "HORIZON_HOME", "HORIZON_LAUNCHER_HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME",
        )], ["/a", "/b", "/c", "/d"])
        self.assertNotIn("HORIZON_SESSION_FILE", env)
        self.assertNotIn("HORIZON_PLAYER_NAME", env)
        self.assertNotIn("HORIZON_COMMAND_SCRIPT", env)

    def test_dead_child_is_not_retried_as_socket_readiness(self):
        def exited():
            raise ChildProcessError("client exited; see log")
        with self.assertRaisesRegex(ChildProcessError, "see log"):
            lab.until(exited)

    def test_panel_cannot_inject_commands_or_leave_arena(self):
        instance = object.__new__(lab.Lab)
        for action in (
            [], {"action": "command", "text": "op anyone"},
            {"action": "follow", "name": "x\nquit"},
            {"action": "goto", "x": "nan", "y": 65, "z": 0},
            {"action": "goto", "x": 1000, "y": 65, "z": 0},
        ):
            with self.subTest(action=action), self.assertRaises(ValueError):
                instance.action(action)


if __name__ == "__main__":
    unittest.main()
