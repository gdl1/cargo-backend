# 1차 반영 범위

기준은 7단계가 병합된 `main`의 `bfa2a953`입니다. 별도로 제공했던 `cargo-backend-phase1.zip`을 적용하지 않은 상태에서 시작합니다. 최신 API 명세와 이후 대화에서 정한 내용을 아래와 같이 연결했습니다.

| 결정 | 구현 위치 |
| --- | --- |
| 목록에 짐 이름과 수량 표시, 계획 이름·분류·메모·즐겨찾기 | `services/plans.py` |
| 필수 수량, 운반 상태, 허용 회전, AR·직접 입력 치수 보존 | `schemas/plan.py`, `services/ai.py` |
| 계획 수정과 새 계획 저장 모두 지원, 과거 결과 보존 | `services/plans.py`, `services/calculations.py` |
| 선택한 배치가 현재 적재 상태, 최신 계산과 선택 배치는 구분 | `plans/{plan_id}/selection`, `services/calculations.py` |
| 탑승 인원·카시트·유지 좌석 보호, 탑승 인원 감소 추천 없음 | `services/recommendations.py` |
| 트렁크·일반 좌석·등록된 폴딩 공간을 사용 | `data/vehicles/avante-demo-v2.json` |
| 여러 짐의 수량·회전·경계·겹침·받침 검증 | `services/packing.py` |
| 전체 재배치 또는 기존 위치·방향·받침을 유지한 추가 적재 | `services/recommendations.py` |
| 필수·고정 짐을 제외하지 않고 실제 배치한 제외 대안 제공 | `services/recommendations.py` |
| 제외 수량, 좌석 변경 횟수, 트렁크 내부 적재 여부에 따른 정렬 | `services/recommendations.py` |
| 제한시간이 지나면 발견한 후보도 폐기하고 종료 | `services/packing.py`, `services/calculations.py` |
| 계획 없이 확인·계획 변경·적재한 짐에 추가의 자연어 요청 | `services/ai.py` |
| 상품은 요청한 경우에 검색, 운반 상태별 후보와 출처 제공, 미확인 치수는 null | `services/products.py` |
| 쌓기는 LLM의 대략적인 물품 판단과 서버의 기하 검증을 함께 사용 | `services/llm.py`, `services/packing.py` |
| 차량 상태는 Pleos 앱이 읽어 전달, 서버는 등록된 공간과 연결 | `schemas/calculation.py`, `services/recommendations.py` |
| 내 짐 수량은 기본값이며 계획마다 독립적으로 변경 | `services/items.py`, `services/plans.py` |

기존 명세의 16개 API에 `PATCH /api/v1/plans/{plan_id}/metadata`를 추가했습니다. 이름이나 즐겨찾기만 변경할 때 계산 입력 버전을 올리지 않기 위한 경로입니다. `passenger_seats`는 회의에서 나온 직접 좌석 선택을 위한 선택 필드이며, 생략하면 기존 자동 배정을 유지합니다. 카시트가 설치되어 있다는 이유만으로 사람이 앉았다고 추정하지 않습니다.

## 남아 있는 확인

- 필수 체크박스가 항목 전체 수량에 적용되는지는 앱 화면의 동작 합의가 필요합니다. 서버는 `required_quantity`로 일부 수량도 표현합니다.
- 추가한 좌석 공간과 폴딩 치수는 합성 더미입니다. 실측값과 실제 지원되는 폴딩 조합으로 교체해야 합니다.
- SDK에서 제공하는 관측값을 좌석 코드로 변환하는 작업과 Pleos 기기 호출은 앱에서 확인해야 합니다. 미확인 값은 빈 배열로 바꾸지 않습니다.
- 계산은 최대 64개와 기본 2초로 제한합니다. 프로토타입 보호 한도이며 배포 서버에서 조정할 기준을 측정해야 합니다.
- LLM 쌓기 판단은 추정입니다. 무게·하중이나 실제 운반 안전성이 검증되었다는 뜻은 아닙니다.

학습 기반 배치 정책, 설명서 RAG, 한 짐이 연결된 여러 공간에 걸치는 배치, 제한시간 이후 이어서 계산은 이번 구현에 포함하지 않습니다.
