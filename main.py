# hf_client.py ─ Discord.py + HuggingFace InferenceClient  (Python 3.9 호환)
import os, asyncio, io, httpx, discord
from discord.ext import commands
from typing import Optional, List
from huggingface_hub import InferenceClient
from dotenv import load_dotenv           # NEW

# ────── 환경 변수 로드 ──────
load_dotenv()                            # .env → os.environ 으로 주입

HF_TOKEN      = os.environ.get("HF_TOKEN")        # 반드시 설정해야 함
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")

if not HF_TOKEN or not DISCORD_TOKEN:
    raise RuntimeError(
        "HF_TOKEN 또는 DISCORD_TOKEN 환경변수가 설정되지 않았습니다.\n"
        "• 로컬 개발: .env 파일에 두 값 작성 후 재실행\n"
        "• 배포: 플랫폼 환경변수 / GitHub Secrets 로 주입"
    )

# ────── 고정 설정 ──────
PROVIDER = "featherless-ai"
MODEL    = "deepseek-ai/DeepSeek-V3-0324"

MAX_MSG  = 1_900        # 메시지 한 덩어리 최대 길이
FILE_TH  = 6_000        # 6k↑면 txt 파일로 첨부

SYS_PROMPT = (
    "You are a concise assistant. "
    "For every query, respond in **Korean** within **4 sentences or fewer**, "
    "highlighting key points only."
)

hf  = InferenceClient(provider=PROVIDER, api_key=HF_TOKEN)
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ────── 헬퍼 ──────
def split_paragraphs(text: str, lim: int = MAX_MSG) -> List[str]:
    parts, buf = [], ""
    for line in text.splitlines(keepends=True):
        if len(buf) + len(line) > lim:
            parts.append(buf); buf = line
        else:
            buf += line
    if buf:
        parts.append(buf)
    return parts

def fix_code(chunks: List[str]) -> List[str]:
    fixed, open_block = [], False
    for ch in chunks:
        if ch.count("```") % 2:
            ch = ("```\n" + ch) if open_block else (ch + "\n```")
            open_block = not open_block
        fixed.append(ch)
    return fixed

# ────── 커맨드 ──────
@bot.command(name="ask", help="!ask <질문>")
async def ask(ctx: commands.Context, *, prompt: Optional[str] = None):
    # 프롬프트 없으면 예시 질문
    if prompt is None:
        prompt   = "애플페이가 뭐야?"
        preface  = ("💡 예시 질문으로 ‘애플페이가 뭐야?’를 보여 드릴게요!\n"
                    "다음부터는 `!ask 질문내용` 형식으로 물어보시면 됩니다.\n\n")
    else:
        preface = ""

    async with ctx.typing():
        try:
            completion = hf.chat.completions.create(
                model=MODEL,
                messages=[
                    {"role": "system", "content": SYS_PROMPT},
                    {"role": "user",   "content": prompt},
                ],
                max_tokens=300,
            )
            answer = preface + completion.choices[0].message.content.strip()

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                answer = (f"⚠️ **404**: `{MODEL}` 모델은 Provider **{PROVIDER}** "
                          "에서 Serverless Inference를 지원하지 않아요.")
            else:
                answer = f"⚠️ HTTP {e.response.status_code}: {e.response.text[:200]}"
        except Exception as e:
            answer = f"⚠️ HF 호출 오류: {e}"

    # 길이·파일 처리
    if len(answer) > FILE_TH:
        io_buf = io.StringIO(answer)
        await ctx.reply("📄 답변이 길어 파일로 첨부했어요!",
                        file=discord.File(io_buf, "answer.txt"))
        return

    for part in fix_code(split_paragraphs(answer)):
        await ctx.reply(part)
        await asyncio.sleep(0.2)

# ────── 봇 상태 설정 ──────
@bot.event
async def on_ready():
    await bot.change_presence(
        status=discord.Status.online,
        activity=discord.Game("!ask로 질문해 보세요!")
    )
    print(f"✅ Logged in as {bot.user} (ID {bot.user.id})")

# ────── 실행 ──────
if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
