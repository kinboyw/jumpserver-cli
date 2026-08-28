"""State and data helpers shared by the JumpServer TUI.

This module intentionally contains no prompt-toolkit widgets. Keeping asset
normalization, filtering, and local history here makes those behaviors easy to
test without constructing a fullscreen application.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .cli import secure_write_text


HISTORY_PATH = Path.home() / ".local" / "state" / "jumpserver-cli" / "history.json"
MAX_HISTORY = 60


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
