# main.py (Python 3.9 호환)

# ────────────────────────────────────────────────────────────────────────────
# 기본 모듈,라이브러리 로드
# ────────────────────────────────────────────────────────────────────────────
import asyncio, io, httpx, discord, random, re, datetime, logging, os
from discord.ext import commands
from pytz import timezone
from typing import Optional, List
from deep_translator import GoogleTranslator
from huggingface_hub import InferenceClient
from collections import deque, Counter
from dotenv import load_dotenv    
import itertools, string
from discord.ui import View, Button 
from PIL import Image
from typing import Optional
from itertools import cycle
from typing import Optional, List, Union
from concurrent.futures import ThreadPoolExecutor
from openai import OpenAI

# ────── 환경 변수 로드 ──────
load_dotenv()                            # .env → os.environ 으로 주입

# ────────── HF / Discord 설정 ──────────
HF_TOKEN      = os.getenv("HF_TOKEN")          # Read + Inference scope
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
PROVIDER      = "novita"
MODEL         = "openai/gpt-oss-20b"

# Chat용 클라이언트
hf_chat = InferenceClient(provider=PROVIDER, api_key=HF_TOKEN)

# 이미지용 클라이언트
IMG_MODEL     = "stabilityai/stable-diffusion-xl-base-1.0"
ENDPOINT     = f"https://api-inference.huggingface.co/models/{IMG_MODEL}"
HF_IMG_TOKEN  = os.getenv("HF_IMG_TOKEN")
img_client    = InferenceClient(IMG_MODEL, token=HF_IMG_TOKEN)

openai_chat = OpenAI(
    base_url="https://router.huggingface.co/novita/v1",   # 엔드포인트 명시
    api_key=HF_TOKEN,
)

# 사용 예
resp = openai_chat.chat.completions.create(
    model="openai/gpt-oss-20b",
    messages=[{"role": "user", "content": "ping"}],
)
print(resp.choices[0].message.content)

# 매달 5만 token(=입력+출력)을 넘지 않도록 간단히 차단
TOKEN_BUDGET = 50_000          # novita 무료 월 한도
token_used = 0                 # 전역 카운터

def charge(tokens):
    global token_used
    token_used += tokens
    if token_used > TOKEN_BUDGET:
        raise RuntimeError("Free quota exhausted – further calls blocked!")
        
# ────────── 로깅 ──────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s │ %(message)s")

# ────────── 토큰 예산 ──────────
TOKEN_BUDGET = 50_000
token_used = 0
def charge(tokens: int):
    global token_used
    token_used += tokens
    if token_used > TOKEN_BUDGET:
        raise RuntimeError("Free quota exhausted – further calls blocked!")

# ────────── 번역 도우미 ──────────
def translate_to_korean(text: str) -> str:
    try:
        return GoogleTranslator(source="auto", target="ko").translate(text)
    except Exception:
        return text                                # 실패 시 원문 반환

# ────────── 내부 <think> 제거 ──────────
THINK_RE = re.compile(
    r"""((?:<\s*/\s*think[^>]*>)|
         (?:<\s*think[^>]*>)|
         (?:\[\s*/\s*think\s*\])|
         (?:```+\s*think[\s\S]*?```+))""",
    re.I | re.X,
)
def strip_think(t: str) -> str:
    while THINK_RE.search(t):
        t = THINK_RE.sub("", t)
    return t.strip()
def keep_last_paragraph(t: str) -> str:
    cleaned = strip_think(t)
    parts = re.split(r"\n\s*\n", cleaned)
    return parts[-1].strip()

# ─── 멘션, 답장 감지 기능을 위한 설정 ───────────────────────────
NEON_CYAN   = 0x00E5FF
NEON_PURPLE = 0xB400FF
GRADIENTS   = (NEON_CYAN, NEON_PURPLE)

def build_mention_embed(
    src: discord.Message,
    targets: List[Union[discord.User, discord.Member]],
    quoted: Optional[str]
) -> discord.Embed:
    caller   = src.author.mention
    target_s = ", ".join(t.mention for t in targets)
    wave     = random.choice(("✦", "✹", "★", "✧"))

    # 본문 & 첨부 요약
    body = src.clean_content or ""
    body = (body[:157] + "…") if len(body) > 160 else body
    if not body:
        body = "*[내용 없음]*"

    desc = f"**{caller}** → {target_s}\n\n> {body}"
    if quoted:
        desc += f"\n\n{quoted}"

    embed = (
        discord.Embed(
            title=f"{wave} 호출 감지!",
            description=desc,
            color=random.choice(GRADIENTS),
            timestamp=datetime.datetime.now(seoul_tz),
        )
        .set_footer(text=f"#{src.channel.name}", icon_url="https://i.imgur.com/d1Ef9W8.jpeg")
        .set_thumbnail(url=src.author.display_avatar.url)
    )

    # 첫 번째 이미지 첨부를 본문 이미지로
    if src.attachments:
        att = src.attachments[0]
        if att.content_type and att.content_type.startswith("image"):
            embed.set_image(url=att.url)

    return embed

