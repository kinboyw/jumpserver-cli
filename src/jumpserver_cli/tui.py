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
from prompt_toolkit.layout import ConditionalContainer, HSplit, Layout, VSplit, Window
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
        return HSplit(
            [
                Window(content=FormattedTextControl(self._header_text), height=1, style="class:header"),
                ConditionalContainer(self.search_input_window, filter=Condition(lambda: self.filter_mode)),
                ConditionalContainer(self.search_status_window, filter=Condition(lambda: not self.filter_mode)),
                body,
                Window(content=FormattedTextControl(self._footer_text), height=1, style="class:footer"),
            ],
            style="class:root",
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
        if self.view == "users":
            if self.users:
                self._run_ssh(self._selected_asset(), self.users[self.user_index])
        elif self.focus == "history":
            self._connect_history()
        else:
            self._select_asset()

    def _run_ssh(self, asset: dict[str, Any] | None, user: dict[str, Any]) -> None:
        if not asset:
            return
        return_to_assets = self.view == "users"

        def connect() -> None:
            try:
                # Start every SSH session with a clean terminal, including the
                # scrollback left by the previously exited session.
                sys.stdout.write("\033[2J\033[3J\033[H")
                sys.stdout.flush()
                token = get_token_for_resolved(self.store, self.client, asset, user, quiet=True)
                self.history.record(asset, user)
                self.status = f"Connected: {asset_ip(asset)}"
                if getattr(self.args, "pty_mode", False):
                    command = build_ssh_command(token, ssh_options=[], force_tty=True)
                    run_pty_ssh(command)
                else:
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

        @keys.add("c-c", eager=True)
        def _quit(event: Any) -> None:
            event.app.exit(result=0)

        @keys.add("escape", eager=True)
        def _escape(event: Any) -> None:
            if self.filter_mode:
                self.filter_mode = False
                self._focus_navigation()
            elif self.view == "users":
                self.view = "assets"
                self.users = []
            else:
                event.app.exit(result=0)
            self._invalidate()

        @keys.add("tab", eager=True)
        def _tab(event: Any) -> None:
            self._toggle_focus()

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
            if self.filter_mode:
                self.filter_mode = False
                self._focus_navigation()
            self._connect_current()

        @keys.add("c-u", eager=True)
        def _clear(event: Any) -> None:
            if self.filter_mode:
                self.search_input.buffer.text = ""
                self._invalidate()

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
