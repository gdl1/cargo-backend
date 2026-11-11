# cargo-backend

현대 앱 개발 공모전 백엔드입니다. FastAPI, PostgreSQL, 상태 확인 API와 적재 계획 저장을 위한 DB 모델을 구성했습니다.

## 로컬 실행

Python 3.12와 Docker Desktop을 준비합니다. Docker Desktop을 실행한 뒤 저장소 최상위에서 아래 명령을 순서대로 실행하세요. Python 3.12 가상환경이 이미 있으면 첫 줄은 생략해도 됩니다.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
docker compose up -d --wait db
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

서버가 뜨면 브라우저에서 다음 주소를 확인합니다.

| 주소 | 확인할 내용 |
| --- | --- |
| [API 문서](http://127.0.0.1:8000/docs) | API 목록 |
| [서버 상태](http://127.0.0.1:8000/api/v1/health) | HTTP 200 |
| [DB 상태](http://127.0.0.1:8000/api/v1/health/db) | HTTP 200 |

FastAPI는 PC에서 실행하고 PostgreSQL은 Docker에서 실행합니다. 5432 포트를 이미 쓰고 있다면 `.env`의 `POSTGRES_PORT`를 변경하세요.

## DB 모델

`alembic upgrade head`로 아래 테이블을 생성합니다. 초기 버전은 `0001`입니다.

| 테이블 | 저장할 내용 |
| --- | --- |
| vehicle | 차량 ID와 이름 |
| plan | 사용자, 차량, 탑승 인원, 생성 시각 |
| plan_item | 짐 이름, cm 치수, 수량, 입력 순서 |
| plan_car_seat | 계획에 포함된 카시트 좌석 |
| calculation | 계획별 계산 결과와 현재 폴딩 상태 |
| arrangement | 계산별 추천 순위, 탑승 좌석, 목표 폴딩 상태 |
| placement | 짐 한 개의 좌표, 회전 방향, 적재 순서 |

승용님의 11월 10일 수정 ERD를 기준으로 작성했습니다. 목록 조회를 위해 `plan.user_id`, `created_at`과 `plan_item.position`을 보완했습니다. `arrangement`와 `placement`의 `plan_id`는 다른 계획의 짐이 연결되는 것을 DB에서 막는 용도입니다.

치수와 좌표는 cm 단위의 `NUMERIC`으로 저장해 소수를 보존합니다. 현재 폴딩 상태의 `null`은 미확인, `[]`는 모두 펴진 상태입니다. 좌석과 폴딩 목록은 배열로 저장합니다.

사용 중인 차량이나 계산 결과에 연결된 짐은 개별 삭제할 수 없습니다. 계획을 삭제하면 해당 짐, 카시트, 계산 결과도 함께 삭제됩니다. 짐 연결 제약은 트랜잭션 커밋 시 검사하므로 저장 성공 처리는 커밋 이후에 해야 합니다.

이번 단계는 빈 테이블과 관계를 생성합니다. 차량 데이터 입력, 고정 데모 사용자 적용, 저장·조회 API와 배치 계산은 후속 작업입니다. 차량별 좌석·폴딩 유효성, 중복 좌석, 배치 수량·겹침·경계 및 성공 판정은 해당 서비스에서 검증합니다.

## 검사

```powershell
.\.venv\Scripts\python.exe -m alembic check
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m pytest
```

기본 `pytest`는 DB 통합 검사를 건너뜁니다. PostgreSQL을 켜 둔 상태에서 모델과 마이그레이션 검사까지 실행하려면 아래 명령을 사용하세요.

```powershell
.\.venv\Scripts\python.exe -c "from dotenv import load_dotenv; import os, pytest; load_dotenv(); os.environ['RUN_DB_TESTS']='1'; raise SystemExit(pytest.main())"
```

모델 검사는 임시 스키마에서 실행한 뒤 롤백합니다. 실행 계정에는 해당 DB의 스키마 생성 권한이 필요합니다. 로컬 Compose의 기본 계정과 CI 계정으로 실행할 수 있습니다.

GitHub Actions에서는 Ruff, Alembic, PostgreSQL 연결과 모델 검사를 실행합니다. 마이그레이션 되돌리기·재적용 검사도 임시 스키마에서 수행합니다.

## 종료

서버 터미널에서 `Ctrl+C`를 누른 뒤 `docker compose down`으로 PostgreSQL을 종료합니다. 다음에 실행할 때도 DB 데이터는 볼륨에 남아 있습니다.

## 패키지

개발 PC에서는 `requirements-dev.txt` 하나를 설치합니다. 서버에서 실행 패키지만 설치할 때는 `requirements.txt`를 사용합니다. 실행 패키지의 버전을 바꾸면 두 파일에 같이 반영하세요.

macOS와 Linux에서는 `python3.12 -m venv .venv`로 가상환경을 만들고 `.venv/bin/python`을 사용합니다. `.env`가 없으면 `.env.example`을 복사하세요.