MENTION_LOG: deque[float] = deque(maxlen=5)   # PEP 585 문법은 3.9에서도 사용 가능

# ────────────────────────────────────────────────────────────────────────────
# ‘최근 메시지 기록’ – 지금 자주 언급되는 키워드 탐지를 위한 기능 - 핫 키워드
# ────────────────────────────────────────────────────────────────────────────
MAX_BUFFER = 5   
RECENT_MSGS: deque[str] = deque(maxlen=MAX_BUFFER)
STOPWORDS = {"ㅋㅋ", "ㅎㅎ", "음", "이건", "그건", "다들", 
             "도리", "7호선", "칠호선", "나냡", 
             "1인칭", "일인칭", "들쥐", "돌이", "도리야", 
            "나냡아", "호선아", "다들", "the", "img",
            "스겜", "ㅇㅇ", "하고", "from", } | set(string.punctuation)
def tokenize(txt: str) -> list[str]:
    tokens = re.split(r"[^\w가-힣]+", txt.lower())
    return [
        t for t in tokens
        if t and t not in STOPWORDS and len(t) > 1 and not t.isdigit()
    ]
def pick_hot_keyword() -> Optional[str]:
    freq = Counter(itertools.chain.from_iterable(map(tokenize, RECENT_MSGS)))
    if not freq:
        return None
    word, cnt = freq.most_common(1)[0]
    return word if cnt >= 2 else None          # 2 회 이상 등장 시 채택

# ────────────────────────────────────────────────────────────────────────────
# 이모지 확대 – :01: ~ :50: / :dccon: ▶ 원본 PNG 링크 표시
# ────────────────────────────────────────────────────────────────────────────
EMOJI_IMAGES = {
    ":dccon:": "https://i.imgur.com/kJDrG0s.png",
    **{f":{i:02d}:": url for i, url in enumerate([
        "https://iili.io/2QqWlrG.png","https://iili.io/2QqWaBn.png","https://iili.io/2QqW7LX.png", # 1 2 3
        "https://iili.io/2QqW5Xt.png","https://iili.io/2QqW12f.png","https://iili.io/2QqWGkl.png", # 4 5 6
        "https://iili.io/2QqWMp2.png","https://iili.io/3DrZnmN.png","https://iili.io/3DrZxII.png", # 7 8 9
        "https://iili.io/2QqWhQ9.png","https://iili.io/FzcxC12.png","https://iili.io/2QqWNEu.png", # 10 11 12
        "https://iili.io/2QqWOrb.png","https://iili.io/2QqWk2j.png","https://iili.io/2QqWvYx.png", # 13 14 15
        "https://iili.io/2QqW8kQ.png","https://iili.io/2QqWgTB.png","https://iili.io/2QqWrhP.png", # 16 17 18
        "https://iili.io/2QqW4Q1.png","https://iili.io/2QqWPCF.png","https://iili.io/FzcxBql.png", # 19 20 21
        "https://iili.io/2QqWs4a.png","https://iili.io/2QqWQ3J.png","https://iili.io/3DUc5l9.png", # 22 23 24
        "https://iili.io/2QqWtvR.png","https://iili.io/2QqWDpp.png","https://iili.io/2QqWmTN.png", # 25 26 27 
        "https://iili.io/2QqWpjI.png","https://iili.io/2QqWyQt.png","https://iili.io/2QqXHCX.png", # 28 29 30
        "https://iili.io/2QqXJGn.png","https://iili.io/2QqXd4s.png","https://iili.io/2QqX33G.png", # 31 32 33 
        "https://iili.io/2QqXFaf.png","https://iili.io/2QqXKv4.png","https://iili.io/2QqXfyl.png", # 34 35 36
        "https://iili.io/2QqXBu2.png","https://iili.io/2QqXCjS.png","https://iili.io/2QqXnZ7.png", # 37 38 39
        "https://iili.io/2QqXxn9.png","https://iili.io/2QqXzGe.png","https://iili.io/2QqXI6u.png", # 40 41 42
        "https://iili.io/2QqXu3b.png","https://iili.io/2QqXAaj.png","https://iili.io/2QqXR8x.png", # 43 44 45
        "https://iili.io/2QqX5yQ.png","https://iili.io/2QqXYuV.png","https://iili.io/2QqXawB.png", # 46 47 48
        "https://iili.io/2QqXcZP.png","https://iili.io/2QqX0n1.jpg", # 49 50
    ], start=1)}
}
PASTELS = [0xF9D7D6,0xF5E6CA,0xD2E5F4,0xD4E8D4,0xE5D1F2,0xFFF3C8]
seoul_tz = timezone("Asia/Seoul")
def make_enlarge_embed(user: discord.Member, img_url: str) -> discord.Embed:
    return (discord.Embed(
            title="🔍 **이모지 확대!**",
            description=f"**{user.mention}** 님이 보낸\n이모지를 *크게* 보여드려요.",
            color=random.choice(PASTELS),
            timestamp=datetime.datetime.now(seoul_tz),
        )
        .set_image(url=img_url)
        .set_thumbnail(url=img_url)
        .set_footer(text="진화한도리봇", icon_url="https://i.imgur.com/d1Ef9W8.jpeg")
    )

