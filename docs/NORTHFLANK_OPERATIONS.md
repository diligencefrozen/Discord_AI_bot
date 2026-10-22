# Northflank 운영 절차

갱신: 2026-10-22 (Asia/Seoul). 이 저장소 변경은 실제 Northflank 계정이나 런타임 변수를 자동 변경하지 않습니다.
운영자가 아래 항목만 기존 설정에 병합하고 수정판을 배포해야 합니다.

## 빌드와 상태 보존

1. Python 3.13 빌드팩으로 `requirements.txt`를 설치하고 `worker` 프로세스(`python main.py`)를 실행합니다.
2. 서비스는 한 replica로 유지합니다. 동일 토큰으로 구/신 버전을 동시에 실행하지 않습니다.
3. 영속 볼륨을 `/data` 등에 마운트하고 `DATA_DIR=/data`를 설정합니다. `STATE_DIR`는 하위 호환 별칭이며 `DATA_DIR`가 우선합니다.
4. 배포 전 `daily_xp_data.pkl`, `achievements_data.pkl`, 인접 `.bak.1`~`.bak.3`를 외부에도 백업합니다.
5. 첫 실행은 영속 경로에 없는 기존 CWD Pickle과 백업만 복사합니다. 기존 영속 파일은 덮어쓰지 않습니다.
6. 같은 경로에 `typing_cooldowns.json`, `bot_metrics.json`이 보존됩니다. 금칙어 조회 전용 저장 파일은 없고 SQL/외부 DB도 필요 없습니다.

타이핑 공지는 **사용자×서버별 마지막 성공 전송부터 24시간에 한 번**입니다.
기본 1.3초의 첫 타이머는 연속 타이핑으로 연장되지 않으며 채널 전체 쿨다운은 없습니다.
같은 서버의 채널·스레드는 제한을 공유하고 다른 서버는 독립적입니다.
서버 정보가 있는 구형 키는 서버×사용자로 합치고, `user:<id>` 등 서버 정보가 없는 기록은
기존 24시간이 만료될 때까지만 보수적으로 적용합니다. 신규 전송은 서버별 키로 저장합니다.
전송 중 새 타이핑 이벤트가 와도 전송과 기록 저장을 마무리하여 중복 알림을 막습니다.
먼저 보낸 메시지는 타이머를 취소하고 적격 일반 메시지에 보조 트리거를 적용합니다.
검열·스팸·명령·봇·DM은 보조 안내 대상이 아닙니다. 타이핑만 멈추면 최초 타이머는 한 번 실행됩니다.
타이핑 Gateway 이벤트 도착이나 실제 전송의 1.5초 이내 완료를 보장하지는 않습니다.
`typing_cooldowns.json`을 지우거나 영속 볼륨 없이 재배포하면 기록이 없어지므로,
한 인스턴스·같은 영속 경로를 유지해야 합니다. 디스크 저장 실패 중에도 현재 프로세스의 제한은 유지됩니다.
`event=typing_cooldowns_save_failed`/`typing_cooldowns_load_failed` 로그가 있으면 볼륨·쓰기 권한·파일 상태를 확인하세요.

기존 Pickle 이름·dict 구조·프로토콜과 원자적 저장·백업 원칙을 유지하고 XP 필드를 호환 확장합니다.
Gemini의 로컬 요청 카운터만 메모리에 있어 프로세스 재시작 시 초기화됩니다.

## 기존 환경에 병합할 설정

아래 키만 추가/갱신합니다. 기존 JSON 전체를 교체하지 마세요.

```dotenv
TEXT_AI_PROVIDER=gemini
GEMINI_TEXT_MODEL=gemini-3.5-flash-lite
GEMINI_API_KEY=
GEMINI_TIMEOUT_SECONDS=60
AI_PROVIDER=aihorde
SPECIAL_ADMIN_USER_ID=855749166764654653
SPECIAL_ADMIN_USER_IDS=855749166764654653,491239880005910538
TYPING_INTRO_DELAY_SECONDS=1.3
```

이번 명령 동기화 배포에는 `SLASH_SYNC_ON_STARTUP=1`도 명시합니다. 성공을 확인한 뒤
다음 일반 배포/재시작 설정은 `0`으로 돌립니다. 새 설치나 명령 정의 변경 시에만 다시 `1`을
명시하세요. 미설정 기본값은 `0`으로, 로컬 핸들러는 등록하되 Discord 원격 명령은 갱신하지 않습니다.
동기화 실패 시 재연결로 재시도하지 않으며 원인을 확인한 뒤 의도한 한 번의 재시작으로 재시도합니다.

`GEMINI_API_KEY`의 실제 값은 운영자가 Northflank secret에 직접 입력합니다.
`tbBot` 무료 프로젝트에 연결된 키를 사용하고 Billing은 활성화하지 않습니다.
Gemini API 자체에는 이 코드가 결제 상태를 강제로 잠그는 옵션이 없으므로 실제 계정의 무료 상태는 운영자가 확인해야 합니다.

