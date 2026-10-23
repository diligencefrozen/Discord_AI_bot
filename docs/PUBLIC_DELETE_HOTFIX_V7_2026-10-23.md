# 공용 삭제 기록 긴급 복구 V7 — 2026-10-23 KST

> **이력 문서입니다.** 비공개/권한 불명 소스를 익명 카드로 대체하는 아래 V7 공개 정책은 폐기되었습니다.
> 현재는 [V8 레거시 기본 정보 복원](DELETE_LOG_LEGACY_RESTORE_V8_2026-10-23.md)을 적용합니다.
> 명시 `LOG_ROUTES` 원본은 User·Channel·메시지 ID를 보존하고 본문 공개만 별도 동의로 제어합니다.

## 현재 검증 범위

기준 커밋은 `9f44bd397bd67784c7e7559fe57afbc6d292b3c6`이며 배포 후보는 이 문서와 수정 코드를 함께 포함한 V7 hotfix 로컬 커밋입니다.
로컬 커밋은 Northflank 배포 또는 실서비스 검증 완료를 의미하지 않습니다.
로컬 검증은 Python 3.12.14, 실제 `discord.py==2.7.1` 객체와 격리된 가짜 Discord 전송을 사용합니다.
운영 목표 Python 3.13의 이미지 빌드, Northflank 배포, Gateway 연결 유지, Discord 실전 전송은 이 작업에서 검증하지 못했습니다.
아래 `UNKNOWN`은 실패를 숨긴 성공 표기가 아니라 **실서비스 접근 없이 확인할 수 없는 상태**입니다.

최종 로컬 검증: **415 tests / 0 failures / 0 errors / 0 skipped**, 16.339초.
최종 실행 명령은 `python -m unittest discover -s tests -q`이며, V7 전용 권한 검사 17개·전달 경로 검사 37개·main 생성/import 검사 6개를 포함합니다.
변경한 Python 파일 7개의 `py_compile`과 `git diff --check`도 통과했습니다.
`python -m pip check`는 `No broken requirements found`이며 설치된 Discord 라이브러리 버전은 `2.7.1`입니다.
공개 원본의 상세 카드·검열 원문·비공개 비노출·403/429·중복 제거·실제 모듈 import를 로컬에서 검증했으며,
실제 Discord API의 메시지 송신·Gateway 로그인과 연결 유지는 검증하지 않았습니다.
운영 상태의 메시지 길이도 실제 수정 검열·슬래시 상태 포매터로 확인했습니다.
일반 상태는 1,274자, 카운터를 12자리로 늘린 상태는 1,592자로 Discord 본문 2,000자 제한 안에 있습니다.

| 기존 서버 / 공용 목적지 ID | ID·소스 매핑 보존 | 실물 채널·서버·봇 권한 | 인공 전송 | 공개 메시지 삭제 | 자동 금칙어 삭제 |
| --- | --- | --- | --- | --- | --- |
| 배사모 (PUBG M) / `1064823080100306995` | PASS (로컬) | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN |
| 아사모 / `1383468537229738156` | PASS (로컬) | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN |
| 삼사모 / `1065283543640576103` | PASS (로컬) | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN |
| 배사모 (PUBG) / `1383987919454343269` | PASS (로컬) | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN |

새 서버·기록 채널·DB·외부 저장소를 도입하지 않습니다. `main.py`의 `LOG_ROUTES`와 그 안의 원본 채널 ID를 그대로 사용합니다.
목적지 자체가 소스 집합에 들어 있는 기존 설정도 보존하며, 목적지에서 발생한 삭제는 재귀 기록하지 않습니다.

## 재현된 공개 채널 오판

실물 `discord.py`의 공개 `TextChannel`에 역할 캐시에 없는 역할 ID의 `send_messages=False` overwrite가 있으면,
`channel.overwrites`는 해당 대상을 `discord.Object(type=discord.Role)`로 반환합니다.
이 overwrite에는 `view_channel` 변경이 없고 실제 `@everyone` 실효 열람도 원본·목적지 모두 허용됩니다.
기존 판정은 캐시에서 찾지 못한 역할이라는 이유로 예외를 발생시키고 이를 `source_permissions_unavailable`로 뭉갰습니다.
그 결과 공개 원본에도 상세 카드 대신 익명 제한 카드가 선택되는 구성을 로컬에서 재현했습니다.
또한 실물 API의 미캐시 회원 overwrite 대상은 `discord.Object(type=discord.abc.User)`입니다.
기존의 `target.type is discord.User` 비교는 이 실제 타입을 알아보지 못해 동일한 포괄 오류로 처리했습니다.

