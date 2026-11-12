# cargo-backend

현대자동차그룹 차량용 앱 개발 공모전의 FastAPI 백엔드입니다. 적재 계획 저장·조회, 좌석 조건별 배치 계산과 추천, 내 짐 관리, 자연어 요청과 제품 규격 검색을 제공합니다.

## 실행

Python 3.12, Docker Desktop과 D 드라이브를 준비하고 저장소 최상위에서 실행합니다. 가상환경이 이미 있으면 생성 명령은 생략합니다.

```powershell
$cargoPython = 'D:\venvs\cargo-backend\Scripts\python.exe'
py -3.12 -m venv 'D:\venvs\cargo-backend'
& $cargoPython -m pip install -r requirements-dev.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }

docker compose up -d --wait db
& $cargoPython -m alembic upgrade head
& $cargoPython -m app.db.seed
& $cargoPython -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

FastAPI는 PC, PostgreSQL은 Docker에서 실행합니다. 기존 `.env`의 DB 설정을 유지하고 새 항목은 `.env.example`을 참고해 추가하세요. 5432 포트를 사용 중이면 `POSTGRES_PORT`를 변경합니다. 현재 마이그레이션은 `0003`이며 기존 계획을 보존합니다. 별도 ZIP을 적용하지 않은 `0002` 저장소에서 바로 올리는 변경입니다. 시드는 반복 실행해도 차량을 중복 생성하지 않습니다. 이번 변경의 추가 패키지는 없습니다.

| 주소 | 확인할 내용 |
| --- | --- |
| [API 문서](http://127.0.0.1:8000/docs) | 요청·응답 형식과 직접 호출 |
| [서버 상태](http://127.0.0.1:8000/api/v1/health) | HTTP 200 |
| [DB 상태](http://127.0.0.1:8000/api/v1/health/db) | HTTP 200 |
| [차량 정보](http://127.0.0.1:8000/api/v1/vehicles/avante_demo) | 좌석, 폴딩 구간과 공간 치수 |

## AI 설정

일반 계획·계산 API는 API 키 없이 사용할 수 있습니다. 자연어 요청, 제품 검색과 쌓기 추정을 사용하려면 `.env`에 다음 값을 설정하고 서버를 다시 실행합니다.

```dotenv
OPENAI_API_KEY=발급받은_키
OPENAI_MODEL=gpt-6.1-terra
LLM_TIMEOUT_SECONDS=20
LLM_MAX_OUTPUT_TOKENS=4000
```

기본 모델은 GPT-6.1 Terra이며 `reasoning.effort="none"`으로 호출합니다. 키가 없거나 쌓기 추정에 실패하면 새 쌓기를 만들지 않고 바닥 배치를 계산하며 `STACKING_ASSESSMENT_UNAVAILABLE` 경고를 반환합니다. 기존 배치 유지에서는 변경되지 않은 짐의 저장된 쌓기 판단을 재사용할 수 있습니다. 제품 검색·자연어 요청은 설정이 없으면 해당 기능의 사용 불가 오류를 반환합니다.

LLM은 물품명·재질·운반 상태로 쌓기 적합성을 추정하고, Python 계산 모듈이 받침·겹침·공간 경계·허용 회전을 검사합니다. 추정 결과는 하중 검증이나 운반 안전 인증이 아닙니다. 제품 검색은 필요한 경우에만 사용하며 앱의 AR 측정값·직접 입력도 받습니다. 제품의 포장·조립·접힘 상태는 별도 후보로 반환하고, 확인하지 못한 치수는 `null`입니다.

## API

모든 경로의 앞부분은 `/api/v1`입니다. 전체 17개 API의 자세한 형식은 `/docs`에서 확인합니다.

| 메서드·경로 | 기능 |
| --- | --- |
| `GET /health`, `GET /health/db` | 서버·DB 상태 확인 |
| `GET /vehicles`, `GET /vehicles/{vehicle_id}` | 차량 목록·상세 조회 |
| `POST /plans` | 직접 입력 또는 선택한 계산 결과로 계획 저장 |
| `GET /plans` | 이름 검색, 분류·즐겨찾기 필터, 최신순·즐겨찾기순 목록 |
| `GET /plans/{plan_id}` | 짐·좌석 조건과 선택한 배치 조회 |
| `PUT /plans/{plan_id}` | 입력 또는 계산 결과로 기존 계획 변경 |
| `PATCH /plans/{plan_id}/metadata` | 이름·분류·메모·즐겨찾기 변경 |
| `PUT /plans/{plan_id}/selection` | 사용할 배치 선택 |
| `POST /plans/{plan_id}/calculations` | 계획 계산·재계산 |
| `GET /plans/{plan_id}/calculations/{calculation_id}` | 저장된 계산 결과 조회 |
| `POST /calculations/preview` | 원본을 변경하지 않는 임시 계산 |
| `POST /items`, `GET /items` | 내 짐 등록·조회 |
| `POST /products/search` | 출처가 있는 제품 규격 후보 검색 |
| `POST /ai/messages` | 물건 적재 확인·계획 변경·현재 배치에 짐 추가 요청 |

### 입력과 저장

- 조회를 제외한 요청에는 UUID 형식의 `Idempotency-Key` 헤더가 필요합니다. 새 작업은 새 키, 같은 요청의 재전송은 같은 키와 본문을 사용합니다.
- 치수와 API 좌표는 **cm, 소수 첫째 자리**입니다. 좌표는 공간 안의 로컬 좌표이며 전역 좌표는 `origin_cm + 로컬 좌표`입니다. 계산 내부에서는 mm 정수를 사용합니다.
- `passenger_count`는 운전자 포함 인원입니다. 선택 항목인 `passenger_seats`를 보내면 운전석을 포함해 인원수만큼 지정합니다. 카시트·유지 좌석과 실제 착석 상태는 별도 조건입니다.
- 짐의 `required_quantity`는 반드시 실을 수량입니다. `allowed_orientations`는 허용 방향이며 이름의 `W`, `D`, `H`는 입력한 폭·깊이·높이를 뜻합니다. 필수 체크박스를 전체 수량에 적용할지는 앱 명세에서 확인해야 합니다.
- `name`, `category`(`TRAVEL`·`LEISURE`·`MOVING`), `memo`, `is_favorite`를 저장할 수 있습니다. 목록의 `item_summaries`에는 짐별 ID·이름·수량이 들어갑니다.
- 변경·선택 요청에는 상세 조회에서 받은 `revision`을 `expected_revision`으로 보냅니다. 버전 충돌 시 최신 계획을 다시 조회합니다. 계산 입력을 바꾸면 기존 선택이 해제되며 이전 계산 기록은 보존됩니다.
- 미리보기는 기본 600초 동안 유효합니다. 결과를 확인한 뒤 기존 계획에 반영하거나 새 계획으로 저장합니다. 내 짐의 `default_quantity`는 계획 작성의 기본 수량이며 계획 변경이 내 짐에 전파되지 않습니다.

직접 저장의 간단한 본문은 [plan-create.json](examples/plan-create.json), 전체 호출 흐름은 [phase1-smoke.ps1](examples/phase1-smoke.ps1)에 있습니다.

### 계산과 차량 상태

`REPACK_ALL`은 전체 재배치, `KEEP_EXISTING`은 선택한 배치의 기존 짐 위치를 유지하며 추가하는 모드입니다. 현재 실린 상태는 선택한 배치와 같다고 가정합니다. 점유·보호 좌석은 지키고, 가능한 폴딩 조합과 등록된 빈 좌석 공간을 사용합니다. 탑승 인원을 줄이는 제안은 하지 않습니다.

전체 짐의 배치를 우선하며, 제외를 허용하면 필수 수량을 지키는 대안을 찾습니다. 물건별 제외 수량과 부피를 보존하고 좌석 변경 횟수·트렁크 내부 적재 여부 등을 비교하는 규칙 기반 Reranking으로 순위를 반환합니다. 한 물건은 하나의 공간에 들어가야 하며 여러 물건을 여러 공간에 나눌 수 있습니다. 긴 짐 하나가 두 공간에 걸치는 계산은 지원하지 않습니다.

현재 점유·폴딩 상태는 **Pleos 앱이 SDK로 읽어 서버에 전달**합니다. 백엔드가 Pleos 차량 상태 API를 직접 조회하지 않습니다. `null`은 미확인, `[]`는 해당 상태가 없음을 확인했다는 뜻입니다. `vehicle_state`로 좌석별 상태·출처를 보완할 수 있으며 확인이 필요한 경우 `STATE_REQUIRED`와 `required_inputs`를 반환합니다.

차량 ID는 `avante_demo`, 데이터 버전은 `avante-demo-v2`입니다. 좌석 공간·위치·폴딩 조합은 **시연용 합성값**이며 실차 아반떼의 기능이나 치수로 검증한 값이 아닙니다. 자세한 구분과 교체 기준은 [차량 데이터 안내](app/data/vehicles/README.md)를 참고하세요. 과거 결과는 계산 당시 공간과 버전을 보존합니다.

`CALCULATION_TIMEOUT_SECONDS=2`는 배치 탐색의 시간 예산입니다. LLM·DB·네트워크 시간을 포함한 API 전체 응답 제한이 아닙니다. 시간이 지나면 찾았던 후보도 폐기하고 `TIME_LIMIT`을 반환하며, 1차에는 탐색을 이어서 실행하는 기능이 없습니다. 제한 내 탐색에서 배치를 못 찾은 경우는 `NO_LAYOUT_FOUND`로 구분합니다. `MAX_UNITS=64`는 계산 한 번의 짐 총수량에 적용한 프로토타입 보호 상한이며 실측한 최적값은 아닙니다.

이번에 반영한 결정과 남은 확인은 [1차 범위](docs/phase1-scope.md)에 정리했습니다.

## 검사

서버와 별도 터미널에서 실행합니다.

```powershell
$cargoPython = 'D:\venvs\cargo-backend\Scripts\python.exe'
& $cargoPython -m alembic check
& $cargoPython -m ruff check .
& $cargoPython -m ruff format --check .
& $cargoPython -m pytest