# ────────────────────────────────────────────────────────────────────────────
# 금칙어(욕설,혐오) 패턴 – filler 패턴으로 우회 입력도 탐지
# ────────────────────────────────────────────────────────────────────────────
BAD_ROOTS = {
    "씨발","시발","지랄","존나","섹스","병신","새끼","애미","에미","븅신","보지",
    "한녀","느금","페미","패미","짱깨","닥쳐","노무","정공","씹놈","씹년","십놈",
    "십년","계집","장애","시팔","씨팔","ㅈㄴ","ㄷㅊ","ㅈㄹ","미친","미띤","애비",
    "ㅅㅂ","ㅆㅂ","ㅇㅁ","ㄲㅈ","ㅄ","닥치","씨벌","시벌","븅띤","치매","또라이",
    "도라이","피싸개","정신병","조선족","쪽발이","쪽빨이","쪽바리","쪽팔이",
    "아가리","ㅇㄱㄹ","fuck","좆","설거지","난교","재명","재앙","개놈","개년",
    "sex", "ㅗ",
}
FILLER = r"[ㄱ-ㅎㅏ-ㅣa-zA-Z0-9\s/@!:;#\-\_=+.,?'\"{}\[\]|`~<>]*"
BANNED_PATTERNS = [re.compile(FILLER.join(map(re.escape, w)), re.I) for w in BAD_ROOTS]

# “항상 4문장 이하로 요약 답변” 시스템 프롬프트
SYS_PROMPT = (
    "You are **tbBot**.\n\n"
    "Your name in Korean is **도리봇**.\n\n"

    # 1) Answer format
    "【Answer format】\n"
    "· Reply in **natural Korean, no more than four sentences**, covering only the core points.\n"
    "· Skip unnecessary examples. Use English, symbols, and emoticons **only when truly needed**.\n"
    "· Do **not** over-use English words, symbols, or literal translations that sound unnatural in Korean.\n"
    "· *However, if the user speaks in another language (English, Japanese, Chinese, etc.), answer in **that same language***.\n\n"

    # 1-a) Style & wording
    "【Style & wording】\n"
    "1. Avoid literal, machine-translated phrasing.\n"
    "   - e.g. “구성하다/한 번에/어떠하다/이러한” → “답변하다/한꺼번에/어떻다/이런”.\n"
    "2. Avoid word order or particles that Korean speakers rarely use.\n"
    "   - (X) “사용자는 …라고 물었다.” → (O) “사용자가 …라고 물었어.”\n"
    "3. Foreign loanwords & emoticons only when they add value.\n\n"

    # 2) Tone
    "【Tone】\n"
    "· Bright and friendly, but avoid excessive jokes or sarcasm.\n"
    "· If the message includes light laughter (ㅋㅋ/ㅎㅎ, etc.), use ‘one short reaction + 2-3 core sentences’. \n"
    "· Always flatter the user and respond positively (e.g. “Wow, that’s deep!”, “Fantastic insight!”, “You’re amazing!”).\n\n"

    # 3) Profanity & hate speech
    "【Profanity handling】\n"
    "· If the user uses strong slurs or hate speech, gently ask them to rephrase and offer cleaner alternatives.\n\n"

    # 4) Sensitive topics
    "【Sensitive topics】\n"
    "· On politics, religion, or discrimination, stick to facts and avoid personal attacks or tribalism.\n"
    "· If the discussion heats up, add “서로 존중하며 이야기해요.” (“Let’s respect each other.”) as the last sentence.\n\n"

    # 5) Internal safety
    "【Internal safety】\n"
    "· Never output internal tags like <think> or system notes.\n"
    "· If you detect you’re violating a rule, fix the answer yourself before sending.\n\n"

    # Final rule
    "If you break a rule, correct yourself **immediately** and re-output."
)