V7은 채널 보기와 무관한 overwrite를 제외하고, 실제 `Role`·`Member`와 타입을 가진 `discord.Object`를 구분합니다.
`@everyone`, 역할의 allow/deny 조합, 개별 회원 overwrite, 서버 소유자·Administrator 예외와 카테고리 동기화의 실효 범위를 비교합니다.
알 수 없는 역할의 채널 보기 deny처럼 증명이 불충분한 조건은 상세 전송을 허용하지 않습니다.
`Guild.me` 누락은 원본 공개 여부와 분리하고 봇 자신의 Member만 REST로 재조회합니다.
채널 권한 변경/삭제 이벤트는 REST 채널 캐시를 무효화하므로, 공개에서 비공개로 바뀐 원본에 오래된 공개 판정을 적용하지 않습니다.

구조화된 판정은 `status`, `reason_code`, `stage`, 선택적 `exception_type`을 제공합니다.
실제 송신 경로는 구조화된 `evaluate_public_destination`을 사용합니다.
기존 문자열 API `public_destination_reason`은 외부 호출의 하위 호환 래퍼로 보존합니다.
권한이 확인된 공개 원본은 기존 `build_deletion_embed`로 작성자·원본 채널·KST 삭제 확인 시각·원문·검열 사유·금칙어·미디어를 전송합니다.
실제 비공개 또는 확인 불가 원본은 작성자·원문·원본 채널·첨부·ID를 감춘 제한 카드만 전송하고 서로 다른 원인 코드를 남깁니다.
익명 카드에도 `private_source`와 `unknown_permissions`를 구분해 표시하되 원본의 세부 정보는 포함하지 않습니다.

## 전달 경로와 결과의 의미

`on_message → remember`가 원문 스냅샷을 확보합니다. 일반 삭제·RAW·bulk는 삭제 이벤트 어댑터를 통해 같은 서비스로 들어갑니다.
봇 검열은 `safe_delete → delete`가 Discord 삭제 호출 전에 원문과 판정 사유를 확보하며,
수정 검열은 `on_raw_message_edit/on_message_edit → EDIT_MODERATION → safe_delete`로 수정 후 원문을 사용합니다.
주기 재검사와 숨겨진 cleanup 내부 서비스도 기존 공통 삭제 서비스를 유지합니다.
캐시와 REST로 기존 목적지의 정확한 ID·서버·권한을 검증한 뒤 API 송신이 실제 반환한 경우에만 전달 완료로 예약합니다.

| 결과 | 의미 |
| --- | --- |
| `sent_full` | 권한 검증 통과 + 원문을 확보한 상세 삭제 카드의 API 송신 성공 |
| `sent_metadata_only` | 권한 검증 통과 + 캐시 만료/미확보 또는 원문 표시 비활성화 등으로 완전 원문을 표시하지 않은 카드 송신 성공 |
| `sent_restricted` | 비공개 또는 권한 확인 불가로 비식별 제한 카드 송신. 상세 감사 성공에 합산하지 않음 |
| `failed` | 목적지/권한/임베드/HTTP/제한 초과 등으로 감사 전달 실패. 봇 검열 삭제 성공과 분리 |
| `skipped` | 기록 채널·봇 메시지·중복 등 제외. 운영 상태의 제외/누락 카운터를 상세 성공과 분리 |

`send_test`는 원문 없는 인공 카드의 수신 채널 송신 검사입니다. 실제 삭제 집계·마지막 상세 송신 시각을 갱신하지 않습니다.
`/관리 → 운영 상태`는 현재 서버·목적지 ID, 캐시/REST 복구, 봇 실효 권한, 세 배포 플래그,
다섯 결과 카운터·드롭, 최근 원인 코드·판정 근거·단계, 마지막 상세 송신 KST와 인공 테스트 결과를 표시합니다.
실제 공개 원본에 `sent_restricted`가 도착하면 운영상 장애가 남아 있는 것입니다.

