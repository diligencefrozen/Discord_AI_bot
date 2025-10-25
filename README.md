# Discord_AI_bot

## tbBOT3rd keeps your server tidy *and* lively.

• Auto-deletes rule-breaking links or slurs, logs deleted messages,
and even warns about gaming addiction — all in a friendly, humorous tone.

• Enlarges custom emoji codes (`:01:`–`:50:` , `:dccon:`) on the fly, reacts to laughter,
highlights trending keywords in recent chat,
and lets users query `!ask <topic>` to get AI-powered answers in ≤4 Korean lines.

Powered by **HuggingFace Inference (DeepSeek-R1-Qwen3-8B)**,
built for seamless **Korean / English bilingual servers**.

---

## Invite Blurb

> “Moderation, emoji magic & AI answers — all in one cute package.”

---

## Technical Highlights

| Category                     | Implementation                                                                     | Notes                                                                           |
| ---------------------------- | ---------------------------------------------------------------------------------- | ------------------------------------------------------------------------------- |
| **Async event handling**     | Built on `discord.py`’s asynchronous loop (`on_message`, `on_ready`, etc.)         | Handles concurrent user events without blocking.                                |
| **XP / Level system**        | Tracks user activity in memory and writes periodically to disk (JSON).             | Prevents race conditions using **async locks** and **queued write tasks**.      |
| **AI integration**           | HuggingFace Inference API (DeepSeek-R1-Qwen3-8B) for fast bilingual summarization. | Implements **timeout & retry** fallback when the API fails or hits rate limits. |
| **Spam & content filtering** | Frequency-based message checks, banned-word lists, and emoji-spam detection.       | Adaptive thresholds minimize false positives.                                   |
| **Data persistence**         | JSON for lightweight single-file storage.                                          | Modular design allows migration to **SQLite / Redis** for scalability.          |
| **Error handling**           | All async calls wrapped in structured `try/except` blocks with logging.            | Keeps the bot responsive under heavy load.                                      |

---

## Notes

> **Q: How do you prevent race conditions in async XP updates?**
>
> * XP writes are protected by `asyncio.Lock()` and processed through a **write-queue system**, ensuring ordered, atomic updates.

> **Q: How do you handle API call failures or rate limits?**
>
> * Each AI call includes `timeout + retry` logic.
> * After three failures, the bot falls back to cached summaries or a default response template.

> **Q: Why use JSON for data storage?**
>
> * JSON keeps the deployment lightweight and transparent for early testing.
> * The persistence layer is modularized so it can be swapped with **SQLite, Redis, or MongoDB** in a multi-instance environment.

---

## Summary

> A full-stack Discord bot blending **moderation**, **social interaction**, and **AI summarization** — all fully asynchronous and bilingual-ready.
>
> Designed not just to *work*, but to *govern gracefully.*