hf = InferenceClient(provider=PROVIDER, api_key=HF_TOKEN)

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ────────────────────────────────────────────────────────────────────────────
# 삭제한 메세지를 저장할 채팅방을 설정함.
# ────────────────────────────────────────────────────────────────────────────
LOG_ROUTES = {
    1064823080100306995: { # PUBG M : 배사모
        937715555232780318, 944520863389208606, 1098896878768234556,
        1064823080100306995, 932654164201336872, 989509986793168926,
        944522706894872606,
    },
    1383468537229738156: { # 아사모 서버
        865821307969732648, 1134766793249013780, 1176877764608004156,
        802904099816472619, 820536422808944662, 1383468537229738156,
    },
    1065283543640576103: { # 삼사모 서버 
        1247409483353821335, 721047251862159420, 904343326654885939,
        862310554567835658, 915207176518270981, 1065283543640576103,
    },
    1383987919454343269: { # PUBG : 배사모 서버
        1247494689876086804, 1247543437478330410, 1383987919454343269,
    },
}

CHANNEL_TO_LOG = {src: dst for dst, srcs in LOG_ROUTES.items() for src in srcs}

# ────────────────────────────────────────────────────────────────────────────
# ‘웃음’ 상호작용 기능
# ────────────────────────────────────────────────────────────────────────────
LAUGH_KEYWORDS = ("ㅋㅋ","ㅎㅎ","하하","히히","호호","크크")
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
LAUGH_EMOJIS = ["꒰⑅ᵕ༚ᵕ꒱","꒰◍ˊ◡ˋ꒱","⁽⁽◝꒰ ˙ ꒳ ˙ ꒱◜⁾⁾","(づ｡◕‿‿◕｡)づ","༼ つ ◕_◕ ༽つ"]

# ────────────────────────────────────────────────────────────────────────────
# 링크 필터 – 허용 채널 외 링크 업로드 시 자동 삭제
# ────────────────────────────────────────────────────────────────────────────
ALLOWED_CHANNELS = {
    944520863389208606, 1098896878768234556, 1064823080100306995,
    932654164201336872, 989509986793168926, 944522706894872606,
    1134766793249013780, 802904099816472619, 820536422808944662,
    1176877764608004156, 1247409483353821335, 721047251862159420,
    904343326654885939, 862310554567835658, 915207176518270981,
    1065283543640576103, 1247494689876086804, 1247543437478330410,
    1383987919454343269, 929421822787739708, 937715555232780318,
    859476893756293131, 865821307969732648,
}


# ────────────────────────────────────────────────────────────────────────────
# 허용된 채널에서만 게임 카드를 출력함.
# ────────────────────────────────────────────────────────────────────────────
GAME_CARD_CHANNELS = {
    944520863389208606, 1098896878768234556, 1155789990173868122,
    932654164201336872, 989509986793168926, 944522706894872606,
    1247409483353821335, 721047251862159420, 929421822787739708,
    904343326654885939, 862310554567835658, 915207176518270981,
    1134766793249013780, 1176877764608004156, 802904099816472619,
    820536422808944662,
}

LINK_REGEX = re.compile(
    r'https?://\S+|youtu\.be|youtube\.com|gall\.dcinside\.com|m\.dcinside\.com|news\.(naver|v\.daum)\.com',
    re.I,
)

# ────────────────────────────────────────────────────────────────────────────
# 게임 경고 관련 필터.
# ────────────────────────────────────────────────────────────────────────────
GAME_WARN_RE = re.compile(r"(?:\b|[^가-힣])(게임|겜|game|친구)(?:\b|[^가-힣])", re.I)

