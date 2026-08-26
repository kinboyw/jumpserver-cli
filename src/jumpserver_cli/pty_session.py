"""Experimental native PTY session with an lrzsz bridge.

The regular CLI/TUI SSH path intentionally remains an external subprocess. This
module is only used when the TUI is started with ``--pty``.
"""

from __future__ import annotations

import os
import pty
import select
import shlex
import signal
import subprocess
import sys
import termios
import time
import tty
from pathlib import Path


TRANSFER_PREFIX = b"\x18"  # Ctrl-X, kept out of the remote stream for our shortcuts.
TRANSFER_ESCAPE_TIMEOUT = 1.0


def _write(fd: int, data: bytes) -> None:
    while data:
        try:
            written = os.write(fd, data)
        except InterruptedError:
            continue
        if written <= 0:
            return
        data = data[written:]


def _set_raw(fd: int) -> None:
    tty.setraw(fd)


def _prompt_in_cooked_terminal(fd: int, cooked_attrs: list, prompt: str) -> str:
    """Temporarily restore line editing while the PTY session is paused."""
    try:
        termios.tcsetattr(fd, termios.TCSADRAIN, cooked_attrs)
        return input(prompt).strip()
    finally:
        _set_raw(fd)


def _remote_command(master_fd: int, command: str) -> None:
    _write(master_fd, command.encode("utf-8") + b"\r")


def _resync_remote_shell(master_fd: int, terminal_fd: int) -> None:
    """Drop late ZMODEM trailer bytes and redraw the remote shell prompt."""
    time.sleep(0.15)
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        readable, _, _ = select.select([master_fd], [], [], 0.05)
        if master_fd not in readable:
            continue
        try:
            os.read(master_fd, 65536)
        except OSError:
            break

    # rz/sz normally returns to the shell, but the prompt can remain on the
    # same input line. An empty command gives the user a clean, visible PS1.
    _write(master_fd, b"\r")
    deadline = time.monotonic() + 0.8
    while time.monotonic() < deadline:
        readable, _, _ = select.select([master_fd], [], [], 0.05)
        if master_fd not in readable:
            continue
        try:
            data = os.read(master_fd, 65536)
        except OSError:
            break
        if data:
            _write(terminal_fd, data)


def _run_transfer_process(
    command: list[str],
    master_fd: int,
    terminal_fd: int,
    *,
    cwd: str | None = None,
) -> bool:
    """Run lrzsz while keeping Ctrl-C responsive in the parent relay."""
    process = subprocess.Popen(
        command,
        stdin=master_fd,
        stdout=master_fd,
        stderr=terminal_fd,
        cwd=cwd,
        close_fds=True,
    )
    cancelled = False
    try:
        while process.poll() is None:
            readable, _, _ = select.select([terminal_fd], [], [], 0.2)
            if terminal_fd not in readable:
                continue
            data = os.read(terminal_fd, 4096)
            if not data:
                continue
            if b"\x03" in data:
                cancelled = True
                process.send_signal(signal.SIGINT)
                # CAN cancels the ZMODEM receiver; ETX also returns the
                # remote shell from rz/sz on implementations that need it.
                _write(master_fd, b"\x18" * 8 + b"\x03")
                break
            _write(master_fd, data)
        if cancelled:
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        else:
            process.wait()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    return not cancelled and process.returncode == 0