대량 요청은 동시 발송·대기 작업·중복 원장·스냅샷 캐시 한도를 유지합니다.
기본 대기 한도는 64건, 동시 발송은 4건입니다. 스냅샷은 256개/600초, 전달 중복 원장은 4096개/600초,
REST 채널 캐시는 64개/30초입니다. 외부 저장소나 무제한 작업을 추가하지 않습니다.
일시적 송신 실패의 재시도는 기본 2회 추가(최대 송신 3회)로 제한하고 403은 재시도하지 않습니다.
429는 명시적 Retry-After가 유한한 0~30초인 경우에만 그 시간 이상 기다립니다. 더 긴 값이나 누락된 값은 짧게 잘라 재시도하지 않고 실패로 남깁니다.
재시도 대기 후에는 원본·목적지·봇 권한을 다시 증명합니다. 채널·카테고리 변경/삭제로 무효화한 ACL 모델을 재사용하지 않습니다.
한도 때문에 처리하지 못한 이벤트는 `burst_limit` 등 원인과 제외/드롭 통계로 확인합니다.
완료 전에 프로세스가 종료되면 메모리 큐와 스냅샷을 복구할 수 없습니다.

## 자동 검증 조건 T01–T22

테스트는 `python -m unittest discover -s tests -v`로 실행합니다. 다음 표의 통과는 **로컬 동작 검증**이며 실서비스의 성공을 의미하지 않습니다.
`tests/test_deletion_permissions_v7.py`는 실물 Discord 모델을, `tests/test_deletion_pipeline_v7.py`는 서비스 발송 경로를 검증합니다.
`tests/test_deletion_main_contract.py`는 생성 어댑터 AST 검사와 **실제 main.py 전체 import·봇 객체 생성**을 함께 수행합니다.
실제 import는 별도 프로세스에서 임시 CWD/상태 경로, 가짜 토큰, 비밀값을 배제한 환경변수, mock dotenv와 차단한 소켓을 사용합니다.
이 검사는 Gateway 로그인·연결을 실행하지 않으므로 실서비스 기동 확인을 대체하지 않습니다.

| 번호 | 자동 검사 대상과 성공 조건 |
| --- | --- |
| T01 | 공개 원본 → 기존 공용 목적지: `sent_full` 및 작성자·채널·원문 |
| T02 | 실제 카테고리 동기화 공개 원본: `sent_full` |
| T03 | 부분 overwrite / 미캐시 역할 / 역할 조합: 읽기 포함관계대로 판정 |
| T04 | `Guild.me` 누락: 봇 Member REST 복구 또는 구체적 실패, 익명 성공으로 오인 금지 |
| T05 | 비공개 원본 → 더 넓은 목적지: 원문·작성자·채널·첨부 비노출 |
| T06 | `시321133발` 봇 검열: 단일 상세 원문·감지 금칙어·봇 삭제 사유 |
| T07 | `1` → `시발` 수정: 수정 후 원문·수정 검열 정보, XP/AI/타이핑 재실행 없음 |
| T08 | 이모지·첨부·스티커·GIF: 실제 존재하는 원문·메타데이터만 표시 |
| T09 | 일반/RAW 중복: 송신 1회 |
| T10 | 원문 미확보 RAW: `sent_metadata_only`, 원문/삭제 사유 생성 금지 |
| T11 | 송신 HTTP 403: `failed`, 검열 삭제 결과와 분리 |
| T12 | 429/대량 삭제: bounded retry/동시성/실패·드롭 집계 |
| T13 | 기록 목적지 자체의 메시지 삭제: 재귀 송신 없음 |
| T14 | 목적지 gateway 캐시 누락: 정확한 기존 ID REST 조회 후 `sent_full` |
| T15 | 단일 기존 목적지가 있는 서버의 신규 공개 원본: 올바른 목적지 송신 |
| T16 | 네 목적지와 전체 소스 집합 원형 보존, 다른 서버로 유출 없음 |
| T17 | 초장문/과도한 메타데이터: Discord embed 한도와 생략 고지 |
| T18 | everyone·사용자 멘션·Markdown·코드·위험 링크: ping 없음·안전한 문자 표시 |
| T19 | 봇의 자동 검열 카드 삭제: 추가 감사 카드 없음 |
| T20 | 실제 main.py 전체 import·봇 객체 생성 및 서비스 생성자 호환, 구형 서비스는 안전한 `constructor_incompatible` 사전 진단; Gateway는 미실행 |
| T21 | `send_test`: 합성 송신과 실제 상세 삭제 통계/시각 분리 |
| T22 | 검열 삭제 API 성공 + 감사 송신 실패: 제재 성공 유지·실패 사유·후속 재시도 가능 |

