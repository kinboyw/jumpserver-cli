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

    def test_split_line_preserves_remote_color_runs(self):
        tui = JumpServerTui.__new__(JumpServerTui)
        default = ("default", "default", False, False, False, False, False, False)
        green = ("green", "default", True, False, False, False, False, False)

        fragments = tui._terminal_render_colored_line(
            "abCde",
            tuple((char, green if char == "C" else default) for char in "abCde"),
            {},
        )

        self.assertEqual("".join(text for _, text in fragments), "abCde")
        self.assertTrue(any("#00aa00" in style and text == "C" for style, text in fragments))

    def test_selection_reads_from_the_session_where_it_started(self):
        class FakeSession:
            def display_snapshot(self):
                return ("first pane", "selected text")

        tui = JumpServerTui.__new__(JumpServerTui)
        tui.terminal_selection_anchor = (1, 0)
        tui.terminal_selection_end = (1, 8)
        tui.terminal_selection_session = FakeSession()
        tui.embedded_session = None

        self.assertEqual(tui._terminal_selection_text(), "selected")

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

    def test_user_lookup_timeout_drops_pending_request(self):
        asset = {
            "id": "asset-1",
            "title": "10.0.0.1",
            "meta": {"data": {"ip": "10.0.0.1", "hostname": "api-host"}},
        }
        tui = JumpServerTui.__new__(JumpServerTui)
        tui._pending_user_assets = {"asset-1"}
        tui._user_request_timers = {}
        tui.last_error = ""
        tui.status = ""
        tui._invalidate = lambda **kwargs: None

        tui._asset_users_timed_out(asset)
        tui._asset_users_loaded(asset, [{"id": "u"}], "")

        self.assertNotIn("asset-1", tui._pending_user_assets)
        self.assertIn("timed out", tui.last_error)
        self.assertNotIn("users", tui.__dict__)

    def test_cancel_pending_auth_ignores_late_result(self):
        asset = {"id": "asset-1", "title": "10.0.0.1", "meta": {"data": {"ip": "10.0.0.1"}}}
        user = {"id": "user-1", "username": "ops"}
        tui = JumpServerTui.__new__(JumpServerTui)
        tui._pending_connection_keys = {("asset-1", "user-1")}
        tui._connection_request_timers = {}
        tui.embedded_sessions = []
        tui.embedded_session = None
        tui.view = "terminal"
        tui.users = [user]
        tui.user_index = 0
        tui.status = ""
        tui._focus_navigation = lambda: None
        tui._invalidate = lambda **kwargs: None
        tui._cancel_pending_connections()
        tui._authenticated_for_ssh(asset, user, "late-token", "", False)

        self.assertFalse(tui._pending_connection_keys)
        self.assertFalse(tui.embedded_sessions)
        self.assertEqual(tui.view, "assets")

    def test_retry_last_action_reselects_failed_asset(self):
        asset = {
            "id": "asset-1",
            "title": "10.0.0.1",
            "meta": {"data": {"ip": "10.0.0.1", "hostname": "api-host"}},
        }
        tui = JumpServerTui.__new__(JumpServerTui)
        tui.retry_asset = asset
        tui.retry_user = None
        tui.last_error = "lookup failed"
        tui.users = [{"id": "stale"}]
        tui.user_index = 2
        tui.view = "users"
        tui.focus = "history"
        tui.assets = [asset]
        tui.query = "api"
        tui.asset_index = 0
        tui._focus_navigation = lambda: None
        tui._invalidate = lambda **kwargs: None
        tui._select_asset = lambda: setattr(tui, "retried", True)

        tui._retry_last_action()

        self.assertEqual(tui.view, "assets")
        self.assertEqual(tui.focus, "assets")
        self.assertEqual(tui.users, [])
        self.assertTrue(tui.retried)
        self.assertEqual(tui.asset_index, 0)


if __name__ == "__main__":
    unittest.main()
