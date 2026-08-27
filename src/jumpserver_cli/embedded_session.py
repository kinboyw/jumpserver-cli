"""An SSH PTY session owned by the TUI.

This is deliberately separate from ``pty_session.py``. The latter is the
compatibility bridge used by the experimental blocking PTY mode; this module
keeps the PTY readable while the prompt-toolkit application remains active.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import pty
import re
import select
import signal
import struct
import subprocess
import termios
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pyte


ZMODEM_MARKER = re.compile(rb"(?:\*\*)?\x18B([0-9a-fA-F]{2})")
ZMODEM_PREFIXES = tuple(
    marker[:length]
    for marker in (b"**\x18B", b"\x18B")
    for length in range(1, len(marker) + 1)
)


class SessionScreen(pyte.HistoryScreen):
    """pyte screen that can answer terminal queries from remote programs."""

    def __init__(self, columns: int, lines: int, send: Callable[[bytes], None]) -> None:
        super().__init__(columns, lines, history=2000, ratio=0.5)
        self._send = send

    def write_process_input(self, data: str) -> None:
        self._send(data.encode("utf-8"))


class EmbeddedPtySession:
    """Own an SSH PTY and expose screen, transfer, and lifecycle callbacks."""

    def __init__(
        self,
        command: list[str],
        *,
        on_change: Callable[[], None],
        on_zmodem: Callable[[str], None],
        on_exit: Callable[[int], None],
        columns: int = 120,
        lines: int = 40,
    ) -> None:
        self.command = command
        self.on_change = on_change
        self.on_zmodem = on_zmodem
        self.on_exit = on_exit
        self.screen = SessionScreen(columns, lines, self.send_immediate)
        self.stream = pyte.ByteStream(self.screen)
        self.master_fd: int | None = None
        self.pid: int | None = None
        self.transfer_process: subprocess.Popen[bytes] | None = None
        self.transfer_pending = bytearray()
        self.transfer_direction: str | None = None
        self.transfer_finished = threading.Event()
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stopping = False
        self._detect_buffer = bytearray()
        self._suppress_protocol_until = 0.0
        self._input_pending = bytearray()
        self._input_ready = False
        self._input_started_at = time.monotonic()

    @property
    def alive(self) -> bool:
        return self.pid is not None and not self._stopping

    def start(self) -> None:
        pid, master_fd = pty.fork()
        if pid == 0:
            os.execvp(self.command[0], self.command)
        self.pid = pid
        self.master_fd = master_fd
        self._input_started_at = time.monotonic()
        self._set_pty_size(notify=False)
        self._thread = threading.Thread(target=self._reader, name="jumpcli-ssh-pty", daemon=True)
        self._thread.start()

    def send(self, data: bytes) -> None:
        with self._lock:
            if not data:
                return
            self._release_pending_if_ready()
            if not self._input_ready:
                self._input_pending.extend(data)
            elif self.master_fd is not None:
                self._write(self.master_fd, data)

    def send_immediate(self, data: bytes) -> None:
        """Send terminal-protocol replies even before the shell prompt exists."""
        with self._lock:
            if self.master_fd is not None and data:
                self._write(self.master_fd, data)

    def resize(self, columns: int, lines: int) -> None:
        """Resize both the local screen model and the SSH client's PTY."""
        columns = max(20, columns)
        lines = max(5, lines)
        with self._lock:
            changed = self.screen.columns != columns or self.screen.lines != lines
            if changed:
                self.screen.resize(lines, columns)
            if self.master_fd is not None:
                if changed:
                    self._set_pty_size(notify=True)
        if changed:
            self.on_change()

    def _set_pty_size(self, *, notify: bool) -> None:
        if self.master_fd is None:
            return
        with contextlib.suppress(OSError):
            fcntl.ioctl(
                self.master_fd,
                termios.TIOCSWINSZ,
                struct.pack("HHHH", self.screen.lines, self.screen.columns, 0, 0),
            )
        if notify and self.pid is not None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(self.pid, signal.SIGWINCH)

    def display_snapshot(self) -> tuple[str, ...]:
        """Return a consistent screen image for the prompt-toolkit renderer."""
        with self._lock:
            lines = list(self.screen.display)
            title = self.screen.title
            if title:
                lines = [line[len(title) :] if line.startswith(title) else line for line in lines]
            return tuple(lines)

    def cursor_snapshot(self) -> tuple[int, int, bool]:
        with self._lock:
            x, y, hidden = self.screen.cursor.x, self.screen.cursor.y, self.screen.cursor.hidden
            title = self.screen.title
            if title and 0 <= y < self.screen.lines:
                line = self.screen.display[y]
                if line.startswith(title):
                    x = max(0, x - len(title))
            return x, y, hidden

    def styled_snapshot(self) -> tuple[tuple[tuple[str, tuple[Any, ...]], ...], ...]:
        """Return character data and terminal attributes as one render snapshot."""
        with self._lock:
            rows = []
            title = self.screen.title
            for row in range(self.screen.lines):
                chars = []
                for column in range(self.screen.columns):
                    char = self.screen.buffer[row][column]
                    attrs = (
                        char.fg,
                        char.bg,
                        char.bold,
                        char.italics,
                        char.underscore,
                        char.strikethrough,
                        char.reverse,
                        char.blink,
                    )
                    chars.append((char.data, attrs))
                if title and "".join(char for char, _ in chars).startswith(title):
                    chars = chars[len(title) :]
                rows.append(tuple(chars))
            return tuple(rows)

    def scroll_history(self, direction: int) -> None:
        with self._lock:
            if direction < 0:
                self.screen.prev_page()
            elif direction > 0:
                self.screen.next_page()
        self.on_change()

    def start_transfer(self, command: list[str], *, cwd: str | None = None) -> None:
        with self._lock:
            if self.master_fd is None or self.transfer_process is not None:
                return
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=self.master_fd,
                stderr=subprocess.DEVNULL,
                cwd=cwd,
                close_fds=True,
            )
            self.transfer_process = process
            pending = bytes(self.transfer_pending)
            self.transfer_pending.clear()
            if pending and process.stdin is not None:
                process.stdin.write(pending)
                process.stdin.flush()

    def cancel_transfer(self) -> None:
        with self._lock:
            process = self.transfer_process
            master_fd = self.master_fd
            if process is None and self.transfer_direction is None:
                return
            if process is not None and process.poll() is None:
                process.send_signal(signal.SIGINT)
            if master_fd is not None:
                self._write(master_fd, b"\x18" * 8 + b"\x03")
            if process is not None:
                return
            self.transfer_process = None
            self.transfer_direction = None
            self.transfer_pending.clear()
            self._detect_buffer.clear()
            self._suppress_protocol_until = time.monotonic() + 1.5
            self.on_change()

    def stop(self) -> None:
        self._stopping = True
        self.cancel_transfer()
        with self._lock:
            pid = self.pid
        if pid is not None:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    def _reader(self) -> None:
        assert self.master_fd is not None
        exit_code = 1
        try:
            while not self._stopping:
                readable, _, _ = select.select([self.master_fd], [], [], 0.1)
                if self.master_fd not in readable:
                    with self._lock:
                        self._release_pending_if_ready()
                    self._poll_transfer()
                    continue
                try:
                    data = os.read(self.master_fd, 65536)
                except OSError:
                    break
                if not data:
                    break
                if self.transfer_process is not None:
                    self._feed_transfer(data)
                elif self.transfer_direction is not None:
                    self.transfer_pending.extend(data)
                else:
                    self._consume_or_detect(data)
                self._poll_transfer()
        finally:
            self._poll_transfer(wait=True)
            if self.pid is not None:
                try:
                    _, status = os.waitpid(self.pid, 0)
                    if os.WIFEXITED(status):
                        exit_code = os.WEXITSTATUS(status)
                    elif os.WIFSIGNALED(status):
                        exit_code = 128 + os.WTERMSIG(status)
                except ChildProcessError:
                    pass
            if self.master_fd is not None:
                try:
                    os.close(self.master_fd)
                except OSError:
                    pass
                self.master_fd = None
            self._stopping = True
            self.on_exit(exit_code)

    def _consume_screen(self, data: bytes) -> None:
        with self._lock:
            self.stream.feed(data)
            self._release_pending_if_ready()
        self.on_change()

    def _release_pending_if_ready(self) -> None:
        if not self._input_ready:
            for line in reversed(self.screen.display):
                if line.rstrip() and re.search(r"(?:[$#>%])\s*$", line.rstrip()):
                    self._input_ready = True
                    break
        # A custom prompt may not end in a conventional shell marker. Do not
        # keep input trapped forever after the SSH startup grace period.
        if not self._input_ready and time.monotonic() - self._input_started_at >= 1.0:
            self._input_ready = True
        if self._input_ready and self._input_pending and self.master_fd is not None:
            self._write(self.master_fd, bytes(self._input_pending))
            self._input_pending.clear()

    def _consume_or_detect(self, data: bytes) -> None:
        if time.monotonic() < self._suppress_protocol_until:
            match = ZMODEM_MARKER.search(data)
            if match:
                if match.start():
                    self._consume_screen(data[:match.start()])
                newline = data.find(b"\n", match.end())
                if newline >= 0 and newline + 1 < len(data):
                    self._consume_screen(data[newline + 1 :])
            else:
                self._consume_screen(data)
            return

        self._detect_buffer.extend(data)
        buffered = bytes(self._detect_buffer)
        match = ZMODEM_MARKER.search(buffered)
        if not match:
            # Keep only a real possible marker prefix. Caching arbitrary
            # trailing bytes makes normal shell echo appear one keypress late.
            remainder_length = max(
                (len(prefix) for prefix in ZMODEM_PREFIXES if buffered.endswith(prefix)),
                default=0,
            )
            safe_length = len(buffered) - remainder_length
            if safe_length:
                self._consume_screen(buffered[:safe_length])
                self._detect_buffer = bytearray(buffered[safe_length:])
            return
        frame_type = int(match.group(1), 16)
        # ZRINIT means the remote rz is waiting for a local sender (upload).
        # ZRQINIT/ZFILE announce a remote sender (download).
        direction = "upload" if frame_type == 1 else "download" if frame_type in (0, 4) else "unknown"
        if direction == "unknown":
            if match.start():
                self._consume_screen(buffered[:match.start()])
            # ZFIN/ZACK and other protocol frames must never be rendered as
            # terminal text. Preserve any shell output after their line.
            newline = buffered.find(b"\n", match.end())
            if newline >= 0 and newline + 1 < len(buffered):
                self._consume_screen(buffered[newline + 1 :])
            self._detect_buffer.clear()
            return
        if match.start():
            self._consume_screen(buffered[:match.start()])
        self.transfer_direction = direction
        self.transfer_pending.extend(buffered[match.start():])
        self._detect_buffer.clear()
        self.on_zmodem(direction)

    def _feed_transfer(self, data: bytes) -> None:
        process = self.transfer_process
        if process is None or process.stdin is None:
            return
        try:
            process.stdin.write(data)
            process.stdin.flush()
        except (BrokenPipeError, OSError):
            self._finish_transfer()

    def _poll_transfer(self, *, wait: bool = False) -> None:
        process = self.transfer_process
        if process is None:
            return
        if wait:
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        elif process.poll() is None:
            return
        self._finish_transfer()

    def _finish_transfer(self) -> None:
        process = self.transfer_process
        if process is None:
            return
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        self.transfer_process = None
        self.transfer_direction = None
        self.transfer_pending.clear()
        self._suppress_protocol_until = time.monotonic() + 1.5
        if self.master_fd is not None:
            self._write(self.master_fd, b"\r")
        self.transfer_finished.set()
        self.on_change()

    @staticmethod
    def _write(fd: int, data: bytes) -> None:
        while data:
            try:
                written = os.write(fd, data)
            except InterruptedError:
                continue
            if written <= 0:
                return
            data = data[written:]
