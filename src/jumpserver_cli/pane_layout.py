"""Pure pane layout model for the embedded terminal workspace.

The model is deliberately independent from prompt-toolkit and SSH session
objects. Rendering and session lifecycle can adopt it incrementally after its
tree operations are covered by tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


Orientation = Literal["horizontal", "vertical"]


@dataclass
class Pane:
    """A leaf in the layout tree, identified independently from its session."""

    key: str


@dataclass
class SplitNode:
    """A binary split with a ratio allocated to the first child."""

    orientation: Orientation
    first: Pane | SplitNode
    second: Pane | SplitNode
    ratio: float = 0.5

    def __post_init__(self) -> None:
        self.ratio = min(0.9, max(0.1, float(self.ratio)))


Node = Pane | SplitNode


class PaneLayout:
    """Manage a binary pane tree without knowing how panes are rendered."""

    def __init__(self) -> None:
        self.root: Node | None = None
        self.active_key: str | None = None

    @property
    def panes(self) -> list[Pane]:
        result: list[Pane] = []

        def visit(node: Node) -> None:
            if isinstance(node, Pane):
                result.append(node)
            else:
                visit(node.first)
                visit(node.second)

        if self.root is not None:
            visit(self.root)
        return result

    @property
    def keys(self) -> list[str]:
        return [pane.key for pane in self.panes]

    def open(
        self,
        key: str,
        *,
        orientation: Orientation = "vertical",
        ratio: float = 0.5,
        target_key: str | None = None,
    ) -> None:
        if not key:
            raise ValueError("pane key cannot be empty")
        if orientation not in {"horizontal", "vertical"}:
            raise ValueError(f"unsupported pane orientation: {orientation}")
        if key in self.keys:
            self.active_key = key
            return
        if self.root is None:
            self.root = Pane(key)
            self.active_key = key
            return

        target_key = target_key or self.active_key
        if target_key not in self.keys:
            raise KeyError(target_key)
        new_node = SplitNode(orientation, Pane(target_key), Pane(key), ratio)
        self.root = self._replace_leaf(self.root, target_key, new_node)
        self.active_key = key

    def close(self, key: str) -> bool:
        if key not in self.keys:
            return False
        self.root = self._remove_leaf(self.root, key)
        if self.active_key == key:
            keys = self.keys
            self.active_key = keys[0] if keys else None
        return True

    def focus(self, key: str) -> bool:
        if key not in self.keys:
            return False
        self.active_key = key
        return True

    def focus_next(self, step: int = 1) -> str | None:
        keys = self.keys
        if not keys:
            self.active_key = None
            return None
        if self.active_key not in keys:
            self.active_key = keys[0]
            return self.active_key
        self.active_key = keys[(keys.index(self.active_key) + step) % len(keys)]
        return self.active_key

    def set_ratio(self, path: tuple[int, ...], ratio: float) -> bool:
        """Set a split ratio; path uses 0 for first and 1 for second child."""
        node = self.root
        for branch in path:
            if not isinstance(node, SplitNode) or branch not in (0, 1):
                return False
            node = node.first if branch == 0 else node.second
        if not isinstance(node, SplitNode):
            return False
        node.ratio = min(0.9, max(0.1, float(ratio)))
        return True

    @staticmethod
    def _replace_leaf(node: Node, key: str, replacement: Node) -> Node:
        if isinstance(node, Pane):
            return replacement if node.key == key else node
        node.first = PaneLayout._replace_leaf(node.first, key, replacement)
        node.second = PaneLayout._replace_leaf(node.second, key, replacement)
        return node

    @staticmethod
    def _remove_leaf(node: Node | None, key: str) -> Node | None:
        if node is None:
            return None
        if isinstance(node, Pane):
            return None if node.key == key else node
        if isinstance(node.first, Pane) and node.first.key == key:
            return node.second
        if isinstance(node.second, Pane) and node.second.key == key:
            return node.first
        node.first = PaneLayout._remove_leaf(node.first, key) or node.first
        node.second = PaneLayout._remove_leaf(node.second, key) or node.second
        return node
