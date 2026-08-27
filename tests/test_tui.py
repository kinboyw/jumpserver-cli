import threading
import unittest
from types import SimpleNamespace

import pyte

from jumpserver_cli.embedded_session import EmbeddedPtySession, SessionScreen
from jumpserver_cli.tui import JumpServerTui, fuzzy_match


class EmbeddedScreenTests(unittest.TestCase):
    def make_session(self, columns=100, lines=12):
        session = EmbeddedPtySession.__new__(EmbeddedPtySession)
        session._lock = threading.RLock()
        session.screen = SessionScreen(columns, lines, lambda data: None)
        session.stream = pyte.ByteStream(session.screen)
        return session

    def test_render_snapshot_keeps_text_and_attributes_aligned(self):
        session = self.make_session()
        session.stream.feed(
            b"[ops@host ~]$ ll\r\n"
            b"-rw-r--r--. 1 root root 13312 Dec 26 2025 app.txt\r\n"
        )

        lines, rows, cursor = session.render_snapshot()

        self.assertTrue(lines[0].startswith("[ops@host ~]$ ll"))
        self.assertTrue(lines[1].startswith("-rw-r--r--."))
        self.assertEqual(len(lines), len(rows))
        self.assertEqual(len(rows[1]), 100)
        self.assertEqual(len(cursor), 3)

    def test_title_cleanup_only_removes_verified_duplicate_prompt(self):
        session = self.make_session()
        session.screen.title = "ops@host:~"

        normal = session._clean_display_line("ops@host:~$ ")
        duplicate = session._clean_display_line("ops@host:~[ops@host ~]$ ")

        self.assertEqual(normal[0], "ops@host:~$ ")
        self.assertEqual(duplicate[0], "[ops@host ~]$ ")


class TuiLogicTests(unittest.TestCase):
    def test_filter_requires_each_term_to_match_ip_or_hostname(self):
        self.assertTrue(fuzzy_match("ott 10.0", "10.0.0.1", "ott-api"))
        self.assertFalse(fuzzy_match("ott 10.1", "10.0.0.1", "ott-api"))

    def test_compact_asset_row_keeps_ip_and_hostname(self):
        tui = JumpServerTui.__new__(JumpServerTui)
        tui._pending_user_assets = set()
        tui.embedded_sessions = []
        tui.selected_asset_ids = set()
        tui._compact_layout = lambda: True
        asset = {
            "id": "asset-1",
            "title": "10.0.0.1",
            "name": "api-host",
            "meta": {"data": {"ip": "10.0.0.1", "hostname": "api-host"}},
        }

        row = tui._asset_row(asset, False)

        self.assertIn("10.0.0.1", row)
        self.assertIn("api-host", row)

    def test_termination_marks_session_before_stop(self):
        class FakeSession:
            alive = True
            asset_label = "10.0.0.1 api-host"
            connection_status = "connected"

            def __init__(self):
                self.stopped = False

            def stop(self):
                self.stopped = True
                self.alive = False

        session = FakeSession()
        tui = JumpServerTui.__new__(JumpServerTui)
        tui.embedded_sessions = [session]
        tui.embedded_session = session
        tui.status = ""
        tui._invalidate = lambda: None

        tui._terminate_active_session()

        self.assertTrue(session.stopped)
        self.assertEqual(session.connection_status, "terminating")


if __name__ == "__main__":
    unittest.main()
