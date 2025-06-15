# hf_client.py ─ Discord.py + HuggingFace InferenceClient  (Python 3.9 호환)
import os, asyncio, io, httpx, discord, random, re, datetime
from discord.ext import commands
from typing import Optional, List
from pytz import timezone
from huggingface_hub import InferenceClient
from dotenv import load_dotenv           
from deep_translator import GoogleTranslator

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

# 매달 5만 token(=입력+출력)을 넘지 않도록 간단히 차단
TOKEN_BUDGET = 50_000          # novita 무료 월 한도
token_used = 0                 # 전역 카운터

def charge(tokens):
    global token_used
    token_used += tokens
    if token_used > TOKEN_BUDGET:
        raise RuntimeError("Free quota exhausted – further calls blocked!")

def translate_to_korean(text: str) -> str:
    try:
        return GoogleTranslator(source='auto', target='ko').translate(text)
    except Exception:
        return text  # 실패 시 원문 반환

# --------------------------------------------------------------------------------
# ❶ <think> 블록 제거 + 내부 독백 제거 헬퍼
# --------------------------------------------------------------------------------
THINK_RE = re.compile(
    r"""
    (                
      (?:<\s*\/\s*think[^>]*>)        
    | (?:<\s*think[^>]*>)               
    | (?:\[\s*\/\s*think\s*\])     
    | (?:```+\s*think[\s\S]*?```+)    
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

def strip_think(text: str) -> str:

    while THINK_RE.search(text):
        text = THINK_RE.sub("", text)
    return text.strip()


def keep_last_paragraph(text: str) -> str:

    cleaned = strip_think(text)
    parts = re.split(r"\n\s*\n", cleaned)
    return parts[-1].strip()
    
# ────────── 금칙어 사전 ──────────
BAD_ROOTS = {
    "씨발", "시발", "지랄", "존나", "섹스", "병신", "새끼", "애미", "에미", "븅신",
    "보지", "한녀", "느금", "페미", "패미", "짱깨", "닥쳐", "노무", "정공",
    "씹놈", "씹년", "십놈", "십년", "계집", "장애", "시팔", "씨팔", "ㅈㄴ",
    "ㄷㅊ", "ㅈㄹ", "미친", "미띤", "애비", "ㅅㅂ", "ㅆㅂ", "ㅇㅁ", "ㄲㅈ", "ㅄ"
}

FILLER = r"[ㄱ-ㅎㅏ-ㅣa-zA-Z0-9\s/@!:;#\-\_=+.,?'\"{}\[\]|`~<>]*"

def make_pattern(word: str) -> re.Pattern:
    return re.compile(FILLER.join(map(re.escape, word)), re.IGNORECASE)

BANNED_PATTERNS = [make_pattern(w) for w in BAD_ROOTS]

# ────── 고정 설정 ──────
PROVIDER = "novita"
MODEL    = "deepseek-ai/DeepSeek-R1-0528-Qwen3-8B"

MAX_TOKENS = 512

MAX_MSG  = 1_900        # 메시지 한 덩어리 최대 길이
FILE_TH  = 6_000        # 6k↑면 txt 파일로 첨부

SYS_PROMPT = (
    "너는 도리봇이야. 어떤 질문에도 핵심만 골라 **자연스런 한국어 4문장 이하**로 답해. "
    "영어·기호 없이 간결하게. 내부 추론이나 메모는 절대 노출하지 마."
)

hf  = InferenceClient(provider=PROVIDER, api_key=HF_TOKEN)
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ────── 삭제 로그 채널 매핑 ──────
LOG_ROUTES = {
    1064823080100306995: {937715555232780318, 944520863389208606, 1098896878768234556,
                           1064823080100306995, 932654164201336872,
                           989509986793168926, 944522706894872606}, 
    1383468537229738156: {865821307969732648, 1134766793249013780, 1176877764608004156,
                           802904099816472619, 820536422808944662, 1383468537229738156},
    1065283543640576103: {1247409483353821335, 721047251862159420, 904343326654885939,
                           862310554567835658, 915207176518270981, 1065283543640576103},
}
# 채널별 빠른 조회용 딕셔너리
CHANNEL_TO_LOG = {src: dst for dst, src_set in LOG_ROUTES.items() for src in src_set}

# ────── 웃음 반응 데이터 ──────  
LAUGH_KEYWORDS = ("ㅋㅋ", "ㅎㅎ", "하하", "히히", "호호", "크크")
LAUGH_QUOTES = [
    "보통 사람은 남을 보고 웃지만, 꿈이 있는 사람은 꿈을 보고 웃어요.",
    "행복하기 때문에 웃는 것이 아니라, 웃기 때문에 행복해지는 거죠.",
    "사람은 함께 웃을 때 서로 가까워지는 것을 느낀다네요.",
    "웃음은 전염돼요. 우리 함께 웃읍시다.",
    "웃음은 만국공통의 언어죠.",
    "그거 알아요? 당신은 웃을 때 매력적이에요.",
    "제가 웃음거리라면 친구들이 즐거울 수 있다면 얼마든지 바보가 될 수 있어요.",
    "오늘 가장 밝게 웃는 사람은 내일도 웃을 힘을 얻습니다.",
    "유머감각은 리더의 필수 조건이죠!",
    "웃음은 최고의 결말을 보장하죠.",
    "하루 15번만 웃어도 병원이 한가해질 거예요. 항상 웃으세요!",
    "웃음은 늘 지니고 있어야 합니다.",
    "웃음은 가장 값싸고 효과 좋은 만병통치약이에요.",
]
LAUGH_EMOJIS = [
    "꒰⑅ᵕ༚ᵕ꒱", "꒰◍ˊ◡ˋ꒱", "⁽⁽◝꒰ ˙ ꒳ ˙ ꒱◜⁾⁾", "(づ｡◕‿‿◕｡)づ",
    "༼ つ ◕_◕ ༽つ", "( ･ิᴥ･ิ)", "٩(͡◕_͡◕)", "(///▽///)", "(╯°□°）╯︵ ┻━┻",
    "(っ˘ڡ˘ς)", "ʕ•ᴥ•ʔ", "٩(｡•́‿•̀｡)۶", "ヽ(´▽`)/", "(๑˃̵ᴗ˂̵)و",
]


# ────── 링크 필터링 설정 
ALLOWED_CHANNELS = {
    944520863389208606, 1098896878768234556, 1064823080100306995,
    932654164201336872, 989509986793168926, 944522706894872606,
    1134766793249013780, 802904099816472619, 820536422808944662,
    1176877764608004156, 1247409483353821335, 721047251862159420,
    904343326654885939, 862310554567835658, 915207176518270981,
    1065283543640576103,  
}
LINK_REGEX = re.compile(
    r'https?://\S+'
    r'|youtu\.be'
    r'|youtube\.com'
    r'|gall\.dcinside\.com'
    r'|m\.dcinside\.com'
    r'|news\.(naver|v\.daum)\.com',
    re.IGNORECASE,    
)

# ────── 메시지 삭제 로그 ──────
@bot.event
async def on_message_delete(message: discord.Message):
    log_ch_id = CHANNEL_TO_LOG.get(message.channel.id)
    if not log_ch_id:                       # 로그 대상 아님
        return

    log_channel = bot.get_channel(log_ch_id)
    if not log_channel:
        return

    seoul_time = datetime.datetime.now(timezone("Asia/Seoul")).strftime("%Y-%m-%d %H:%M:%S")
    content = message.content or "[첨부 파일 / 스티커 등]"
    if len(content) > 1024:
        content = content[:1021] + "…"

    embed = discord.Embed(
        title="메시지 삭제 기록",
        description=f"**User:** {message.author.mention}\n**Channel:** {message.channel.mention}",
        color=0xFF0000,
    )
    embed.add_field(name="Deleted Content", value=content, inline=False)
    embed.set_footer(text=f"{message.guild.name} | {seoul_time}")

    await log_channel.send(embed=embed)
    
# ────── on_message: 웃음 반응 + 링크 필터  
@bot.event
async def on_message(message: discord.Message):
    # 1) 봇 자신의 메시지는 무시
    if message.author.id == bot.user.id:
        return

    # 2) 허용 채널에서 링크 감지 시 삭제
    if (
        message.channel.id in ALLOWED_CHANNELS
        and LINK_REGEX.search(message.content)
    ):
        await message.delete()
        warn = discord.Embed(
            description=f"{message.author.mention} 이런; 규칙을 위반하지마세요.",
            color=0xff0000,
        )
        await message.channel.send(embed=warn)
        # 이후 명령 파싱은 불필요(삭제된 메시지이므로) → 조기 return
        return

    # 금칙어 필터
    for pat in BANNED_PATTERNS:
        if pat.search(message.content):
            await message.delete()
            await message.channel.send(embed=discord.Embed(
                description=f"{message.author.mention} 이런; 말을 순화하세요.",
                color=0xff0000))
            return
            
    # 2) 웃음 키워드 반응
    if any(k in message.content for k in LAUGH_KEYWORDS):
        quote = random.choice(LAUGH_QUOTES)
        emoji = random.choice(LAUGH_EMOJIS)
        await message.channel.send(
            embed=discord.Embed(title=quote, description=emoji, color=0x00ff00)
        )

    # 3) 다른 명령 처리 계속
    await bot.process_commands(message)


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

# ────── ask 커맨드 ──────
@bot.command(name="ask", help="!ask <질문>")
async def ask(ctx: commands.Context, *, prompt: Optional[str] = None):
    if prompt is None:
        prompt   = "애플페이가 뭐야?"
        preface  = (
            "💡 예시 질문으로 ‘애플페이가 뭐야?’를 보여 드릴게요!\n"
            "다음부터는 `!ask 질문내용` 형식으로 물어보시면 됩니다.\n\n"
        )
    else:
        preface = ""

    async with ctx.typing():
        try:
            completion = hf.chat.completions.create(
                model       = MODEL,
                messages    = [
                    {"role": "system", "content": SYS_PROMPT},
                    {"role": "user",   "content": prompt},
                ],
                max_tokens  = MAX_TOKENS,
                temperature = 0.3,
            )

            raw_answer = completion.choices[0].message.content
            answer     = preface + keep_last_paragraph(raw_answer)

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                answer = (
                    f"⚠️ **404**: `{MODEL}` 모델은 Provider **{PROVIDER}** 에서 "
                    "Serverless Inference를 지원하지 않아요."
                )
            else:
                answer = f"⚠️ HTTP {e.response.status_code}: {e.response.text[:200]}"
        except Exception as e:
            answer = f"⚠️ HF 호출 오류: {e}"

    # 너무 길면 파일로 전달
    if len(answer) > FILE_TH:
        io_buf = io.StringIO(answer)
        await ctx.reply("📄 답변이 길어 파일로 첨부했어요!", file=discord.File(io_buf, "answer.txt"))
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
