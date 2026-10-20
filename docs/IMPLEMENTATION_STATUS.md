# v6 Implementation Status

Updated: 2026-10-20 (Asia/Seoul)

The repository now contains the first complete incremental implementation of the v6 direction while preserving the legacy prefix commands and Pickle file names.

## Implemented

- `bot_storage.py`: protocol-4 Pickle persistence, same-directory temporary files, fsync, atomic replacement, rotating `.bak.N` generations, corruption quarantine, backup recovery, schema validation, and a process-wide writer lock.
- `moderation_rules.py`: NFKC and zero-width normalization, bounded separator matching, preserved `BAD_ROOTS`, known-command gating, and separate exemption lookup/consumption decisions.
- `bot_ui.py`: four-button public intro view, question/image/moderation-test modals, ephemeral administrator panel, repeated authorization checks, and slash-command registration adapters.
- `main.py`: storage integration, strict moderation matcher, explicit moderation for `!ask`/`!img` prompt arguments, explicit exemption consumption, persisted typing-reminder cooldowns, four-button typing reminder, asynchronous AI interaction work, slash command sync, and idempotent reconnect tasks.
- `ai_providers.py`: AI Horde text/image submit-and-poll adapters are the default free provider. Hugging Face remains available only with explicit `AI_PROVIDER=huggingface`; provider failures never trigger a paid fallback.
- v8 emergency hardening: image results from buttons and `/그림` are posted publicly only after generation succeeds, while failures remain private; image bytes are size/format checked and URL hosts are explicit; image requests have a concurrency limit and per-user cooldown.
- v8 UI: typing reminders use short Korean content with a user-only mention, neutral persistent buttons, and a private select menu for feature navigation.
- XP awards now avoid counting filtered link/profanity messages and achievement bonus XP no longer consumes the normal message cooldown or creates a second message event.
- `.env.example`: Discord, AI Horde, optional Hugging Face, and v6 administrator configuration.
- `bot_metrics.py`: privacy-minimized aggregate UX counters stored in a JSON file, with no prompts, message content, or user IDs.
- `.python-version` and dependency pins target Python 3.13 and discord.py 2.7.1. The original requests/urllib3, PyNaCl, and Pillow pins were replaced after a fresh-environment import failure or Python 3.13 support review.

## Verification

- The prior v6 verification used a fresh Python 3.12.14 environment: requirements installation, `pip check`, dependency imports, token-free Bot construction, and slash registration all passed. The current workstation no longer has that environment; the v7 provider transition adds no package requirement, but a fresh dependency-backed import remains a staging check.
- Slash command registration previously passed for 시작, 질문, 그림, 내정보, and 관리.
- Discord UI smoke checks passed: exactly four intro buttons in a 2×2 layout and four administrator buttons.
- Offline regression suite contains 28 tests covering Pickle recovery/concurrency, moderation normalization and AI prompt command checks, administrator authorization, privacy-safe metrics, slash-sync retry behavior, AI Horde submit/poll behavior, image validation, and the v8 UI contract. The bundled minimal runtime passed 23 tests and skipped five provider tests because `httpx` is not installed; the full 28 passed with a deterministic HTTP test double.
- State files keep their historical names and Pickle structures. Set `DATA_DIR` to the mounted Northflank volume path (or `STATE_DIR` as an alias) to persist XP, achievements, typing cooldowns, and aggregate metrics; when unset, the historical current-working-directory behavior is retained.
- CPython 3.13 manylinux dependency resolution passed for all 47 artifacts after pinning Pillow 12.2.0; a clean Python 3.12 environment also passed `pip check`, main import, Bot construction, and slash registration.
- A temporary-volume smoke test verified that a legacy CWD Pickle is copied into `DATA_DIR` without changing its bytes.

## Still requiring an external environment

- Python 3.13 itself is not installed on the current workstation, so the 3.13 interpreter run is not claimed as complete.
- Discord gateway login, command sync against a real guild, interaction callbacks, AI Horde/Hugging Face API calls, crawler behavior, persistent hosting volume, and 24-hour reconnect behavior require staging credentials and a staging server.
- The v8 public-image interaction, persistent-view reattachment after restart, and special-admin environment configuration still require a real staging guild test.
- Existing user Pickle files were not present in the workspace; production backup/restore must be exercised against a copy before rollout.

No database or Pickle-to-JSON migration was added. Existing `!` commands remain registered for backward compatibility.
