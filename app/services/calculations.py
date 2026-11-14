from copy import deepcopy
from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.config import Settings
from app.core.exceptions import AppError
from app.models.calculation import Calculation
from app.models.plan import Plan
from app.schemas.calculation import CalculationCreate, CalculationData
from app.schemas.packing import MAX_UNITS, PackingItem
from app.schemas.plan import PlanInputUpdate
from app.services.operations import json_data, replay_request, request_guard, save_response
from app.services.vehicles import get_vehicle_catalog, space_matches_snapshot


def owned_plan(session: Session, user_id: str, plan_id: UUID, *, lock: bool = False) -> Plan:
    query = (
        select(Plan)
        .where(Plan.plan_id == plan_id, Plan.user_id == user_id)
        .options(
            selectinload(Plan.items),
            selectinload(Plan.car_seats),
            selectinload(Plan.retained_seats),
        )
        .execution_options(populate_existing=True)
    )
    if lock:
        query = query.with_for_update()
    plan = session.scalar(query)
    if plan is None:
        raise AppError(404, "PLAN_NOT_FOUND", "적재 계획을 찾을 수 없습니다.")
    return plan


def check_revision(plan: Plan, expected: int | None) -> None:
    if expected is not None and plan.revision != expected:
        raise AppError(
            409, "PLAN_REVISION_CONFLICT", "계획이 변경되었습니다. 최신 계획을 확인해 주세요."
        )


def plan_snapshot(plan: Plan) -> dict:
    from app.services.plans import get_plan_snapshot

    data = get_plan_snapshot(plan)
    # 조회 전용 필드는 계산 입력과 분리합니다.
    return PlanInputUpdate.model_validate(
        {name: data[name] for name in PlanInputUpdate.model_fields if name in data}
    ).model_dump(mode="json")


def vehicle_snapshot(vehicle_id: str) -> dict:
    vehicle = get_vehicle_catalog().get(vehicle_id)
    if vehicle is None:
        raise AppError(503, "VEHICLE_MODEL_UNAVAILABLE", "차량 계산 모델이 준비되지 않았습니다.")
    return vehicle.model_dump(mode="json")


def result_payload(calculation: Calculation) -> dict:
    payload = calculation.result_payload
    if not payload or not payload.get("result_code") or not payload.get("input_snapshot"):
        raise AppError(
            503,
            "CALCULATION_RESULT_UNAVAILABLE",
            "이전 계산의 재현 정보가 부족합니다. 새 계산을 요청해 주세요.",
        )
    return deepcopy(payload)


def get_calculation(
    session: Session, user_id: str, plan_id: UUID, calculation_id: UUID
) -> CalculationData:
    owned_plan(session, user_id, plan_id)
    calculation = session.scalar(
        select(Calculation).where(
            Calculation.calculation_id == calculation_id, Calculation.plan_id == plan_id
        )
    )
    if calculation is None:
        raise AppError(404, "CALCULATION_NOT_FOUND", "계산 결과를 찾을 수 없습니다.")
    return CalculationData.model_validate(result_payload(calculation))


def get_base_arrangement(
    session: Session, user_id: str, plan: Plan | None, payload: CalculationCreate, vehicle: dict
) -> dict | None:
    if payload.base_calculation_id is None:
        return None
    if plan is None:
        raise AppError(409, "BASE_LAYOUT_STALE", "기준 배치의 원본 계획을 확인해 주세요.")
    if (
        plan.selected_calculation_id != payload.base_calculation_id
        or plan.selected_arrangement_id != payload.base_arrangement_id
    ):
        raise AppError(409, "BASE_LAYOUT_STALE", "현재 선택된 배치와 기준 배치가 다릅니다.")
    row = session.scalar(
        select(Calculation).where(
            Calculation.calculation_id == payload.base_calculation_id,
            Calculation.plan_id == plan.plan_id,
        )
    )
    if row is None:
        raise AppError(404, "CALCULATION_NOT_FOUND", "기준 계산을 찾을 수 없습니다.")
    result = result_payload(row)
    if (
        row.plan_revision != plan.revision
        or result["vehicle_data_version"] != vehicle["data_version"]
    ):
        raise AppError(
            409, "BASE_LAYOUT_STALE", "기준 배치의 계획 또는 차량 정보가 변경되었습니다."
        )
    candidate = next(
        (
            candidate
            for candidate in result["arrangements"]
            if candidate["arrangement_id"] == str(payload.base_arrangement_id)
        ),
        None,
    )
    if candidate is None:
        raise AppError(409, "BASE_LAYOUT_STALE", "선택된 기준 배치를 사용할 수 없습니다.")
    profiles = {profile["profile_id"]: profile for profile in vehicle["packing_profiles"]}
    profile = profiles.get(candidate.get("profile_id"))
    if profile is None:
        raise AppError(409, "BASE_LAYOUT_STALE", "기준 배치의 공간 정보가 변경되었습니다.")
    actual_spaces = {space["space_id"]: space for space in profile["spaces"]}
    for space in candidate.get("packing_spaces", []):
        actual = actual_spaces.get(space["space_id"])
        if actual is None or not space_matches_snapshot(actual, space):
            raise AppError(409, "BASE_LAYOUT_STALE", "기준 배치의 공간 정보가 변경되었습니다.")
    return {**deepcopy(candidate), "input_snapshot": deepcopy(result["input_snapshot"])}


