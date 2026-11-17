# 차곡차곡 백엔드

현대자동차그룹 앱 개발 공모전을 위해 만든 **차곡차곡**의 백엔드입니다. 짐을 싣기 전에 차량에 들어가는지 확인하고, 탑승 인원과 좌석 조건에 맞춰 어디에 어떤 방향으로 놓을지 안내합니다.

## 시연 흐름

1. **Android에서 적재 계획을 준비합니다.** AR로 잰 치수나 직접 입력한 치수로 짐을 등록합니다. 차량, 탑승 인원, 카시트와 접지 않을 좌석을 정해 계획을 저장합니다.
2. **Pleos에서 저장한 계획을 불러옵니다.** 현재 좌석 조건으로 계산한 배치를 보고 짐을 싣습니다. 음성으로 제품 규격을 물어보거나 짐을 추가할 수 있고, 선택한 배치에 맞춰 한국어로 짐 놓는 순서와 방향을 안내받습니다.
3. **목적지에 도착하면 휴대폰으로 짐을 확인합니다.** Pleos가 알림을 요청하면 서버가 등록된 Android 기기에 짐 챙기기 알림을 보냅니다.

AR 측정과 음성 인식 및 재생은 앱에서 처리합니다. 백엔드는 앱이 보낸 치수, 좌석 상태와 텍스트를 받아 계산과 안내에 사용합니다.

## 백엔드 구현

| 기능 | 처리 방식 |
| --- | --- |
| **3차원 적재 계산** | 같은 짐도 수량만큼 따로 배치하며 차량 입구, 공간 경계, 허용 회전, 겹침과 받침을 검사합니다. 좌석 변경이 적은 배치를 우선하고, 짐 제외를 허용한 경우에는 필수 수량을 지키는 대안을 찾습니다. [배치 탐색](app/services/packing.py), [후보 순위](app/services/recommendations.py) |
| **계획 저장과 재계산** | 두 앱에서 같은 계획을 조회합니다. 사용자는 계산 결과를 미리 본 뒤 저장할 수 있고, 이미 실은 짐의 위치와 방향을 유지하면서 새 짐을 추가할 수도 있습니다. 변경 전 계산은 당시 입력과 함께 보존합니다. [계획](app/services/plans.py), [미리보기](app/services/previews.py) |
| **자연어 요청과 배치 가이드** | OpenAI로 앱이 보낸 텍스트를 해석하고, 애매한 짐이나 빠진 치수는 사용자에게 다시 확인합니다. 제품 검색은 운반 상태별 규격과 출처, 확인된 대표 사진 주소를 반환합니다. 짐마다 놓을 위치와 방향은 계산 결과에서 읽어 가이드 문장으로 만듭니다. [대화](app/services/ai.py), [제품 검색](app/services/products.py), [가이드](app/services/loading_guides.py) |
| **실측 사진과 모바일 알림** | 실측 사진은 비공개 Amazon S3에 저장하고 백엔드에서 조회합니다. Firebase Cloud Messaging으로 Android에 알림을 보내며 기기별 발송 상태를 기록합니다. [사진](app/services/items.py), [알림](app/services/notifications.py) |

FastAPI로 요청을 받고 PostgreSQL에 저장합니다. 치수는 cm로 주고받고 내부 계산에는 정수 mm를 씁니다. DB는 SQLAlchemy와 Alembic으로 관리합니다. GitHub Actions에서 검사를 통과한 `main` 커밋은 AWS Systems Manager를 통해 EC2에 배포합니다. [검사 및 배포 설정](.github/workflows/ci.yml)

## 로컬에서 확인하기

Python 3.12와 Docker Compose가 필요합니다. 저장소를 내려받고 루트 폴더에서 실행합니다.

<details>
<summary>Windows PowerShell 실행 명령</summary>

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
docker compose up -d --wait db
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m app.db.seed
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

</details>

<details>
<summary>macOS 또는 Linux 실행 명령</summary>

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
test -f .env || cp .env.example .env
docker compose up -d --wait db
python -m alembic upgrade head
python -m app.db.seed
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

</details>

서버가 실행되면 [Swagger](http://127.0.0.1:8000/docs)에서 API를 열 수 있습니다. 아래 예제는 [기본 환경 설정](.env.example)으로 실행합니다.

### 투싼에 긴 짐 싣기

투싼 하이브리드에 앞좌석 탑승자 두 명이 있고, 가로 40cm, 깊이 140cm, 높이 20cm인 상자 하나를 싣는 예제입니다.

1. Swagger에서 `POST /api/v1/calculations/preview`를 열고 `Try it out`을 선택합니다. `Idempotency-Key`에 새 UUID를 넣고 아래 본문을 실행합니다. UUID는 PowerShell의 `(New-Guid).Guid` 또는 활성화한 가상환경의 `python -c "from uuid import uuid4; print(uuid4())"`로 생성할 수 있습니다.
2. 응답의 `data.calculation.result_code`가 `LAYOUT_FOUND`인지 확인합니다. 첫 배치의 `target_folded_segments`는 `["REAR_ALL"]`이고, 짐 한 개의 `space_id`는 `TRUNK`, `spanning_space_id`는 `REAR_FOLDED`입니다.
3. 이 배치를 저장하려면 `POST /api/v1/plans`에 새 `Idempotency-Key`를 쓰고, `data.preview_id`를 `source_preview_id`에, 고른 `arrangement_id`를 `selected_arrangement_id`에 넣습니다. 반환된 `data.plan_id`로 `GET /api/v1/plans/{plan_id}`에서 계획을 열 수 있습니다.

<details>
<summary>미리보기 요청 본문</summary>

```json
{
  "plan": {
    "vehicle_id": "tucson_hybrid",
    "passenger_count": 2,
    "items": [
      {
        "name": "긴 상자",
        "width_cm": 40,
        "depth_cm": 140,
        "height_cm": 20,
        "quantity": 1,
        "allowed_orientations": ["WDH"]
      }
    ]
  },
  "calculation_options": {
    "cargo_state": "EMPTY",
    "current_folded_segments": [],
    "occupied_seats": ["ROW_1_LEFT", "ROW_1_RIGHT"]
  }
}
```

</details>

확인을 마치면 서버를 실행한 터미널에서 `Ctrl+C`를 누르고, 같은 폴더에서 `docker compose down`으로 DB 컨테이너를 종료합니다.

## 차량 데이터와 검증

- **아반떼**는 트렁크와 좌석 폴딩 조합을 데모 치수로 모델링했습니다. **투싼 하이브리드**는 트렁크와 입구에 실측값을 쓰며, 접힌 2열의 폭과 트렁크에 이어지는 바닥 높이에는 가정값을 적용했습니다. [아반떼 데이터](app/data/vehicles/avante-demo-v3.json), [투싼 데이터](app/data/vehicles/tucson-hybrid-v1.json)
- **자동 검사**는 PostgreSQL에서 마이그레이션과 저장·재계산을 검증하고 Docker 이미지의 서버 응답도 확인합니다. 위 투싼 예제는 미리보기, 저장, 한국어 안내와 기존 짐 유지 재계산까지 테스트에 포함됩니다. [투싼 통합 테스트](tests/test_tucson_cargo.py), [GitHub Actions](https://github.com/gdl1/chagokchagok-backend/actions/workflows/ci.yml)
- **외부 서비스 연동**에는 OpenAI, 사진용 S3, Firebase 설정이 필요합니다. 자동 검사에서는 이 서비스들의 응답을 모의 처리합니다. [환경 변수 예시](.env.example)