선택 설정:

| 변수 | 기본값/처리 |
| --- | --- |
| `GEMINI_TIMEOUT_SECONDS` | 운영 권장 60초, 미설정 코드 기본값 30초; 각 HTTP 단계 제한 |
| `GEMINI_RPM_LIMIT` | 전체 프로세스 15회/롤링 60초 |
| `GEMINI_RPD_LIMIT` | 전체 프로세스 500회/태평양 날짜, IANA 시간대가 없으면 보수적 롤링 24시간 |
| `GEMINI_USER_COOLDOWN_SECONDS` | 사용자별 10초 |
| `SLASH_SYNC_ON_STARTUP` | `0` 기본, `1`로 명시한 프로세스 시작에서만 전역 명령 동기화 |
| `SPECIAL_ADMIN_GUILD_IDS` | 비어 있으면 파딱 서버 제한 없음; 쉼표 구분 서버 ID로 제한 가능 |
| `TYPING_INTRO_DELAY_SECONDS` | 1.3초 기본; 유한한 0.1~1.5초만 허용, 잘못된 값은 기본값 |
| `MOD_LOG_CHANNEL_ID` | 선택; 같은 서버의 관리 메타데이터 기록 채널, 본문/사유 원문 없음 |
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
회원 선택·타임아웃/해제·서버 별명/초기화·밴/수동 해제·추방·최근 메시지 정리·슬로우모드를 확인합니다.
각 조치는 대상/기한/사유 확인 후 실행하며 인증과 봇 권한·역할 순서를 다시 검사합니다.
최근 관리 기록은 한도 있는 메모리 버퍼로 재시작하면 초기화됩니다. 운영 상태는 조회용입니다.
native timeout·서버 별명·밴·슬로우모드는 Discord 상태로 유지됩니다.
역할 지급·경고 누적·필터 실시간 편집·채널 잠금·임시 밴 자동 해제·장기 감사 보존은 제공하지 않습니다.

### `/관리` 접근 실패를 네 층으로 구분

1. **명령 등록/게시:** `event=slash_registered count=5`는 로컬 등록입니다. 원격 게시가 필요한 배포에서만
   `SLASH_SYNC_ON_STARTUP=1`로 한 번 시작해 `slash_sync_success`의 이름/개수를 확인하고 `0`으로 복원합니다.
   앱 ID와 봇 ID가 다른 경우 또는 의도한 5개 밖의 원격 명령이 있으면 일괄 게시를 거부합니다.
   원격 조회 실패도 게시를 중단합니다. 다른 명령을 삭제해 해결하지 마세요.