def run_calculation(
    snapshot: dict,
    vehicle: dict,
    payload: CalculationCreate,
    settings: Settings,
    *,
    base_arrangement: dict | None = None,
    stacking_assessments: list[dict] | None = None,
) -> dict:
    from app.services.llm import StackingUnavailable, estimate_stacking
    from app.services.recommendations import normalize_vehicle_state, recommend

    if sum(item["quantity"] for item in snapshot["items"]) > MAX_UNITS:
        raise AppError(422, "VALIDATION_ERROR", f"한 번에 계산할 짐은 최대 {MAX_UNITS}개입니다.")
    try:
        for item in snapshot["items"]:
            PackingItem.model_validate(item)
    except ValidationError as exc:
        raise AppError(
            422, "VALIDATION_ERROR", "계산할 짐의 치수와 회전 조건을 확인해 주세요."
        ) from exc
    request = payload.model_dump(mode="json")
    normalize_vehicle_state(vehicle, request, snapshot)
    warnings = []
    assessments = stacking_assessments
    if payload.cargo_state == "UNKNOWN" or sum(item["quantity"] for item in snapshot["items"]) == 1:
        assessments = []
    if assessments is None:
        try:
            assessments = estimate_stacking(snapshot["items"], settings)
        except StackingUnavailable:
            old_items = {
                item["item_id"]: item
                for item in (base_arrangement or {}).get("input_snapshot", {}).get("items", [])
            }
            new_items = {item["item_id"]: item for item in snapshot["items"]}
            fields = (
                "name",
                "width_cm",
                "depth_cm",
                "height_cm",
                "material",
                "transport_state",
                "allowed_orientations",
            )
            unchanged = {
                item_id
                for item_id in old_items.keys() & new_items.keys()
                if all(
                    old_items[item_id].get(field) == new_items[item_id].get(field)
                    for field in fields
                )
            }
            assessments = [
                deepcopy(row)
                for row in (base_arrangement or {}).get("stacking_assessments", [])
                if row["lower_item_id"] in unchanged and row["upper_item_id"] in unchanged
            ]
            warnings.append("STACKING_ASSESSMENT_UNAVAILABLE")
    result = recommend(
        snapshot,
        vehicle,
        request,
        base_arrangement=base_arrangement,
        stacking_assessments=assessments,
        timeout_seconds=settings.calculation_timeout_seconds,
        max_candidates=settings.calculation_max_candidates,
    )
    result["warnings"] = list(dict.fromkeys(result.get("warnings", []) + warnings))
    if result["result_code"] == "TIME_LIMIT":
        result.update(is_success=False, search_complete=False, arrangements=[], alternatives=[])
    # 필수 결과 필드만 저장하여 엔진 내부 진단값에 API가 종속되지 않게 합니다.
    return {key: value for key, value in result.items() if key in CalculationData.model_fields}