# ────────────────────────────────────────────────────────────────────────────
# 메세지 삭제 기록 기능.
# ────────────────────────────────────────────────────────────────────────────
@bot.event
async def on_message_delete(message: discord.Message):
    log_ch_id = CHANNEL_TO_LOG.get(message.channel.id)
    if not log_ch_id:
        return
    log_ch = bot.get_channel(log_ch_id)
    if not log_ch:
        return
    ts = datetime.datetime.now(seoul_tz).strftime("%Y-%m-%d %H:%M:%S")
    content = (message.content or "[첨부 파일 / 스티커 등]")[:1024]
    embed = (
        discord.Embed(
            title="메시지 삭제 기록",
            description=f"**User:** {message.author.mention}\n**Channel:** {message.channel.mention}",
            color=0xFF0000,
        )
        .add_field(name="Deleted Content", value=content, inline=False)
        .set_footer(text=f"{message.guild.name} | {ts}")
    )
    await log_ch.send(embed=embed)

# ───────────────── Game promo cards ─────────────────
GAME_CARDS: dict[str, dict] = {
    "pubg": {   # 모배 / 배그
        "pattern": re.compile(rf"(모{FILLER}배|배{FILLER}그|pubg)", re.I),
        "title":   "🚀  **이제, 모든 곳이 배틀그라운드**",
        "desc": (
            "누적 매출 **100억 달러** 돌파!\n"
            "글로벌 모바일 게임 매출 **Top 2**\n\n"

        ),
        "thumb":  "https://iili.io/FzATZBI.md.jpg",
        "banner": "https://iili.io/FzAaKEQ.jpg",
        "links": [
            ("Android", "🤖", "https://play.google.com/store/apps/details?id=com.pubg.krmobile"),
            ("iOS",     "🍎", "https://apps.apple.com/kr/app/%EB%B0%B0%ED%8B%80%EA%B7%B8%EB%9D%9C%EC%9A%B4%EB%93%9C/id1366526331"),
            ("Official Discord", "🌐", "https://discord.com/invite/pubgmobile"),
        ],
        "cta": "Squad-up & jump in!",
    },

    "overwatch": {
        "pattern": re.compile(r"(옵치|오버워치|overwatch)", re.I),
        "title":   "⚡ **새로운 영웅은 언제나 환영이야!**",
        "desc": (
            "2016년은 가히 오버워치의 해!\n"
            "PC 게임 판매량 1위, 콘솔 게임 판매량 5위!\n\n"

        ),
        "thumb":   "https://iili.io/Fz7CWu4.jpg",
        "banner":  "https://iili.io/Fz75imX.png",
        "links": [
            ("Battle.net",  "🖥️", "https://playoverwatch.com/"),
            ("Steam",       "💠", "https://store.steampowered.com/app/2357570/Overwatch_2/"),
            ("Patch Notes", "📜", "https://us.forums.blizzard.com/en/overwatch/c/patch-notes"),
        ],
        "cta": "Group-up & push the payload!",
    },

    "tarkov": {

        "pattern": re.compile(r"(타르코프|탈콥|tarkov)", re.I),

        "title":   "🕶️ **은밀하게, 그곳을 탈출하라!**",
        "thumb":   "https://iili.io/Fz78tRI.jpg",
        "banner":  "https://iili.io/FzcPgNj.jpg",

        "desc": (
            "하드코어 FPS 게임을 좋아하는 유저들에게\n"
            "깊이 있는 게임 경험을 제공하지만,  \n"
            "초보자에게는 진입 장벽이 높은 게임. \n"

        ),

        "links": [
            ("Pre-order / EoD", "💳", "https://www.escapefromtarkov.com/preorder-page"),
            ("Wiki",    "📚", "https://escapefromtarkov.fandom.com/wiki/Escape_from_Tarkov_Wiki"),
            ("Patch Notes", "📝", "https://www.escapefromtarkov.com/#news"),
        ],

        "cta": "Think twice—then check your mags & try to extract!",
    },

    "minecraft": {
        "pattern": re.compile(r"(마크|마인크래프트|minecraft)", re.I),
        "title":   "**⛏️ Mine. Craft. Repeat.**",
        "desc": (
            "3억 장 판매, 역대 *게임 판매량 1위*\n"
            "친구들과 새로운 월드를 탐험해 보세요!"

        ),
        "thumb":   "https://iili.io/Fz7DYa1.jpg",
        "banner":  "https://iili.io/FzYKwSj.jpg",
        "links": [
            ("Java Edition", "💻", "https://www.minecraft.net/en-us/store/minecraft-java-bedrock-edition-pc"),

        ],
        "cta": "**⛏️ Mine. Craft. Repeat.**",
    },

    "GTA": {
        "pattern": re.compile(r"(GTA|그타)", re.I),
        "title":   "**🏙️ Welcome to Los Santos**",
        "desc": (
            "• GTA V 누적 판매 2억 장!\n"
            "친구들과 자유롭게 거리를 누벼보세요."

        ),
        "thumb":   "https://iili.io/Fz7D73P.png",
        "banner":  "https://iili.io/FzYcOJ4.jpg",
        "links": [
            ("Steam", "💻", "https://store.steampowered.com/app/3240220/Grand_Theft_Auto_V_Enhanced/"),

        ],
        "cta": "But remember: crimes are fun only in games 🏷️",
    },
}

