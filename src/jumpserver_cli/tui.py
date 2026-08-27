"""Fullscreen asset browser for the JumpServer CLI.

The TUI deliberately delegates authentication, token generation and SSH
construction to the existing CLI code. It only owns navigation and local
session history.
"""

from __future__ import annotations

import base64
import difflib
import json
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from prompt_toolkit.application import Application, run_in_terminal
from prompt_toolkit.application.current import get_app
from prompt_toolkit.clipboard.base import ClipboardData
from prompt_toolkit.data_structures import Point
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.filters import Condition
from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import ConditionalContainer, Float, FloatContainer, HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.dimension import Dimension as D
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType, MouseModifier
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Frame, TextArea

from .cli import (
    DEFAULT_BASE_URL,
    JumpCliError,
    SessionStore,
    build_ssh_command,
    build_client,
    configured_base_url,
    configured_org_id,
    ensure_auth,
    get_token_for_resolved,
    secure_write_text,
    save_config,
)
from .pty_session import run_pty_ssh
from .embedded_session import EmbeddedPtySession


# Some WSL terminal frontends emit these xterm modifier sequences directly.
# Without explicit mappings, their trailing ``5~`` can reach the remote shell.
ANSI_SEQUENCES["\x1b[2;2~"] = Keys.ShiftInsert
ANSI_SEQUENCES["\x1b[2;5~"] = Keys.ControlInsert
ANSI_SEQUENCES["\x1b[2;4~"] = Keys.ShiftInsert
ANSI_SEQUENCES["\x1b[2;7~"] = Keys.ControlInsert
ANSI_SEQUENCES["\x1b[27;2;2~"] = Keys.ShiftInsert
ANSI_SEQUENCES["\x1b[27;5;2~"] = Keys.ControlInsert


HISTORY_PATH = Path.home() / ".local" / "state" / "jumpserver-cli" / "history.json"
MAX_HISTORY = 60

STYLE = Style.from_dict(
    {
        "root": "bg:#0a0e14 #d0d7e2",
        "header": "bg:#010409 #8b949e",
        "header.brand": "bg:#010409 #58a6ff bold",
        "header.mode": "bg:#010409 #3fb950 bold",
        "header.muted": "bg:#010409 #6e7681",
        "footer": "bg:#010409 #6e7681",
        "footer.key": "bg:#010409 #f0f6fc bold",
        "footer.search": "bg:#010409 #ffa657 bold",
        "frame": "#30363d",
        "popup": "bg:#161b22 #c9d1d9",
        "frame.title": "#8b949e bold",
        "frame.focused": "#58a6ff bold",
        "item": "#c9d1d9",
        "item.selected": "bg:#1f6feb #ffffff bold",
        "item.muted": "#8b949e",
        "item.accent": "#3fb950 bold",
        "item.warn": "#ffa657 bold",
        "terminal": "#f0f6fc",
        "terminal.selected": "bg:#264f78 #ffffff",
        "terminal.cursor": "bg:#f0f6fc #0a0e14",
        "error": "#ff7b72 bold",
        "detail.label": "#8b949e",
        "detail.value": "#f0f6fc",
    }
)

ANSI_COLOR_HEX = {
    "black": "#000000", "red": "#aa0000", "green": "#00aa00", "yellow": "#aa5500",
    "blue": "#0000aa", "magenta": "#aa00aa", "cyan": "#00aaaa", "white": "#aaaaaa",
    "brightblack": "#555555", "brightred": "#ff5555", "brightgreen": "#55ff55",
    "brightyellow": "#ffff55", "brightblue": "#5555ff", "brightmagenta": "#ff55ff",
    "brightcyan": "#55ffff", "brightwhite": "#ffffff",
}


def fuzzy_match(query: str, *parts: str) -> bool:
    """Match every whitespace-separated term as a contiguous substring."""
    needles = query.casefold().split()
    if not needles:
        return True
    haystacks = [str(part).casefold() for part in parts]
    return all(any(needle in part for part in haystacks) for needle in needles)


def asset_data(asset: dict[str, Any]) -> dict[str, Any]:
    meta = asset.get("meta") or {}
    return meta.get("data") or {}


def asset_ip(asset: dict[str, Any]) -> str:
    data = asset_data(asset)
    return str(data.get("ip") or asset.get("title") or "-")


def asset_hostname(asset: dict[str, Any]) -> str:
    data = asset_data(asset)
    return str(data.get("hostname") or asset.get("name") or "-")


def is_asset(item: dict[str, Any]) -> bool:
    data = asset_data(item)
    return data.get("type") == "asset" or bool(data.get("ip") or data.get("hostname"))


class SessionHistory:
    """Small non-secret history index used by the TUI."""

    def __init__(self, path: Path = HISTORY_PATH) -> None:
        self.path = path
        self.entries: list[dict[str, Any]] = []
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(payload, list):
            self.entries = [entry for entry in payload if isinstance(entry, dict)]

    def sorted_entries(self) -> list[dict[str, Any]]:
        return sorted(
            self.entries,
            key=lambda entry: (int(entry.get("count") or 0), int(entry.get("last_used") or 0)),
            reverse=True,
        )

    def record(self, asset: dict[str, Any], user: dict[str, Any]) -> None:
        asset_id = str(asset.get("id") or "")
        user_id = str(user.get("id") or "")
        if not asset_id or not user_id:
            return
        match = next(
            (
                entry
                for entry in self.entries
                if entry.get("asset_id") == asset_id and entry.get("system_user_id") == user_id
            ),
            None,
        )
        now = int(time.time())
        if match is None:
            match = {
                "asset_id": asset_id,
                "system_user_id": user_id,
                "count": 0,
            }
            self.entries.append(match)
        match.update(
            {
                "ip": asset_ip(asset),
                "hostname": asset_hostname(asset),
                "platform": asset_data(asset).get("platform") or "",
                "system_user": str(user.get("name") or user.get("username") or "-"),
                "username": str(user.get("username") or "-"),
                "last_used": now,
            }
        )
        match["count"] = int(match.get("count") or 0) + 1
        self.entries = self.sorted_entries()[:MAX_HISTORY]
        try:
            secure_write_text(self.path, json.dumps(self.entries, ensure_ascii=False, indent=2) + "\n")
        except OSError:
            # History is convenience state; a read-only home must not break SSH.
            pass


class MouseTextControl(FormattedTextControl):
    """Formatted text control with a pane-level mouse callback."""

    def __init__(self, text: Any, handler: Any, cursor_position: Any) -> None:
        super().__init__(text, focusable=True, show_cursor=False, get_cursor_position=cursor_position)
        self._mouse_callback = handler

    def mouse_handler(self, mouse_event: MouseEvent) -> object:
        return self._mouse_callback(mouse_event)


