"""Immediate edit moderation and bounded, content-free history scan state.

Only the injected deletion service handles Discord audit content. This module
never persists or logs message bodies, URLs, exception text or rule matches.
History searches filter creation time, not edit time; they are not an index of
all historical edits. A restart loses the bounded exemption/revision ledger.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import logging
import math
import os
import time
from types import SimpleNamespace
from typing import Any, Callable, Mapping


LOGGER = logging.getLogger("edit_moderation")
UTC = timezone.utc


@dataclass(frozen=True)
class PolicyViolation:
    reason: str
    matched_rule: str | None
    link: bool
    profanity: bool


def evaluate_policy(content: str, channel_id: int, *, link_regex: Any,
                    find_badroot: Callable[[str], str | None],
                    allowed_link_channel_ids: Any, exempt_profanity_channel_ids: Any,
                    parent_channel_id: int | None = None) -> PolicyViolation | None:
    """Evaluate the unchanged link/root rules, without consuming rewards.

    Threads inherit their parent's policy exceptions. Both identifiers remain
    eligible so an existing explicit thread exception also continues to work.
    """
    channel_ids = {channel_id, parent_channel_id} - {None}
    link = bool(link_regex.search(content)) and not channel_ids.intersection(allowed_link_channel_ids)
    root = find_badroot(content)
    profanity = bool(root) and not channel_ids.intersection(exempt_profanity_channel_ids)
    if not link and not profanity:
        return None
    return PolicyViolation("link+profanity" if link and profanity else "link" if link else "profanity",
                           root if profanity else None, link, profanity)


def _number(env: Mapping[str, str], key: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(env.get(key, str(default)))
        if not math.isfinite(value) or not minimum <= value <= maximum:
            raise ValueError
        return value
    except (TypeError, ValueError, OverflowError):
        LOGGER.warning("event=edit_config result=default reason=invalid_number key=%s", key)
        return default


def _enabled(env: Mapping[str, str], key: str, default: bool = True) -> bool:
    value = env.get(key, "1" if default else "0").strip()
    if value not in {"0", "1"}:
        LOGGER.warning("event=edit_config result=default reason=invalid_boolean key=%s", key)
        return default
    return value == "1"


@dataclass(frozen=True)
class EditModerationConfig:
    enabled: bool = True
    scan_enabled: bool = True
    interval_hours: float = 72
    lookback_hours: float = 96
    max_messages_per_channel: int = 2000
    concurrency: int = 1
    startup_delay_seconds: float = 45
    max_messages_total: int = 20000
    fetch_timeout_seconds: float = 8
    ledger_ttl_seconds: float = 7 * 86400
    ledger_limit: int = 8192

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "EditModerationConfig":
        env = os.environ if env is None else env
        interval = _number(env, "EDIT_MODERATION_SCAN_INTERVAL_HOURS", 72, 1, 168)
        lookback = _number(env, "EDIT_MODERATION_SCAN_LOOKBACK_HOURS", 96, 24, 720)
        if lookback < interval + 24:
            LOGGER.warning("event=edit_config result=expanded reason=scan_coverage_gap")
            lookback = interval + 24
        return cls(
            enabled=_enabled(env, "EDIT_MODERATION_ENABLED"),
            scan_enabled=_enabled(env, "EDIT_MODERATION_SCAN_ENABLED"),
            interval_hours=interval, lookback_hours=lookback,
            max_messages_per_channel=int(_number(env, "EDIT_MODERATION_SCAN_MAX_MESSAGES_PER_CHANNEL", 2000, 1, 10000)),
            concurrency=int(_number(env, "EDIT_MODERATION_SCAN_CONCURRENCY", 1, 1, 4)),
            startup_delay_seconds=_number(env, "EDIT_MODERATION_SCAN_STARTUP_DELAY_SECONDS", 45, 0, 3600),
            max_messages_total=int(_number(env, "EDIT_MODERATION_SCAN_MAX_MESSAGES_TOTAL", 20000, 1, 100000)),
            ledger_ttl_seconds=max(7 * 86400, (interval + lookback) * 3600),
        )


@dataclass
class ScanStatus:
    last_started: datetime | None = None
    last_finished: datetime | None = None
    messages_examined: int = 0
    edits_seen: int = 0
    violations_found: int = 0
    deleted: int = 0
    forbidden: int = 0
    rate_limited: int = 0
    partial_window: int = 0
    excluded: int = 0
    historical_exemption_unknown: int = 0
    errors: int = 0
    scan_in_progress: bool = False
    interrupted: bool = False
    next_run_at: datetime | None = None


def _aware(value: Any) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _error_code(exc: BaseException) -> str:
    name = type(exc).__name__.lstrip("_")
    if name == "NotFound" or getattr(exc, "status", None) == 404:
        return "not_found"
    if name == "Forbidden" or getattr(exc, "status", None) == 403:
        return "forbidden"
    if getattr(exc, "status", None) == 429:
        return "rate_limited"
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return "timeout"
    return "http_error" if name == "HTTPException" else "internal_error"


class _CurrentMessage(SimpleNamespace):
    async def delete(self) -> None:
        await self.channel.get_partial_message(self.id).delete()


class EditModerationService:
    """Use one service for cached/raw edits and startup/periodic scans.

    Dependencies intentionally expose no XP, AI, typing or warning operations.
    Per-message futures reserve only active work; network operations on unrelated
    messages do not share a global lock. Outcomes contain hashes and IDs only.
    """

    def __init__(self, bot: Any, *, evaluate: Callable[[Any], PolicyViolation | None],
                 delete: Any, consume_pass: Any, has_pass: Any,
                 remember: Any = None, log_channel_ids: Any = (),
                 config: EditModerationConfig | None = None,
                 clock: Any = None, monotonic: Any = None, sleep: Any = None):
        self.bot, self.evaluate, self.delete = bot, evaluate, delete
        self.consume_pass, self.has_pass, self.remember = consume_pass, has_pass, remember
        self.log_channel_ids = log_channel_ids
        self.config = config or EditModerationConfig.from_env()
        self.clock = clock or (lambda: datetime.now(UTC))
        self.monotonic, self.sleep = monotonic or time.monotonic, sleep or asyncio.sleep
        self._outcomes: OrderedDict[tuple, tuple[float, str]] = OrderedDict()
        self._content_outcomes: OrderedDict[tuple, tuple[float, str, datetime | None]] = OrderedDict()
        self._deleted: OrderedDict[tuple, float] = OrderedDict()
        self._latest_seen: OrderedDict[tuple, datetime] = OrderedDict()
        self._latest_fingerprints: OrderedDict[tuple, str] = OrderedDict()
        self._inflight: dict[tuple, asyncio.Future] = {}
        self._fetch_slots = asyncio.Semaphore(2)
        self._task: asyncio.Task | None = None
        self._scan_lock = asyncio.Lock()
        self.status = ScanStatus()
        self.guild_status: dict[int, ScanStatus] = {}
        self.last_event_code = "ready"

    def _keys(self, message: Any) -> tuple[tuple, tuple, tuple]:
        key = (getattr(getattr(message, "guild", None), "id", 0), getattr(message, "id", 0))
        digest = hashlib.sha256(getattr(message, "content", "").encode("utf-8", "surrogatepass")).hexdigest()
        edited = _aware(getattr(message, "edited_at", None))
        return key, (*key, edited.isoformat() if edited else "", digest), (*key, digest)

    def _trim(self) -> None:
        now, limit = self.monotonic(), self.config.ledger_limit
        for cache in (self._outcomes, self._content_outcomes):
            while cache and next(iter(cache.values()))[0] <= now:
                cache.popitem(last=False)
            while len(cache) > limit:
                cache.popitem(last=False)
        while self._deleted and next(iter(self._deleted.values())) <= now:
            self._deleted.popitem(last=False)
        while len(self._deleted) > limit:
            self._deleted.popitem(last=False)
        while len(self._latest_seen) > limit:
            self._latest_seen.popitem(last=False)
        while len(self._latest_fingerprints) > limit:
            self._latest_fingerprints.popitem(last=False)

    def known_outcome(self, message: Any) -> str | None:
        self._trim()
        key, revision, content_key = self._keys(message)
        if self._deleted.get(key, 0) > self.monotonic():
            return "deleted"
        self._deleted.pop(key, None)
        exact = self._outcomes.get(revision)
        if exact and exact[0] > self.monotonic():
            return exact[1]
        if exact:
            self._outcomes.pop(revision, None)
        alias = self._content_outcomes.get(content_key)
        if alias and alias[0] <= self.monotonic():
            self._content_outcomes.pop(content_key, None)
            alias = None
        current_stamp = _aware(getattr(message, "edited_at", None))
        # An alias only reconciles a missing-timestamp duplicate of the latest
        # body. A -> clean B -> A at a NEW timestamp is a new decision; an old
        # consumed pass must not exempt that later revision for a whole week.
        if (alias and (current_stamp is None or alias[2] is None)
                and self._latest_fingerprints.get(key) == content_key[-1]):
            return alias[1]
        return None

    def record_new_outcome(self, message: Any, outcome: str) -> None:
        """Bridge the existing new-message flow without replaying its effects."""
        key, revision, content_key = self._keys(message)
        # Retry transport failures after a short pause; confirmed decisions last
        # across overlapping scans, with a hard memory limit.
        expiry = self.monotonic() + (5 if outcome == "delete_failed" else self.config.ledger_ttl_seconds)
        self._outcomes[revision] = (expiry, outcome)
        self._outcomes.move_to_end(revision)
        self._content_outcomes[content_key] = (expiry, outcome, _aware(getattr(message, "edited_at", None)))
        self._content_outcomes.move_to_end(content_key)
        self._latest_fingerprints[key] = content_key[-1]
        self._latest_fingerprints.move_to_end(key)
        if outcome == "deleted":
            self._deleted[key] = expiry
            self._deleted.move_to_end(key)
        self._trim()

    def _eligibility(self, message: Any, *, permissions: bool = True, require_history: bool = False) -> str | None:
        guild, channel, author = (getattr(message, key, None) for key in ("guild", "channel", "author"))
        if guild is None:
            return "dm"
        if author is None or getattr(author, "bot", False) or getattr(author, "id", 0) == getattr(getattr(self.bot, "user", None), "id", None):
            return "bot_or_unknown_author"
        destinations = self.log_channel_ids() if callable(self.log_channel_ids) else self.log_channel_ids
        if getattr(channel, "id", 0) in destinations:
            return "log_destination"
        channel_type = getattr(getattr(channel, "type", None), "name", str(getattr(channel, "type", "")))
        if channel_type not in {"text", "news", "public_thread", "private_thread", "news_thread"}:
            return "unsupported_channel"
        if permissions:
            try:
                perms = channel.permissions_for(guild.me)
                if not all(getattr(perms, permission, False) for permission in ("view_channel", "manage_messages")):
                    return "forbidden"
                if require_history and not getattr(perms, "read_message_history", False):
                    return "history_forbidden"
            except Exception:
                return "forbidden"
        return None

    async def inspect(self, message: Any, *, trigger: str = "message_edit", enforce_permissions: bool = True) -> str:
        if not self.config.enabled:
            return "disabled"
        excluded = self._eligibility(message, permissions=enforce_permissions, require_history=trigger == "history_scan")
        if excluded:
            self.last_event_code = excluded
            return excluded
        if not isinstance(getattr(message, "content", None), str):
            return "content_unavailable"
        key, _, _ = self._keys(message)
        pending = self._inflight.get(key)
        while pending is not None:
            await asyncio.shield(pending)
            pending = self._inflight.get(key)
        edited_at = _aware(getattr(message, "edited_at", None))
        if edited_at is not None:
            if key in self._latest_seen and edited_at < self._latest_seen[key]:
                return "stale_revision"
            self._latest_seen[key] = edited_at
            self._latest_seen.move_to_end(key)
            self._trim()
        known = self.known_outcome(message)
        if known and known != "delete_failed":
            return "already_deleted" if known == "deleted" else known
        # A failure's five-second ledger entry suppresses duplicate API bursts.
        if known == "delete_failed":
            return known
        future = asyncio.get_running_loop().create_future()
        self._inflight[key] = future
        try:
            violation = self.evaluate(message)
            if violation is None:
                self.record_new_outcome(message, "clean")
                return "clean"
            user_id = message.author.id
            if trigger == "history_scan":
                edited = _aware(getattr(message, "edited_at", None))
                created = _aware(getattr(message, "created_at", None))
                if edited is None or (created is not None and edited <= created):
                    return "historical_exemption_unknown"
                # A scan never spends rewards. Unknown historical unedited
                # exceptions cannot safely be reconstructed after a restart.
                if self.has_pass(user_id):
                    return "exempt"
            elif self.consume_pass(user_id):
                self.record_new_outcome(message, "exempt")
                return "exempt"
            if self.remember is not None:
                self.remember(message)
            try:
                deleted = await self.delete(message, reason=violation.reason,
                                            matched_rule=violation.matched_rule,
                                            trigger=trigger, edited_at=_aware(getattr(message, "edited_at", None)))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_event_code = _error_code(exc)
                LOGGER.warning("event=edit_delete result=failed reason=%s guild_id=%s message_id=%s",
                               self.last_event_code, *key)
                deleted = False
            outcome = "deleted" if deleted else "delete_failed"
            self.last_event_code = "ready" if deleted else "delete_failed"
            self.record_new_outcome(message, outcome)
            return outcome
        except asyncio.CancelledError:
            raise
        except Exception:
            self.last_event_code = "internal_error"
            LOGGER.warning("event=edit_inspect result=failed reason=internal_error guild_id=%s message_id=%s", *key)
            return "internal_error"
        finally:
            self._inflight.pop(key, None)
            if not future.done():
                future.set_result(None)

    async def on_cached_edit(self, before: Any, after: Any) -> str:
        if getattr(before, "content", None) == getattr(after, "content", None):
            return "unchanged_content"
        return await self.inspect(after)

    def _raw_current(self, payload: Any, channel: Any) -> Any | None:
        data, cached = payload.data, getattr(payload, "cached_message", None)
        # discord.py 2.5+ exposes the parsed updated Message on raw events.
        # It is authoritative only for an explicitly supplied content value;
        # partial embed events can otherwise look like empty messages.
        parsed = getattr(payload, "message", None)
        if (isinstance(data.get("content"), str) and parsed is not None
                and getattr(parsed, "id", None) == payload.message_id
                and getattr(getattr(parsed, "guild", None), "id", None) == payload.guild_id
                and getattr(parsed, "content", None) == data["content"]):
            return parsed
        author_data = data.get("author")
        author = getattr(cached, "author", None)
        if author_data:
            try:
                author_id = int(author_data["id"])
            except (KeyError, TypeError, ValueError):
                return None
            author = SimpleNamespace(id=author_id, bot=bool(author_data.get("bot", False)),
                                     display_name=author_data.get("global_name") or author_data.get("username") or str(author_id),
                                     name=author_data.get("username", str(author_id)), mention=f"<@{author_id}>")
        if author is None or "content" not in data or not isinstance(data["content"], str):
            return None
        def metadata(key: str, fallback: str | None = None) -> Any:
            if key not in data:
                return getattr(cached, fallback or key, ()) if cached is not None else ()
            items = []
            for item in data.get(key) or ():
                if not isinstance(item, dict):
                    continue
                values = dict(item)
                for number in ("id", "size", "width", "height"):
                    if values.get(number) is not None:
                        try:
                            values[number] = int(values[number])
                        except (ValueError, TypeError):
                            values[number] = 0
                if key == "attachments":
                    spoiler = str(values.get("filename", "")).startswith("SPOILER_")
                    values["is_spoiler"] = lambda spoiler=spoiler: spoiler
                items.append(SimpleNamespace(**values))
            return items
        # Timestamp is supplied by Discord; no previous body is substituted.
        return _CurrentMessage(id=payload.message_id, channel=channel, guild=channel.guild,
                               author=author, content=data["content"],
                               edited_at=_aware(data.get("edited_timestamp")),
                               created_at=_aware(data.get("timestamp")) or getattr(cached, "created_at", None),
                               attachments=metadata("attachments"), stickers=metadata("sticker_items", "stickers"),
                               embeds=metadata("embeds"))

    async def on_raw_edit(self, payload: Any) -> str:
        if not self.config.enabled:
            return "disabled"
        if not getattr(payload, "guild_id", None):
            return "dm"
        data = getattr(payload, "data", {})
        # Link previews may be the only partial update we observe. They are
        # a signal to fetch the CURRENT body, never evidence for deletion by
        # themselves. Decorative/embed-only updates still avoid network I/O.
        if "content" not in data and "embeds" in data and set(data) <= {"id", "channel_id", "guild_id", "embeds"}:
            embeds = data.get("embeds")
            preview = (isinstance(embeds, (list, tuple)) and any(
                isinstance(item, Mapping) and isinstance(item.get("url"), str)
                and item["url"][:8].lower().startswith(("http://", "https://")) for item in embeds[:10]))
            if not preview:
                return "embed_only"
        channel = self.bot.get_channel(payload.channel_id)
        if channel is None or getattr(getattr(channel, "guild", None), "id", None) != payload.guild_id:
            self.last_event_code = "channel_unavailable"
            return self.last_event_code
        # Without an edit timestamp a content-bearing old payload can arrive
        # after a newer clean edit. Resolve the current server state instead of
        # deleting the newer revision based on an ambiguous stale event.
        current = self._raw_current(payload, channel) if _aware(data.get("edited_timestamp")) is not None else None
        # Permission checks precede even the optional fetch. Unknown author is
        # checked on the fetched current message rather than the partial event.
        probe = SimpleNamespace(guild=channel.guild, channel=channel,
                                author=SimpleNamespace(id=-1, bot=False))
        excluded = self._eligibility(probe, require_history=current is None)
        if excluded:
            self.last_event_code = excluded
            return excluded
        if current is None:
            try:
                async with self._fetch_slots:
                    current = await asyncio.wait_for(channel.fetch_message(payload.message_id),
                                                     timeout=self.config.fetch_timeout_seconds)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_event_code = _error_code(exc)
                LOGGER.info("event=edit_fetch result=skipped reason=%s guild_id=%s message_id=%s",
                            self.last_event_code, payload.guild_id, payload.message_id)
                return self.last_event_code
        return await self.inspect(current)

    def start(self) -> asyncio.Task | None:
        if not self.config.enabled or not self.config.scan_enabled:
            return None
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._schedule(), name="edit-moderation-history-scan")
        return self._task

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _schedule(self) -> None:
        await self.bot.wait_until_ready()
        self.status.next_run_at = self.clock() + timedelta(seconds=self.config.startup_delay_seconds)
        await self.sleep(self.config.startup_delay_seconds)
        while True:
            await self.scan_once()
            self.status.next_run_at = self.clock() + timedelta(hours=self.config.interval_hours)
            await self.sleep(self.config.interval_hours * 3600)

    def _channels(self) -> list[Any]:
        channels, seen = [], set()
        for guild in tuple(getattr(self.bot, "guilds", ())):
            for channel in (*tuple(getattr(guild, "text_channels", ())), *tuple(getattr(guild, "threads", ()))):
                key = (guild.id, channel.id)
                if key not in seen:
                    seen.add(key)
                    channels.append(channel)
        return channels

    async def scan_once(self) -> ScanStatus:
        if self._scan_lock.locked():
            return self.status
        if not self.config.enabled or not self.config.scan_enabled:
            return self.status
        async with self._scan_lock:
            now = _aware(self.clock())
            self.status = ScanStatus(last_started=now, scan_in_progress=True)
            self.guild_status = {}
            stats = self.status
            channels = self._channels()
            counts = {}
            partial_channels = set()
            seen_messages = set()
            excluded_channels = set()
            slots = asyncio.Semaphore(self.config.concurrency)
            recent = now - timedelta(hours=min(48, self.config.lookback_hours))
            oldest = now - timedelta(hours=self.config.lookback_hours)

            def increment(guild_id: int, name: str, amount: int = 1) -> None:
                setattr(stats, name, getattr(stats, name) + amount)
                guild_stats = self.guild_status.setdefault(guild_id, ScanStatus(last_started=now, scan_in_progress=True))
                setattr(guild_stats, name, getattr(guild_stats, name) + amount)

            def partial(guild_id: int, channel_id: int) -> None:
                key = (guild_id, channel_id)
                if key not in partial_channels:
                    partial_channels.add(key)
                    increment(guild_id, "partial_window")

            async def channel_window(channel: Any, after: datetime, before: datetime) -> None:
                guild_id, channel_id = channel.guild.id, channel.id
                probe = SimpleNamespace(guild=channel.guild, channel=channel, author=SimpleNamespace(id=-1, bot=False))
                excluded = self._eligibility(probe, require_history=True)
                if excluded:
                    if (guild_id, channel_id) not in excluded_channels:
                        excluded_channels.add((guild_id, channel_id))
                        increment(guild_id, "forbidden" if excluded in {"forbidden", "history_forbidden"} else "excluded")
                    return
                key = (guild_id, channel_id)
                remaining = self.config.max_messages_per_channel - counts.get(key, 0)
                if remaining <= 0 or stats.messages_examined >= self.config.max_messages_total:
                    partial(guild_id, channel_id)
                    return
                async with slots:
                    fetched = 0
                    try:
                        async for message in channel.history(after=after, before=before, limit=remaining, oldest_first=False):
                            fetched += 1
                            message_key = (guild_id, message.id)
                            if message_key in seen_messages:
                                continue
                            if stats.messages_examined >= self.config.max_messages_total:
                                partial(guild_id, channel_id)
                                break
                            counts[key] = counts.get(key, 0) + 1
                            seen_messages.add(message_key)
                            increment(guild_id, "messages_examined")
                            if getattr(message, "edited_at", None):
                                increment(guild_id, "edits_seen")
                            if self._eligibility(message) is None:
                                violation = self.evaluate(message)
                                if violation is not None:
                                    increment(guild_id, "violations_found")
                                result = await self.inspect(message, trigger="history_scan")
                                if result == "deleted":
                                    increment(guild_id, "deleted")
                                elif result == "historical_exemption_unknown":
                                    increment(guild_id, result)
                            await self.sleep(0)
                        if counts.get(key, 0) >= self.config.max_messages_per_channel or fetched >= remaining:
                            partial(guild_id, channel_id)
                    except asyncio.CancelledError:
                        partial(guild_id, channel_id)
                        raise
                    except Exception as exc:
                        code = _error_code(exc)
                        increment(guild_id, code if code in {"forbidden", "rate_limited"} else "errors")
                        partial(guild_id, channel_id)
                        LOGGER.warning("event=edit_scan result=partial reason=%s guild_id=%s", code, guild_id)
                        await self.sleep(1 if code == "rate_limited" else 0)

            completed = False
            try:
                # First prioritize the last 48h across all channels. The second
                # phase covers the older overlap instead of leaving a 24h gap.
                # Date-to-snowflake conversion has millisecond boundaries;
                # overlap the phases by 1ms and suppress repeated IDs so a
                # message exactly at the 48h split is not silently omitted.
                for after, before in ((recent - timedelta(milliseconds=1), now),
                                      (oldest - timedelta(milliseconds=1), recent + timedelta(milliseconds=1))):
                    if after >= before:
                        continue
                    # Bounded workers avoid hundreds of simultaneously waiting
                    # tasks, even on bots present in hundreds of channels.
                    iterator = iter(channels)
                    async def worker() -> None:
                        for channel in iterator:
                            await channel_window(channel, after, before)
                            await self.sleep(0)
                    await asyncio.gather(*(worker() for _ in range(self.config.concurrency)))
                completed = True
            finally:
                stats.scan_in_progress = False
                stats.interrupted = not completed
                stats.last_finished = _aware(self.clock()) if completed else None
                stats.next_run_at = _aware(self.clock()) + timedelta(hours=self.config.interval_hours)
                for guild_stats in self.guild_status.values():
                    guild_stats.scan_in_progress = False
                    guild_stats.interrupted = not completed
                    guild_stats.last_finished = stats.last_finished
                    guild_stats.next_run_at = stats.next_run_at
            return stats

    def status_text(self, guild_id: int | None = None) -> str:
        stats = self.status if guild_id is None else self.guild_status.get(guild_id, ScanStatus())
        def timestamp(value: datetime | None) -> str:
            return value.astimezone(timezone(timedelta(hours=9))).strftime("%m-%d %H:%M KST") if value else "없음"
        started, finished = timestamp(stats.last_started), timestamp(stats.last_finished)
        next_run = timestamp(stats.next_run_at or self.status.next_run_at)
        return ("🛡️ 검열 상태\n"
                f"수정 메시지 검열: {'활성' if self.config.enabled else '사용 안 함'} · {self.last_event_code}\n"
                f"백스캔: {'사용' if self.config.scan_enabled and self.config.enabled else '사용 안 함'}"
                f" · {self.config.interval_hours:g}시간 간격 / 생성 후 {self.config.lookback_hours:g}시간 범위\n"
                f"마지막 완료: {finished} · {'검사 중' if stats.scan_in_progress else '중단됨' if stats.interrupted else '대기'}\n"
                f"최근 시작: {started} · 다음 예정: {next_run}\n"
                f"검사 {stats.messages_examined} · 수정 {stats.edits_seen} · 위반 {stats.violations_found} · 삭제 {stats.deleted}\n"
                f"부분 채널 {stats.partial_window} · 권한 제한 {stats.forbidden} · 제외 {stats.excluded}"
                f" · 속도 제한 {stats.rate_limited} · 이력 면제 불명 {stats.historical_exemption_unknown} · 오류 {stats.errors}\n"
                "재시작 시 검사 상태·중복/면제 기록 초기화. 비공개 채널 내용은 표시하지 않습니다.\n"
                "조회는 생성 시각 기준: 오프라인 중 96시간보다 오래된 글의 수정은 탐지를 보장하지 않습니다.")


__all__ = ["PolicyViolation", "evaluate_policy", "EditModerationConfig", "EditModerationService", "ScanStatus"]