2. **Discord 설치/권한:** 올바른 앱의 서버 설치와 `applications.commands` 범위를 확인합니다.
   서버 설정 → 연동/Integrations → 도리봇 → 명령 권한에서 `/관리`의 사용자·역할·채널 허용을 확인하고,
   문제 채널의 **애플리케이션 명령 사용** 권한도 확인합니다. 사용자 설치만 한 경우 서버 설치를 확인하세요.
   [Discord 공식 명령 권한](https://support-apps.discord.com/hc/en-us/articles/26501869403159-Command-Permissions),
   [설치 범위·명령 컨텍스트](https://docs.discord.com/developers/docs/interactions/slash-commands).
3. **환경:** Northflank의 실제 서비스/배포에 두 ID가 적용됐는지 확인하고 프로세스를 재시작합니다.
   리터럴 따옴표, 전각 쉼표, 연속/끝 쉼표, 빈 항목은 오류입니다. 쉼표 양쪽 공백은 허용합니다.
   `event=admin_configuration`의 유효성·등록 개수(중복 제거 후 2)·서버 범위 개수·사유 코드만 봅니다.
   `SPECIAL_ADMIN_GUILD_IDS`를 설정하면 두 특수관리자에 동일하게 적용됩니다. 소유자는 기존 접근을 유지합니다.
4. **서버 측 인증:** 명령 선택 후 `event=admin_open result=allowed|denied reason=... guild_id=... source=slash`를 확인합니다.
   버튼·모달·확정·실행에서도 인증을 재검사합니다. Discord 관리자 역할 자체는 도리봇 접근 권한을 주지 않습니다.

사진처럼 `/관리`가 일반 메시지로 보이고 `!ask` 추천이 나왔다면 실제 slash interaction이 아닙니다.
클라이언트의 `/` 선택기에서 **도리봇 → 관리**를 선택해 보세요. 코드의 허용 ID 검사만 바꿔서는
Discord가 전달하지 않는 상호작용을 실행할 수 없습니다. 서버 인증 로그가 없으면 앞의 게시·설치·권한부터 확인합니다.
실제 선택기 표시·명령별 Discord 권한·캐시 전파는 로컬 테스트로 검증할 수 없습니다.

### XP 이행과 오늘의 잘못된 기록 선택 정리

활동 XP만 등급을 판단하며 업적 보너스는 별도입니다. 월~목 25 / 금~일 45 XP, 쿨다운 5초,
전설 450 활동 XP이므로 평일 18회 / 주말 10회 적격 메시지가 필요합니다.
총 XP는 `activity_xp + bonus_xp + legacy_xp`이고, 기존 총점은 추정 없이 `legacy_xp`로 보존합니다.
이행 당일 새 활동 XP는 0부터 계산하되 기존 수령/효과/업적은 보존합니다. VIP는 사용자·날짜별 한 번입니다.

기본 조치는 **백업 후 보존 이행**입니다. 부풀려진 오늘의 데이터 정리는 선택 사항입니다.
필요하면 봇을 중지하고 두 Pickle과 `.bak.*`를 날짜가 있는 별도 안전한 경로로 복사하세요.
오프라인에서 정확한 대상 ID의 오늘 일일 XP/업적 항목만 검토·수정한 사본을 준비하고,
총점 보존/초기화·기존 면제권 철회 여부를 운영자가 결정한 후 같은 `PickleStore`로 원자 저장합니다.
원본 전체 삭제나 활동 횟수 추측 복원은 하지 않습니다. 검증한 사본으로 교체하고 한 replica만 시작하세요.
자동 정리 명령은 제공하지 않습니다. 문제가 없다면 자정 일일 리셋까지 보존하는 방법도 있습니다.
영속 볼륨이 없으면 파일과 타이핑 제한은 재배포 시 유실될 수 있습니다. Discord timeout·별명·밴에는 영향이 없습니다.

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

## Gemini 시간 초과와 명령 outdated 진단

실제 운영 로그를 확인하기 전에는 Google 모델 지연·네트워크·연결 풀·프록시 중 원인을 확정하지 않습니다.
`GEMINI_TIMEOUT_SECONDS=60`은 임시 완화이며 속도 개선이나 성공을 보장하지 않습니다.
httpx timeout은 connect/read/write/pool의 단계별 제한이고 전체 요청의 절대 시간 제한이 아닙니다.
기존 RPM/RPD와 사용자 쿨다운은 유지되며 실패한 요청도 예약을 환불하거나 자동 재시도하지 않습니다.

Gemini 호출 로그는 시작 `event=gemini_request_start`와 종료 `event=gemini_request_end`로 남습니다.
`elapsed_ms`는 단조 시계로 잰 해당 호출의 실제 소요 시간이며 `model`, `result`, 가능한 `http_status`를 포함합니다.
시간 초과는 `timeout_phase=connect|read|write|pool|unspecified`로 구분합니다.
`result=quota`는 429/로컬 사용량 제한, `authentication`은 400/401/403,
`model_unavailable`은 404, `server`는 5xx, `network`는 기타 HTTP 전송 실패입니다.
키·프롬프트·시스템 지침·사용자 메시지·응답 원문·예외 원문은 로그에 기록하지 않습니다.

Discord의 `This command is outdated`는 클라이언트/서버 단계에서 나오는 메시지이며,
파딱 권한 부족 안내와 다릅니다. 봇 코드에서 가로채 비공개 안내로 바꿀 수 없고 이 패치로 즉시 해결을 보장하지 않습니다.
한 replica와 구/신 버전 동시 실행 여부를 확인한 뒤 아래 로그를 확인합니다.

- `event=slash_registered scope=global count=5`: 로컬 다섯 명령 등록.
- `event=slash_sync_success scope=global count=5`: Discord 응답의 실제 명령 이름·개수와 Application ID·Bot ID.
- `event=slash_sync_failed`: 예외 종류/HTTP 상태만 기록. 상세 원문이나 토큰은 출력하지 않음.
- `event=slash_sync_skipped reason=not_requested|invalid_configuration`: 의도적으로 원격 쓰기 생략.
- `event=discord_ready`: 실행 중인 Application ID·Bot ID·로컬 등록/동기화 상태.

Developer Portal의 실제 봇 Application ID와 로그의 ID를 비교하고 올바른 서비스에 토큰이 연결됐는지 확인합니다.
ID는 토큰 자체가 아닙니다. 같은 봇을 사용하는 다른 복제 인스턴스가 명령 정의를 덮어쓰는지도 확인하세요.
`setup_hook`에서 명령을 한 번 등록하고 명시적 설정이 있을 때만 전역 `tree.sync()`를 호출합니다.
`on_ready`는 재연결 때 동기화하지 않습니다. 전체 로컬 명령 집합이 정확히 다섯 개인지 확인한 뒤 게시하고,
명령 삭제·`clear_commands`·길드 복사·전역/길드 양쪽 자동 등록은 하지 않습니다.
`/관리`의 `guild_only`는 서버에서만 실행 가능하다는 뜻이며 명령 자체의 등록 범위는 전역입니다.
[공식 sync 설명](https://discordpy.readthedocs.io/en/stable/interactions/api.html#discord.app_commands.CommandTree.sync).

동기화 성공 후에도 stale 캐시가 남으면 Discord 클라이언트를 다시 실행하고 명령을 새로 선택한 뒤 전파를 기다립니다.
반복 배포나 명령 삭제/재생성으로 해결하려 하지 않습니다. `/시작`, `/질문`, `/그림`, `/내정보`, `/관리`를
서버 소유자·두 파딱·일반 사용자로 확인하고, 권한 거부/DM 거부와 기존 활성 목록·페이지·탐지 테스트를 유지합니다.

## 스테이징 검증과 배포

1. 노출된 Discord/Hugging Face 토큰은 기존 값을 폐기하고 새로 발급하여 해당 사용처의 secret을 교체합니다. 이 저장소·로그·채팅에 값을 복사하지 마세요.
2. 자동 CD를 잠시 중단하고 후보 버전 빌드 로그, 한 replica, 영속 경로와 런타임 설정을 확인합니다.
3. 실제 Discord 서버에서 기본 1.3초 타이핑 안내가 하나의 컨테이너로 보이는지 확인합니다.
   한 채널에서 두 사용자가 연속 타이핑해 각자 안내를 받고, 같은 사용자/서버는 24시간 제한되는지 확인합니다.
   다른 서버의 신규 기록은 독립적이어야 합니다. 2×2 버튼·멘션·옛/새 메시지 버튼·재시작 기록도 확인합니다.
4. 일반 채팅·`!ask`·`!img`·질문/그림 모달의 필터를 확인합니다. `병  신`이 서버에서 통과하면 배포 commit·예외 채널·실제 Unicode 공백·실행 경로를 확인합니다. 로컬 통과를 서버 검증으로 대신하지 않습니다.
5. XP 보상/업적 면제권으로 금칙어·링크·동시 위반이 한 번만 소모되는지, 0/1/2회·시간제 면제·허용 채널을 확인합니다.
6. 버튼·전체 메뉴·`/질문`·`!ask`에 한국어/영어 질문을 넣고 Gemini 답변이 요청 채널에 공개되는지 두 번째 일반 계정으로 확인합니다. 질문 원문을 봇이 재게시하지 않는지, 입력 전 공개 안내도 확인합니다.
7. 긴 답변 파일의 전체 내용, 임의 멘션 억제, 쓰기 권한 없음/채널 없음, 429·잘못된 키·차단 응답에서 공개 메시지 0건을 확인합니다. 비공개 완료 확인 실패 후 중복 게시가 없어야 합니다.
8. 그림은 Gemini 키가 없어도 Horde로 공개 게시되는지, 내 성장·전체 메뉴·관리·검열 오류가 비공개인지 확인합니다. 접두사 명령의 기존 오류 안내는 ephemeral을 지원하지 않는 Discord 제약상 채널의 안전한 문구입니다.
9. 위 검증과 토큰 교체를 기록한 후 운영 배포하고 필요한 CD 설정을 복원합니다.

추가 필수 시나리오:

- (a) 두 특수관리자와 서버 소유자가 `/관리`에 들어가고, (b) 일반 사용자·Discord 역할 관리자·DM·범위 밖 계정은 거부됩니다.
- (c) 평일 첫 1/2/3회 적격 입력으로 전설이 되지 않고, (d) 평일 18회 / 금~일 10회에 활동 XP 450과 사용자별 VIP 1회가 됩니다.
- (e) 같은 채널의 다른 사용자가 각각 안내를 받습니다. 전송 실패 후 다시 시도할 수 있어야 합니다.
- (f) 낮은 역할의 테스트 멤버 별명 변경/초기화와 1분 타임아웃/해제를 확인합니다.
- (g) 봇의 해당 권한을 제거하면 Discord 상태를 바꾸지 않고 비공개 오류를 보여야 합니다. 보호 대상도 거부합니다.
- (h) 봇 재시작 후 Discord timeout·서버 별명은 유지되고 메모리 관리 기록은 초기화됩니다.

이 작업은 실제 계정 접근·비밀키 호출·라이브 서버 검증을 수행하지 않았습니다.
로컬 구조/모의 HTTP 회귀 테스트와 실제 타 사용자 열람·무료 계정 상태 검증은 구분해야 합니다.

롤백은 후보 서비스를 먼저 중지하고 같은 볼륨을 유지한 채 이전 commit을 배포합니다.
필요한 경우 검증한 백업으로 Pickle을 복구합니다. 구/신 프로세스를 동시에 실행하지 않습니다.