def _run_zmodem(master_fd: int, terminal_fd: int, cooked_attrs: list, *, upload: bool) -> None:
    """Run local rz/sz against the remote shell through the SSH PTY.

    JumpServer and SSH only see the resulting PTY byte stream. The local
    lrzsz process consumes that stream while the regular PTY relay is paused.
    """
    if upload:
        source = _prompt_in_cooked_terminal(terminal_fd, cooked_attrs, "\nUpload local file: ")
        if not source:
            return
        source_path = Path(source).expanduser()
        if not source_path.is_file():
            print(f"jump-cli: local file does not exist: {source_path}", file=sys.stderr)
            return
        if not _command_exists("sz"):
            print("jump-cli: sz is required; install the lrzsz package", file=sys.stderr)
            return
        print("Starting ZMODEM upload...", file=sys.stderr)
        _remote_command(master_fd, "rz -be")
        time.sleep(0.2)
        ok = _run_transfer_process(["sz", "-be", "--", str(source_path)], master_fd, terminal_fd)
    else:
        remote_path = _prompt_in_cooked_terminal(terminal_fd, cooked_attrs, "\nDownload remote file: ")
        if not remote_path:
            return
        destination = _prompt_in_cooked_terminal(terminal_fd, cooked_attrs, "Save into local directory [.] : ") or "."
        destination_path = Path(destination).expanduser()
        if not destination_path.is_dir():
            print(f"jump-cli: local directory does not exist: {destination_path}", file=sys.stderr)
            return
        if not _command_exists("rz"):
            print("jump-cli: rz is required; install the lrzsz package", file=sys.stderr)
            return
        print("Starting ZMODEM download...", file=sys.stderr)
        command = f"sz -be -- {shlex.quote(remote_path)}"
        _remote_command(master_fd, command)
        time.sleep(0.2)
        ok = _run_transfer_process(["rz", "-be"], master_fd, terminal_fd, cwd=str(destination_path))
    _resync_remote_shell(master_fd, terminal_fd)
    if ok:
        print("ZMODEM transfer finished.\n", file=sys.stderr)
    else:
        print("ZMODEM transfer cancelled or failed.\n", file=sys.stderr)


def _command_exists(name: str) -> bool:
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(directory) / name
        if candidate.exists() and os.access(candidate, os.X_OK):
            return True
    return False


def run_pty_ssh(command: list[str]) -> int:
    """Run an SSH command through an independent local PTY.

    Ctrl-X followed by U uploads a local file with ZMODEM. Ctrl-X followed by
    D downloads a remote file. Any other Ctrl-X sequence is forwarded to SSH.
    """
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise RuntimeError("the PTY session requires an interactive terminal")

    terminal_fd = sys.stdin.fileno()
    pid, master_fd = pty.fork()
    if pid == 0:
        os.execvp(command[0], command)

    saved_terminal = termios.tcgetattr(terminal_fd)
    pending_prefix = False
    prefix_started = 0.0
    try:
        _set_raw(terminal_fd)
        while True:
            if pending_prefix and time.monotonic() - prefix_started > TRANSFER_ESCAPE_TIMEOUT:
                _write(master_fd, TRANSFER_PREFIX)
                pending_prefix = False

            readable, _, _ = select.select([terminal_fd, master_fd], [], [], 0.1)
            if terminal_fd in readable:
                data = os.read(terminal_fd, 4096)
                if not data:
                    break
                for byte in data:
                    if pending_prefix:
                        pending_prefix = False
                        if byte in (ord("u"), ord("U")):
                            _run_zmodem(master_fd, terminal_fd, saved_terminal, upload=True)
                        elif byte in (ord("d"), ord("D")):
                            _run_zmodem(master_fd, terminal_fd, saved_terminal, upload=False)
                        else:
                            _write(master_fd, TRANSFER_PREFIX + bytes([byte]))
                    elif byte == TRANSFER_PREFIX[0]:
                        pending_prefix = True
                        prefix_started = time.monotonic()
                    else:
                        _write(master_fd, bytes([byte]))

            if master_fd in readable:
                try:
                    data = os.read(master_fd, 65536)
                except OSError:
                    data = b""
                if not data:
                    break
                _write(sys.stdout.fileno(), data)
    except KeyboardInterrupt:
        _write(master_fd, b"\x03")
    finally:
        termios.tcsetattr(terminal_fd, termios.TCSADRAIN, saved_terminal)
        try:
            os.close(master_fd)
        except OSError:
            pass
        _, status = os.waitpid(pid, 0)

    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return 128 + os.WTERMSIG(status)
    return 1
