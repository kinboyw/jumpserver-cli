"""Fullscreen asset browser for the JumpServer CLI.

The TUI deliberately delegates authentication, token generation and SSH
construction to the existing CLI code. It only owns navigation and local
session history.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from prompt_toolkit.application import Application, run_in_terminal
from prompt_toolkit.application.current import get_app
from prompt_toolkit.data_structures import Point
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.filters import Condition
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import ConditionalContainer, Float, FloatContainer, HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.dimension import Dimension as D
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import TextArea

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
        "frame.title": "#8b949e bold",
        "frame.focused": "#58a6ff bold",
        "item": "#c9d1d9",
        "item.selected": "bg:#1f6feb #ffffff bold",
        "item.muted": "#8b949e",
        "item.accent": "#3fb950 bold",
        "item.warn": "#ffa657 bold",
        "terminal": "#f0f6fc",
        "error": "#ff7b72 bold",
        "detail.label": "#8b949e",
        "detail.value": "#f0f6fc",
    }
)


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
        self.view = "assets"
        self.asset_index = 0
        self.user_index = 0
        self.history_index = 0
        self.users: list[dict[str, Any]] = []
        self.status = "Ready"
        self.last_error = ""
        self.embedded_sessions: list[EmbeddedPtySession] = []
        self.embedded_session: EmbeddedPtySession | None = None
        self.active_session_index = 0
        self.split_mode = False
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
        self.terminal_control = FormattedTextControl(self._terminal_text, focusable=True)
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
        )

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

    def _layout(self) -> HSplit:
        body = VSplit(
            [
                Window(
                    content=self.asset_control,
                    width=D(preferred=68, min=42),
                    get_vertical_scroll=self._asset_vertical_scroll,
                ),
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
        base = HSplit(
            [
                Window(content=FormattedTextControl(self._header_text), height=1, style="class:header"),
                ConditionalContainer(self.search_input_window, filter=Condition(lambda: self.filter_mode)),
                ConditionalContainer(self.search_status_window, filter=Condition(lambda: not self.filter_mode)),
                ConditionalContainer(body, filter=Condition(lambda: self.view != "terminal")),
                ConditionalContainer(self.terminal_window, filter=Condition(lambda: self.view == "terminal")),
                Window(content=FormattedTextControl(self._footer_text), height=1, style="class:footer"),
            ],
            style="class:root",
        )
        return FloatContainer(
            content=base,
            floats=[
                Float(
                    content=ConditionalContainer(self.picker_window, filter=Condition(lambda: self.picker_open)),
                    top=3,
                    left=8,
                    width=88,
                    height=22,
                )
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
        if self.split_mode and len(sessions) >= 2:
            return self._terminal_split_text(sessions[:2])
        session = self.embedded_session
        if session is None:
            return FormattedText([("class:item.muted", "SSH session is not running")])
        rows: FormattedText = []
        for line in session.screen.display:
            rows.append(("class:terminal", line + "\n"))
        return rows

    def _terminal_split_text(self, sessions: list[EmbeddedPtySession]) -> FormattedText:
        left, right = sessions[0].screen.display, sessions[1].screen.display
        rows: FormattedText = []
        width = max(20, max((len(line) for line in left), default=20))
        for index in range(max(len(left), len(right))):
            left_line = left[index] if index < len(left) else ""
            right_line = right[index] if index < len(right) else ""
            rows.append(("class:terminal", f"{left_line:<{width}} | {right_line}\n"))
        return rows

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
            rows.append(("class:item.selected" if selected else "class:item", self._asset_row(asset, selected) + "\n"))
        return FormattedText(rows)

    def _detail_frame_text(self) -> FormattedText:
        style = "class:frame.focused" if self.focus == "assets" else "class:frame.title"
        return FormattedText([(style, " DETAILS\n"), *self._detail_text()])

    def _asset_row(self, asset: dict[str, Any], selected: bool) -> str:
        marker = ">" if selected else " "
        data = asset_data(asset)
        platform = str(data.get("platform") or "?")
        protocol = ",".join(str(item) for item in data.get("protocols") or []) or "ssh/?"
        return f"{marker} +-- {asset_ip(asset):<16} {asset_hostname(asset)[:34]:<34} {platform:<7} {protocol}"

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
            return FormattedText(
                [
                    ("class:item.muted", status),
                    ("class:footer.key", "Ctrl-N"), ("class:item.muted", " new session  "),
                    ("class:footer.key", "F2"), ("class:item.muted", " next  "),
                    ("class:footer.key", "F3"), ("class:item.muted", " split  "),
                    ("class:footer.key", "Ctrl-X U/D"), ("class:item.muted", " ZMODEM"),
                ]
            )
        return FormattedText(
            [
                ("class:item.muted", status),
                ("class:footer.key", "Tab"), ("class:item.muted", " focus  "),
                ("class:footer.key", "Enter"), ("class:item.muted", " connect  "),
                ("class:footer.key", "type"), ("class:item.muted", " search  "),
                ("class:footer.key", "r"), ("class:item.muted", " reload  "),
                ("class:footer.key", "q"), ("class:item.muted", " quit"),
            ]
        )

    def _selected_asset(self) -> dict[str, Any] | None:
        assets = self.filtered_assets
        if not assets:
            return None
        self.asset_index = max(0, min(self.asset_index, len(assets) - 1))
        return assets[self.asset_index]

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
        try:
            self.users = self.client.system_users(str(asset["id"]))
        except JumpCliError as exc:
            self.last_error = str(exc)
            self._invalidate()
            return
        if not self.users:
            self.last_error = "asset has no available system users"
            self._invalidate()
            return
        if len(self.users) == 1 and self.users[0].get("username") == "ops":
            self.status = "Connecting with ops"
            self._run_ssh(asset, self.users[0])
            return
        self.view = "users"
        self.user_index = 0
        self.status = f"{len(self.users)} system users available"
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
                self.active_session_index = min(self.active_session_index, len(self.embedded_sessions) - 1)
                self.embedded_session = self.embedded_sessions[self.active_session_index]
                self.status = f"SSH session exited ({exit_code}); active sessions: {len(self.embedded_sessions)}"
                if was_terminal:
                    self.view = "terminal"
                    self._focus_terminal()
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
        self.status = f"Select an asset for a new SSH session ({len(self.embedded_sessions)} open)"
        self._focus_navigation()
        self._invalidate()

    def _switch_session(self, delta: int) -> None:
        if not self.embedded_sessions:
            return
        self.active_session_index = (self.active_session_index + delta) % len(self.embedded_sessions)
        self.embedded_session = self.embedded_sessions[self.active_session_index]
        self.view = "terminal"
        self._focus_terminal()
        self.status = f"Session {self.active_session_index + 1}/{len(self.embedded_sessions)}"
        self._invalidate()

    def _toggle_split(self) -> None:
        if len(self.embedded_sessions) < 2:
            self.status = "Open another PTY session before enabling split view"
            self._invalidate()
            return
        self.split_mode = not self.split_mode
        self.status = "Split view enabled" if self.split_mode else "Split view disabled"
        self._invalidate()

    def _focus_terminal(self) -> None:
        with __import__("contextlib").suppress(Exception):
            get_app().layout.focus(self.terminal_control)

    def _stop_embedded_sessions(self) -> None:
        for session in list(self.embedded_sessions):
            session.stop()

    def _run_ssh(self, asset: dict[str, Any] | None, user: dict[str, Any]) -> None:
        if not asset:
            return
        return_to_assets = self.view == "users"

        if getattr(self.args, "pty_mode", False):
            try:
                token = get_token_for_resolved(self.store, self.client, asset, user, quiet=True)
            except JumpCliError as exc:
                self.last_error = str(exc)
                self._invalidate()
                return
            command = build_ssh_command(token, ssh_options=[], force_tty=True)
            self.history.record(asset, user)
            self.view = "terminal"
            self.status = f"Opening SSH: {asset_ip(asset)}"
            holder: dict[str, EmbeddedPtySession] = {}
            session = EmbeddedPtySession(
                command,
                on_change=lambda: self._terminal_changed(holder["session"]),
                on_zmodem=lambda direction: self._terminal_zmodem(holder["session"], direction),
                on_exit=lambda code: self._terminal_exited(holder["session"], code),
            )
            holder["session"] = session
            self.embedded_sessions.append(session)
            self.active_session_index = len(self.embedded_sessions) - 1
            self.embedded_session = session
            session.start()
            with __import__("contextlib").suppress(Exception):
                get_app().layout.focus(self.terminal_control)
            self._invalidate()
            return

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

    def _bindings(self) -> KeyBindings:
        keys = KeyBindings()
        navigating = Condition(lambda: not self.filter_mode)
        terminal_active = Condition(lambda: self.view == "terminal" and not self.picker_open)
        picker_active = Condition(lambda: self.picker_open)

        @keys.add("c-c", filter=Condition(lambda: self.view != "terminal"), eager=True)
        def _quit(event: Any) -> None:
            self._stop_embedded_sessions()
            event.app.exit(result=0)

        @keys.add("c-c", filter=terminal_active, eager=True)
        def _terminal_interrupt(event: Any) -> None:
            self._send_terminal(b"\x03")

        @keys.add("c-c", filter=picker_active, eager=True)
        def _picker_cancel(event: Any) -> None:
            self._close_picker(cancel_transfer=True)

        @keys.add("escape", eager=True)
        def _escape(event: Any) -> None:
            if self.picker_open:
                self._close_picker(cancel_transfer=True)
            elif self.view == "terminal":
                self._send_terminal(b"\x04")
            elif self.filter_mode:
                self.filter_mode = False
                self._focus_navigation()
            elif self.view == "users":
                self.view = "assets"
                self.users = []
            else:
                self._stop_embedded_sessions()
                event.app.exit(result=0)
            self._invalidate()

        @keys.add("tab", eager=True)
        def _tab(event: Any) -> None:
            if self.view == "terminal":
                self._send_terminal(b"\t")
            elif not self.picker_open:
                self._toggle_focus()

        @keys.add("c-n", filter=terminal_active, eager=True)
        def _new_session(event: Any) -> None:
            self._open_new_session()

        @keys.add("f2", filter=terminal_active, eager=True)
        def _next_session(event: Any) -> None:
            self._switch_session(1)

        @keys.add("f3", filter=terminal_active, eager=True)
        def _split_session(event: Any) -> None:
            self._toggle_split()

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

        @keys.add("down", filter=navigating, eager=True)
        @keys.add("j", filter=Condition(lambda: not self.filter_mode and self.focus != "assets"), eager=True)
        def _down(event: Any) -> None:
            self._move(1)

        @keys.add("up", filter=navigating, eager=True)
        @keys.add("k", filter=Condition(lambda: not self.filter_mode and self.focus != "assets"), eager=True)
        def _up(event: Any) -> None:
            self._move(-1)

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
            elif self.view == "terminal":
                self._send_terminal(b"\r")
            elif self.filter_mode:
                self.filter_mode = False
                self._focus_navigation()
                self._connect_current()
            else:
                self._connect_current()

        @keys.add("c-u", eager=True)
        def _clear(event: Any) -> None:
            if self.view == "terminal":
                self._send_terminal(b"\x15")
            elif self.filter_mode:
                self.search_input.buffer.text = ""
                self._invalidate()

        @keys.add("up", filter=terminal_active, eager=True)
        def _terminal_up(event: Any) -> None:
            self._send_terminal(b"\x1b[A")

        @keys.add("down", filter=terminal_active, eager=True)
        def _terminal_down(event: Any) -> None:
            self._send_terminal(b"\x1b[B")

        @keys.add("left", filter=terminal_active, eager=True)
        def _terminal_left(event: Any) -> None:
            self._send_terminal(b"\x1b[D")

        @keys.add("right", filter=terminal_active, eager=True)
        def _terminal_right(event: Any) -> None:
            self._send_terminal(b"\x1b[C")

        @keys.add("backspace", filter=terminal_active, eager=True)
        def _terminal_backspace(event: Any) -> None:
            self._send_terminal(b"\x7f")

        @keys.add("<any>", filter=terminal_active, eager=True)
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

        @keys.add("q", filter=Condition(lambda: self.view != "terminal" and not self.filter_mode and not self.picker_open), eager=True)
        def _quit_key(event: Any) -> None:
            self._stop_embedded_sessions()
            event.app.exit(result=0)

        @keys.add("r", filter=Condition(lambda: not self.filter_mode and self.focus != "assets"), eager=True)
        def _reload(event: Any) -> None:
            self._reload()

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