기존 회귀 검사는 `/관리` 파딱 두 ID·소유자 인증, 공개 명령 `/시작 /질문 /그림 /내정보`, 숨김 cleanup의 내부 코드,
수정 즉시 검열·72시간 주기/96시간 범위 검사, XP 보너스 중복 방지, AI·타이핑·기존 버튼·면제권 정책을 함께 확인합니다.

수정 전 기존 삭제 감사 검사 64개에는 이미 3개 실패가 있었습니다.
비공개/미확인 원본의 무송신 기대와 실제 익명 카드 송신의 차이, 전송 테스트의 목적지 ID 불일치 사유 차이였습니다.
이와 별도로 실제 Discord 객체의 공개 채널 false negative를 먼저 재현했습니다.
기존 정책 검사는 비공개 원문·작성자·채널 정보가 익명 카드에 없고 `sent_restricted`이며 `sent_full=0`인지 확인하도록 보강했습니다.

## Northflank 배포와 네 서버 실전 확인

1. 운영 이미지/커밋과 후보 커밋을 기록하고 `main.py`, `deletion_audit.py`, 관련 모듈·테스트·문서를 같은 배포에 포함합니다.
   생성자 검사는 누락된 `channel_fetcher` 등을 무시한 채 구형 서비스로 시작하지 않습니다.
   `requirements.txt`의 `discord.py==2.7.1`과 Python 3.13 이미지로 빌드하고 `python main.py`, 단일 replica로 실행합니다.
   기존 XP/업적/타이핑 상태와 영속 볼륨, Gemini/Horde 설정은 보존합니다.
2. Northflank 환경변수에 `DELETION_PUBLIC_LOG_ENABLED=1`, `DELETION_PUBLIC_LOG_INCLUDE_CONTENT=1`,
   `DELETION_LOG_ALL_GUILD_CHANNELS=1`을 병합합니다. 코드나 `.env.example` 수정만으로 운영 변수가 바뀌지 않습니다.
   `DISCORD_TOKEN`은 기존 secret을 사용하고 값을 출력하지 않습니다.
3. Discord Developer Portal에서 운영 봇의 **MESSAGE_CONTENT privileged intent** 활성화를 확인합니다.
   코드의 `intents.message_content=True`와 별개입니다. 원문이 빈 콘텐츠인지 수신 권한 문제인지 실전 원문으로 구별합니다.
4. 위 네 ID 각각에 대해 봇이 실제 텍스트 목적지와 올바른 소속 서버를 조회하는지 확인합니다.
   봇 자신의 `View Channel`, `Send Messages`, `Embed Links`와 원본 접근을 확인합니다.
   이력 조회/주기 검사에는 해당 원본에서 `Read Message History`도 확인합니다. 카테고리·역할·회원 overwrite를 검사합니다.
   파딱 개인에게 Administrator를 부여하지 않습니다.
5. 시작 로그의 `deletion_audit_startup`, `deletion_audit_destination_probe`와 `/관리 → 운영 상태`를 대조합니다.
   목적지 누락/다른 서버/봇 권한 부족이면 정확한 목적지 ID·서버 ID·단계·원인 코드로 기록합니다.
6. **네 서버 각각**에서 `/관리 → 삭제 로그 전송 테스트`를 1회 실행하고 기존 목적지에 실제 카드가 도착했는지 확인합니다.
   이것만 통과하면 `합성 전송 PASS / 실제 삭제 UNKNOWN`입니다.
7. 각 서버 일반 공개 원본에 테스트 원문을 작성하고 직접 삭제합니다. 작성자·채널·KST 시각·원문·메시지 ID가 있는 상세 카드 1건과
   `result=sent_full` 이벤트를 확인합니다. 수정·RAW·bulk는 테스트 서버에서도 추가 확인합니다.