# PostgreSQL 통합 검사 포함
& $cargoPython -c "from dotenv import load_dotenv; import os,pytest; load_dotenv(); os.environ['RUN_DB_TESTS']='1'; raise SystemExit(pytest.main())"

# 서버가 실행 중일 때 실제 API 흐름 확인
.\examples\phase1-smoke.ps1
```

연동 예제와 같은 요청 흐름은 Linux·Python 3.12·PostgreSQL 16.2에서 실제 HTTP로 확인했습니다. PowerShell 스크립트 자체는 Windows에서 아직 실행하지 않았습니다.

DB 검사는 임시 스키마에서 실행하며 스키마 생성 권한이 필요합니다. GitHub Actions에서도 Ruff, 마이그레이션과 테스트를 실행합니다. 실 LLM 응답 품질과 실제 Pleos 기기의 호출은 별도 검증이 필요합니다. `127.0.0.1`은 개발 PC 확인용이며 앱 연동에는 팀이 접근할 서버 주소가 필요합니다.

서버 종료는 `Ctrl+C`, DB 종료는 `docker compose down`입니다. DB 데이터는 Docker 볼륨에 남습니다. macOS·Linux에서는 Python 3.12 가상환경의 `.venv/bin/python`을 사용하면 됩니다.

새 버전에서 계획을 수정한 뒤 `0002`로 되돌리면 이력 항목의 의미가 달라질 수 있어 마이그레이션이 중단됩니다. 되돌려야 한다면 변경 전 DB 백업을 사용하세요.