def complete_payload(
    result: dict,
    snapshot: dict,
    vehicle: dict,
    payload: CalculationCreate,
    *,
    plan_id: UUID | None,
    revision: int | None,
    calculation_id: UUID | None,
    created_at: datetime,
) -> dict:
    data = {
        **result,
        "calculation_id": calculation_id,
        "plan_id": plan_id,
        "plan_revision": revision,
        "created_at": created_at,
        "vehicle_data_version": vehicle["data_version"],
        "packing_mode": payload.packing_mode,
        "base_calculation_id": payload.base_calculation_id,
        "base_arrangement_id": payload.base_arrangement_id,
        "input_snapshot": {
            **snapshot,
            "vehicle_state": payload.vehicle_state.model_dump(mode="json")
            if payload.vehicle_state
            else None,
        },
    }
    return CalculationData.model_validate(data).model_dump(mode="json")


def create_calculation(
    session: Session,
    payload: CalculationCreate,
    user_id: str,
    plan_id: UUID,
    key: UUID,
    settings: Settings,
) -> tuple[CalculationData, bool]:
    with request_guard(session, user_id, f"/plans/{plan_id}/calculations", key):
        return _create_calculation(session, payload, user_id, plan_id, key, settings)


def _create_calculation(
    session: Session,
    payload: CalculationCreate,
    user_id: str,
    plan_id: UUID,
    key: UUID,
    settings: Settings,
) -> tuple[CalculationData, bool]:
    scope = f"/plans/{plan_id}/calculations"
    request = payload.model_dump(mode="json")
    with session.begin():
        replay = replay_request(session, user_id, scope, key, request)
        if replay is not None:
            return CalculationData.model_validate(replay), False
        plan = owned_plan(session, user_id, plan_id)
        check_revision(plan, payload.expected_revision)
        revision = plan.revision
        snapshot = plan_snapshot(plan)
        vehicle = vehicle_snapshot(plan.vehicle_id)
        base = get_base_arrangement(session, user_id, plan, payload, vehicle)
    result = run_calculation(snapshot, vehicle, payload, settings, base_arrangement=base)
    data = complete_payload(
        result,
        snapshot,
        vehicle,
        payload,
        plan_id=plan_id,
        revision=revision,
        calculation_id=uuid4(),
        created_at=datetime.now(UTC),
    )
    with session.begin():
        replay = replay_request(session, user_id, scope, key, request)
        if replay is not None:
            return CalculationData.model_validate(replay), False
        plan = owned_plan(session, user_id, plan_id, lock=True)
        check_revision(plan, revision)
        if vehicle_snapshot(plan.vehicle_id)["data_version"] != vehicle["data_version"]:
            raise AppError(
                409, "BASE_LAYOUT_STALE", "차량 정보가 변경되었습니다. 다시 계산해 주세요."
            )
        get_base_arrangement(session, user_id, plan, payload, vehicle)
        session.add(
            Calculation(
                calculation_id=UUID(data["calculation_id"]),
                plan_id=plan_id,
                plan_revision=revision,
                is_success=data["is_success"],
                current_folded_segments=data["current_folded_segments"],
                created_at=datetime.fromisoformat(data["created_at"].replace("Z", "+00:00")),
                result_payload=data,
            )
        )
        session.flush()
        save_response(session, user_id, scope, key, request, data)
    return CalculationData.model_validate(data), True


def snapshot_before_edit(session: Session, plan: Plan) -> None:
    snapshot = plan_snapshot(plan)
    for calculation in session.scalars(
        select(Calculation).where(
            Calculation.plan_id == plan.plan_id, Calculation.result_payload.is_(None)
        )
    ):
        # 기존 행에는 공간/모델/낱개 정보가 없어 당시 입력만 보존합니다.
        calculation.result_payload = {"input_snapshot": snapshot, "legacy_unavailable": True}


