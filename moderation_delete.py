"""Dependency-free message deletion telemetry for moderation paths."""

from __future__ import annotations

import logging
from typing import Any


def _metadata(message: Any) -> str:
    guild = getattr(message, "guild", None)
    channel = getattr(message, "channel", None)
    author = getattr(message, "author", None)
    return (
        f"message_id={getattr(message, 'id', None)} "
        f"guild_id={getattr(guild, 'id', 0) if guild is not None else 0} "
        f"channel_id={getattr(channel, 'id', None)} "
        f"user_id={getattr(author, 'id', None)}"
    )


async def delete_message(message: Any, *, reason: str, logger: logging.Logger | None = None) -> bool:
    """Delete a message and emit structured, content-free failure telemetry."""

    logger = logger or logging.getLogger("moderation")
    metadata = _metadata(message)
    try:
        await message.delete()
    except Exception as exc:
        error_type = type(exc).__name__
        normalized_error_type = error_type.lstrip("_")
        status = getattr(exc, "status", None)
        if normalized_error_type == "NotFound":
            logger.info(
                "[MODERATION_DELETE] status=already_gone reason=%s error=%s %s",
                reason,
                error_type,
                metadata,
            )
        elif normalized_error_type in {"Forbidden", "HTTPException"}:
            logger.error(
                "[MODERATION_DELETE_FAILED] reason=%s error=%s status=%s %s",
                reason,
                error_type,
                status,
                metadata,
            )
        else:
            logger.error(
                "[MODERATION_DELETE_FAILED] reason=%s error=%s status=%s %s",
                reason,
                error_type,
                status,
                metadata,
            )
        return False
    logger.info("[MODERATION_DELETE] status=deleted reason=%s %s", reason, metadata)
    return True


__all__ = ["delete_message"]
