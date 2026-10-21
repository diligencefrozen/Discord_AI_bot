# Discord_AI_bot · 도리봇

도리봇은 금칙어·링크 검열, XP·레벨·업적과 면제권, 커스텀 이모지,
AI 질문과 그림 생성을 지원하는 discord.py 봇입니다.
원본: [discordbot-main](https://github.com/diligencefrozen/discordbot-main).

## 실행

대상 런타임은 Python 3.13, Discord 라이브러리는 `discord.py==2.7.1`입니다.
`.env.example`을 `.env`로 복사하고 `DISCORD_TOKEN`, `GEMINI_API_KEY`를 설정하세요.
기존 호스팅의 다른 토큰·설정 전체를 이 예시 파일로 덮어쓰지 마세요.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe main.py
```

새 봇의 첫 실행이나 슬래시 명령 정의가 바뀐 배포에서는 `SLASH_SYNC_ON_STARTUP=1`로
한 번 전역 명령을 게시하세요. 이후 일반 실행에는 `0`을 사용합니다(미설정 기본값도 `0`).
로컬 명령·버튼 등록은 `setup_hook`에서 수행하며 게이트웨이 재연결은 전역 동기화를 일으키지 않습니다.
동기화 실패는 자동 반복하지 않습니다. `/관리`의 `guild_only`는 DM 사용 금지이며,
별도의 특정 서버 명령 등록을 의미하지 않습니다.

`!` 명령은 유지됩니다. `/시작`, `/질문`, `/그림`, `/내정보`, `/관리`와
타이핑 알림의 4개 버튼도 지원합니다. 타이핑 안내문과 2×2 버튼은 Components V2의
하나의 컨테이너에 표시됩니다. 감지 지연 5초와 기존 쿨다운을 유지합니다.

## AI 질문과 그림

질문은 `TEXT_AI_PROVIDER=gemini`, `GEMINI_TEXT_MODEL=gemini-3.5-flash-lite`,
`GEMINI_API_KEY`를 사용합니다. `!ask`, `/질문`, 버튼·전체 메뉴의 질문이 같은
Gemini 함수와 요청 제한기를 사용합니다. 키가 없거나 잘못되면 질문만 안전하게 실패하며,
검열·XP·그림 기능은 계속 사용할 수 있습니다. 자동 재시도나 다른 제공자로의 전환은 없습니다.
[공식 모델 문서](https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite).

응답 시간 초과의 운영 완화값은 `GEMINI_TIMEOUT_SECONDS=60`입니다. 코드의 미설정 기본값은
기존 30초로 유지됩니다. httpx의 connect/read/write/pool 단계별 제한이므로 전체 요청이 반드시
60초 안에 끝난다는 뜻은 아닙니다. 이는 임시 완화이며 원인이 Google 지연·네트워크·프록시 중
무엇인지 증명하지 않습니다. `event=gemini_request_start/end` 로그의 `elapsed_ms`, `result`,
`http_status`, `timeout_phase`로 구분하세요. 로그에는 프롬프트·키·응답 원문을 남기지 않습니다.
질문 인터랙션은 HTTP 호출 전에 비공개 defer하여 Discord의 초기 응답 제한을 지킵니다.

**AI 답변은 요청한 채널에 공개됩니다.** 질문 모달에 제출 전 공개 안내가 표시됩니다.
질문 원문을 별도 재게시하지 않으며, 완료 확인·검열 거부·API 오류는 인터랙션에서 비공개로 표시됩니다.
긴 답변은 전체 UTF-8 텍스트 파일로 공개하여 코드블록이나 내용을 잘라내지 않습니다.
알림 멘션은 요청자에게만 허용합니다. `!ask`는 기존 공개 답변을 유지하며,
접두사 명령의 오류 안내는 Discord의 ephemeral 응답을 지원하지 않아 채널에 안전한 문구로 표시됩니다.
내 성장·전체 메뉴·관리 정보는 계속 비공개입니다.

그림(`!img`, `/그림`, 그림 버튼)은 **항상 AI Horde**를 사용합니다.
`AI_PROVIDER=aihorde`는 기존 배포 호환 설정이며 `AI_BACKEND`도 제공자를 바꾸지 않습니다.
Horde 키가 비어 있으면 기존 익명 키를 사용합니다.
키 우선순위는 `AI_HORDE_API_KEY` > `HORDE_API_KEY` > `AIHORDE_API_KEY`,
이미지 모델은 `HORDE_IMAGE_MODEL` > `AIHORDE_IMAGE_MODEL`입니다.
`HF_TOKEN`·`HF_IMG_TOKEN`은 이 AI 경로에 쓰이지 않으며 마이그레이션 중 기존 환경에서 임의 삭제하지 않습니다.

기본 로컬 방어 한도는 분당 15회·일 500회·사용자별 10초입니다.
`GEMINI_RPM_LIMIT`, `GEMINI_RPD_LIMIT`, `GEMINI_USER_COOLDOWN_SECONDS`로 조정합니다.
메모리 카운터는 재시작 시 초기화되고 실제 프로젝트 한도나 TPM을 대신하지 않습니다.
무료 프로젝트 상태와 현재 한도는 운영자가 [AI Studio](https://aistudio.google.com/rate-limit)에서 확인해야 합니다.
질문과 시스템 지침은 Google에 전송되며, 무료 등급 데이터는 제품 개선에 사용될 수 있습니다.
[가격·데이터 처리 조건](https://ai.google.dev/gemini-api/docs/pricing),
[사용량 제한](https://ai.google.dev/gemini-api/docs/rate-limits).

## 파딱과 금칙어 조회

파딱은 '파란 딱지'의 준말입니다. 런타임 환경에 두 계정을 모두 등록하세요.

```dotenv
SPECIAL_ADMIN_USER_ID=855749166764654653
SPECIAL_ADMIN_USER_IDS=855749166764654653,491239880005910538
```

구형 단일 ID와 복수 ID를 합집합으로 병합하고 중복 정수 ID를 제거합니다.
비어 있으면 서버 소유자만 허용하고, 어느 설정이든 잘못된 값이 있으면 파딱 예외를 모두 거부합니다.
`SPECIAL_ADMIN_GUILD_IDS`는 파딱의 서버 범위를 제한하며 서버 소유자의 기존 접근은 유지합니다.
DM·역할·Administrator 권한·닉네임은 관리 권한을 부여하지 않습니다.
각 버튼·모달·페이지 이동에서 권한을 다시 확인합니다.

`/관리 → ⚙️ 금칙어`는 실제 활성 목록과 `N개`, 이전/다음 페이지를 비공개로 표시합니다.
정확히 같은 원문만 중복 제거하므로 기존 `fuck`, `Fuck`, `FUCK`도 모두 표시됩니다.
목록은 `moderation_rules.BAD_ROOTS` 하나에서 가져오며 추가·삭제 기능은 없습니다.
관리 로그 조회·운영 설정 변경 버튼은 현재 미지원 상태를 안내합니다.

## 데이터와 배포

SQL/외부 DB 없이 `daily_xp_data.pkl`, `achievements_data.pkl`을 유지합니다.
`DATA_DIR`(구형 `STATE_DIR` 호환)에 영속 볼륨을 연결하면 Pickle·백업·타이핑 쿨다운·집계 지표가 보존됩니다.
금칙어와 링크가 함께 위반되어도 메시지/프롬프트당 면제권은 최대 1회 사용합니다.
시간제 `all`·`trial`은 횟수를 줄이지 않습니다. 삭제 기록·운영 로그에는 메시지 원문을 남기지 않습니다.

실제 Northflank 변수 변경·토큰 교체·스테이징 검증은
[운영 절차](docs/NORTHFLANK_OPERATIONS.md), 구현과 검증 범위는
[구현 상태](docs/IMPLEMENTATION_STATUS.md)를 참고하세요.