def load_candidate(
    session: Session,
    user_id: str,
    *,
    source_calculation_id: UUID | None = None,
    source_preview_id: UUID | None = None,
    arrangement_id: UUID,
    target_plan: Plan | None = None,
    expected_revision: int | None = None,
) -> dict:
    if (source_calculation_id is None) == (source_preview_id is None):
        raise AppError(422, "VALIDATION_ERROR", "계산 또는 미리보기 중 하나를 지정해 주세요.")
    if source_preview_id is not None:
        from app.models.preview import CalculationPreview

        preview = session.scalar(
            select(CalculationPreview).where(
                CalculationPreview.preview_id == source_preview_id,
                CalculationPreview.user_id == user_id,
            )
        )
        if preview is None:
            raise AppError(404, "PREVIEW_NOT_FOUND", "미리보기를 찾을 수 없습니다.")
        if preview.expires_at <= datetime.now(UTC):
            raise AppError(
                410, "PREVIEW_EXPIRED", "미리보기 유효기간이 지났습니다. 다시 계산해 주세요."
            )
        result = deepcopy(preview.result_payload)
        source_plan_id, source_revision = preview.source_plan_id, preview.source_plan_revision
    else:
        row = session.scalar(
            select(Calculation)
            .join(Plan)
            .where(Calculation.calculation_id == source_calculation_id, Plan.user_id == user_id)
        )
        if row is None:
            raise AppError(404, "CALCULATION_NOT_FOUND", "계산 결과를 찾을 수 없습니다.")
        result = result_payload(row)
        source_plan_id, source_revision = row.plan_id, row.plan_revision
    if source_plan_id is not None:
        source_plan = owned_plan(session, user_id, source_plan_id, lock=True)
        if source_plan.revision != source_revision:
            code = "PREVIEW_STALE" if source_preview_id else "PLAN_REVISION_CONFLICT"
            raise AppError(409, code, "원본 계획이 변경되었습니다. 다시 계산해 주세요.")
    if target_plan is not None:
        if target_plan.user_id != user_id or source_plan_id != target_plan.plan_id:
            raise AppError(
                409, "BASE_LAYOUT_STALE", "다른 계획의 결과로 기존 계획을 수정할 수 없습니다."
            )
        check_revision(target_plan, expected_revision)
    candidate = next(
        (
            candidate
            for candidate in result.get("arrangements", []) + result.get("alternatives", [])
            if candidate["arrangement_id"] == str(arrangement_id)
        ),
        None,
    )
    if candidate is None or not candidate.get("placements"):
        raise AppError(
            422, "INVALID_ARRANGEMENT_SELECTION", "저장할 수 있는 배치안을 선택해 주세요."
        )
    snapshot = result["input_snapshot"]
    vehicle = vehicle_snapshot(snapshot["vehicle_id"])
    if result["vehicle_data_version"] != vehicle["data_version"]:
        code = "PREVIEW_STALE" if source_preview_id else "BASE_LAYOUT_STALE"
        raise AppError(409, code, "차량 정보가 변경되었습니다. 다시 계산해 주세요.")
    selected_result_itself = (
        source_plan_id is not None
        and source_calculation_id is not None
        and source_plan.selected_calculation_id == source_calculation_id
        and source_plan.selected_arrangement_id == arrangement_id
    )
    if result.get("base_calculation_id") is not None and not selected_result_itself:
        options = CalculationCreate(
            packing_mode=result["packing_mode"],
            cargo_state="SELECTED_ARRANGEMENT",
            base_calculation_id=result["base_calculation_id"],
            base_arrangement_id=result["base_arrangement_id"],
        )
        get_base_arrangement(
            session, user_id, source_plan if source_plan_id else None, options, vehicle
        )
    from app.services.recommendations import validate_candidate

    try:
        validate_candidate(
            snapshot,
            vehicle,
            candidate,
            {
                "vehicle_state": snapshot.get("vehicle_state"),
                "current_folded_segments": result.get("current_folded_segments"),
                "occupied_seats": result.get("occupied_seats"),
            },
        )
    except (ValueError, KeyError) as exc:
        raise AppError(
            422, "INVALID_ARRANGEMENT_SELECTION", "현재 조건과 맞는 배치안을 선택해 주세요."
        ) from exc
    quantities = {}
    for placement in candidate["placements"]:
        quantities[placement["item_id"]] = quantities.get(placement["item_id"], 0) + 1
    for item in snapshot["items"]:
        if quantities.get(item["item_id"], 0) < item.get("required_quantity", 0):
            raise AppError(
                422, "INVALID_ARRANGEMENT_SELECTION", "필수 짐을 지키는 배치안이 아닙니다."
            )
    return {
        "input_snapshot": deepcopy(snapshot),
        "plan_input": deepcopy(snapshot),
        "candidate": deepcopy(candidate),
        "result_payload": result,
        "source_plan_id": source_plan_id,
        "source_revision": source_revision,
        "vehicle_data_version": result["vehicle_data_version"],
        "source_calculation_id": source_calculation_id,
        "source_preview_id": source_preview_id,
    }