class JumpServerTui:
    def __init__(self, args: Any, store: SessionStore, client: Any, assets: list[dict[str, Any]]) -> None:
        self.args = args
        self.store = store
        self.client = client
        self.assets = [item for item in assets if is_asset(item)]
        self.history = SessionHistory()
        self.query = ""
        self.filter_mode = False
        self.focus = "assets"
        self.session_search_query = ""
        self.view = "assets"
        self.asset_index = 0
        self._last_asset_click: tuple[int, float] | None = None
        self.selected_asset_ids: set[str] = set()
        self.user_index = 0
        self.history_index = 0
        self.users: list[dict[str, Any]] = []
        self._pending_user_assets: set[str] = set()
        self._pending_connection_keys: set[tuple[str, str]] = set()
        self.status = "Ready"
        self.last_error = ""
        self.embedded_sessions: list[EmbeddedPtySession] = []
        self.embedded_session: EmbeddedPtySession | None = None
        self.active_session_index = 0
        self.split_mode = False
        self.batch_connecting = False
        self._batch_pending = 0
        self._batch_opened = 0
        self._batch_skipped = 0
        self._batch_anchor_index = 0
        self.terminal_command_prefix = False
        self.context_menu_open = False
        self.context_menu_index = 0
        self.session_menu_open = False
        self.session_menu_index = 0
        self.session_menu_target: EmbeddedPtySession | None = None
        self.terminate_confirm_open = False
        self._clipboard_owned = False
        self.terminal_selection_anchor: tuple[int, int] | None = None
        self.terminal_selection_end: tuple[int, int] | None = None
        self.terminal_selecting = False
        self.picker_session: EmbeddedPtySession | None = None
        self.picker_open = False
        self.picker_kind = "upload"
        self.picker_path = Path.cwd()
        self.picker_entries: list[Path] = []
        self.picker_index = 0
        self.picker_selected: set[Path] = set()
        self.picker_list_focused = False

        self.asset_control = MouseTextControl(
            self._asset_frame_text,
            self._asset_mouse_handler,
            self._asset_cursor_position,
        )
        self.history_control = MouseTextControl(
            self._right_text,
            self._history_mouse_handler,
            self._history_cursor_position,
        )
        self.search_status_control = FormattedTextControl(self._search_text)
        self.search_input = TextArea(
            text="",
            multiline=False,
            height=1,
            prompt="  SEARCH > ",
            style="class:search",
            wrap_lines=False,
        )
        self.search_input.buffer.on_text_changed += self._search_changed
        self.search_status_window = Window(content=self.search_status_control, height=1)
        self.search_input_window = Window(content=self.search_input.control, height=1)
        self.right_window = Window(content=self.history_control, wrap_lines=False)
        self.session_control = MouseTextControl(
            self._session_list_text,
            self._session_mouse_handler,
            self._session_cursor_position,
        )
        self.session_window = Window(content=self.session_control, wrap_lines=False)
        self.terminal_control = MouseTextControl(
            self._terminal_text,
            self._terminal_mouse_handler,
            self._terminal_cursor_position,
        )
        self.context_menu_control = MouseTextControl(
            self._context_menu_text,
            self._context_menu_mouse_handler,
            lambda: Point(x=0, y=self.context_menu_index),
        )
        self.context_menu_float = Float(
            content=ConditionalContainer(
                Frame(
                    Window(content=self.context_menu_control, wrap_lines=False),
                    title=" ACTIONS ",
                    style="class:popup",
                ),
                filter=Condition(lambda: self.context_menu_open),
            ),
            top=4,
            left=8,
            width=28,
            height=5,
        )
        self.session_menu_control = MouseTextControl(
            self._session_menu_text,
            self._session_menu_mouse_handler,
            lambda: Point(x=0, y=self.session_menu_index),
        )
        self.session_menu_float = Float(
            content=ConditionalContainer(
                Frame(
                    Window(content=self.session_menu_control, wrap_lines=False),
                    title=" SESSION ",
                    style="class:popup",
                ),
                filter=Condition(lambda: self.session_menu_open),
            ),
            top=4,
            left=4,
            width=32,
            height=5,
        )
        self.terminate_confirm_control = MouseTextControl(
            self._terminate_confirm_text,
            self._terminate_confirm_mouse_handler,
            lambda: Point(x=0, y=0),
        )
        self.terminate_confirm_float = Float(
            content=ConditionalContainer(
                Frame(
                    Window(content=self.terminate_confirm_control, wrap_lines=False),
                    title=" CONFIRM ",
                    style="class:popup",
                ),
                filter=Condition(lambda: self.terminate_confirm_open),
            ),
            top=4,
            left=4,
            width=40,
            height=5,
        )
        self.terminal_window = Window(content=self.terminal_control, wrap_lines=False)
        self.picker_path_input = TextArea(
            text=str(self.picker_path),
            multiline=False,
            height=1,
            prompt="  Path > ",
            wrap_lines=False,
        )
        self.picker_path_input.buffer.on_text_changed += self._picker_path_changed
        self.picker_list_control = MouseTextControl(
            self._picker_text,
            self._picker_mouse_handler,
            self._picker_cursor_position,
        )
        self.picker_list_window = Window(content=self.picker_list_control, wrap_lines=False)
        self.picker_window = HSplit(
            [
                Window(content=FormattedTextControl(self._picker_header), height=1),
                Window(content=self.picker_path_input.control, height=1),
                self.picker_list_window,
                Window(content=FormattedTextControl(self._picker_footer), height=1),
            ],
            style="class:root",
        )
        self._refresh_picker_entries()
        self.application = Application(
            layout=Layout(self._layout(), focused_element=self.asset_control),
            key_bindings=self._bindings(),
            style=STYLE,
            full_screen=True,
            mouse_support=True,
            max_render_postpone_time=0,
            refresh_interval=0.5,
        )
        # Escape is both a standalone key and the prefix of arrow/Alt
        # sequences. Keep the disambiguation window short so leaving an SSH
        # pane does not feel like a one-second pause.
        self.application.ttimeoutlen = 0.05

    @property
    def filtered_assets(self) -> list[dict[str, Any]]:
        return [
            asset
            for asset in self.assets
            if fuzzy_match(self.query, asset_ip(asset), asset_hostname(asset))
        ]

    @property
    def history_entries(self) -> list[dict[str, Any]]:
        return self.history.sorted_entries()

    def _compact_layout(self) -> bool:
        try:
            return get_app().output.get_size().columns < 110
        except Exception:
            return False

    def _layout(self) -> HSplit:
        asset_window = Window(
            content=self.asset_control,
            width=D(preferred=68, min=42),
            get_vertical_scroll=self._asset_vertical_scroll,
        )
        compact_asset_window = Window(
            content=self.asset_control,
            get_vertical_scroll=self._asset_vertical_scroll,
        )
        body = VSplit(
            [
                asset_window,
                Window(width=1, char="|", style="class:frame"),
                HSplit(
                    [
                Window(content=FormattedTextControl(self._detail_frame_text), height=D(preferred=11, min=8)),
                        Window(height=1, char="-", style="class:frame"),
                self.right_window,
                    ]
                ),
            ],
            padding=1,
        )
        compact_body = HSplit([compact_asset_window], padding=1)
        terminal_body = ConditionalContainer(
            Window(content=self.terminal_control, wrap_lines=False),
            filter=Condition(lambda: len(self.embedded_sessions) <= 1),
        )
        multi_terminal_body = ConditionalContainer(
            VSplit(
                [
                    Window(content=self.session_control, width=D(preferred=34, min=24)),
                    Window(width=1, char="|", style="class:frame"),
                    Window(content=self.terminal_control, wrap_lines=False),
                ],
                padding=1,
            ),
            filter=Condition(lambda: len(self.embedded_sessions) > 1),
        )
        base = HSplit(
            [
                Window(content=FormattedTextControl(self._header_text), height=1, style="class:header"),
                ConditionalContainer(self.search_input_window, filter=Condition(lambda: self.filter_mode)),
                ConditionalContainer(self.search_status_window, filter=Condition(lambda: not self.filter_mode)),
                ConditionalContainer(
                    ConditionalContainer(body, filter=Condition(lambda: not self._compact_layout())),
                    filter=Condition(lambda: self.view != "terminal"),
                ),
                ConditionalContainer(
                    ConditionalContainer(compact_body, filter=Condition(self._compact_layout)),
                    filter=Condition(lambda: self.view != "terminal"),
                ),
                ConditionalContainer(
                    HSplit([terminal_body, multi_terminal_body]),
                    filter=Condition(lambda: self.view == "terminal"),
                ),
                Window(content=FormattedTextControl(self._footer_text), height=1, style="class:footer"),
            ],
            style="class:root",
        )
        return FloatContainer(
            content=base,
            floats=[
                Float(
                    content=ConditionalContainer(
                        Frame(self.picker_window, title=" FILE TRANSFER ", style="class:popup"),
                        filter=Condition(lambda: self.picker_open),
                    ),
                    xcursor=True,
                    ycursor=True,
                    attach_to_window=self.terminal_window,
                    width=88,
                    height=22,
                ),
                self.context_menu_float,
                self.session_menu_float,
                self.terminate_confirm_float,
            ],
        )

    def _header_text(self) -> FormattedText:
        auth = self.store.auth_mode()
        return FormattedText(
            [
                ("class:header.brand", "  JUMPCLI "),
                ("class:header.muted", " / "),
                ("class:header.mode", " ASSET CONSOLE "),
                ("class:header.muted", f"  {self.args.base_url}  auth:{auth}"),
            ]
        )

    def _search_text(self) -> FormattedText:
        prefix = "  SEARCH > " if self.filter_mode else "  FILTER   "
        hint = "type to filter assets" if not self.query else f"{len(self.filtered_assets)} matches"
        return FormattedText(
            [
                ("class:footer.search", prefix),
                ("class:item", self.query),
                ("class:item.muted", f"  [{hint}]"),
            ]
        )

    def _search_changed(self, buffer: Any) -> None:
        self.query = buffer.text
        self.asset_index = 0
        self._invalidate()

    def _terminal_text(self) -> FormattedText:
        sessions = self._visible_sessions()
        self._sync_terminal_size(sessions)
        if self.split_mode and len(sessions) >= 2:
            return self._terminal_split_text(sessions[:2])
        session = self.embedded_session
        if session is None:
            return FormattedText([("class:item.muted", "SSH session is not running")])
        rows: FormattedText = []
        lines = session.display_snapshot()
        styled = session.styled_snapshot()
        cursor = session.cursor_snapshot()
        style_cache: dict[tuple[Any, ...], str] = {}
        for row, line in enumerate(lines):
            attrs = styled[row] if row < len(styled) else ()
            rows.extend(self._terminal_render_line(row, line, attrs, cursor, style_cache))
            rows.append(("class:terminal", "\n"))
        return rows

    def _terminal_split_text(self, sessions: list[EmbeddedPtySession]) -> FormattedText:
        left, right = sessions[0].display_snapshot(), sessions[1].display_snapshot()
        rows: FormattedText = []
        width = max(20, max((len(line) for line in left), default=20))
        for index in range(max(len(left), len(right))):
            left_line = left[index] if index < len(left) else ""
            right_line = right[index] if index < len(right) else ""
            rows.append(("class:terminal", f"{left_line:<{width}} | {right_line}\n"))
        return rows

    def _terminal_cursor_position(self) -> Point:
        session = self.embedded_session
        if session is None:
            return Point(x=0, y=0)
        x, y, _ = session.cursor_snapshot()
        return Point(x=x, y=y)

    def _session_cursor_position(self) -> Point:
        return Point(x=0, y=self.active_session_index + 1)

    def _session_list_text(self) -> FormattedText:
        title = " ACTIVE SESSIONS"
        if self.focus == "sessions":
            title += f"  FIND: {self.session_search_query}_"
        rows: FormattedText = [("class:frame.focused", title + "\n")]
        for index, session in enumerate(self.embedded_sessions):
            asset = getattr(session, "asset_label", "SSH session")
            marker = ">" if index == self.active_session_index else " "
            style = "class:item.selected" if index == self.active_session_index else "class:item"
            state = getattr(session, "connection_status", "connected")
            rows.append((style, f"{marker} {index + 1:02d} {asset} [{state}]\n"))
        rows.append(("class:item.muted", "\n  Enter switch  Ctrl-N new"))
        return rows

    def _session_mouse_handler(self, event: MouseEvent) -> None:
        if event.event_type == MouseEventType.SCROLL_UP:
            self._switch_session(-1)
            return
        if event.event_type == MouseEventType.SCROLL_DOWN:
            self._switch_session(1)
            return
        if event.event_type == MouseEventType.MOUSE_DOWN and event.button == MouseButton.RIGHT:
            index = event.position.y - 1
            if 0 <= index < len(self.embedded_sessions):
                self.session_menu_target = self.embedded_sessions[index]
                self.session_menu_index = 0
                self.session_menu_open = True
                try:
                    size = get_app().output.get_size()
                    self.session_menu_float.left = min(max(0, event.position.x + 1), max(0, size.columns - 33))
                    self.session_menu_float.top = min(max(2, event.position.y + 2), max(2, size.rows - 6))
                except Exception:
                    self.session_menu_float.left = max(0, event.position.x + 1)
                    self.session_menu_float.top = max(2, event.position.y + 2)
                self._invalidate()
            return
        if event.event_type != MouseEventType.MOUSE_DOWN or event.button != MouseButton.LEFT:
            return
        index = event.position.y - 1
        if 0 <= index < len(self.embedded_sessions):
            self._switch_session_to(index)

    def _session_menu_text(self) -> FormattedText:
        items = ("Terminate session", "Terminate all sessions", "Close menu")
        rows: FormattedText = []
        for index, item in enumerate(items):
            style = "class:item.selected" if index == self.session_menu_index else "class:item"
            rows.append((style, f"  {item}\n"))
        return rows

    def _terminate_confirm_text(self) -> FormattedText:
        return FormattedText(
            [
                ("class:item.warn", "  Terminate all active sessions?\n"),
                ("class:item", "  Yes, terminate\n"),
                ("class:item", "  Cancel"),
            ]
        )

    def _open_terminate_confirm(self) -> None:
        self._close_session_menu()
        self.terminate_confirm_open = True
        try:
            self.terminate_confirm_float.left = self.session_menu_float.left
            self.terminate_confirm_float.top = self.session_menu_float.top
        except Exception:
            pass
        self.status = "Confirm terminating all SSH sessions"
        self._invalidate()

    def _close_terminate_confirm(self) -> None:
        self.terminate_confirm_open = False
        self._invalidate()

    def _confirm_terminate_all(self, confirmed: bool) -> None:
        self._close_terminate_confirm()
        if confirmed:
            self._terminate_all_sessions()

    def _terminate_confirm_mouse_handler(self, event: MouseEvent) -> None:
        if event.event_type != MouseEventType.MOUSE_DOWN or event.button != MouseButton.LEFT:
            return
        # The first line is explanatory text; the following two rows are the
        # clickable Yes/Cancel actions.
        if event.position.y == 1:
            self._confirm_terminate_all(True)
        elif event.position.y == 2:
            self._confirm_terminate_all(False)

    def _close_session_menu(self) -> None:
        self.session_menu_open = False
        self.session_menu_target = None
        if self.view == "terminal" and len(self.embedded_sessions) > 1:
            self._focus_sessions()
        self._invalidate()

    def _terminate_session(self, session: EmbeddedPtySession | None) -> None:
        if session is None or session not in self.embedded_sessions:
            return
        if not session.alive:
            return
        session.connection_status = "terminating"
        label = getattr(session, "asset_label", "SSH session")
        self.status = f"Terminating {label}"
        session.stop()
        self._invalidate()

    def _terminate_active_session(self) -> None:
        self._terminate_session(self.embedded_session)

    def _terminate_all_sessions(self) -> None:
        sessions = list(self.embedded_sessions)
        if not sessions:
            return
        self.status = f"Terminating {len(sessions)} SSH sessions"
        for session in sessions:
            self._terminate_session(session)

    def _session_menu_activate(self) -> None:
        if self.session_menu_index == 0:
            self._terminate_session(self.session_menu_target)
        elif self.session_menu_index == 1:
            self._open_terminate_confirm()
        self._close_session_menu()

    def _session_menu_mouse_handler(self, event: MouseEvent) -> None:
        if event.event_type == MouseEventType.SCROLL_UP:
            self.session_menu_index = max(0, self.session_menu_index - 1)
        elif event.event_type == MouseEventType.SCROLL_DOWN:
            self.session_menu_index = min(2, self.session_menu_index + 1)
        elif event.event_type == MouseEventType.MOUSE_DOWN and event.button == MouseButton.LEFT:
            index = event.position.y
            if 0 <= index <= 2:
                self.session_menu_index = index
                self._session_menu_activate()
                return
        self._invalidate()

    def _focus_sessions(self) -> None:
        if len(self.embedded_sessions) <= 1:
            return
        self.focus = "sessions"
        self.session_search_query = ""
        with __import__("contextlib").suppress(Exception):
            get_app().layout.focus(self.session_control)
        self._invalidate()

    def _session_search(self, value: str) -> None:
        self.session_search_query = value
        query = value.casefold().strip()
        if query and self.embedded_sessions:
            ranked = sorted(
                range(len(self.embedded_sessions)),
                key=lambda index: (
                    -difflib.SequenceMatcher(
                        None,
                        query,
                        str(getattr(self.embedded_sessions[index], "asset_label", "SSH session")).casefold(),
                    ).ratio(),
                    index,
                ),
            )
            self.active_session_index = ranked[0]
            self.embedded_session = self.embedded_sessions[self.active_session_index]
        self._invalidate()

    def _leave_session_search(self) -> None:
        self.session_search_query = ""
        self.focus = "terminal"
        self._focus_terminal()
        self._invalidate()

    def _move_session(self, delta: int) -> None:
        if not self.embedded_sessions:
            return
        self._switch_session_to((self.active_session_index + delta) % len(self.embedded_sessions))

    def _switch_session_to(self, index: int) -> None:
        if not self.embedded_sessions:
            return
        self.active_session_index = max(0, min(index, len(self.embedded_sessions) - 1))
        self.embedded_session = self.embedded_sessions[self.active_session_index]
        self.view = "terminal"
        self._focus_terminal()
        self.status = f"Session {self.active_session_index + 1}/{len(self.embedded_sessions)}"
        self._invalidate()

    def _terminal_selection_bounds(self) -> tuple[tuple[int, int], tuple[int, int]] | None:
        if self.terminal_selection_anchor is None or self.terminal_selection_end is None:
            return None
        first, second = self.terminal_selection_anchor, self.terminal_selection_end
        return (first, second) if first <= second else (second, first)

    def _terminal_selection_text(self) -> str:
        bounds = self._terminal_selection_bounds()
        session = self.embedded_session
        if bounds is None or session is None:
            return ""
        (start_y, start_x), (end_y, end_x) = bounds
        lines = session.display_snapshot()
        if not lines:
            return ""
        start_y = max(0, min(start_y, len(lines) - 1))
        end_y = max(0, min(end_y, len(lines) - 1))
        selected: list[str] = []
        for row in range(start_y, end_y + 1):
            line = lines[row]
            left = start_x if row == start_y else 0
            right = end_x if row == end_y else len(line)
            selected.append(line[max(0, left) : max(left, right)].rstrip())
        return "\n".join(selected)

    def _terminal_is_selected(self, row: int, column: int) -> bool:
        bounds = self._terminal_selection_bounds()
        if bounds is None:
            return False
        (start_y, start_x), (end_y, end_x) = bounds
        return (start_y < row < end_y) or (
            start_y == end_y == row and start_x <= column < end_x
        ) or (row == start_y and row != end_y and column >= start_x) or (
            row == end_y and row != start_y and column < end_x
        )

    def _terminal_render_line(
        self,
        row: int,
        line: str,
        attrs: tuple[tuple[str, tuple[Any, ...]], ...] = (),
        cursor: tuple[int, int, bool] = (-1, -1, True),
        style_cache: dict[tuple[Any, ...], str] | None = None,
    ) -> list[tuple[str, str]]:
        fragments: list[tuple[str, str]] = []
        if not line:
            return [("class:terminal", "")]
        cursor_x, cursor_y, cursor_hidden = cursor
        cursor_visible = self._cursor_blink_visible()
        begin = 0
        selected = self._terminal_is_selected(row, 0)
        cursor = cursor_visible and not cursor_hidden and row == cursor_y and cursor_x == 0
        for column in range(1, len(line) + 1):
            current = column < len(line) and self._terminal_is_selected(row, column)
            current_cursor = cursor_visible and not cursor_hidden and row == cursor_y and column == cursor_x
            char_style = self._terminal_char_style(attrs, begin, style_cache)
            next_style = self._terminal_char_style(attrs, column, style_cache)
            if current != selected or current_cursor != cursor or char_style != next_style:
                style = self._terminal_display_style(char_style, selected, cursor)
                fragments.append((style, line[begin:column]))
                begin = column
                selected = current
                cursor = current_cursor
        style = self._terminal_display_style(
            self._terminal_char_style(attrs, begin, style_cache), selected, cursor
        )
        fragments.append((style, line[begin:]))
        return fragments

    @staticmethod
    def _terminal_char_style(
        attrs: tuple[tuple[str, tuple[Any, ...]], ...],
        column: int,
        style_cache: dict[tuple[Any, ...], str] | None = None,
    ) -> str:
        if column >= len(attrs):
            return "class:terminal"
        _, values = attrs[column]
        if style_cache is not None and values in style_cache:
            return style_cache[values]
        fg, bg, bold, italics, underscore, strikethrough, reverse, blink = values
        if reverse:
            fg, bg = bg, fg

        def color(value: str) -> str | None:
            if value == "default":
                return None
            if value in ANSI_COLOR_HEX:
                return ANSI_COLOR_HEX[value]
            return f"#{value}" if len(value) == 6 and all(char in "0123456789abcdefABCDEF" for char in value) else value

        parts = ["class:terminal"]
        if color(fg):
            parts.append(f"fg:{color(fg)}")
        if color(bg):
            parts.append(f"bg:{color(bg)}")
        if bold:
            parts.append("bold")
        if italics:
            parts.append("italic")
        if underscore:
            parts.append("underline")
        if strikethrough:
            parts.append("strike")
        if blink:
            parts.append("blink")
        style = " ".join(parts)
        if style_cache is not None:
            style_cache[values] = style
        return style

    @staticmethod
    def _terminal_display_style(base: str, selected: bool, cursor: bool) -> str:
        if cursor:
            return "class:terminal.cursor"
        if selected:
            return f"{base} bg:#264f78"
        return base

    @staticmethod
    def _cursor_blink_visible() -> bool:
        return int(time.monotonic() * 2) % 2 == 0

    def _terminal_mouse_handler(self, event: MouseEvent) -> None:
        if event.event_type == MouseEventType.MOUSE_DOWN and event.button == MouseButton.RIGHT:
            self.context_menu_open = True
            self.context_menu_index = 0
            try:
                size = get_app().output.get_size()
                self.context_menu_float.left = min(max(0, event.position.x + 1), max(0, size.columns - 29))
                self.context_menu_float.top = min(max(2, event.position.y + 2), max(2, size.rows - 6))
            except Exception:
                self.context_menu_float.left = max(0, event.position.x + 1)
                self.context_menu_float.top = max(2, event.position.y + 2)
            self._invalidate()
            return
        if self.context_menu_open and event.event_type == MouseEventType.MOUSE_DOWN:
            self._close_context_menu()
        if event.event_type == MouseEventType.SCROLL_UP:
            if self.embedded_session is not None:
                self.embedded_session.scroll_history(-1)
            return
        if event.event_type == MouseEventType.SCROLL_DOWN:
            if self.embedded_session is not None:
                self.embedded_session.scroll_history(1)
            return
        point = (max(0, event.position.y), max(0, event.position.x))
        if event.event_type == MouseEventType.MOUSE_DOWN and event.button == MouseButton.LEFT:
            self.terminal_selection_anchor = point
            self.terminal_selection_end = point
            self.terminal_selecting = True
            self._focus_terminal()
            self._invalidate()
        elif event.event_type == MouseEventType.MOUSE_MOVE and self.terminal_selecting:
            self.terminal_selection_end = point
            self._invalidate()
        elif event.event_type == MouseEventType.MOUSE_UP and self.terminal_selecting:
            self.terminal_selection_end = point
            self.terminal_selecting = False
            self._copy_terminal_selection()

    def _context_menu_text(self) -> FormattedText:
        items = ("Copy selection", "Paste clipboard", "Close menu")
        rows: FormattedText = []
        for index, item in enumerate(items):
            style = "class:item.selected" if index == self.context_menu_index else "class:item"
            rows.append((style, f"  {item}\n"))
        return rows

    def _close_context_menu(self) -> None:
        self.context_menu_open = False
        self._focus_terminal()
        self._invalidate()

    def _context_menu_activate(self) -> None:
        if self.context_menu_index == 0:
            self._copy_terminal_selection()
        elif self.context_menu_index == 1:
            self._paste_terminal_clipboard()
        self._close_context_menu()

    def _context_menu_mouse_handler(self, event: MouseEvent) -> None:
        if event.event_type == MouseEventType.SCROLL_UP:
            self.context_menu_index = max(0, self.context_menu_index - 1)
        elif event.event_type == MouseEventType.SCROLL_DOWN:
            self.context_menu_index = min(2, self.context_menu_index + 1)
        elif event.event_type == MouseEventType.MOUSE_DOWN and event.button == MouseButton.LEFT:
            index = event.position.y
            if 0 <= index <= 2:
                self.context_menu_index = index
                self._context_menu_activate()
                return
        self._invalidate()

    def _copy_terminal_selection(self) -> None:
        text = self._terminal_selection_text()
        if not text:
            return
        with __import__("contextlib").suppress(Exception):
            get_app().clipboard.set_data(ClipboardData(text))
            self._clipboard_owned = True
        copied = False
        encoded_text = base64.b64encode(text.encode("utf-8")).decode("ascii")
        powershell_script = (
            "$b=[Convert]::FromBase64String('" + encoded_text + "');"
            "Set-Clipboard -Value ([Text.Encoding]::UTF8.GetString($b))"
        )
        for command in (
            ("powershell.exe", "-NoProfile", "-Command", powershell_script),
            ("pwsh.exe", "-NoProfile", "-Command", powershell_script),
            ("clip.exe",),
            ("/mnt/c/Windows/System32/clip.exe",),
            ("wl-copy",),
            ("pbcopy",),
        ):
            try:
                subprocess.run(command, input=text.encode("utf-8"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=0.35)
            except (FileNotFoundError, OSError, subprocess.SubprocessError):
                continue
            copied = True
            break
        if not copied:
            for command in (("xclip", "-selection", "clipboard"), ("xsel", "--clipboard", "--input")):
                try:
                    process = subprocess.Popen(
                        command,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True,
                    )
                    if process.stdin is not None:
                        process.stdin.write(text.encode("utf-8"))
                        process.stdin.close()
                    copied = True
                    break
                except (FileNotFoundError, OSError, subprocess.SubprocessError):
                    continue
        # OSC 52 is understood by modern terminal emulators and works even
        # when no clipboard helper is installed inside the host environment.
        with __import__("contextlib").suppress(Exception):
            get_app().output.write_raw(f"\033]52;c;{encoded_text}\a")
            get_app().output.flush()
            copied = True
        self.status = "Selected text copied to system clipboard" if copied else "Unable to access system clipboard"
        self._invalidate()

    def _paste_terminal_clipboard(self) -> None:
        if self.embedded_session is None or self.picker_open:
            return
        # In WSL, prompt-toolkit's in-memory clipboard is not necessarily the
        # same clipboard as Windows. Read the host clipboard first, then use
        # the TUI clipboard as a fallback for native Linux/macOS terminals.
        text = ""
        if self._clipboard_owned:
            with __import__("contextlib").suppress(Exception):
                text = get_app().clipboard.get_data().text
        if not text:
            text = self._read_system_clipboard()
        if text:
            self._send_terminal(text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8"))

    @staticmethod
    def _read_system_clipboard() -> str:
        """Read clipboard text across WSL, Linux, and macOS hosts."""
        powershell_script = (
            "$OutputEncoding=[Console]::OutputEncoding=[Text.UTF8Encoding]::new();"
            "Get-Clipboard -Raw"
        )
        for command in (
            ("powershell.exe", "-NoProfile", "-Command", powershell_script),
            ("pwsh.exe", "-NoProfile", "-Command", powershell_script),
            ("wl-paste", "--no-newline"),
            ("xclip", "-selection", "clipboard", "-o"),
            ("xsel", "--clipboard", "--output"),
            ("pbpaste",),
        ):
            try:
                result = subprocess.run(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    check=True,
                    timeout=0.35,
                )
            except (FileNotFoundError, OSError, subprocess.SubprocessError):
                continue
            raw = result.stdout
            text = raw.decode("utf-8", errors="replace")
            if "\ufffd" in text or "\x00" in text:
                text = raw.decode("utf-16", errors="replace")
            if "\ufffd" in text:
                text = raw.decode("gb18030", errors="replace")
            return text
        return ""

    def _sync_terminal_size(self, sessions: list[EmbeddedPtySession]) -> None:
        """Keep pyte and the child SSH PTY aligned with the visible pane."""
        dimensions = self._terminal_dimensions()
        if dimensions is None:
            return
        columns, rows = dimensions
        if self.split_mode and len(sessions) >= 2:
            columns = max(20, (columns - 3) // 2)
            for session in sessions[:2]:
                session.resize(columns, rows)
        else:
            for session in self.embedded_sessions:
                session.resize(columns, rows)

    def _terminal_dimensions(self) -> tuple[int, int] | None:
        try:
            size = get_app().output.get_size()
        except Exception:
            return None
        return max(20, size.columns), max(5, size.rows - 3)

    def _visible_sessions(self) -> list[EmbeddedPtySession]:
        if not self.embedded_sessions:
            return []
        if not self.split_mode:
            return [self.embedded_sessions[self.active_session_index]]
        start = min(self.active_session_index, max(0, len(self.embedded_sessions) - 2))
        return self.embedded_sessions[start : start + 2]

    def _picker_header(self) -> FormattedText:
        title = " UPLOAD FILES " if self.picker_kind == "upload" else " DOWNLOAD TO "
        return FormattedText([("class:frame.focused", title)])

    def _picker_footer(self) -> FormattedText:
        if self.picker_kind == "upload":
            hint = "Space select  Enter upload  Up/Down navigate  Esc cancel"
        else:
            hint = "Enter choose directory  Up/Down navigate  Esc cancel"
        return FormattedText([("class:item.muted", f"  {hint}")])

    def _picker_text(self) -> FormattedText:
        rows: FormattedText = []
        if not self.picker_entries:
            return FormattedText([("class:item.muted", "  Directory is empty or unavailable\n")])
        for index, entry in enumerate(self.picker_entries):
            selected = entry in self.picker_selected
            marker = "[x]" if selected else "[ ]"
            if entry.is_dir():
                marker = "[+]" if not selected else "[x]"
                label = f"{entry.name}/"
            else:
                label = entry.name
            prefix = "> " if index == self.picker_index else "  "
            style = "class:item.selected" if index == self.picker_index else "class:item"
            rows.append((style, f"{prefix}{marker} {label}\n"))
        return rows

    def _picker_cursor_position(self) -> Point:
        return Point(x=0, y=self.picker_index)

    def _picker_mouse_handler(self, event: MouseEvent) -> None:
        if event.event_type == MouseEventType.SCROLL_UP:
            self._picker_move(-1)
            return
        if event.event_type == MouseEventType.SCROLL_DOWN:
            self._picker_move(1)
            return
        if event.event_type != MouseEventType.MOUSE_DOWN or event.button != MouseButton.LEFT:
            return
        index = event.position.y
        if 0 <= index < len(self.picker_entries):
            self.picker_list_focused = True
            with __import__("contextlib").suppress(Exception):
                get_app().layout.focus(self.picker_list_control)
            self.picker_index = index
            if self.picker_kind == "upload":
                self._picker_toggle()
            else:
                entry = self.picker_entries[index]
                if entry.is_dir():
                    self.picker_path = entry
                    self.picker_path_input.buffer.text = str(entry)
                    self.picker_index = 0
                    self._refresh_picker_entries()
                    self._invalidate()

    def _refresh_picker_entries(self) -> None:
        try:
            entries = list(self.picker_path.iterdir())
        except OSError:
            self.picker_entries = []
            self.picker_index = 0
            return
        self.picker_entries = sorted(entries, key=lambda item: (not item.is_dir(), item.name.casefold()))
        self.picker_index = max(0, min(self.picker_index, len(self.picker_entries) - 1)) if self.picker_entries else 0
        self.picker_selected.intersection_update(self.picker_entries)

    def _picker_path_changed(self, buffer: Any) -> None:
        value = buffer.text.strip()
        if not value:
            return
        candidate = Path(value).expanduser()
        if candidate.is_dir() and candidate != self.picker_path:
            self.picker_path = candidate
            self.picker_index = 0
            self.picker_selected.clear()
            self._refresh_picker_entries()
            self._invalidate()

    def _open_picker(self, direction: str, session: EmbeddedPtySession | None = None) -> None:
        session = session or self.embedded_session
        if session is None or self.picker_open:
            return
        self.picker_kind = direction
        self.picker_session = session
        self.picker_open = True
        self.picker_path = Path.cwd()
        self.picker_index = 0
        self.picker_selected.clear()
        self.picker_list_focused = False
        self.picker_path_input.buffer.text = str(self.picker_path)
        self._refresh_picker_entries()
        with __import__("contextlib").suppress(Exception):
            get_app().layout.focus(self.picker_path_input.control)
        self._invalidate()

    def _close_picker(self, *, cancel_transfer: bool = False) -> None:
        if cancel_transfer and self.picker_session is not None:
            self.picker_session.cancel_transfer()
        self.picker_open = False
        self.picker_session = None
        with __import__("contextlib").suppress(Exception):
            get_app().layout.focus(self.terminal_control)
        self._invalidate()

    def _picker_move(self, delta: int) -> None:
        if self.picker_entries:
            self.picker_list_focused = True
            with __import__("contextlib").suppress(Exception):
                get_app().layout.focus(self.picker_list_control)
            self.picker_index = max(0, min(self.picker_index + delta, len(self.picker_entries) - 1))
            self._invalidate()

    def _picker_toggle(self) -> None:
        if not self.picker_entries:
            return
        entry = self.picker_entries[self.picker_index]
        if entry.is_file() and self.picker_kind == "upload":
            if entry in self.picker_selected:
                self.picker_selected.remove(entry)
            else:
                self.picker_selected.add(entry)
            self._invalidate()

    def _picker_confirm(self) -> None:
        session = self.picker_session
        if session is None:
            return
        if self.picker_kind == "download":
            if self.picker_path.is_dir():
                session.start_transfer(["rz", "-be"], cwd=str(self.picker_path))
                self._close_picker()
            return
        if not self.picker_list_focused:
            if self.picker_path.is_dir():
                self._refresh_picker_entries()
                self.picker_list_focused = True
                with __import__("contextlib").suppress(Exception):
                    get_app().layout.focus(self.picker_list_control)
                self._invalidate()
            return
        if not self.picker_entries:
            return
        current = self.picker_entries[self.picker_index]
        if current.is_dir() and not self.picker_selected:
            self.picker_path = current
            self.picker_path_input.buffer.text = str(current)
            self.picker_index = 0
            self._refresh_picker_entries()
            self._invalidate()
            return
        if self.picker_kind == "upload":
            files = list(self.picker_selected)
            if current.is_file() and current not in self.picker_selected:
                files.append(current)
            if not files:
                return
            command = ["sz", "-be", "--", *(str(path) for path in files)]
            session.start_transfer(command)
        self._close_picker()

    def _focus_navigation(self) -> None:
        target = self.history_control if self.focus == "history" else self.asset_control
        with __import__("contextlib").suppress(Exception):
            get_app().layout.focus(target)

    def _asset_cursor_position(self) -> Point:
        index = self.user_index if self.view == "users" else self.asset_index
        return Point(x=0, y=index + 1)

    def _history_cursor_position(self) -> Point:
        return Point(x=0, y=self.history_index + 1)

    def _asset_vertical_scroll(self, window: Window) -> int:
        render_info = window.render_info
        height = render_info.window_height if render_info else 0
        if height <= 0:
            return 0
        return max(0, self._asset_cursor_position().y - height + 1)

    def _asset_mouse_handler(self, event: MouseEvent) -> None:
        if event.event_type == MouseEventType.SCROLL_UP:
            self.focus = "assets"
            self._focus_navigation()
            self._move(-1)
            return
        if event.event_type == MouseEventType.SCROLL_DOWN:
            self.focus = "assets"
            self._focus_navigation()
            self._move(1)
            return
        if event.event_type != MouseEventType.MOUSE_DOWN or event.button != MouseButton.LEFT:
            return
        self.focus = "assets"
        self._focus_navigation()
        index = event.position.y - 1
        if self.view == "users":
            if 0 <= index < len(self.users):
                self.user_index = index
        elif 0 <= index < len(self.filtered_assets):
            self.asset_index = index
            if MouseModifier.SHIFT in event.modifiers or 2 <= event.position.x <= 4:
                self._last_asset_click = None
                self._toggle_asset_selection()
                return
            now = time.monotonic()
            previous = self._last_asset_click
            self._last_asset_click = (index, now)
            if previous is not None and previous[0] == index and now - previous[1] <= 0.45:
                # prompt-toolkit exposes mouse presses rather than a
                # platform-specific click count.  A short second press on
                # the same host is therefore treated as a double click.
                self._last_asset_click = None
                self._select_asset()
        self._invalidate()

    def _history_mouse_handler(self, event: MouseEvent) -> None:
        if event.event_type == MouseEventType.SCROLL_UP:
            self.focus = "history"
            self._focus_navigation()
            self._move(-1)
            return
        if event.event_type == MouseEventType.SCROLL_DOWN:
            self.focus = "history"
            self._focus_navigation()
            self._move(1)
            return
        if event.event_type != MouseEventType.MOUSE_DOWN or event.button != MouseButton.LEFT:
            return
        self.focus = "history"
        self._focus_navigation()
        index = event.position.y - 1
        if 0 <= index < len(self.history_entries):
            self.history_index = index
        self._invalidate()

    def _asset_frame_text(self) -> FormattedText:
        title = " ASSET TREE " if self.view != "users" else " SYSTEM USERS "
        style = "class:frame.focused" if self.focus == "assets" else "class:frame.title"
        rows = [(style, title + "\n")]
        if self.view == "users":
            for index, user in enumerate(self.users):
                selected = index == self.user_index
                rows.append(("class:item.selected" if selected else "class:item", self._user_row(user, selected) + "\n"))
            return FormattedText(rows)
        assets = self.filtered_assets
        if not assets:
            rows.append(("class:item.muted", "  No matching assets\n"))
        for index, asset in enumerate(assets):
            selected = index == self.asset_index
            connected = any(
                session.alive and getattr(session, "asset_id", "") == str(asset.get("id") or "")
                for session in self.embedded_sessions
            )
            style = "class:item.selected" if selected else "class:item.accent" if connected else "class:item"
            rows.append((style, self._asset_row(asset, selected) + "\n"))
        return FormattedText(rows)

    def _detail_frame_text(self) -> FormattedText:
        style = "class:frame.focused" if self.focus == "assets" else "class:frame.title"
        return FormattedText([(style, " DETAILS\n"), *self._detail_text()])

    def _asset_row(self, asset: dict[str, Any], selected: bool) -> str:
        marker = ">" if selected else " "
        asset_id = str(asset.get("id") or "")
        selection = "[x]" if asset_id in self.selected_asset_ids else "[ ]"
        connected = any(
            session.alive and getattr(session, "asset_id", "") == asset_id
            for session in self.embedded_sessions
        )
        state = "●" if connected else " "
        data = asset_data(asset)
        try:
            compact = get_app().output.get_size().columns < 110
        except Exception:
            compact = False
        if compact:
            return f"{marker} {selection} {state} {asset_ip(asset)}  {asset_hostname(asset)}"
        platform = str(data.get("platform") or "?")
        protocol = ",".join(str(item) for item in data.get("protocols") or []) or "ssh/?"
        return f"{marker} {selection} {state} {asset_ip(asset):<16} {asset_hostname(asset)[:34]:<34} {platform:<7} {protocol}"

    def _user_row(self, user: dict[str, Any], selected: bool) -> str:
        marker = ">" if selected else " "
        name = str(user.get("name") or user.get("username") or "-")
        login = str(user.get("username") or "-")
        mode = str(user.get("login_mode") or "-")
        return f"{marker} +-- {name[:34]:<34} login={login:<12} mode={mode}"

    def _detail_text(self) -> FormattedText:
        if self.view == "users":
            asset = self._selected_asset()
            if not asset:
                return FormattedText([("class:item.muted", "No asset selected")])
            return FormattedText(
                [
                    ("class:detail.label", "Asset  "), ("class:detail.value", f"{asset_ip(asset)} {asset_hostname(asset)}\n"),
                    ("class:detail.label", "Users  "), ("class:detail.value", "Select a system user, then press Enter\n"),
                    ("class:item.muted", "Esc returns to asset tree"),
                ]
            )
        asset = self._selected_asset()
        if not asset:
            return FormattedText([("class:item.muted", "Select an asset to inspect details")])
        data = asset_data(asset)
        return FormattedText(
            [
                ("class:detail.label", "Address "), ("class:detail.value", f"{asset_ip(asset)}\n"),
                ("class:detail.label", "Host    "), ("class:detail.value", f"{asset_hostname(asset)}\n"),
                ("class:detail.label", "Platform"), ("class:detail.value", f" {data.get('platform') or '-'}\n"),
                ("class:detail.label", "Proto   "), ("class:detail.value", f" {', '.join(data.get('protocols') or []) or '-'}\n"),
                ("class:item.muted", "Enter: choose system user"),
            ]
        )

    def _right_text(self) -> FormattedText:
        entries = self.history_entries
        style = "class:frame.focused" if self.focus == "history" else "class:frame.title"
        rows = [(style, " RECENT SESSIONS  [heat, newest] \n")]
        if not entries:
            rows.append(("class:item.muted", "  No local sessions yet\n"))
        for index, entry in enumerate(entries):
            selected = self.focus == "history" and index == self.history_index
            rows.append(("class:item.selected" if selected else "class:item", self._history_row(entry, selected) + "\n"))
        rows.append(("class:item.muted", "\n  History contains labels and timestamps only."))
        return FormattedText(rows)

    def _history_row(self, entry: dict[str, Any], selected: bool) -> str:
        marker = ">" if selected else " "
        timestamp = time.strftime("%m-%d %H:%M", time.localtime(int(entry.get("last_used") or 0)))
        return f"{marker} {entry.get('ip', '-'): <16} {str(entry.get('hostname', '-'))[:23]:<23} x{entry.get('count', 0):<3} {timestamp}"

    def _footer_text(self) -> FormattedText:
        if self.last_error:
            return FormattedText([("class:error", f"  ERROR: {self.last_error}")])
        if self.status:
            status = f"  {self.status}  |  "
        else:
            status = "  "
        if self.view == "terminal":
            if self.picker_open:
                hint = "click file/dir  Space select  Enter transfer  Esc cancel"
                return FormattedText([("class:item.muted", status + hint)])
            if self.context_menu_open:
                hint = "click action  Up/Down navigate  Enter select  Esc close"
                return FormattedText([("class:item.muted", status + hint)])
            if self.terminate_confirm_open:
                hint = "click Yes/Cancel  Enter confirm  Esc cancel"
                return FormattedText([("class:item.warn", status + hint)])
            if self.session_menu_open:
                hint = "click action  Up/Down navigate  Enter select  Esc close"
                return FormattedText([("class:item.muted", status + hint)])
            if self.terminal_command_prefix:
                return FormattedText(
                    [
                        ("class:item.muted", status),
                        ("class:footer.key", "r"), ("class:item.muted", " refresh  "),
                        ("class:footer.key", "q"), ("class:item.muted", " quit  "),
                        ("class:footer.key", "x"), ("class:item.muted", " terminate  "),
                        ("class:footer.key", "n"), ("class:item.muted", " resources  "),
                        ("class:footer.key", "u/d"), ("class:item.muted", " transfer  "),
                        ("class:footer.key", "Esc"), ("class:item.muted", " cancel"),
                    ]
                )
            return FormattedText(
                [
                    ("class:item.muted", status),
                    ("class:footer.key", "Ctrl-N"), ("class:item.muted", " new session  "),
                    ("class:footer.key", "F2"), ("class:item.muted", " next  "),
                    ("class:footer.key", "F3"), ("class:item.muted", " split  "),
                    ("class:footer.key", "F4/Ctrl-N"), ("class:item.muted", " resources  "),
                    ("class:footer.key", "F6/Tab"), ("class:item.muted", " focus sessions  "),
                    ("class:footer.key", "Ctrl-Insert"), ("class:item.muted", " copy selection  "),
                    ("class:footer.key", "Shift-Insert"), ("class:item.muted", " paste  "),
                    ("class:footer.key", "PgUp/PgDn"), ("class:item.muted", " scrollback  "),
                    ("class:footer.key", "Ctrl-X"), ("class:item.muted", " r refresh  q quit  x terminate  n resources  u/d transfer"),
                ]
            )
        if self.filter_mode:
            return FormattedText(
                [
                    ("class:item.muted", status),
                    ("class:footer.key", "Up/Down"), ("class:item.muted", " navigate  "),
                    ("class:footer.key", "Enter"), ("class:item.muted", " connect  "),
                    ("class:footer.key", "Esc"), ("class:item.muted", " close filter  "),
                    ("class:footer.key", "Ctrl-Space"), ("class:item.muted", " select  "),
                    ("class:footer.key", "Ctrl-Q/C"), ("class:item.muted", " quit"),
                ]
            )
        if self.view == "users":
            return FormattedText(
                [
                    ("class:item.muted", status),
                    ("class:footer.key", "Enter"), ("class:item.muted", " connect  "),
                    ("class:footer.key", "Esc"), ("class:item.muted", " assets  "),
                    ("class:footer.key", "Ctrl-N"), ("class:item.muted", " sessions  "),
                    ("class:footer.key", "Ctrl-Q/C"), ("class:item.muted", " quit"),
                ]
            )
        return FormattedText(
            [
                ("class:item.muted", status),
                ("class:footer.key", "Tab"), ("class:item.muted", " focus  "),
                ("class:footer.key", "Enter"), ("class:item.muted", " connect  "),
                ("class:footer.key", "Space"), ("class:item.muted", " select  "),
                ("class:footer.key", "Ctrl-A"), ("class:item.muted", " all/none  "),
                ("class:footer.key", "type"), ("class:item.muted", " search  "),
                ("class:footer.key", "Ctrl-Q/C"), ("class:item.muted", " quit"),
            ]
        )

    def _selected_asset(self) -> dict[str, Any] | None:
        assets = self.filtered_assets
        if not assets:
            return None
        self.asset_index = max(0, min(self.asset_index, len(assets) - 1))
        return assets[self.asset_index]

    def _toggle_asset_selection(self) -> None:
        asset = self._selected_asset()
        if not asset:
            return
        asset_id = str(asset.get("id") or "")
        if not asset_id:
            return
        if asset_id in self.selected_asset_ids:
            self.selected_asset_ids.remove(asset_id)
        else:
            self.selected_asset_ids.add(asset_id)
        self.status = f"Selected assets: {len(self.selected_asset_ids)}"
        self._invalidate()

    def _toggle_all_asset_selection(self) -> None:
        visible_ids = {
            str(asset.get("id") or "")
            for asset in self.filtered_assets
            if asset.get("id")
        }
        if not visible_ids:
            return
        if visible_ids <= self.selected_asset_ids:
            self.selected_asset_ids.difference_update(visible_ids)
        else:
            self.selected_asset_ids.update(visible_ids)
        self.status = f"Selected assets: {len(self.selected_asset_ids)}"
        self._invalidate()

    def _toggle_terminal_navigation(self) -> None:
        if self.view == "terminal":
            self._open_new_session()
        elif self.embedded_sessions:
            self.view = "terminal"
            self.users = []
            self.user_index = 0
            self.embedded_session = self.embedded_sessions[self.active_session_index]
            self.status = "Returned to active SSH session"
            self._focus_terminal()
            self._invalidate()

    def _batch_connect_selected(self) -> None:
        if not self.selected_asset_ids:
            self._connect_current()
            return
        if not getattr(self.args, "pty_mode", False):
            self.status = "Batch open requires embedded PTY mode"
            self._invalidate()
            return
        assets = [asset for asset in self.assets if str(asset.get("id") or "") in self.selected_asset_ids]
        if not assets:
            return
        self.selected_asset_ids.clear()
        self.batch_connecting = True
        self._batch_pending = len(assets)
        self._batch_opened = 0
        self._batch_skipped = 0
        self._batch_anchor_index = len(self.embedded_sessions)
        self.view = "terminal"
        self._focus_terminal()
        self.status = f"Opening {len(assets)} SSH sessions"
        self._invalidate()
        for asset in assets:
            self._request_batch_connection(asset)

    def _request_batch_connection(self, asset: dict[str, Any]) -> None:
        asset_id = str(asset.get("id") or "")

        def request() -> None:
            users: list[dict[str, Any]] = []
            error = ""
            try:
                users = self.client.system_users(asset_id)
                if len(users) != 1:
                    error = "requires exactly one system user"
                else:
                    user = users[0]
                    token = get_token_for_resolved(self.store, self.client, asset, user, quiet=True)
                    self._from_session_thread(
                        lambda: self._batch_connection_ready(asset, user, token, "")
                    )
                    return
            except JumpCliError as exc:
                error = str(exc)
            self._from_session_thread(
                lambda: self._batch_connection_ready(asset, {}, None, error)
            )

        threading.Thread(target=request, name="jumpcli-batch-ssh-auth", daemon=True).start()

    def _batch_connection_ready(
        self,
        asset: dict[str, Any],
        user: dict[str, Any],
        token: str | None,
        error: str,
    ) -> None:
        if error or token is None or not user:
            self._batch_skipped += 1
        else:
            key = (str(asset.get("id") or ""), str(user.get("id") or ""))
            duplicate = any(
                existing.alive
                and (getattr(existing, "asset_id", ""), getattr(existing, "system_user_id", "")) == key
                for existing in self.embedded_sessions
            )
            if duplicate:
                self._batch_skipped += 1
            else:
                self._start_embedded_ssh(asset, user, token)
                self._batch_opened += 1
        self._batch_pending -= 1
        if self._batch_pending > 0:
            self.status = f"Opening SSH sessions ({self._batch_pending} remaining)"
            self._invalidate()
            return
        self.batch_connecting = False
        if self._batch_opened and self._batch_anchor_index < len(self.embedded_sessions):
            self.active_session_index = self._batch_anchor_index
            self.embedded_session = self.embedded_sessions[self._batch_anchor_index]
            self.view = "terminal"
            self._focus_terminal()
        elif not self.embedded_sessions:
            self.view = "assets"
            self.focus = "assets"
            self._focus_navigation()
        suffix = f"; skipped {self._batch_skipped}" if self._batch_skipped else ""
        self.status = f"Opened {self._batch_opened} session(s){suffix}"
        self._invalidate()

    def _invalidate(self) -> None:
        self.last_error = ""
        with __import__("contextlib").suppress(Exception):
            get_app().invalidate()

    def _move(self, delta: int) -> None:
        if self.view == "users":
            if self.users:
                self.user_index = max(0, min(self.user_index + delta, len(self.users) - 1))
        elif self.focus == "history":
            if self.history_entries:
                self.history_index = max(0, min(self.history_index + delta, len(self.history_entries) - 1))
        else:
            if self.filtered_assets:
                self.asset_index = max(0, min(self.asset_index + delta, len(self.filtered_assets) - 1))
        self._invalidate()

    def _toggle_focus(self) -> None:
        if self.view == "users":
            return
        self.focus = "history" if self.focus == "assets" else "assets"
        self._focus_navigation()
        self._invalidate()

    def _reload(self) -> None:
        try:
            self.assets = [item for item in self.client.assets_tree("") if is_asset(item)]
            self.asset_index = 0
            self.status = f"Loaded {len(self.assets)} assets"
        except JumpCliError as exc:
            self.last_error = str(exc)
        self._invalidate()

    def _begin_search(self) -> None:
        if self.view == "assets":
            self.focus = "assets"
            self.filter_mode = True
            self.search_input.buffer.text = self.query
            self.search_input.buffer.cursor_position = len(self.query)
            with __import__("contextlib").suppress(Exception):
                get_app().layout.focus(self.search_input.control)
            self._invalidate()

    def _start_search_with(self, value: str) -> None:
        """Enter filter mode with the cursor ready for the next character."""
        if self.view != "assets":
            return
        self.focus = "assets"
        self.filter_mode = True
        self.search_input.buffer.text = value
        self.search_input.buffer.cursor_position = len(value)
        with __import__("contextlib").suppress(Exception):
            get_app().layout.focus(self.search_input.control)
        self._invalidate()

    def _select_asset(self) -> None:
        asset = self._selected_asset()
        if not asset:
            return
        asset_id = str(asset.get("id") or "")
        if not asset_id or asset_id in self._pending_user_assets:
            return
        self._pending_user_assets.add(asset_id)
        self.status = f"Loading system users: {asset_ip(asset)}"
        self.last_error = ""
        self._invalidate()

        def request() -> None:
            users: list[dict[str, Any]] = []
            error = ""
            try:
                users = self.client.system_users(asset_id)
            except JumpCliError as exc:
                error = str(exc)
            self._from_session_thread(lambda: self._asset_users_loaded(asset, users, error))

        threading.Thread(target=request, name="jumpcli-system-users", daemon=True).start()

    def _asset_users_loaded(
        self,
        asset: dict[str, Any],
        users: list[dict[str, Any]],
        error: str,
    ) -> None:
        asset_id = str(asset.get("id") or "")
        self._pending_user_assets.discard(asset_id)
        if error:
            self.last_error = error
            self.status = f"Unable to load users: {asset_ip(asset)}"
            self._invalidate()
            return
        if not users:
            self.last_error = "asset has no available system users"
            self.status = f"No system users: {asset_ip(asset)}"
            self._invalidate()
            return
        current = self._selected_asset()
        if self.view != "assets" or not current or str(current.get("id") or "") != asset_id:
            # The user moved on while the request was in flight. Keep the
            # result available only to the originating action; do not switch
            # the visible user pane underneath the new selection.
            return
        self.users = users
        if len(users) == 1 and users[0].get("username") == "ops":
            self.status = "Authenticating with ops"
            self._run_ssh(asset, users[0])
            return
        self.view = "users"
        self.user_index = 0
        self.status = f"{len(users)} system users available"
        self._invalidate()

    def _connect_history(self) -> None:
        entries = self.history_entries
        if not entries:
            return
        entry = entries[self.history_index]
        asset = {
            "id": entry.get("asset_id"),
            "name": entry.get("hostname"),
            "title": entry.get("ip"),
            "meta": {"data": {"ip": entry.get("ip"), "hostname": entry.get("hostname"), "platform": entry.get("platform")}},
        }
        user = {"id": entry.get("system_user_id"), "name": entry.get("system_user"), "username": entry.get("username")}
        self._run_ssh(asset, user)

    def _connect_current(self) -> None:
        if self.view == "terminal":
            return
        if self.view == "users":
            if self.users:
                self._run_ssh(self._selected_asset(), self.users[self.user_index])
        elif self.focus == "history":
            self._connect_history()
        else:
            self._select_asset()

    def _from_session_thread(self, callback: Any) -> None:
        loop = getattr(self.application, "loop", None)
        if loop is None:
            callback()
        else:
            loop.call_soon_threadsafe(callback)

    def _terminal_changed(self, session: EmbeddedPtySession) -> None:
        if getattr(session, "connection_status", "") == "connecting":
            session.connection_status = "connected"
        self._from_session_thread(self._invalidate)

    def _terminal_zmodem(self, session: EmbeddedPtySession, direction: str) -> None:
        self._from_session_thread(lambda: self._open_picker(direction, session))

    def _terminal_exited(self, session: EmbeddedPtySession, exit_code: int) -> None:
        def restore() -> None:
            was_terminal = self.view == "terminal"
            if self.picker_session is session:
                self.picker_open = False
                self.picker_session = None
            if session in self.embedded_sessions:
                index = self.embedded_sessions.index(session)
                self.embedded_sessions.remove(session)
                if index < self.active_session_index:
                    self.active_session_index -= 1
            if self.embedded_sessions:
                if len(self.embedded_sessions) < 2:
                    self.split_mode = False
                self.active_session_index = min(self.active_session_index, len(self.embedded_sessions) - 1)
                self.embedded_session = self.embedded_sessions[self.active_session_index]
                self.status = f"SSH session exited ({exit_code}); active sessions: {len(self.embedded_sessions)}"
                if was_terminal:
                    self.view = "terminal"
                    self._focus_terminal()
                self._invalidate()
                return
            if self.batch_connecting and self._batch_pending > 0:
                self.embedded_session = None
                self.status = f"Waiting for batch connections ({self._batch_pending} remaining)"
                self._invalidate()
                return
            self.embedded_session = None
            self.picker_open = False
            self.view = "assets"
            self.users = []
            self.user_index = 0
            self.focus = "assets"
            self.status = f"SSH session exited ({exit_code})"
            self._focus_navigation()
            self._invalidate()

        self._from_session_thread(restore)

    def _send_terminal(self, data: bytes) -> None:
        if self.embedded_session is not None and not self.picker_open:
            self.embedded_session.send(data)

    def _open_new_session(self) -> None:
        if self.view != "terminal":
            return
        self.view = "assets"
        self.users = []
        self.user_index = 0
        self.focus = "assets"
        self.terminal_selection_anchor = None
        self.terminal_selection_end = None
        self.status = f"Select an asset for a new SSH session ({len(self.embedded_sessions)} open)"
        self._focus_navigation()
        self._invalidate()

    def _switch_session(self, delta: int) -> None:
        if not self.embedded_sessions:
            return
        self._switch_session_to((self.active_session_index + delta) % len(self.embedded_sessions))

    def _toggle_split(self) -> None:
        if len(self.embedded_sessions) < 2:
            self.status = "Open another PTY session before enabling split view"
            self._invalidate()
            return
        self.split_mode = not self.split_mode
        self.status = "Split view enabled" if self.split_mode else "Split view disabled"
        self._invalidate()

    def _focus_terminal(self) -> None:
        self.focus = "terminal"
        with __import__("contextlib").suppress(Exception):
            get_app().layout.focus(self.terminal_control)

    def _stop_embedded_sessions(self) -> None:
        for session in list(self.embedded_sessions):
            session.stop()

    def _start_embedded_ssh(
        self,
        asset: dict[str, Any],
        user: dict[str, Any],
        token: str,
    ) -> None:
        """Create the PTY only after background authentication succeeds."""
        asset_id = str(asset.get("id") or "")
        user_id = str(user.get("id") or "")
        command = build_ssh_command(token, ssh_options=[], force_tty=True)
        self.history.record(asset, user)
        self.view = "terminal"
        self.terminal_selection_anchor = None
        self.terminal_selection_end = None
        self.status = f"Opening SSH: {asset_ip(asset)}"
        holder: dict[str, EmbeddedPtySession] = {}
        dimensions = self._terminal_dimensions() or (120, 40)
        session = EmbeddedPtySession(
            command,
            columns=dimensions[0],
            lines=dimensions[1],
            on_change=lambda: self._terminal_changed(holder["session"]),
            on_zmodem=lambda direction: self._terminal_zmodem(holder["session"], direction),
            on_exit=lambda code: self._terminal_exited(holder["session"], code),
        )
        holder["session"] = session
        session.asset_id = asset_id
        session.system_user_id = user_id
        session.asset_label = f"{asset_ip(asset)} {asset_hostname(asset)}"
        session.connection_status = "connecting"
        self.embedded_sessions.append(session)
        if not self.batch_connecting:
            self.active_session_index = len(self.embedded_sessions) - 1
            self.embedded_session = session
        session.start()
        if not self.batch_connecting:
            self._focus_terminal()
        self._invalidate()

    def _authenticated_for_ssh(
        self,
        asset: dict[str, Any],
        user: dict[str, Any],
        token: str | None,
        error: str,
        return_to_assets: bool,
    ) -> None:
        key = (str(asset.get("id") or ""), str(user.get("id") or ""))
        self._pending_connection_keys.discard(key)
        if error or token is None:
            self.last_error = error or "unable to obtain SSH token"
            self.status = f"Authentication failed: {asset_ip(asset)}"
            if return_to_assets:
                self.view = "assets"
                self.users = []
                self.user_index = 0
                self._focus_navigation()
            self._invalidate()
            return
        self._start_embedded_ssh(asset, user, token)

    def _run_ssh(self, asset: dict[str, Any] | None, user: dict[str, Any]) -> bool:
        if not asset:
            return False
        return_to_assets = self.view == "users"

        if getattr(self.args, "pty_mode", False):
            asset_id = str(asset.get("id") or "")
            user_id = str(user.get("id") or "")
            for index, existing in enumerate(self.embedded_sessions):
                if (
                    existing.alive
                    and getattr(existing, "asset_id", "") == asset_id
                    and getattr(existing, "system_user_id", "") == user_id
                ):
                    self.active_session_index = index
                    self.embedded_session = existing
                    self.view = "terminal"
                    self.status = "Reusing active SSH session"
                    self._focus_terminal()
                    self._invalidate()
                    return True
            key = (asset_id, user_id)
            if key in self._pending_connection_keys:
                return True
            self._pending_connection_keys.add(key)
            self.view = "terminal"
            self.terminal_selection_anchor = None
            self.terminal_selection_end = None
            self.status = f"Authenticating: {asset_ip(asset)}"
            self._invalidate()

            def authenticate() -> None:
                token: str | None = None
                error = ""
                try:
                    token = get_token_for_resolved(self.store, self.client, asset, user, quiet=True)
                except JumpCliError as exc:
                    error = str(exc)
                self._from_session_thread(
                    lambda: self._authenticated_for_ssh(asset, user, token, error, return_to_assets)
                )

            threading.Thread(target=authenticate, name="jumpcli-ssh-auth", daemon=True).start()
            return True

        def connect() -> None:
            try:
                # Start every SSH session with a clean terminal, including the
                # scrollback left by the previously exited session.
                sys.stdout.write("\033[2J\033[3J\033[H")
                sys.stdout.flush()
                token = get_token_for_resolved(self.store, self.client, asset, user, quiet=True)
                self.history.record(asset, user)
                self.status = f"Connected: {asset_ip(asset)}"
                command = build_ssh_command(token, ssh_options=[])
                subprocess.call(command)
            except JumpCliError as exc:
                self.last_error = str(exc)
            finally:
                self._invalidate()

        run_in_terminal(connect)
        # SSH may leave its output in the terminal after prompt_toolkit resumes.
        sys.stdout.write("\033[2J\033[3J\033[H")
        sys.stdout.flush()
        if return_to_assets:
            self.view = "assets"
            self.users = []
            self.user_index = 0
            self.focus = "assets"
            self._focus_navigation()
            self._invalidate()
        return True

    def _bindings(self) -> KeyBindings:
        keys = KeyBindings()
        navigating = Condition(lambda: not self.filter_mode and self.view != "terminal")
        terminal_active = Condition(lambda: self.view == "terminal" and not self.picker_open)
        terminal_input_active = Condition(
            lambda: self.view == "terminal" and not self.picker_open and self.focus == "terminal"
        )
        context_menu_active = Condition(lambda: self.context_menu_open)
        terminate_confirm_active = Condition(lambda: self.terminate_confirm_open)
        session_focus = Condition(
            lambda: self.view == "terminal" and not self.picker_open and self.focus == "sessions"
        )
        picker_active = Condition(lambda: self.picker_open)

        @keys.add("c-c", filter=Condition(lambda: self.view != "terminal"), eager=True)
        def _quit(event: Any) -> None:
            self._stop_embedded_sessions()
            event.app.exit(result=0)

        @keys.add("c-c", filter=picker_active, eager=True)
        def _picker_cancel(event: Any) -> None:
            self._close_picker(cancel_transfer=True)

        @keys.add("escape", eager=True)
        def _escape(event: Any) -> None:
            if self.picker_open:
                self._close_picker(cancel_transfer=True)
            elif self.terminate_confirm_open:
                self._close_terminate_confirm()
            elif self.session_menu_open:
                self._close_session_menu()
            elif self.context_menu_open:
                self._close_context_menu()
            elif self.view == "terminal":
                if self.terminal_command_prefix:
                    self.terminal_command_prefix = False
                    self.status = "Command mode cancelled"
                elif self.focus == "sessions":
                    self._leave_session_search()
                else:
                    # Esc belongs to the remote terminal (for example, Vim
                    # uses it to leave insert mode). Use Ctrl-X for TUI
                    # commands so a shell application cannot be interrupted.
                    self._send_terminal(b"\x1b")
            elif self.filter_mode:
                self.filter_mode = False
                self._focus_navigation()
            elif self.view == "users":
                self.view = "assets"
                self.users = []
            elif self.view == "assets":
                self.status = "Press q to quit"
            else:
                self._stop_embedded_sessions()
                event.app.exit(result=0)
            self._invalidate()

        @keys.add("tab", eager=True)
        def _tab(event: Any) -> None:
            if self.view == "terminal":
                if len(self.embedded_sessions) > 1:
                    if self.focus == "sessions":
                        self._focus_terminal()
                    else:
                        self._focus_sessions()
                else:
                    self._send_terminal(b"\t")
            elif not self.picker_open:
                self._toggle_focus()

        @keys.add("c-n", filter=terminal_active, eager=True)
        def _new_session(event: Any) -> None:
            self._toggle_terminal_navigation()

        # Ctrl-N is also the return-to-session toggle while the asset filter
        # is active.  The search widget must not swallow it after a query has
        # been edited from the resource view.
        @keys.add("c-n", filter=Condition(lambda: self.view != "terminal" and bool(self.embedded_sessions)), eager=True)
        def _return_session(event: Any) -> None:
            self._toggle_terminal_navigation()

        @keys.add("f4", filter=terminal_active, eager=True)
        def _resources(event: Any) -> None:
            self._open_new_session()

        @keys.add("f2", filter=terminal_active, eager=True)
        def _next_session(event: Any) -> None:
            self._switch_session(1)

        @keys.add("f6", filter=terminal_active, eager=True)
        def _focus_session_list(event: Any) -> None:
            self._focus_sessions()

        @keys.add("f3", filter=terminal_active, eager=True)
        def _split_session(event: Any) -> None:
            self._toggle_split()

        terminal_command_mode = Condition(
            lambda: self.view == "terminal"
            and not self.picker_open
            and self.terminal_command_prefix
        )

        @keys.add("c-x", filter=terminal_active, eager=True)
        def _terminal_command_prefix(event: Any) -> None:
            if self.terminal_command_prefix:
                self.terminal_command_prefix = False
                self._send_terminal(b"\x18\x18")
            else:
                self.terminal_command_prefix = True
                self.status = "Command mode: r refresh  q quit  x terminate  n resources  u/d transfer"
                self._invalidate()

        session_menu_active = Condition(lambda: self.session_menu_open)

        @keys.add("up", filter=session_menu_active, eager=True)
        def _session_menu_up(event: Any) -> None:
            self.session_menu_index = max(0, self.session_menu_index - 1)
            self._invalidate()

        @keys.add("down", filter=session_menu_active, eager=True)
        def _session_menu_down(event: Any) -> None:
            self.session_menu_index = min(2, self.session_menu_index + 1)
            self._invalidate()

        @keys.add("enter", filter=session_menu_active, eager=True)
        def _session_menu_enter(event: Any) -> None:
            self._session_menu_activate()

        @keys.add("space", filter=picker_active, eager=True)
        def _picker_space(event: Any) -> None:
            if self.picker_list_focused:
                self._picker_toggle()

        @keys.add("up", filter=picker_active, eager=True)
        def _picker_up(event: Any) -> None:
            self._picker_move(-1)

        @keys.add("down", filter=picker_active, eager=True)
        def _picker_down(event: Any) -> None:
            self._picker_move(1)

        @keys.add("up", filter=context_menu_active, eager=True)
        def _context_up(event: Any) -> None:
            self.context_menu_index = max(0, self.context_menu_index - 1)
            self._invalidate()

        @keys.add("down", filter=context_menu_active, eager=True)
        def _context_down(event: Any) -> None:
            self.context_menu_index = min(2, self.context_menu_index + 1)
            self._invalidate()

        @keys.add("enter", filter=context_menu_active, eager=True)
        def _context_enter(event: Any) -> None:
            self._context_menu_activate()

        @keys.add("down", filter=navigating, eager=True)
        @keys.add("j", filter=Condition(lambda: self.view != "terminal" and not self.filter_mode and self.focus != "assets"), eager=True)
        def _down(event: Any) -> None:
            self._move(1)

        @keys.add("up", filter=navigating, eager=True)
        @keys.add("k", filter=Condition(lambda: self.view != "terminal" and not self.filter_mode and self.focus != "assets"), eager=True)
        def _up(event: Any) -> None:
            self._move(-1)

        @keys.add("up", filter=session_focus, eager=True)
        def _session_up(event: Any) -> None:
            self._move_session(-1)

        @keys.add("down", filter=session_focus, eager=True)
        def _session_down(event: Any) -> None:
            self._move_session(1)

        @keys.add("enter", filter=session_focus, eager=True)
        def _session_enter(event: Any) -> None:
            self._leave_session_search()

        @keys.add("c-w", filter=session_focus, eager=True)
        def _session_terminate(event: Any) -> None:
            self._terminate_active_session()

        @keys.add("backspace", filter=session_focus, eager=True)
        def _session_backspace(event: Any) -> None:
            self._session_search(self.session_search_query[:-1])

        @keys.add("c-u", filter=session_focus, eager=True)
        def _session_clear(event: Any) -> None:
            self._session_search("")

        @keys.add("up", filter=Condition(lambda: self.filter_mode), eager=True)
        def _search_up(event: Any) -> None:
            self._move(-1)

        @keys.add("down", filter=Condition(lambda: self.filter_mode), eager=True)
        def _search_down(event: Any) -> None:
            self._move(1)

        @keys.add("pageup", filter=Condition(lambda: self.filter_mode), eager=True)
        def _search_pageup(event: Any) -> None:
            self._move(-5)

        @keys.add("pagedown", filter=Condition(lambda: self.filter_mode), eager=True)
        def _search_pagedown(event: Any) -> None:
            self._move(5)

        @keys.add("enter", eager=True)
        def _enter(event: Any) -> None:
            if self.picker_open:
                self._picker_confirm()
            elif self.view == "terminal" and self.focus == "terminal":
                self._send_terminal(b"\r")
            elif self.view == "terminal" and self.focus == "sessions":
                self._leave_session_search()
            elif self.filter_mode:
                self.filter_mode = False
                self._focus_navigation()
                if self.selected_asset_ids:
                    self._batch_connect_selected()
                else:
                    self._connect_current()
            elif self.view == "assets" and self.selected_asset_ids:
                self._batch_connect_selected()
            else:
                self._connect_current()

        @keys.add("enter", filter=terminate_confirm_active, eager=True)
        def _confirm_enter(event: Any) -> None:
            self._confirm_terminate_all(True)

        @keys.add("y", filter=terminate_confirm_active, eager=True)
        @keys.add("Y", filter=terminate_confirm_active, eager=True)
        def _confirm_yes(event: Any) -> None:
            self._confirm_terminate_all(True)

        @keys.add("n", filter=terminate_confirm_active, eager=True)
        @keys.add("N", filter=terminate_confirm_active, eager=True)
        def _confirm_no(event: Any) -> None:
            self._confirm_terminate_all(False)

        @keys.add("c-u", eager=True)
        def _clear(event: Any) -> None:
            if self.view == "terminal" and self.focus == "terminal":
                self._send_terminal(b"\x15")
            elif self.view == "terminal" and self.focus == "sessions":
                self._session_search("")
            elif self.filter_mode:
                self.search_input.buffer.text = ""
                self._invalidate()

        @keys.add(Keys.ShiftInsert, filter=terminal_input_active, eager=True)
        def _paste_selection(event: Any) -> None:
            self._paste_terminal_clipboard()

        @keys.add(Keys.ControlInsert, filter=terminal_input_active, eager=True)
        def _copy_selection_insert(event: Any) -> None:
            self._copy_terminal_selection()

        @keys.add("c-w", filter=terminal_input_active, eager=True)
        def _terminal_word_erase(event: Any) -> None:
            self._send_terminal(b"\x17")

        @keys.add("c-l", filter=terminal_input_active, eager=True)
        def _terminal_clear(event: Any) -> None:
            self._send_terminal(b"\x0c")

        handled_controls = {"c-w", "c-l", "c-u", "c-n"}
        for letter in "abcdefghijklmnopqrstuvwxyz":
            key_name = f"c-{letter}"
            if key_name in handled_controls:
                continue

            @keys.add(key_name, filter=terminal_input_active, eager=True)
            def _terminal_control(event: Any, value: str = letter) -> None:
                self._send_terminal(bytes((ord(value) - ord("a") + 1,)))

        @keys.add("up", filter=terminal_input_active, eager=True)
        def _terminal_up(event: Any) -> None:
            self._send_terminal(b"\x1b[A")

        @keys.add("pageup", filter=terminal_input_active, eager=True)
        def _terminal_pageup(event: Any) -> None:
            if self.embedded_session is not None:
                self.embedded_session.scroll_history(-1)

        @keys.add("pagedown", filter=terminal_input_active, eager=True)
        def _terminal_pagedown(event: Any) -> None:
            if self.embedded_session is not None:
                self.embedded_session.scroll_history(1)

        @keys.add("down", filter=terminal_input_active, eager=True)
        def _terminal_down(event: Any) -> None:
            self._send_terminal(b"\x1b[B")

        @keys.add("left", filter=terminal_input_active, eager=True)
        def _terminal_left(event: Any) -> None:
            self._send_terminal(b"\x1b[D")

        @keys.add("right", filter=terminal_input_active, eager=True)
        def _terminal_right(event: Any) -> None:
            self._send_terminal(b"\x1b[C")

        @keys.add("backspace", filter=terminal_input_active, eager=True)
        def _terminal_backspace(event: Any) -> None:
            self._send_terminal(b"\x7f")

        # Bind ordinary characters explicitly so they do not wait for the
        # fallback key matcher. The fallback must remain non-eager: mouse
        # reports are decoded by prompt-toolkit before reaching the control's
        # mouse handler.
        for char in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 `~!@#$%^&*()-_=+[]\\{};:'\",.<>/?|":

            @keys.add(char, filter=terminal_input_active, eager=True)
            def _terminal_printable(event: Any, value: str = char) -> None:
                if self.terminal_command_prefix:
                    self.terminal_command_prefix = False
                    self.status = ""
                    self._send_terminal(b"\x18" + value.encode("utf-8"))
                else:
                    self._send_terminal(value.encode("utf-8"))

        @keys.add("r", filter=terminal_command_mode, eager=True)
        @keys.add("R", filter=terminal_command_mode, eager=True)
        def _command_refresh(event: Any) -> None:
            self.terminal_command_prefix = False
            self._reload()

        @keys.add("x", filter=terminal_command_mode, eager=True)
        @keys.add("X", filter=terminal_command_mode, eager=True)
        def _command_terminate(event: Any) -> None:
            self.terminal_command_prefix = False
            self._terminate_active_session()

        @keys.add("q", filter=terminal_command_mode, eager=True)
        @keys.add("Q", filter=terminal_command_mode, eager=True)
        def _command_quit(event: Any) -> None:
            self.terminal_command_prefix = False
            self._stop_embedded_sessions()
            event.app.exit(result=0)

        @keys.add("n", filter=terminal_command_mode, eager=True)
        @keys.add("N", filter=terminal_command_mode, eager=True)
        def _command_resources(event: Any) -> None:
            self.terminal_command_prefix = False
            self._open_new_session()

        @keys.add("u", filter=terminal_command_mode, eager=True)
        @keys.add("U", filter=terminal_command_mode, eager=True)
        def _command_upload(event: Any) -> None:
            self.terminal_command_prefix = False
            self._open_picker("upload")

        @keys.add("d", filter=terminal_command_mode, eager=True)
        @keys.add("D", filter=terminal_command_mode, eager=True)
        def _command_download(event: Any) -> None:
            self.terminal_command_prefix = False
            self._open_picker("download")

        @keys.add("<any>", filter=terminal_input_active)
        def _terminal_any(event: Any) -> None:
            # prompt-toolkit also delivers responses generated by the outer
            # terminal (CPR and SGR mouse reports).  They belong to the TUI,
            # never to the remote shell.  Restrict the fallback to one
            # printable character so an unrecognised escape sequence cannot
            # be forwarded byte by byte either.
            key = event.key_sequence[-1].key
            if key in {
                Keys.CPRResponse,
                Keys.Vt100MouseEvent,
                Keys.ScrollUp,
                Keys.ScrollDown,
            }:
                return
            data = event.data
            if len(data) == 1 and data.isprintable():
                self._send_terminal(data.encode("utf-8"))

        session_typing = Condition(
            lambda: self.view == "terminal" and not self.picker_open and self.focus == "sessions"
        )
        for char in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 ._-:@/":

            @keys.add(char, filter=session_typing, eager=True)
            def _session_type(event: Any, value: str = char) -> None:
                self._session_search(self.session_search_query + value)

        @keys.add("c-q", filter=Condition(lambda: not self.picker_open), eager=True)
        def _quit_key(event: Any) -> None:
            self._stop_embedded_sessions()
            event.app.exit(result=0)

        # Any printable key starts asset filtering immediately when the asset
        # pane is focused. Arrow keys remain navigation keys; j/k are text here.
        asset_typing = Condition(
            lambda: not self.filter_mode and self.focus == "assets" and self.view == "assets"
        )
        printable = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 ._-:@/"
        for char in printable:

            @keys.add(char, filter=asset_typing, eager=True)
            def _start_search(event: Any, value: str = char) -> None:
                self._start_search_with(value)

        @keys.add("backspace", filter=asset_typing, eager=True)
        def _search_backspace(event: Any) -> None:
            self._start_search_with(self.query[:-1])

        @keys.add("space", filter=asset_typing, eager=True)
        def _select_asset(event: Any) -> None:
            self._toggle_asset_selection()

        @keys.add("c-a", filter=asset_typing, eager=True)
        def _select_all_assets(event: Any) -> None:
            self._toggle_all_asset_selection()

        @keys.add("c-space", filter=Condition(lambda: self.filter_mode and self.view == "assets"), eager=True)
        def _select_filtered_asset(event: Any) -> None:
            self._toggle_all_asset_selection()

        @keys.add("c-a", filter=Condition(lambda: self.filter_mode and self.view == "assets"), eager=True)
        def _select_all_filtered_assets(event: Any) -> None:
            self._toggle_all_asset_selection()

        return keys


def run_tui(args: Any) -> int:
    """Authenticate, fetch the asset tree, then run the fullscreen browser."""
    try:
        if args.base_url == DEFAULT_BASE_URL:
            if not (sys.stdin.isatty() and sys.stdout.isatty()):
                raise JumpCliError(
                    "JumpServer URL is not configured; run "
                    "./jump_cli.py config set --base-url 'https://your-jumpserver.example.com' first"
                )
            args.base_url = first_run_setup()
        store, client = ensure_auth(args, "127.0.0.1")
        assets = client.assets_tree("")
        screen = JumpServerTui(args, store, client, assets)
        screen.status = f"Loaded {len(screen.assets)} assets"
        screen.application.run()
        return 0
    except (JumpCliError, EOFError, KeyboardInterrupt) as exc:
        if isinstance(exc, KeyboardInterrupt):
            return 130
        print(f"jump-cli: TUI error: {exc}", file=sys.stderr)
        return 1


def first_run_setup() -> str:
    print("\nJumpServer CLI first-run setup", file=sys.stderr)
    print("Step 1/2: confirm the JumpServer host.", file=sys.stderr)
    while True:
        value = input("JumpServer URL: ").strip().rstrip("/")
        parsed = urllib.parse.urlparse(value)
        if parsed.scheme in {"http", "https"} and parsed.hostname and parsed.hostname != "jumpserver.example.com":
            break
        print("Enter a full URL such as https://jumpserver.example.com.", file=sys.stderr)

    # The standard single-organization ID is enough for most installations.
    # Multi-organization users can override it later with config set --org-id.
    save_config({"base_url": value, "org_id": configured_org_id()})
    print(f"Saved host to {Path.home() / '.config' / 'jumpserver-cli' / 'config.json'}", file=sys.stderr)
    print("Step 2/2: choose an authentication method.", file=sys.stderr)
    return value


def tui_args() -> Any:
    return SimpleNamespace(
        base_url=configured_base_url(),
        cache_dir=None,
        timeout=20,
        debug=False,
        system_user=None,
        pty_mode=False,
    )
