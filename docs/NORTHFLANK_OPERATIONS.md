# Northflank 운영 절차

갱신: 2026-10-21 (Asia/Seoul). 이 저장소 변경은 실제 Northflank 계정이나 런타임 변수를 자동 변경하지 않습니다.
운영자가 아래 항목만 기존 설정에 병합하고 수정판을 배포해야 합니다.

## 빌드와 상태 보존

1. Python 3.13 빌드팩으로 `requirements.txt`를 설치하고 `worker` 프로세스(`python main.py`)를 실행합니다.
2. 서비스는 한 replica로 유지합니다. 동일 토큰으로 구/신 버전을 동시에 실행하지 않습니다.
3. 영속 볼륨을 `/data` 등에 마운트하고 `DATA_DIR=/data`를 설정합니다. `STATE_DIR`는 하위 호환 별칭이며 `DATA_DIR`가 우선합니다.
4. 배포 전 `daily_xp_data.pkl`, `achievements_data.pkl`, 인접 `.bak.1`~`.bak.3`를 외부에도 백업합니다.
5. 첫 실행은 영속 경로에 없는 기존 CWD Pickle과 백업만 복사합니다. 기존 영속 파일은 덮어쓰지 않습니다.
6. 같은 경로에 `typing_cooldowns.json`, `bot_metrics.json`이 보존됩니다. 금칙어 조회 전용 저장 파일은 없고 SQL/외부 DB도 필요 없습니다.

기존 Pickle 이름·구조·프로토콜과 원자적 저장·백업 원칙을 유지합니다.
Gemini의 로컬 요청 카운터만 메모리에 있어 프로세스 재시작 시 초기화됩니다.

## 기존 환경에 병합할 설정

아래 키만 추가/갱신합니다. 기존 JSON 전체를 교체하지 마세요.

```dotenv
TEXT_AI_PROVIDER=gemini
GEMINI_TEXT_MODEL=gemini-3.5-flash-lite
GEMINI_API_KEY=
AI_PROVIDER=aihorde
SPECIAL_ADMIN_USER_ID=855749166764654653
SPECIAL_ADMIN_USER_IDS=855749166764654653,491239880005910538
```

`GEMINI_API_KEY`의 실제 값은 운영자가 Northflank secret에 직접 입력합니다.
`tbBot` 무료 프로젝트에 연결된 키를 사용하고 Billing은 활성화하지 않습니다.
Gemini API 자체에는 이 코드가 결제 상태를 강제로 잠그는 옵션이 없으므로 실제 계정의 무료 상태는 운영자가 확인해야 합니다.

선택 설정:

| 변수 | 기본값/처리 |
| --- | --- |
| `GEMINI_TIMEOUT_SECONDS` | 30초 |
| `GEMINI_RPM_LIMIT` | 전체 프로세스 15회/롤링 60초 |
| `GEMINI_RPD_LIMIT` | 전체 프로세스 500회/태평양 날짜, IANA 시간대가 없으면 보수적 롤링 24시간 |
| `GEMINI_USER_COOLDOWN_SECONDS` | 사용자별 10초 |
| `SPECIAL_ADMIN_GUILD_IDS` | 비어 있으면 파딱 서버 제한 없음; 쉼표 구분 서버 ID로 제한 가능 |
| `HORDE_API_KEY` | 비어 있으면 익명 Horde 키 |
| `HORDE_IMAGE_MODEL` | 선택 이미지 모델, 비어 있으면 기존 worker 선택 |
| `HORDE_IMAGE_HOSTS` | `aihorde.net,stablehorde.net`, 검증한 호스트만 추가 |
| `HORDE_MAX_IMAGE_BYTES` | 8000000 |
| `IMAGE_CONCURRENCY_LIMIT` / `IMAGE_REQUEST_COOLDOWN` | 2 / 20초, 기존 이미지 보호 설정 |

Horde 키 우선순위는 `AI_HORDE_API_KEY` > `HORDE_API_KEY` > 구형 `AIHORDE_API_KEY`입니다.
이미지 모델은 `HORDE_IMAGE_MODEL` > 구형 `AIHORDE_IMAGE_MODEL`입니다.
`AI_PROVIDER`·`AI_BACKEND`는 구형 배포 호환을 위해 남겨도 되지만 질문/그림의 제공자를 바꾸지 않습니다.
질문은 Gemini, 그림은 Horde 고정이며 실패 시 다른 제공자나 유료 모델로 전환하지 않습니다.
과거 `HF_TOKEN`·`HF_IMG_TOKEN`은 이 질문/그림 경로에서 사용하지 않습니다.
실행에 읽는 봇 토큰 이름은 `DISCORD_TOKEN`입니다. 다른 `BOT_TOKEN`·`BOT_TOKENSEC`·`BOT_TOKEN_241201` 등은
별도 서비스의 사용처를 확인한 뒤 관리하고, 배포 설정 전체를 무심코 삭제하지 마세요.

## 파딱 등록 확인

