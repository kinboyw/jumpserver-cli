# TUI Development Notes

This document records the terminal integration constraints learned while
building the embedded SSH TUI. Read it before changing `tui.py` or
`embedded_session.py`.

## Performance Rules

- Never perform JumpServer API calls or token requests in the prompt-toolkit
  event handler. Use a daemon worker and deliver the result with
  `application.loop.call_soon_threadsafe`.
- Never do full screen formatting while holding `EmbeddedPtySession._lock`.
  The lock is for copying the pyte screen state. ANSI cleanup, color mapping,
  selection calculation, and formatted-text construction happen after the
  lock is released.
- Text, attributes, and cursor coordinates must come from one screen frame.
  Do not call `display_snapshot()`, `styled_snapshot()`, and
  `cursor_snapshot()` independently while rendering. A fast `ll`, `top`, or
  build log can update the PTY between calls and produce half-lines or column
  shifts.
- Avoid invalidating the application for every byte. Batch output is expected;
  coalesce PTY change notifications (the current implementation uses a short
  redraw window) so prompt-toolkit's event loop leaves room for keyboard input.

## PTY and Terminal Semantics

- The child SSH process must have a real PTY and its size must follow the
  visible terminal pane. Resize pyte and the child PTY together.
- Ordinary input belongs to the remote shell. In particular, `Esc`, `Ctrl-C`,
  `Ctrl-V`, arrows, and readline controls must not be repurposed by TUI
  commands while the terminal has focus.
- TUI commands use an explicit prefix (`Ctrl-X`). Keep command-mode bindings
  separate from terminal input bindings so a command key cannot leak a byte
  sequence to SSH.
- Terminal rendering must use `wrap_lines=False`. The pyte screen already has
  terminal rows and columns; local wrapping changes the row mapping used by
  cursor placement and mouse selection.
- Do not strip a line merely because it starts with the remote OSC title.
  Only remove a title when it is followed by a verified duplicated prompt.
  A normal PS1 can legitimately begin with the same `user@host` text.
- Keep trailing screen columns in the render model. They are needed for cursor
  and selection coordinates even when prompt-toolkit optimizes blank cells.

## Input, Mouse, and Clipboard

- Mouse selection coordinates are screen coordinates, not string indexes after
  arbitrary cleanup. If a prefix is removed, apply the same offset to text,
  attributes, cursor, and selection extraction.
- Terminal mouse selection releases should copy automatically. `Ctrl-C` must
  remain remote interrupt; use `Ctrl-Insert` for explicit copy.
- Clipboard support needs both prompt-toolkit's clipboard and host commands
  for WSL/Linux/macOS. Decode clipboard bytes as UTF-8 first and only use a
  legacy encoding as a fallback.
- Context menus must be separate from session-list menus. A terminal menu
  cannot be allowed to capture scrollback or remote mouse reporting.

## Session Lifecycle

- A session is removed from `embedded_sessions` only from its `on_exit`
  callback. `stop()` requests termination; it must not directly mutate the
  session list.
- Remote exit and local termination use the same cleanup path. Preserve the
  active-session index and select another live session when the current one
  disappears.
- Do not reuse an alive session's PTY for a different asset/user pair. Reuse
  only an exact asset-id and system-user-id match.
- Batch connection keeps the first successfully created session as the main
  session. Pending authentication must not make the UI leave terminal mode
  prematurely.

## Regression Checklist

Before committing terminal changes, test at least:

- `ll` with colored filenames and long output
- `vim`, including `Esc`, `Ctrl-C`, and paste
- `top` or another continuously updating screen
- `less` scrollback and mouse selection
- Chinese text and wide characters
- one session and multiple sessions
- local `Ctrl-X x` termination and remote `exit`
- narrow terminal width and resize
- WSL clipboard copy/paste when available

The offline tests should cover prompt cleanup and atomic render snapshots;
network-dependent behavior still needs a real JumpServer session.
