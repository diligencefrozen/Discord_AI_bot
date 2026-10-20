# PR0 환경 분석 및 기준선

작성 기준일: 2026-10-20 (Asia/Seoul)
요구사항 기준: Discord_AI_Bot_UIUX_Codex_Handoff_v6.md
저장소: develop / 기준 커밋 ab6eccfdb5eb2a20f18f17eadce09bf1d014a6ad

이 문서는 v6의 PR0 산출물이다. PR0에서는 런타임 동작을 바꾸지 않고, 원본 코드의 현황·재현 절차·호환성 검증 범위·미검증 위험을 고정한다. v6에 적힌 참고 구현은 현재 구현으로 간주하지 않는다.

## 1. 원본 현황 스냅샷

| 항목 | 확인 결과 |
| --- | --- |
| 브랜치 | develop (origin/develop 추적) |
| 기준 커밋 | ab6eccfdb5eb2a20f18f17eadce09bf1d014a6ad |
| 기준 커밋 git archive SHA-256 | F9A35DCD444A6D86CEBA203FB81F4487C8BA195F5E00F0BE84E4EF9AFE9B43FE |
| 소스 구조 | main.py 단일 파일, 4,237줄 |
| 런타임 선언 | .python-version는 3.11, main.py 첫 주석은 Python 3.9 호환 |
| 실행 명령 | Procfile: worker: python main.py |
| 상태 파일 | daily_xp_data.pkl, achievements_data.pkl (실행 시 생성되는 파일은 기준선에 없음) |
| 테스트 | 저장소에 테스트 디렉터리/테스트 파일 없음 |
| 명령어 | @bot.command 12개, app_commands/slash command 등록 없음 |
| UI | View/link Button 사용은 있으나 v6의 타이핑 안내 4버튼·Modal·관리자 패널은 없음 |

기준 커밋 이후 작업 트리에 소스 변경은 없다. 사용자 데이터 파일과 토큰은 스냅샷·로그·커밋에 포함하지 않는다.

## 2. PR0에서 실제 실행한 검증

| 검증 | 결과 | 해석 |
| --- | --- | --- |
| Python 문법/바이트코드 | 번들 Python 3.12.14에서 py_compile 및 compileall 통과 | 문법 기준선만 통과. 라이브러리 호환성은 확인하지 않음 |
| git diff --check | 통과 | 기준선에 공백 오류 없음 |
| Python 3.11 실행 파일 | 호스트에서 찾지 못함 | 설치/실행 미검증 |
| Python 3.13 실행 파일 | 호스트에서 찾지 못함 | 설치/실행 미검증 |
| 의존성 import sanity | 실패/차단 | 번들 환경에 discord, httpx, aiohttp, pytz, deep_translator, huggingface_hub, dotenv, bs4, requests, certifi, duckduckgo_search, curl_cffi, h2, schedule, nacl 등이 없음 |
| pip install --dry-run -r requirements.txt | 실패 | 현재 pip 인덱스/네트워크에서 discord.py==2.3.2 후보를 받지 못함. 설치 성공으로 간주하지 않음 |
| discord.py==2.7.1 dry-run | 실패 | 동일한 인덱스/네트워크 제한으로 후보를 받지 못함 |
| 봇 생성/토큰 없는 기동 | 미실행 | 위 import 차단과 비밀환경변수 필수 검사 때문 |
| Discord 실서버, HF API, 크롤러, 호스팅/영속 볼륨 | 미실행 | PR0에서 외부 환경 접근 권한 및 스테이징 정보가 없음 |

pip 결과는 패키지 버전의 비호환 증거가 아니라, 현재 실행 환경에서 패키지 인덱스에 접근해 설치할 수 없다는 증거다. Python 3.13 지원을 확정하지 않는다.

## 3. 코드와 v6 요구사항의 불일치

- 현재 on_typing 안내는 5초 지연, 사용자 ID 기준 메모리 12시간 쿨다운, !ask 설명만 전송한다. 유발자 멘션은 있으나 질문·그림·성장·전체 기능 4버튼, 채널/길드 스로틀링, 재시작 지속 쿨다운은 없다.
- commands.Bot(command_prefix="!")와 12개 접두어 명령만 있다. slash command, Modal, 개인 홈, /관리 및 ID 기반 관리자 검사는 없다.
- on_message는 내용이 ! 또는 /로 시작하면 process_commands 후 즉시 반환한다. 등록되지 않은 명령 문자열도 금칙어·링크·도배 검사와 XP 처리를 우회할 수 있다.
- 금칙어는 BAD_ROOTS와 무제한 FILLER* 정규식으로 탐지한다. Unicode 정규화/제로폭·전각 처리/경계 정책/정상 한국어 오탐 테스트가 없다. 제재 메시지에 탐지된 원문 루트가 노출된다.
- is_user_exempt_from_profanity와 is_user_exempt_from_spam가 조회와 면제권 차감을 함께 수행한다. 호출 순서에 따라 정책 판정 중 상태가 소비될 수 있다.
- XP/업적 저장은 pickle.dump(..., "wb") 직접 덮어쓰기이며 임시 파일, fsync, 원자 교체, 백업 세대, 손상 격리, 단일 writer가 없다. 파일명과 Pickle 의미는 유지해야 한다.
- on_ready마다 자정 리셋 및 상태 순환 태스크를 새로 생성한다. 재연결 시 태스크가 중복될 수 있다.
- AI 호출 경로에는 동기 hf.chat.completions.create가 있어 이벤트 루프를 막을 가능성이 있다.
- 저장소에 회귀 테스트가 없어 XP/보상/면제권/필터/타이머/명령 우회에 대한 기준선이 없다.

