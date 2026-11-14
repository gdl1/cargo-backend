# cargo-backend

현대 앱 개발 공모전 백엔드입니다. FastAPI와 PostgreSQL로 차량 조회, 적재 계획 관리, 배치 계산, 자연어 요청, AI 배치 가이드와 모바일 푸시를 제공합니다.

현재 코드는 1차 기능을 구현한 상태입니다. 외부 AI·제품 검색·FCM의 실제 동작과 모바일·Pleos 연동은 별도로 확인해야 하며, 팀용 Base URL은 배포 후 공유합니다.

## 로컬 실행

Python 3.12와 Docker Desktop을 준비합니다. Docker Desktop을 실행한 뒤 저장소 최상위에서 아래 명령을 순서대로 실행하세요. Python 3.12 가상환경이 이미 있으면 첫 줄은 생략해도 됩니다.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
docker compose up -d --wait db
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m app.db.seed
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

서버가 뜨면 브라우저에서 다음 주소를 확인합니다.

| 주소 | 확인할 내용 |
| --- | --- |
| [API 문서](http://127.0.0.1:8000/docs) | API 목록 |
| [서버 상태](http://127.0.0.1:8000/api/v1/health) | HTTP 200 |
| [DB 상태](http://127.0.0.1:8000/api/v1/health/db) | HTTP 200 |
| [차량 목록](http://127.0.0.1:8000/api/v1/vehicles) | `tucson_hybrid`, `avante_demo` 조회 |
| [차량 상세](http://127.0.0.1:8000/api/v1/vehicles/tucson_hybrid) | 투싼 좌석, 입구, 적재 공간과 연결 관계 |
| [계획 목록](http://127.0.0.1:8000/api/v1/plans) | 현재 데모 사용자의 저장된 계획 |

FastAPI는 PC에서 실행하고 PostgreSQL은 Docker에서 실행합니다. 5432 포트를 이미 쓰고 있다면 `.env`의 `POSTGRES_PORT`를 변경하세요.

D 드라이브의 기존 가상환경을 사용한다면 `$cargoPython = 'D:\venvs\cargo-backend\Scripts\python.exe'`로 지정하고, 위 명령의 `.\.venv\Scripts\python.exe`를 `& $cargoPython`으로 바꾸면 됩니다. 명령은 저장소 최상위에서 실행합니다.

## DB 모델

`alembic upgrade head`로 최신 버전 `0005`까지 적용합니다. 기존 DB도 같은 명령으로 갱신하며, 새 DB에서는 전체 테이블을 생성합니다.

| 테이블 | 저장할 내용 |
| --- | --- |
| vehicle | 차량 ID와 이름 |
| plan | 사용자, 차량, 탑승 인원, 이름·카테고리·메모·즐겨찾기, 입력 버전과 선택 배치 |
| plan_item | 짐 이름, cm 치수, 수량·필수 수량, 허용 회전과 운반 상태 |
| plan_car_seat | 계획에 포함된 카시트 좌석 |
| plan_retained_seat | 추가로 접지 않을 유지 좌석 |
| calculation | 계획별 계산 결과와 현재 폴딩 상태 |
| arrangement | 계산별 추천 순위, 탑승 좌석, 목표 폴딩 상태 |
| placement | 짐 한 개의 좌표, 회전 방향, 적재 순서 |
| calculation_preview | 저장 전 계산 결과와 만료 시각 |
| saved_item | 내 짐과 즐겨찾기 |
| ai_conversation / product_candidate | 자연어 대화 상태와 제품 규격 후보 |
| operation_receipt | 요청별 멱등성 처리 |
| push_device / cargo_reminder / push_delivery | 기기 토큰, 알림 요청과 기기별 발송 상태 |

`arrangement`와 `placement`의 `plan_id`는 다른 계획의 짐이 연결되는 것을 DB에서 막는 용도입니다. 계획 입력을 변경하면 `revision`이 증가하고 과거 계산 결과는 저장 당시 입력과 함께 보존됩니다.

치수와 좌표는 cm 단위로 주고받으며 계산할 때 mm 정수로 변환합니다. 현재 폴딩 상태의 `null`은 미확인, `[]`는 모두 펴진 상태입니다. 좌석 점유의 미확인 값도 빈 좌석으로 취급하지 않습니다.

사용 중인 차량이나 계산 결과에 연결된 짐은 개별 삭제할 수 없습니다. 계획을 삭제하면 해당 짐, 카시트, 유지 좌석, 계산 결과도 함께 삭제됩니다. 짐 연결 제약은 트랜잭션 커밋 시 검사하므로 저장 성공 처리는 커밋 이후에 해야 합니다.

배치 계산은 수량, 허용 회전, 공간 경계, 겹침과 받침을 검사하고 필수 짐·탑승 좌석 조건을 지키는 후보를 정렬합니다.

## 데모 사용자와 차량

사용자 ID는 서버의 `DEMO_USER_ID` 설정으로 정하며 기본값은 `demo-user`입니다. 앱에서 사용자 ID를 보내지 않습니다. 계획 저장은 `get_current_user_id` 의존성에서 받은 사용자에 연결합니다. 로그인 기능은 아닙니다.

두 앱은 같은 백엔드 주소를 사용하고, 차량 목록에서 받은 `vehicle_id`로 상세 정보를 조회합니다. 차량 조회에는 사용자별 구분이 없습니다.

투싼 하이브리드의 좌석과 공간은 `app/data/vehicles/tucson-hybrid-v1.json`에서 읽습니다. DB에는 차량 ID와 이름을 저장합니다.

| 항목 | 값 |
| --- | --- |
| 차량 / 버전 | `tucson_hybrid` / `tucson-hybrid-v1` |
| 트렁크 폭 × 깊이 × 높이 | 123 × 100 × 80cm (바닥판 위쪽, 폭은 휠하우스 사이 기준) |
| 트렁크 입구 폭 × 높이 | 116 × 80cm |
| 2열 전체 폴딩 | `REAR_ALL`, 남은 탑승 좌석은 운전석과 조수석 |
| 폴딩 후 전체 적재 깊이 / 2열 위 여유 높이 | 160cm / 70cm |

실측표의 160cm를 총 깊이로 적용해 100cm 지점부터 2열 폴딩 공간 60cm를 배정했습니다. 위쪽 바닥판과 접힌 2열의 연결면을 같은 높이로 모델링했습니다. 2열과 연결부의 폭 실측값이 없어 입구 폭 116cm를 중앙에 배치한 임시 폭으로 씁니다. 이 가정이 들어간 2열 공간과 차량 전체는 `DUMMY`, 실측값을 쓴 트렁크는 `MEASURED`입니다. 아래쪽 바닥판의 10cm 단차와 2열 부분 폴딩은 별도 측정 및 계산 설정이 필요합니다.

트렁크와 2열에 걸친 짐은 배치 한 건으로 반환합니다. `space_id: "TRUNK"`는 좌표 기준 공간이고, `spanning_space_id: "REAR_FOLDED"`가 추가로 점유한 공간입니다. `packing_spaces`의 `origin_cm`에 배치의 상대 좌표를 더해 차량 기준 좌표를 얻습니다. 앱은 짐을 한 번 그리되 트렁크 경계를 넘어 접힌 2열까지 표시해야 합니다. 2열을 점유하므로 `all_in_trunk`는 `false`입니다.

미리보기에서 `vehicle_id`를 `tucson_hybrid`로, `passenger_count`를 `2`로 지정하고 `40 × 140 × 20cm` 짐 하나를 넣으면 연결 배치를 확인할 수 있습니다. `calculation_options`에 `current_folded_segments: []`와 `occupied_seats: ["ROW_1_LEFT", "ROW_1_RIGHT"]`를 넣으면 `REAR_ALL`을 목표로 계산합니다.

기존 `avante_demo` 계획은 `avante-demo-v3` 공간과 폴딩 조합으로 계산합니다. 이전 계산의 차량 정보는 저장 당시 값으로 유지합니다. 이전 차량 버전의 배치를 `KEEP_EXISTING` 기준으로 쓰면 `409 BASE_LAYOUT_STALE`이므로 다시 계산해 배치를 선택해야 합니다.

`python -m app.db.seed`는 차량 ID가 없으면 추가하고 있으면 이름을 갱신합니다. 반복 실행해도 중복되지 않으며 기존 계획이나 다른 차량을 삭제하지 않습니다. 등록 전에는 목록이 비어 있고 상세 조회는 `404 VEHICLE_NOT_FOUND`를 반환합니다.

차량 파일을 수정할 때는 `data_version`도 갱신하고 등록 명령을 실행한 뒤 서버를 재시작하세요. 기준 데이터는 시작 시 검증하며 잘못된 좌석·치수·조합이면 시작을 중단합니다.

## 적재 계획 저장

`POST /api/v1/plans`로 저장합니다. 요청 예시는 `examples/plan-create.json`에 있습니다. `/docs`의 해당 API에서 `Try it out`을 누르고, `Idempotency-Key`에 새 UUID를 입력한 뒤 예시 본문을 전송해도 됩니다.

| 요청 항목 | 규칙 |
| --- | --- |
| `Idempotency-Key` 헤더 | UUID 필수. 새 저장에는 새 값, 같은 요청의 재전송에는 같은 값 |
| `vehicle_id` | 차량 목록에 등록된 ID |
| `passenger_count` | 운전자 포함 1명부터 차량 정원까지의 정수 |
| `car_seats` / `retained_seats` | 생략하면 `[]`. 지원 좌석만 허용하며 각 목록 내 중복 금지 |
| `items` | 1개 이상의 짐 종류. 이름은 공백 제거 후 1~100자, 치수는 양수 cm 소수 첫째 자리까지, 수량은 1 이상 정수 |
| `required_quantity` | 1차 앱에서 필수 체크 시 해당 항목의 `quantity` 전체, 미선택 시 `0` |
| `name` / `category` / `memo` / `is_favorite` | 계획 이름, 여행(`TRAVEL`)·레저 및 취미(`LEISURE`)·이사 및 운반(`MOVING`), 메모, 즐겨찾기 |

치수 문자열·boolean·자동 반올림이 필요한 값과 알 수 없는 필드는 거절합니다. 수량은 DB 정수 범위 안에서 받지만, 한 번의 배치 계산은 종류별 수량을 합쳐 최대 64개까지 지원합니다. 체크한 짐의 수량을 바꾸면 `required_quantity`도 함께 변경합니다.

카시트에는 운전석을 지정할 수 없습니다. 유지 좌석에 운전석을 명시하거나 카시트와 겹치는 좌석을 넣는 것은 허용합니다. 유지 좌석과 카시트는 접지 않을 조건이며 실제 탑승 위치를 뜻하지 않습니다. 트렁크보다 큰 짐도 저장할 수 있으며, 적재 가능성은 이후 계산에서 판단합니다.

최초 저장은 HTTP `201`과 `plan_id`, 짐별 `item_id`, UTC 생성 시각을 반환합니다. 같은 키와 정규화한 입력으로 재전송하면 같은 응답을 `200`으로 반환합니다. `Location`은 상세 조회 주소인 `/api/v1/plans/{plan_id}`입니다. 직접 입력으로 처음 저장한 계획의 `latest_calculation_id`는 `null`이며, 계산 결과로 저장할 때는 해당 결과가 함께 연결됩니다.

| 오류 | HTTP / code |
| --- | --- |
| 입력 형식·범위·좌석 오류 | `422 VALIDATION_ERROR` |
| 등록되지 않은 차량 | `404 VEHICLE_NOT_FOUND` |
| 같은 키에 다른 입력 | `409 IDEMPOTENCY_KEY_REUSED` |
| 같은 요청을 처리 중 | `409 REQUEST_IN_PROGRESS` |

계획·짐·좌석·재시도 정보는 한 트랜잭션으로 저장합니다. 중간에 실패하면 모두 취소하며 같은 키로 다시 요청할 수 있습니다. 처리 중 오류라면 잠시 후 같은 키·본문으로 재시도하세요. 키는 사용자와 계획 저장 작업 단위이며 계획이 존재하는 동안 유지됩니다. 서버 시작 시 초기화되지 않습니다.

## 적재 계획 목록·상세 조회

두 앱은 같은 백엔드 주소와 서버의 `DEMO_USER_ID` 설정으로 계획을 조회합니다. GET 요청에는 본문이나 `Idempotency-Key`가 필요하지 않습니다.

| 경로 | 응답 |
| --- | --- |
| `GET /api/v1/plans?limit=20&offset=0` | 계획 요약 목록과 `limit`, `offset`, `total` |
| `GET /api/v1/plans/{plan_id}` | 선택한 계획의 짐·좌석 조건과 최신 계산 ID |

`limit`은 1~100(기본 20), `offset`은 0 이상(기본 0, DB 정수 범위 안)입니다. 목록은 `created_at` 내림차순이며 같으면 `plan_id` 내림차순입니다. `item_type_count`는 짐 종류 수, `item_total_quantity`는 수량 합계이며, `item_summaries`에 짐별 이름과 수량이 들어갑니다. `q`, `category`, `favorite` 필터와 `sort=favorite`도 지원합니다. 저장된 계획이 없으면 빈 배열과 `total: 0`, 페이지 범위를 벗어나면 빈 배열과 실제 전체 개수를 반환합니다. 조회 개수를 줄여도 기존 계획은 삭제되지 않습니다.

상세는 입력한 짐 순서·ID·치수와 카시트·유지 좌석을 보존합니다. 없는 계획과 다른 사용자에게 속한 계획은 모두 `404 PLAN_NOT_FOUND`, 잘못된 UUID나 페이지 값은 `422 VALIDATION_ERROR`입니다. 사용자 ID, 재시도 키·해시는 응답에 포함하지 않습니다.

계산 이력이 없으면 `latest_calculation_id`는 `null`입니다. 이력이 생기면 성공 여부와 무관하게 최신 생성 시각의 계산을 반환하고, 시각이 같으면 계산 UUID 내림차순으로 선택합니다. 계산이 추가되어도 원래 계획 생성 시각이나 목록 순서는 바뀌지 않습니다. POST 재시도는 별도로 최초 저장 응답을 유지합니다.

실제 Pleos 연동은 앱에서 접근 가능한 서버 주소가 준비된 뒤 확인합니다. `127.0.0.1`은 개발 PC 확인용입니다.

## 계산과 AI 연결

| 경로 | 기능 |
| --- | --- |
| `POST /api/v1/plans/{plan_id}/calculations` | 저장된 계획 계산·재계산 |
| `GET /api/v1/plans/{plan_id}/calculations/{calculation_id}` | 저장된 결과 조회 |
| `PUT /api/v1/plans/{plan_id}/selection` | 사용할 배치 선택 |
| `POST /api/v1/calculations/preview` | 계획 저장·변경 전 계산 |
| `PUT /api/v1/plans/{plan_id}` | 입력 또는 선택한 계산 결과로 기존 계획 변경 |
| `PATCH /api/v1/plans/{plan_id}/metadata` | 이름·카테고리·메모·즐겨찾기만 변경 |
| `POST /api/v1/products/search` | 운반 상태별 제품 규격 후보 검색 |
| `POST /api/v1/ai/messages` | 앱에서 변환한 텍스트로 검색·계산 요청 |
| `POST /api/v1/ai/loading-guides` | 선택한 배치의 상세 AI 가이드 |
| `GET /api/v1/items`, `POST /api/v1/items` | 내 짐 조회·등록 |
| `PATCH /api/v1/items/{saved_item_id}/favorite` | 내 짐 즐겨찾기 변경 |

계획 없는 적재 확인, 기존 계획 변경, 기존 배치 유지 추가 적재를 지원합니다. `KEEP_EXISTING`은 선택한 배치대로 실제 짐이 실렸다고 가정하며, 원래 짐의 위치·방향을 유지합니다. 계산만으로 계획을 덮어쓰지 않고, 사용자가 결과를 선택한 뒤 기존 계획에 반영하거나 `POST /api/v1/plans`로 새 계획을 저장합니다. 요청·응답과 필요한 헤더는 `/docs`에서 확인합니다.

제품 검색은 포장·조립·접힘 상태별 후보를 반환합니다. 확인되지 않은 치수는 `null`이며, 앱이 후보와 치수를 확인하거나 AR·직접 입력으로 보완해야 계산합니다. AR 측정과 STT·TTS는 앱에서 처리합니다. 그림을 먼저 표시한 뒤 가이드 API를 호출하며, AI 호출·문구 검증 실패 시 기본 안내인 “위 그림을 보고 배치해 주세요.”를 반환합니다. 유효하지 않거나 만료된 원본 결과는 오류로 반환합니다.

`.env`의 `OPENAI_API_KEY`, `OPENAI_MODEL`로 외부 AI를 설정합니다. 키가 없으면 자연어 요청·제품 검색은 사용할 수 없고 상세 가이드는 기본 안내를 반환합니다. 일반 계산은 가능하지만 새 쌓기 판단이 불가능하면 바닥 배치만 탐색합니다. 기존 배치를 유지할 때는 변경되지 않은 짐의 저장된 쌓기 판단을 재사용할 수 있습니다.

| 설정 | 기본값과 의미 |
| --- | --- |
| `CALCULATION_TIMEOUT_SECONDS` | 2초, 배치 탐색 시간 |
| `CALCULATION_MAX_CANDIDATES` | 반환 후보 최대 20개 |
| `PREVIEW_TTL_SECONDS` | 미리보기 보관 600초 |
| `LLM_TIMEOUT_SECONDS` / `LLM_MAX_OUTPUT_TOKENS` | AI 호출별 20초 / 출력 토큰 4000 |

1차에서는 계산 제한시간 초과 시 `TIME_LIMIT`과 빈 배치 목록을 반환합니다. 이 시간은 AI·검색·DB·네트워크를 포함한 전체 응답시간이 아니며 실제 서버에서 측정해야 합니다. 남은 공간의 cm·L·% 수치 안내와 제한시간 이후 탐색 이어하기는 2차 범위입니다.

## 모바일 짐 챙기기 알림

| 경로 | 기능 |
| --- | --- |
| `PUT /api/v1/notifications/devices/{device_id}` | Android FCM 토큰 등록·갱신 |
| `DELETE /api/v1/notifications/devices/{device_id}` | 토큰 해제 |
| `POST /api/v1/plans/{plan_id}/cargo-reminders` | Pleos에서 모바일로 알림 요청 |

`.env`에 `NOTIFICATION_API_KEY`(공백 없는 ASCII 32자 이상), `FIREBASE_PROJECT_ID`, `GOOGLE_APPLICATION_CREDENTIALS`를 설정합니다. 서비스 계정 JSON은 저장소 밖에 두고 절대 경로를 지정합니다. 세 API는 `X-Notification-Key`가 필요하며, 알림 요청에는 UUID `Idempotency-Key`도 필요합니다. 실제 키·서비스 계정 파일·기기 토큰은 커밋하지 않습니다.

알림 시점과 센서 조건은 기획과 Pleos 앱에서 정합니다. 서버는 SDK를 직접 조회하지 않습니다. `ACCEPTED`는 FCM 접수이며 실제 휴대폰 수신을 보장하지 않습니다. 서버의 백그라운드 자동 재시도는 없으므로 앱이 `RETRYABLE` 기기에 대해 `retry_at` 이후 같은 키·본문으로 재요청합니다. 이미 접수된 기기는 중복 발송하지 않고, 결과가 불명확한 `UNKNOWN`은 재발송하지 않습니다.

`PUSH_HTTP_TIMEOUT_SECONDS`는 발송 요청 제한(기본 5초), `PUSH_RETRY_DELAY_SECONDS`는 재시도 대기(60초), `NOTIFICATION_EVENT_MAX_AGE_SECONDS`는 이벤트 허용 경과 시간(600초)입니다. Android 알림 권한·`cargo_reminders` 채널, 전경·배경 수신, 알림 클릭 후 해당 계획 이동은 실제 기기로 확인합니다.

## 검사

```powershell
.\.venv\Scripts\python.exe -m alembic check
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m pytest
```

기본 `pytest`는 DB 통합 검사를 건너뜁니다. PostgreSQL을 켜 둔 상태에서 모델·마이그레이션·API 저장·조회 검사까지 실행하려면 아래 명령을 사용하세요.

```powershell
.\.venv\Scripts\python.exe -c "from dotenv import load_dotenv; import os, pytest; load_dotenv(); os.environ['RUN_DB_TESTS']='1'; raise SystemExit(pytest.main())"
```

DB 검사는 임시 스키마에서 실행하고 롤백하거나 검사 종료 시 해당 스키마를 삭제합니다. 동시 요청 검사는 서로 다른 DB 연결에서 수행합니다. 실행 계정에는 스키마 생성 권한이 필요하며, 로컬 Compose의 기본 계정과 CI 계정으로 실행할 수 있습니다.

GitHub Actions에서는 Ruff, Alembic과 전체 테스트를 실행합니다. 마이그레이션 되돌리기·재적용 검사도 임시 스키마에서 수행합니다. 테스트의 외부 AI·검색·FCM 응답은 모의 처리하므로 실제 서비스 호출 성공을 뜻하지 않습니다. 배포 후 Base URL·Swagger와 세 가지 계산 흐름, 앱 음성 출력·푸시 수신·전체 응답시간을 별도로 확인합니다.

## 외부 서비스 설정 확인

기본 검사는 외부 호출 없이 설정 여부만 확인하며 비밀값은 출력하지 않습니다. `--live`를 붙인 경우에만 실제 호출합니다. AI·검색 호출에는 요금이 발생할 수 있습니다.

```powershell
$cargoPython = 'D:\venvs\cargo-backend\Scripts\python.exe'
& $cargoPython -m app.check_integrations
& $cargoPython -m app.check_integrations --live --service ai
& $cargoPython -m app.check_integrations --live --service search
& $cargoPython -m app.check_integrations --live --service push
```

`ai`는 한국어 예제 한 건, `search`는 웹 검색 응답, `push`는 Firebase 인증만 확인합니다. DB 변경이나 푸시 발송은 하지 않습니다. 설정 누락·실패 시 종료코드는 1입니다. 실제 상품 규격의 정확도, FCM 발송 권한·휴대폰 수신과 앱 응답 속도는 별도 확인해야 합니다.

## 서버 컨테이너 실행

이미지는 실행에 필요한 코드만 포함하고 일반 사용자로 실행합니다. `.env`와 Firebase 인증 파일은 이미지에 포함하지 않습니다. 아래는 기존 Compose DB에 연결하는 로컬 검증 명령이며, 실행 중인 로컬 Uvicorn은 먼저 종료합니다.

```powershell
docker compose -p cargo-backend up -d --wait db
docker build -t cargo-backend:local .
docker run --rm --env-file .env --network cargo-backend_default -e POSTGRES_HOST=db -e POSTGRES_PORT=5432 cargo-backend:local python -m alembic upgrade head
docker run --rm --env-file .env --network cargo-backend_default -e POSTGRES_HOST=db -e POSTGRES_PORT=5432 cargo-backend:local python -m app.db.seed
docker run -d --name cargo-api --restart unless-stopped --log-opt max-size=10m --log-opt max-file=3 --env-file .env --network cargo-backend_default -e POSTGRES_HOST=db -e POSTGRES_PORT=5432 -p 127.0.0.1:8000:8000 cargo-backend:local
docker logs --tail 100 cargo-api
docker inspect --format '{{.State.Health.Status}}' cargo-api
```

DB 상태 확인에 성공하면 컨테이너 상태가 `healthy`가 됩니다. 기존과 같은 `/docs`에서 API를 확인합니다. 재실행할 때는 `docker rm -f cargo-api`로 기존 API 컨테이너를 제거한 뒤 이미지 빌드부터 반복합니다. DB 볼륨은 삭제하지 않습니다.

Firebase 인증 파일이 필요하면 `docker run`에 읽기 전용 마운트를 추가하고 컨테이너 안의 경로를 지정합니다. 호스트 파일 경로를 `.env`에 적는 것만으로 컨테이너에 파일이 전달되지는 않습니다.

```powershell
--mount "type=bind,source=D:\secrets\firebase-service-account.json,target=/run/secrets/firebase.json,readonly" -e GOOGLE_APPLICATION_CREDENTIALS=/run/secrets/firebase.json
```

위 옵션은 이미지 이름 `cargo-backend:local` 앞에 넣고 실제 파일 경로로 바꿉니다. Linux에서는 컨테이너 사용자 UID 10001이 해당 파일을 읽을 수 있어야 합니다. 팀용 서버에는 실제 DB·인증 설정을 주입하고, 공유 데모 데이터의 접근 범위와 HTTPS 주소를 정한 뒤 Base URL·`/docs`·`/openapi.json`의 외부 접속을 확인합니다. 로컬 컨테이너 실행만으로 외부 배포가 완료되는 것은 아닙니다.

CI에서는 이미지 빌드, 이미지 안의 마이그레이션·차량 등록, 실제 서버·DB 상태와 OpenAPI 응답까지 검사합니다.

## 종료

서버 터미널에서 `Ctrl+C`를 누른 뒤 `docker compose down`으로 PostgreSQL을 종료합니다. 다음에 실행할 때도 DB 데이터는 볼륨에 남아 있습니다.

## 패키지

개발 PC에서는 `requirements-dev.txt` 하나를 설치합니다. 서버에서 실행 패키지만 설치할 때는 `requirements.txt`를 사용합니다. 실행 패키지의 버전을 바꾸면 두 파일에 같이 반영하세요.

macOS와 Linux에서는 `python3.12 -m venv .venv`로 가상환경을 만들고 `.venv/bin/python`을 사용합니다. `.env`가 없으면 `.env.example`을 복사하세요.
