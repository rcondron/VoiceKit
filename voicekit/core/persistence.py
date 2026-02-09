"""Conversation persistence for VoiceKit.

Saves and restores conversation state across sessions so that AI
providers can maintain context between calls. Stores conversation
history, caller metadata, and session timestamps.

Storage backends:
  - JSON file (default, zero dependencies)
  - Future: SQLite, Redis, PostgreSQL

Usage:
  store = ConversationStore("./conversations")
  store.save("caller_123", conversation_data)
  history = store.load("caller_123")
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class ConversationStore:
    """File-based conversation persistence.

    Stores conversations as JSON files keyed by caller/session ID.
    Each file contains the conversation history, metadata, and timestamps.

    Args:
        storage_dir: Directory to store conversation files.
        max_history: Maximum number of messages to retain per conversation.
        ttl_hours: How long to keep conversations before expiry (0 = forever).
    """

    def __init__(
        self,
        storage_dir: str = "conversations",
        max_history: int = 50,
        ttl_hours: float = 0,
    ) -> None:
        self._dir = Path(storage_dir)
        self._max_history = max_history
        self._ttl_seconds = ttl_hours * 3600 if ttl_hours > 0 else 0
        self._dir.mkdir(parents=True, exist_ok=True)

    def _key_path(self, caller_id: str) -> Path:
        """Get the file path for a caller's conversation."""
        # Sanitize caller_id for filesystem safety
        safe_id = "".join(c if c.isalnum() or c in "-_+" else "_" for c in caller_id)
        return self._dir / f"{safe_id}.json"

    def save(
        self,
        caller_id: str,
        messages: list[dict[str, str]],
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Save conversation history for a caller.

        Args:
            caller_id: Unique identifier (phone number, username, etc.)
            messages: List of message dicts with 'role' and 'content' keys.
            metadata: Optional metadata (platform, display name, etc.)
        """
        # Trim to max history
        if len(messages) > self._max_history:
            messages = messages[-self._max_history:]

        data = {
            "caller_id": caller_id,
            "messages": messages,
            "metadata": metadata or {},
            "updated_at": time.time(),
            "message_count": len(messages),
        }

        path = self._key_path(caller_id)
        try:
            path.write_text(json.dumps(data, indent=2))
            logger.debug(
                "Saved conversation for %s (%d messages)", caller_id, len(messages)
            )
        except Exception:
            logger.warning("Failed to save conversation for %s", caller_id, exc_info=True)

    def load(self, caller_id: str) -> list[dict[str, str]]:
        """Load conversation history for a caller.

        Returns an empty list if no history exists or if it has expired.

        Args:
            caller_id: Unique identifier.

        Returns:
            List of message dicts (may be empty).
        """
        path = self._key_path(caller_id)

        if not path.exists():
            return []

        try:
            data = json.loads(path.read_text())
        except Exception:
            logger.warning("Failed to load conversation for %s", caller_id, exc_info=True)
            return []

        # Check TTL
        if self._ttl_seconds > 0:
            updated_at = data.get("updated_at", 0)
            if time.time() - updated_at > self._ttl_seconds:
                logger.debug("Conversation for %s expired (TTL)", caller_id)
                path.unlink(missing_ok=True)
                return []

        return data.get("messages", [])

    def load_with_metadata(self, caller_id: str) -> dict[str, Any]:
        """Load full conversation data including metadata.

        Returns:
            Dict with 'messages', 'metadata', 'updated_at', etc.
            Empty dict if not found.
        """
        path = self._key_path(caller_id)

        if not path.exists():
            return {}

        try:
            data = json.loads(path.read_text())
            if self._ttl_seconds > 0:
                if time.time() - data.get("updated_at", 0) > self._ttl_seconds:
                    path.unlink(missing_ok=True)
                    return {}
            return data
        except Exception:
            return {}

    def delete(self, caller_id: str) -> bool:
        """Delete conversation history for a caller."""
        path = self._key_path(caller_id)
        if path.exists():
            path.unlink()
            return True
        return False

    def list_callers(self) -> list[str]:
        """List all caller IDs with stored conversations."""
        callers = []
        for path in self._dir.glob("*.json"):
            try:
                data = json.loads(path.read_text())
                callers.append(data.get("caller_id", path.stem))
            except Exception:
                callers.append(path.stem)
        return callers

    def cleanup_expired(self) -> int:
        """Remove all expired conversations. Returns count of removed."""
        if self._ttl_seconds <= 0:
            return 0

        removed = 0
        cutoff = time.time() - self._ttl_seconds

        for path in self._dir.glob("*.json"):
            try:
                data = json.loads(path.read_text())
                if data.get("updated_at", 0) < cutoff:
                    path.unlink()
                    removed += 1
            except Exception:
                pass

        if removed > 0:
            logger.info("Cleaned up %d expired conversations", removed)
        return removed