기존 `855749166764654653`와 신규 `491239880005910538`를 실제 런타임의 복수 ID 값에 모두 넣습니다.
구형 단일 ID와 복수 ID는 합집합이며 중복은 제거합니다. 어느 비어 있지 않은 값이라도 잘못되면 파딱 예외 전체를 거부합니다.
미설정도 서버 소유자만 허용합니다. `SPECIAL_ADMIN_GUILD_IDS`는 파딱에 적용하며 소유자의 기존 접근은 유지합니다.
DM·Administrator 역할·이름 변경으로는 권한이 생기지 않습니다.
설정 변경 후 서비스 재시작/재배포까지 해야 신규 파딱 권한이 반영됩니다.

각 계정과 소유자로 `/관리 → ⚙️ 금칙어`의 전체 목록·활성 개수·첫/중간/마지막 페이지·탐지 테스트를 확인합니다.
제3자·역할 관리자·DM·제한 서버에서 거부되는지도 확인합니다. 목록 추가/삭제 기능은 제공하지 않습니다.
관리 로그 조회·운영 설정 변경은 안내용 미지원 상태입니다. 운영 삭제 기록에는 메시지 ID와 메타데이터만 남깁니다.

## 무료 한도와 개인정보

[공식 모델 문서](https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite)의
`gemini-3.5-flash-lite`를 `v1beta/models/...:generateContent`로 호출합니다.
키는 `x-goog-api-key` 헤더에만 전달합니다. 요청 자동 재시도·제공자 fallback은 없습니다.
[공식 요금·데이터 처리](https://ai.google.dev/gemini-api/docs/pricing)에서 무료 제공 여부와 조건을 다시 확인하세요.
질문·시스템 지침은 Google에 전송되며 무료 등급 데이터는 제품 개선에 사용될 수 있습니다.
개인정보나 비밀을 질문에 넣지 않도록 운영 안내를 제공합니다.

운영자가 2026-10-21 확인한 `tbBot` 값은 15 RPM / 250K TPM / 500 RPD입니다.
배포 직전 [AI Studio의 실제 프로젝트 한도](https://aistudio.google.com/rate-limit)를 다시 확인해야 합니다.
30일 15000회는 500×30의 이론값이며 월 정액 할당량이나 보장 질문 수가 아닙니다.
[공식 한도 설명](https://ai.google.dev/gemini-api/docs/rate-limits)에 따라 프로젝트 전체가 한도를 공유하고
일별 한도는 태평양 자정에 초기화됩니다. 로컬 카운터는 다른 앱·replica·재시작·TPM을 통제하지 않습니다.
429·인증 오류·타임아웃·차단/빈 응답은 안전한 오류로 처리하며 재시도하지 않습니다.

## 스테이징 검증과 배포

1. 노출된 Discord/Hugging Face 토큰은 기존 값을 폐기하고 새로 발급하여 해당 사용처의 secret을 교체합니다. 이 저장소·로그·채팅에 값을 복사하지 마세요.
2. 자동 CD를 잠시 중단하고 후보 버전 빌드 로그, 한 replica, 영속 경로와 런타임 설정을 확인합니다.
3. 실제 Discord 서버에서 5초 타이핑 안내가 하나의 컨테이너로 보이는지, 2×2 버튼·멘션·중복 쿨다운과 재시작 후 옛/새 메시지 버튼을 확인합니다.
4. 일반 채팅·`!ask`·`!img`·질문/그림 모달의 필터를 확인합니다. `병  신`이 서버에서 통과하면 배포 commit·예외 채널·실제 Unicode 공백·실행 경로를 확인합니다. 로컬 통과를 서버 검증으로 대신하지 않습니다.
5. XP 보상/업적 면제권으로 금칙어·링크·동시 위반이 한 번만 소모되는지, 0/1/2회·시간제 면제·허용 채널을 확인합니다.
6. 버튼·전체 메뉴·`/질문`·`!ask`에 한국어/영어 질문을 넣고 Gemini 답변이 요청 채널에 공개되는지 두 번째 일반 계정으로 확인합니다. 질문 원문을 봇이 재게시하지 않는지, 입력 전 공개 안내도 확인합니다.
7. 긴 답변 파일의 전체 내용, 임의 멘션 억제, 쓰기 권한 없음/채널 없음, 429·잘못된 키·차단 응답에서 공개 메시지 0건을 확인합니다. 비공개 완료 확인 실패 후 중복 게시가 없어야 합니다.
8. 그림은 Gemini 키가 없어도 Horde로 공개 게시되는지, 내 성장·전체 메뉴·관리·검열 오류가 비공개인지 확인합니다. 접두사 명령의 기존 오류 안내는 ephemeral을 지원하지 않는 Discord 제약상 채널의 안전한 문구입니다.
9. 위 검증과 토큰 교체를 기록한 후 운영 배포하고 필요한 CD 설정을 복원합니다.

이 작업은 실제 계정 접근·비밀키 호출·라이브 서버 검증을 수행하지 않았습니다.
로컬 구조/모의 HTTP 회귀 테스트와 실제 타 사용자 열람·무료 계정 상태 검증은 구분해야 합니다.

롤백은 후보 서비스를 먼저 중지하고 같은 볼륨을 유지한 채 이전 commit을 배포합니다.
필요한 경우 검증한 백업으로 Pickle을 복구합니다. 구/신 프로세스를 동시에 실행하지 않습니다.