def persist_applied_result(
    session: Session, plan: Plan, source: dict, item_id_map: dict[str, str]
) -> dict:
    candidate = deepcopy(source["candidate"])
    placements = candidate["placements"]
    id_map = {placement["placement_id"]: str(uuid4()) for placement in placements}
    unit_map = {}
    for item_id in {p["item_id"] for p in placements}:
        units = sorted(p["unit_index"] for p in placements if p["item_id"] == item_id)
        unit_map.update({(item_id, old): index for index, old in enumerate(units, start=1)})
    item_id_map = {str(key): str(value) for key, value in item_id_map.items()}
    for placement in placements:
        old_item_id, old_unit_index = placement["item_id"], placement["unit_index"]
        placement.update(
            source_item_id=old_item_id,
            source_unit_index=old_unit_index,
            item_id=item_id_map[old_item_id],
            unit_index=unit_map[old_item_id, old_unit_index],
            placement_id=id_map[placement["placement_id"]],
            supporting_placement_ids=[
                id_map[value] for value in placement.get("supporting_placement_ids", [])
            ],
        )
    for assessment in candidate.get("stacking_assessments", []):
        for field in ("lower_item_id", "upper_item_id"):
            if assessment[field] in item_id_map:
                assessment[field] = item_id_map[assessment[field]]
    candidate["stacking_assessments"] = [
        assessment
        for assessment in candidate.get("stacking_assessments", [])
        if assessment["lower_item_id"] in item_id_map.values()
        and assessment["upper_item_id"] in item_id_map.values()
    ]
    candidate.update(arrangement_id=str(uuid4()), rank=1, excluded_total_quantity=0)
    candidate.pop("exclusions", None)
    candidate["reason_codes"] = [
        code for code in candidate.get("reason_codes", []) if "EXCLU" not in code
    ]
    data = deepcopy(source["result_payload"])
    data["provenance"] = {
        "source_calculation_id": str(source["source_calculation_id"])
        if source.get("source_calculation_id")
        else None,
        "source_preview_id": str(source["source_preview_id"])
        if source.get("source_preview_id")
        else None,
        "source_arrangement_id": source["candidate"]["arrangement_id"],
        "source_packing_mode": data["packing_mode"],
        "source_base_calculation_id": data.get("base_calculation_id"),
        "source_base_arrangement_id": data.get("base_arrangement_id"),
    }
    data.update(packing_mode="REPACK_ALL", base_calculation_id=None, base_arrangement_id=None)
    data.update(
        calculation_id=str(uuid4()),
        plan_id=str(plan.plan_id),
        plan_revision=plan.revision,
        created_at=datetime.now(UTC).isoformat(),
        is_success=True,
        result_code="LAYOUT_FOUND",
        search_complete=True,
        arrangements=[candidate],
        alternatives=[],
        input_snapshot={
            **plan_snapshot(plan),
            "vehicle_state": data["input_snapshot"].get("vehicle_state"),
        },
    )
    from app.services.recommendations import validate_candidate

    try:
        validate_candidate(
            data["input_snapshot"],
            vehicle_snapshot(plan.vehicle_id),
            candidate,
            {
                "vehicle_state": data["input_snapshot"].get("vehicle_state"),
                "current_folded_segments": data.get("current_folded_segments"),
                "occupied_seats": data.get("occupied_seats"),
            },
        )
    except (ValueError, KeyError) as exc:
        raise AppError(
            422, "INVALID_ARRANGEMENT_SELECTION", "변경된 짐과 배치가 일치하지 않습니다."
        ) from exc
    data = CalculationData.model_validate(data).model_dump(mode="json")
    # 원본 계산을 바꾸지 않고 새 계획/버전의 독립 결과를 저장합니다.
    session.add(
        Calculation(
            calculation_id=UUID(data["calculation_id"]),
            plan_id=plan.plan_id,
            plan_revision=plan.revision,
            is_success=True,
            current_folded_segments=data.get("current_folded_segments"),
            result_payload=json_data(data),
        )
    )
    session.flush()
    return {
        "calculation_id": UUID(data["calculation_id"]),
        "arrangement_id": UUID(candidate["arrangement_id"]),
    }