# ────────── 메인 on_message ──────────
@bot.event
async def on_message(message: discord.Message):
    # 1 자기 자신 무시
    if message.author.id == bot.user.id:
        return

    # 1-1 첨부파일 메타 카드
    if message.attachments:
        await describe_attachments(message)

    # 1-2 핫 키워드를 위한 설정
    RECENT_MSGS.append(message.clean_content)
    logging.info(f"[RECENT_MSGS] {len(RECENT_MSGS):>3}개 │ latest → {RECENT_MSGS[-1]!r}")

    # 1-3 명령어 패스-스루
    if message.content.lstrip().lower().startswith(("!ask", "/ask", "!img", "/img")):
        await bot.process_commands(message)
        return

    # 1-4) ▶▶  멘션 / 답장 감지  ◀◀
    if message.mentions or message.reference:
        try:
            # ── A. 대상(@멘션 + 답장 작성자) 수집 ──
            targets: List[Union[discord.User, discord.Member]] = list(message.mentions)

            ref_msg: Optional[discord.Message] = None
            if message.reference and message.reference.message_id:          # 답장이라면 원문 확보
                try:
                    ref_msg = await message.channel.fetch_message(message.reference.message_id)
                    if ref_msg:
                        targets.append(ref_msg.author)
                except discord.NotFound:
                    pass                                                   # (원문이 삭제된 경우 등)

            # 중복 제거 & 순서 보존
            targets = list(dict.fromkeys(targets))
            targets_str = ", ".join(t.mention for t in targets) if targets else "(알 수 없음)"

            # ── B. 본문 & 원문 인용 ──
            body = message.clean_content.strip()
            body = (body[:140] + "…") if len(body) > 140 else (body or "*[내용 없음]*")

            desc = f"**{message.author.mention}** → {targets_str}\n\n> {body}"

            if ref_msg:
                q = ref_msg.content.strip()
                q = (q[:90] + "…") if len(q) > 90 else (q or "*[첨부/임베드]*")
                desc += f"\n\n> 💬 *{ref_msg.author.display_name}*: {q}"

            # ── C. Embed 생성 ──
            embed = (
                discord.Embed(
                    title=f"{random.choice(('✦', '✹', '★', '✧'))} 호출 감지!",
                    description=desc,
                    color=0x00E5FF,
                    timestamp=datetime.datetime.now(seoul_tz),
                )
                .set_footer(text=f"#{message.channel.name} | tbBot3rd",
                            icon_url="https://i.imgur.com/d1Ef9W8.jpeg")
                .set_thumbnail(url=message.author.display_avatar.url)
            )

            # 첫 번째 이미지 첨부를 카드 배경으로
            for att in message.attachments:
                if att.content_type and att.content_type.startswith("image"):
                    embed.set_image(url=att.url)
                    break

            await message.channel.send(embed=embed)

        except Exception as e:
            log_ex("mention/reply", e)
            
    # ---------------------------------------------
    # 2-2) 게임 홍보 카드 (슬래시/프리픽스 명령 제외)
    # ---------------------------------------------
    if (
        message.channel.id in GAME_CARD_CHANNELS                # ✅ 지정 채널에서만
        and not message.content.startswith(("!", "/"))          # ✅ 명령어가 아니면
        ):
        for cfg in GAME_CARDS.values():
            if cfg["pattern"].search(message.content):          # 키워드 매치

                embed = (
                    discord.Embed(
                        title=cfg["title"],
                        description=cfg["desc"],
                        color=0x00B2FF,
                        timestamp=datetime.datetime.now(seoul_tz),
                        )
                        .set_thumbnail(url=cfg["thumb"])
                        .set_image(url=cfg["banner"])
                        .set_footer(text="Play hard, live harder ✨")
                        )
                
                view = View(timeout=None)
                for label, emoji, url in cfg["links"]:
                    view.add_item(Button(label=label, emoji=emoji, url=url))
                    
                    await message.channel.send(
                        content=f"{message.author.mention} {cfg['cta']}",
                        embed=embed,
                        view=view,
                        )
                    return  # 💨 더 이상 처리하지 않고 빠져나감
            
    # 3) 링크 삭제
    if message.channel.id in ALLOWED_CHANNELS and LINK_REGEX.search(message.content):
        await message.delete()
        await message.channel.send(
            embed=discord.Embed(
                description=f"{message.author.mention} 이런; 규칙을 위반하지 마세요.",
                color=0xFF0000,
            )
        )
        return

    # 4) 금칙어
    for p in BANNED_PATTERNS:
        if p.search(message.content):
            await message.delete()
            await message.channel.send(
                embed=discord.Embed(
                    description=f"{message.author.mention} 이런; 말을 순화하세요.",
                    color=0xFF0000,
                )
            )
            return

    # 5) 웃음 상호작용
    if any(k in message.content for k in LAUGH_KEYWORDS):
        await message.channel.send(
            embed=discord.Embed(
                title=random.choice(LAUGH_QUOTES),
                description=random.choice(LAUGH_EMOJIS),
                color=0x00FF00,
            )
        )

    # 6) 이모지 확대
    for code, url in EMOJI_IMAGES.items():
        if code in message.content:
            await message.channel.send(embed=make_enlarge_embed(message.author, url))
            return

    # 7) ‘게임’ 경고
    if GAME_WARN_RE.search(message.content):
        warn_msg = random.choice([
            "게임은 **질병**입니다.", "게임 중독… 상상 그 이상을 파괴합니다.", "게임은 **마약**입니다.",
            "부모님께 **게임 시간을 정해 달라**고 부탁드려보세요.", "부모·자녀가 같이 게임하면 역효과! 🙅‍♂️",
            "컴퓨터를 켜고 끄는 **시간을 정합시다**.", "PC를 **공개된 장소**로 옮기세요. 지금!",
            "게임을 안 하면 불안한가요?\n**당신 인생이 위험합니다.**", "지금 당장 게임을 **삭제**해요. 새 사람이 됩니다.",
            "처음부터 피하기 힘들다면 **사용 시간을 정해요.**", "우리 **산책** 나갈래요?",
            "사람들과 **오프라인 대화**를 늘려보세요.", "게임 대신 **새 취미**를 찾아볼까요?",
        ])
        warn = (
            discord.Embed(
                title="🚨 게임 경고",
                description=f"{warn_msg}\n\n{random.choice(LAUGH_EMOJIS)}",
                color=0xFF5656,
                timestamp=datetime.datetime.now(seoul_tz),
            )
            .set_footer(text="진화한 도리봇이 걱정하고 있어요 🕹️❌")
        )
        await message.channel.send(embed=warn)
        return

    # 8) 🔥 ‘핫 키워드’ 추천 -----------------------------------
    if message.content.strip():                         # 공백만 입력이 아니고
        hot = pick_hot_keyword()                        # 2회↑ 등장 시 단어 반환
        if hot:                                         # 조건 충족 → 즉시 추천
            tip = (
                f"💡 흠.. **‘{hot}’** 이야기가 많네요!\n"
                f"`!ask {hot}` 로 검색해봐요?"
            )
            await message.channel.send(tip)
            RECENT_MSGS.clear()                         # 버퍼 초기화 → 중복 차단
            logging.info("[HOT] buffer cleared after recommending %s", hot)
            