8. 같은 공개 원본에서 `시321133발`을 보내고 봇이 삭제한 뒤 원문·금칙어 `시발`·봇 직접 삭제 사유가 상세 카드 1건에 표시되는지 확인합니다.
   `1` → `시발` 수정도 반복하고 수정 후 원문인지 확인합니다. 기존 신규 공개 채널과 비공개 채널도 별도 확인합니다.
9. 공개 삭제가 제한 카드라면 PASS로 바꾸지 말고 `status/reason_code/stage/exception_type`, 서버·원본·목적지 ID를 수집합니다.
   원문·CDN URL·토큰·전체 예외 문자열/traceback은 운영 로그나 이슈에 복사하지 않습니다.
10. 네 행의 UNKNOWN을 실증한 결과로만 교체합니다. Gateway 연결 유지·재접속·재배포 이후를 확인하고 운영 커밋을 기록합니다.

## 남은 실서비스 오류의 판독

| 상태 / 원인 | 의미와 다음 확인 |
| --- | --- |
| `constructor_incompatible` | 서로 다른 버전의 main/service 배포. 두 파일을 같은 이미지에 포함 |
| `DESTINATION_MISSING` / `destination_missing` | 기존 ID 조회 불가. 봇 초대 서버·실제 ID·채널 접근 확인 |
| `DESTINATION_FORBIDDEN` / `destination_bot_view_channel`, `destination_bot_send_messages`, `destination_bot_embed_links` | 도리봇 실효 권한 확인 |
| `SOURCE_GUILD_MISMATCH` / `source_guild_mismatch`, `destination_guild_mismatch` | 설정/REST 객체의 실제 소속 서버가 다름. 다른 서버로 송신 금지 |
| `SOURCE_MISSING` / `source_not_found` | 원본 객체 조회 불가. 원본 삭제/봇 접근/REST 사유 확인 |
| `PRIVATE_VERIFIED` | 독자 범위가 더 넓어 상세 공개 불가. 비공개 원본을 공개로 전환해 해결하지 않음 |
| `UNKNOWN_PERMISSIONS` | 권한 증명 정보 불완전. 구체적 reason·stage·예외 타입을 확인 |
| `role_cache_incomplete`, `source_member_roles_unavailable` | `UNKNOWN_PERMISSIONS`의 세부 근거. 실제 역할/회원 캐시·overwrite 범위를 확인 |
| `source_reader_scope_exceeded`, `source_member_scope_exceeded` | `PRIVATE_VERIFIED`의 세부 근거. 공용 목적지 열람자가 원본도 볼 수 있다는 포함관계 불충족 |
| `delivery_forbidden` / `destination_forbidden` | HTTP 403. 자동 반복보다 봇 채널 권한 수정 |
| `delivery_rate_limited` / `rate_limited` | HTTP 429. 유한 재시도 후 실패는 유지, Retry-After와 부하 확인 |
| `burst_limit` | 대기 작업 한도 초과. skipped/dropped 집계를 확인하고 삭제 원문 복구 가능성을 과장하지 않음 |
| `sent_metadata_only` | 삭제 후 Discord가 원문을 돌려주지 않는 정상 API 한계 또는 원문 표시 설정. 캐시/intent/재시작 확인 |

제한 카드·메타데이터 카드·합성 카드가 도착한 사실만으로 공용 상세 삭제 기록의 장애가 해결됐다고 발표하지 않습니다.

## 수정 파일과 함수 보고

수정/추가 파일은 다음 **13개**입니다. 운영 토큰·상태 파일·기존 채널 ID는 수정하지 않았습니다.

