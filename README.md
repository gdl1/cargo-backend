# cargo-backend

현대 앱 개발 공모전 백엔드입니다. FastAPI 서버와 PostgreSQL 연결, Alembic, 상태 확인 API를 설정했습니다.

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

FastAPI는 PC에서 실행하고 PostgreSQL은 Docker에서 실행합니다. 5432 포트를 이미 쓰고 있다면 `.env`의 `POSTGRES_PORT`를 변경하세요. 적재 계획 테이블은 모델을 추가하는 후속 작업에서 만듭니다.

## 검사

```powershell
.\.venv\Scripts\python.exe -m alembic check
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m pytest
```

기본 `pytest`는 실제 DB 연결 검사를 건너뜁니다. PostgreSQL을 켜 둔 상태에서 그 검사까지 실행하려면 아래 명령을 사용하세요.

```powershell
.\.venv\Scripts\python.exe -c "from dotenv import load_dotenv; import os, pytest; load_dotenv(); os.environ['RUN_DB_TESTS']='1'; raise SystemExit(pytest.main())"
```

GitHub Actions에서는 Ruff, Alembic, PostgreSQL 연결을 포함한 테스트를 실행합니다.

## 종료

서버 터미널에서 `Ctrl+C`를 누른 뒤 `docker compose down`으로 PostgreSQL을 종료합니다. 다음에 실행할 때도 DB 데이터는 볼륨에 남아 있습니다.

## 패키지

개발 PC에서는 `requirements-dev.txt` 하나를 설치합니다. 서버에서 실행 패키지만 설치할 때는 `requirements.txt`를 사용합니다. 실행 패키지의 버전을 바꾸면 두 파일에 같이 반영하세요.

macOS와 Linux에서는 `python3.12 -m venv .venv`로 가상환경을 만들고 `.venv/bin/python`을 사용합니다. `.env`가 없으면 `.env.example`을 복사하세요.