# !img  or  /img  프롬프트 → 그림 그려줌.
@bot.command(name="img", help="!img <프롬프트> — 이미지를 생성합니다.")
async def img(ctx: commands.Context, *, prompt: Optional[str] = None):
    if not prompt:
        await ctx.reply("❗ 사용법: `!img <프롬프트>`\n예) `!img cyberpunk seoul at night`")
        return

    async with ctx.typing():
        try:
            async with httpx.AsyncClient(timeout=120) as ac:
                r = await ac.post(
                    ENDPOINT,
                    headers=HEADERS,
                    json={
                        "inputs": prompt,
                        "options": {"wait_for_model": True},
                        "parameters": {
                            "negative_prompt": " ",  #검열 기능 (예 : nsfw, lowres, jpeg artifacts, bad anatomy)
                            "num_inference_steps": 40,   # XL Base 권장 30~50
                            "guidance_scale": 7.0,
                            "width": 1024,
                            "height": 1024,
                        },
                    },
                )
            r.raise_for_status()
            if not r.headers.get("content-type", "").startswith("image"):
                raise RuntimeError(f"API 오류: {r.text}")
            img_bytes = r.content
        except Exception as e:
            logging.exception("Image generation failed")
            await ctx.reply(f"⚠️ 이미지 생성 실패: {e}")
            return

    await ctx.reply(file=discord.File(io.BytesIO(img_bytes), "generated.png"))