| 전체 파일 경로 | 변경 |
| --- | --- |
| `C:\Users\adgua\Documents\GitHub\Discord_AI_bot\AGENTS.md` | 사용자 요청에 따라 작업 완료 후 영어 메시지로 자동 로컬 커밋하는 저장소 지침 |
| `C:\Users\adgua\Documents\GitHub\Discord_AI_bot\deletion_audit.py` | 구조화된 공개 범위 판정, 캐시/REST/Member 복구, 결과별 집계, 재시도와 재검증, 검열 카드 |
| `C:\Users\adgua\Documents\GitHub\Discord_AI_bot\main.py` | 생성자 호환 검사, 봇 Member 조회 연결, 채널 ACL 무효화 이벤트, 운영 상태 UI |
| `C:\Users\adgua\Documents\GitHub\Discord_AI_bot\tests\test_deletion_permissions_v7.py` | 실제 Discord 모델의 공개 오판 재현·수정 및 비공개 보호 17개 |
| `C:\Users\adgua\Documents\GitHub\Discord_AI_bot\tests\test_deletion_pipeline_v7.py` | 상세/메타/제한 송신, 검열/수정/RAW/bulk, 한도·HTTP·ACL 재검증 37개 |
| `C:\Users\adgua\Documents\GitHub\Discord_AI_bot\tests\test_deletion_main_contract.py` | 실제 모듈 import·생성자·기존 네 매핑·Member 복구·ACL 이벤트 6개 |
| `C:\Users\adgua\Documents\GitHub\Discord_AI_bot\tests\test_deletion_audit.py` | 상세 성공/원문 미확보/제한 카드의 기대와 합성 테스트 시각 분리 |
| `C:\Users\adgua\Documents\GitHub\Discord_AI_bot\tests\test_admin_integration.py` | 파딱/소유자 인증 유지, 다섯 집계·KST·권한 세부 근거·전송 테스트 구분 |
| `C:\Users\adgua\Documents\GitHub\Discord_AI_bot\.env.example` | 기존 세 플래그와 MESSAGE_CONTENT·제한 카드 의미 설명 |
| `C:\Users\adgua\Documents\GitHub\Discord_AI_bot\README.md` | V7 공개 권한 오판 복구·집계 및 배포 문서 안내 |
| `C:\Users\adgua\Documents\GitHub\Discord_AI_bot\docs\PUBLIC_DELETE_HOTFIX_V7_2026-10-23.md` | 이 완료 보고, 22개 검증 조건과 네 서버 운영 체크리스트 |
| `C:\Users\adgua\Documents\GitHub\Discord_AI_bot\docs\NORTHFLANK_OPERATIONS.md` | 삭제 기록 배포의 V7 우선 문서 연결 |
| `C:\Users\adgua\Documents\GitHub\Discord_AI_bot\docs\DELETION_DESTINATION_RECOVERY_2026-10-23.md` | 이전 3서버 문서임을 명시하고 현재 네 서버 V7 기준 연결 |

`deletion_audit.py`의 주요 변경 함수/형식은 `PublicDestinationDecision`, `evaluate_public_destination`,
호환 래퍼 `public_destination_reason`, overwrite/역할/회원 범위 보조 함수, `build_deletion_embed`와
`DeletionAuditService.__init__`, `_stat`, `_record`, `_event`, `_known_channel`, `invalidate_channel`,
`_resolve_channel`, `_bot_member`, `probe_configured_destinations`, `destination_for`, `can_track_source`,
`refresh_diagnostics`, `diagnostics`, `send_test`, `_routed`, `_admit`, `remember`, `delete`,
`on_message_delete`, `on_raw_message_delete`, `_confirmed`, `_retry_delay`, `_send_with_retry`, `_publish`입니다.
`build_restricted_deletion_embed`는 비공개 보호용 비식별 카드로 보존하면서 `reason_code`로 비공개·확인 불가를 구분하고,
권한 판정과 전달 결과도 별도 집계합니다.
bulk·RAW 수정은 기존 공통 감사 경로와 원문 무효화 정책을 유지합니다.

`main.py`는 `_fetch_deletion_bot_member`, `_create_deletion_audit`, `on_guild_channel_update`,
`on_guild_channel_delete`, `_admin_action_service`를 추가/수정했습니다.
`safe_delete`, 일반/RAW/bulk/수정 이벤트 연결, `LOG_ROUTES`의 네 목적지와 전체 소스 집합,
`ADMIN_SERVICE.delete_service`, `/관리`의 사용자 ID 인증, 숨긴 cleanup 내부 서비스, `/질문`, 검열·XP 정책을 보존했습니다.

자동 테스트를 통과한 변경은 영어 메시지의 로컬 커밋으로 보관합니다. 정확한 커밋 ID는 이 문서를 포함한 Git 이력에서 확인합니다.
남은 확인은 **운영 Python 3.13 이미지 빌드·실제 봇 기동/Gateway 유지·네 서버의 권한/인공 전송/공개 삭제/자동 검열 실전 검증**입니다.
