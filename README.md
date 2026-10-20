# Discord_AI_bot


## tbBOT3rd keeps your server tidy and lively at once.
• It auto-deletes rule-breaking links or slurs, logs deleted messages, <br>and even warns about gaming addiction – all in a warm, humorous tone.<br>

• It enlarges custom emoji codes ( `:01:`-`:50:` , `:dccon:` ) on the fly, reacts to laughter, <br>highlights trending keywords in the last few messages, <br>and lets users query “!ask <topic>” to get AI-powered answers summarised in 4 Korean lines or less.
<br>Powered by HuggingFace Inference (DeepSeek-R1-Qwen3-8B) and built for seamless Korean/English servers alike.<br>

## Invite blurb (super-short)

“Moderation, emoji magic & AI answers – all in one cute package.”<br>

## Origin code

<a href="https://github.com/diligencefrozen/discordbot-main">https://github.com/diligencefrozen/discordbot-main</a>

## Local setup

The target runtime is Python 3.13. Copy `.env.example` to `.env`, fill in the
Discord and Hugging Face tokens, and keep `SPECIAL_ADMIN_USER_ID` set only for
the confirmed special administrator account. The server owner is always checked
from the current guild at interaction time.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

The legacy `!` commands remain available. The modern entry points are the four
buttons on the typing reminder and the `/시작`, `/질문`, `/그림`, `/내정보`, and
`/관리` slash commands. `daily_xp_data.pkl` and `achievements_data.pkl` remain
the authoritative user-state files; the storage layer adds backups and atomic
replacement without introducing a database.