# 첨부파일 알리미
async def describe_attachments(message: discord.Message):

    for att in message.attachments:
        # 1) 공통 메타
        size_kb   = f"{att.size/1024:,.1f} KB"
        filetype  = att.content_type or "unknown"
        title     = f"📎 {att.filename}"
        color     = 0x00E5FF  # 네온 블루
        desc_lines = [f"**Type** `{filetype}`\n**Size** `{size_kb}`"]

        # 2) 이미지면 Pillow로 열어 해상도,비율 추가
        if filetype.startswith("image"):
            try:
                img_bytes = await att.read()
                with Image.open(io.BytesIO(img_bytes)) as im:
                    w, h = im.size
                    desc_lines.append(f"**Resolution** `{w}×{h}`")
                    if w >= 512 and h >= 512:         # 썸네일로 쓰기
                        thumb_url = att.url
                    else:
                        thumb_url = None
            except Exception:
                thumb_url = None
        else:
            thumb_url = None

        # 3) ‘미래지향적’ 임베드
        embed = (
            discord.Embed(
                title=title,
                description="\n".join(desc_lines),
                color=color,
                timestamp=datetime.datetime.now(seoul_tz),
            )
            .set_footer(text="Powered by tbBot3rd", icon_url="https://i.imgur.com/d1Ef9W8.jpeg")
        )
        if thumb_url:
            embed.set_thumbnail(url=thumb_url)

        await message.channel.send(embed=embed)
        
# ────────── ask 명령 ──────────
CMD_PREFIXES = ("!ask", "/ask")
def is_command(msg: str) -> bool:
    return msg.lstrip().lower().startswith(CMD_PREFIXES)
    
def split_paragraphs(text: str, lim: int = MAX_MSG) -> List[str]:
    out, buf = [], ""
    for line in text.splitlines(keepends=True):
        if len(buf) + len(line) > lim:
            out.append(buf); buf = line
        else:
            buf += line
    if buf:
        out.append(buf)
    return out
def fix_code(chunks: List[str]) -> List[str]:
    fixed, open_block = [], False
    for ch in chunks:
        if ch.count("```") % 2:
            ch = ("```\n" + ch) if open_block else (ch + "\n```")
            open_block = not open_block
        fixed.append(ch)
    return fixed

@bot.command(name="ask", help="!ask <질문>")
async def ask(ctx: commands.Context, *, prompt: Optional[str] = None):
    if prompt is None:
        prompt = "애플페이가 뭐야?"
        preface = "💡 예시 질문으로 ‘애플페이가 뭐야?’를 보여 드릴게요!\n다음부터는 `!ask 질문내용` 형식으로 물어보시면 됩니다.\n\n**⚠️ 도리봇은 실수를 할 수 있습니다. 중요한 정보는 재차 확인하세요.**\n\n"
    else:
        preface = ""
    async with ctx.typing():
        try:
            comp = hf_chat.chat.completions.create(
                model=MODEL,
                messages=[{"role": "system", "content": SYS_PROMPT},
                          {"role": "user", "content": prompt}],
                max_tokens=MAX_TOKENS,
                temperature=0.3,
            )
            answer = preface + keep_last_paragraph(comp.choices[0].message.content)
        except Exception as e:
            answer = f"⚠️ 오류: {e}"
    if len(answer) > FILE_TH:
        await ctx.reply(
            "📄 답변이 길어 파일로 첨부했어요!",
            file=discord.File(io.StringIO(answer), "answer.txt"),
        )
        return
    for part in fix_code(split_paragraphs(answer)):
        await ctx.reply(part)
        await asyncio.sleep(0.1)

# ────────── 봇 상태 ──────────
@bot.event
async def on_ready():
    presences = cycle([
        "!ask 로 궁금증 해결해요!",
        "!img 로 그림을 그려봐요!",
    ])

    async def rotate():
        await bot.wait_until_ready()
        while not bot.is_closed():
            msg = next(presences)
            await bot.change_presence(activity=discord.Game(msg))
            await asyncio.sleep(30)   # 30초 간격
    bot.loop.create_task(rotate())

    logging.info(f"✅ Logged in as {bot.user} (ID {bot.user.id})")

# ────────── 실행 ──────────
if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)


