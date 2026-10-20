# v6 Implementation Status

Updated: 2026-10-20 (Asia/Seoul)

The repository now contains the first complete incremental implementation of the v6 direction while preserving the legacy prefix commands and Pickle file names.

## Implemented

- `bot_storage.py`: protocol-4 Pickle persistence, same-directory temporary files, fsync, atomic replacement, rotating `.bak.N` generations, corruption quarantine, backup recovery, schema validation, and a process-wide writer lock.
- `moderation_rules.py`: NFKC and zero-width normalization, bounded separator matching, preserved `BAD_ROOTS`, known-command gating, and separate exemption lookup/consumption decisions.
- `bot_ui.py`: four-button public intro view, question/image/moderation-test modals, ephemeral administrator panel, repeated authorization checks, and slash-command registration adapters.
- `main.py`: storage integration, strict moderation matcher, unknown-command moderation path, explicit exemption consumption, persisted typing-reminder cooldowns, four-button typing reminder, asynchronous AI interaction work, slash command sync, and idempotent reconnect tasks.
- XP awards now avoid counting filtered link/profanity messages and achievement bonus XP no longer consumes the normal message cooldown or creates a second message event.
- `.env.example`: required secrets and the v6 administrator ID configuration.
- `bot_metrics.py`: privacy-minimized aggregate UX counters stored in a JSON file, with no prompts, message content, or user IDs.
- `.python-version` and dependency pins target Python 3.13 and discord.py 2.7.1. The original requests/urllib3, PyNaCl, and Pillow pins were replaced after a fresh-environment import failure or Python 3.13 support review.

## Verification

- Fresh Python 3.12.14 virtual environment installed the updated requirements successfully.
- `pip check` passed.
- All dependency imports passed, including discord.py 2.7.1.
- Main module import and token-free Bot construction passed with dummy environment values.
- Slash command registration passed for 시작, 질문, 그림, 내정보, and 관리.
- Discord UI smoke checks passed: exactly four intro buttons in a 2×2 layout and four administrator buttons.
- Offline regression suite passed: 19 tests covering Pickle recovery/concurrency, moderation normalization/command bypass/exemption separation, administrator authorization, privacy-safe metrics, and slash-sync retry behavior.
- State files keep their historical names and Pickle structures. Set `DATA_DIR` to the mounted Northflank volume path (or `STATE_DIR` as an alias) to persist XP, achievements, typing cooldowns, and aggregate metrics; when unset, the historical current-working-directory behavior is retained.

## Still requiring an external environment

- Python 3.13 itself is not installed on the current workstation, so the 3.13 interpreter run is not claimed as complete.
- Discord gateway login, command sync against a real guild, interaction callbacks, Hugging Face API calls, crawler behavior, persistent hosting volume, and 24-hour reconnect behavior require staging credentials and a staging server.
- Existing user Pickle files were not present in the workspace; production backup/restore must be exercised against a copy before rollout.

No database or Pickle-to-JSON migration was added. Existing `!` commands remain registered for backward compatibility.
