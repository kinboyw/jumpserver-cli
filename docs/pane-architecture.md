# Pane Architecture Proposal

This document describes the next embedded-PTY iteration. It is a design
boundary for implementation; the current TUI still uses the stable fixed
two-session split view.

## Goals

- Treat each embedded SSH session as an independently focusable pane.
- Support horizontal and vertical splits, pane clicks, and divider dragging.
- Resize the child PTY and its pyte screen whenever a pane changes size.
- Make pane ownership explicit before adding synchronized input.
- Preserve native shell and full-screen application behavior inside a pane.

## Model

The layout should be a tree rather than a hard-coded `sessions[:2]` pair:

```text
Workspace
└── SplitNode(horizontal, ratio)
    ├── Pane(session-a)
    └── SplitNode(vertical, ratio)
        ├── Pane(session-b)
        └── Pane(session-c)
```

`SplitNode` owns orientation, children, and divider ratio. `Pane` owns a
session ID, active state, selection, scroll position, and connection status.
The layout tree must not own SSH process lifecycle; session creation and exit
remain in the session manager.

## Interaction

- Click a pane to make it active.
- Click and drag a divider to change its ratio.
- `Ctrl-X h` creates a horizontal split.
- `Ctrl-X v` creates a vertical split.
- `Ctrl-X` plus an arrow moves focus to an adjacent pane.
- `Ctrl-X w` closes the active pane.
- `Ctrl-X s` toggles synchronized input for the selected pane group.

The existing `Ctrl-X` command prefix remains the only TUI command mode while
terminal focus is active. `Esc`, `Ctrl-C`, `Ctrl-V`, readline controls, and
application mouse events must continue to reach the remote terminal.

## Resize and rendering

Each pane gets its actual inner width and height. Resize events update the
local pyte screen and the child PTY together. Divider dragging should be
throttled to a small interval so repeated `SIGWINCH` and redraw work cannot
starve keyboard input. Pane rendering must use one atomic session snapshot;
the snapshot must include text, attributes, and cursor coordinates.

Selection and clipboard coordinates are local to a pane. A pane must never
read another pane's selection or terminal history. Remote mouse reporting is
handled by the active terminal; only pane content boundaries and dividers are
owned by the TUI.

## Synchronized input

Synchronized input is opt-in and broadcasts raw terminal bytes through an
independent queue per target session. A disconnected or failed target is
marked in the pane list without blocking other sessions. The active pane,
selection, scrollback, file transfer, and resize state remain independent.

Before enabling it, the implementation should define target membership,
feedback for partial delivery, and how dangerous controls such as `Ctrl-C`,
`Ctrl-D`, and `Ctrl-X` are handled. Text-only replication is insufficient for
Vim, readline, and other full-screen terminal applications.

## Migration order

1. Extract pure TUI data helpers and keep existing behavior unchanged.
2. Introduce a `Pane` and `SplitNode` model without changing rendering.
3. Replace the fixed two-pane layout with tree-based layout and hit testing.
4. Add divider dragging and throttled PTY resize.
5. Add synchronized input only after independent pane selection and lifecycle
   tests are stable.