위 항목은 문제를 기록한 것이며 PR0에서 동작 코드를 수정하지 않는다. 정책·보상 수치 변경은 운영자 승인과 shadow 비교 뒤 별도 PR에서 다룬다.

## 4. 의존성 결정표(초안)

| 패키지/영역 | 현재 고정 | PR0 판단 |
| --- | --- | --- |
| discord.py | 2.3.2 | 2.7.1 후보를 별도 검증해야 함. 설치·import·봇 생성 전에는 지원 버전으로 확정하지 않음 |
| Pillow | 10.3.0 | Python 3.13 목표와의 공식 지원 여부를 확인한 뒤 교체 여부 결정. PR0에서 임의 업그레이드 금지 |
| requests/urllib3 | 2.25.1 / 1.26.2 | 구형 핀 유지 여부와 다른 HTTP 스택과의 충돌을 fresh venv에서 확인 |
| PyNaCl | 1.4.0 | Python 3.13 wheel/build 가능성을 확인 |
| duckduckgo-search | <5.0 | API/패키지 유지 가능성을 확인. 최신화는 별도 변경 |
| 나머지 HTTP·크롤링·AI 패키지 | 여러 범위 핀 | 실제 설치 후 pip check, import sanity, 기능별 smoke test로 유지/교체/제외를 결정 |

Python 3.13과 검증된 discord.py 및 종속성 조합은 아직 고정하지 않는다. 3.11/3.12/3.13 각각의 새 가상환경 설치 결과가 필요하다.

## 5. 재현 절차와 smoke test 목록

검증 환경에서는 각 Python 버전으로 새 가상환경을 만들고 다음을 순서대로 실행한다.

    python -m venv .venv-pr0
    .\.venv-pr0\Scripts\python.exe -m pip install -r requirements.txt
    .\.venv-pr0\Scripts\python.exe -m pip check
    .\.venv-pr0\Scripts\python.exe -m py_compile main.py

토큰을 넣지 않는 import sanity와 bot 생성 smoke test를 별도 스크립트로 실행한다. 실서버 토큰/API 호출은 이 테스트에 넣지 않는다.

기능별 기준선 목록:

1. 12개 ! 명령(web, trending, img, xp, claim, leaderboard, xphelp, 전설체험, 업적, 업적상세, ask, 모배갤)의 등록·입력 오류·응답 경로.
2. on_typing 5초 지연, 메시지 작성 시 취소, 사용자별 12시간 쿨다운.
3. XP 쿨다운·주말 수치·일일 초기화·레벨/업적/보상 수령·중복 수령.
4. 도배·링크·미디어·금칙어 탐지, 삭제/타임아웃, 면제권 판정·차감.
5. AI/HF, 이미지, Wikipedia/검색, 모배갤 크롤러의 성공·실패·타임아웃.
6. 재연결/on_ready 태스크 중복, 종료 시 Pickle 저장.
7. Pickle 정상 로드·손상·부분 쓰기·권한 오류·복구 시나리오.
8. v6 이후 관리자 인가(소유자/지정 ID/비인가/DM/다른 길드)는 PR1 이후 전용 테스트로 추가.

현재 외부 패키지 설치·Discord/HF/API·호스팅 검증은 모두 미실행이다.

## 6. 다음 단계 계획과 롤백

1. 운영자가 제공할 스테이징 서버, 배포 Python, 호스팅 종류, 영속 디스크, 인스턴스 수, 쓰기 권한을 확인한다.
2. Python 3.11/3.12/3.13 fresh venv와 discord.py 후보 조합을 설치하고 pip check·import·토큰 없는 bot 생성까지 수행한다. 성공한 조합 하나만 지원 기준으로 기록한다.
3. PR1에서 순수 함수 회귀 테스트를 먼저 추가하고 XP/면제권/명령 우회/금칙어 기준선을 고정한다. 런타임 업그레이드는 별도 커밋으로 분리한다.
4. 각 PR은 작은 커밋으로 유지하고, 기준 커밋과 Pickle 파일을 보존한 상태에서 실패 시 직전 커밋으로 되돌린다. 사용자 데이터 파일은 별도 백업 후 테스트한다.

PR0 종료 시점에는 구현 기능을 정상 작동한다고 보고하지 않는다. 설치·실행·외부 API·실서버 결과가 없는 항목은 계속 미검증으로 표시한다.

