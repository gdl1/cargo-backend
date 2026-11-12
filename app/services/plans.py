import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from typing import NoReturn
from uuid import UUID

from sqlalchemy import delete, func, select, text, true
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.orm import Session, selectinload

from app.core.exceptions import AppError
from app.models import Calculation, Plan, PlanCarSeat, PlanItem, PlanRetainedSeat, Vehicle
from app.models.item import SavedItem
from app.models.operation import OperationReceipt
from app.schemas.plan import (
    PlanCreate,
    PlanData,
    PlanFromResult,
    PlanInputUpdate,
    PlanItemData,
    PlanListData,
    PlanMetadataUpdate,
    PlanSelection,
    PlanSelectionData,
    PlanSummary,
    PlanUpdate,
)
from app.schemas.vehicle import VehicleDetail
from app.services.operations import replay_request, save_response
from app.services.vehicles import get_vehicle_catalog

PLAN_LOAD = (
    selectinload(Plan.items),
    selectinload(Plan.car_seats),
    selectinload(Plan.retained_seats),
)
METADATA_FIELDS = ("name", "category", "memo", "is_favorite")


def _decimal_text(value: Decimal | UUID) -> str:
    if isinstance(value, UUID):
        return str(value)
    value_text = format(value, "f")
    return value_text.rstrip("0").rstrip(".") if "." in value_text else value_text


def _normalized_payload(payload) -> dict:
    values = payload.model_dump(mode="json", exclude_unset=isinstance(payload, PlanMetadataUpdate))
    for part in (values, values.get("plan")):
        if isinstance(part, dict):
            for field in ("car_seats", "retained_seats", "passenger_seats"):
                if field in part and part[field] is not None:
                    part[field] = sorted(part[field])
    return values


def _request_hash(payload: PlanCreate) -> str:
    values = payload.model_dump()
    for field in ("car_seats", "retained_seats"):
        values[field] = sorted(values[field])
    normalized = json.dumps(
        values, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=_decimal_text
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _legacy_request_hash(payload: PlanCreate) -> str:
    values = payload.model_dump(
        include={"vehicle_id", "passenger_count", "car_seats", "retained_seats", "items"}
    )
    for field in ("car_seats", "retained_seats"):
        values[field] = sorted(values[field])
    legacy_fields = {"name", "width_cm", "depth_cm", "height_cm", "quantity"}
    values["items"] = [
        {key: value for key, value in item.items() if key in legacy_fields}
        for item in values["items"]
    ]
    normalized = json.dumps(
        values, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=_decimal_text
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _is_legacy_input(payload: PlanCreate) -> bool:
    legacy = payload.model_dump(
        include={"vehicle_id", "passenger_count", "car_seats", "retained_seats", "items"}
    )
    item_fields = {"name", "width_cm", "depth_cm", "height_cm", "quantity"}
    legacy["items"] = [
        {key: value for key, value in item.items() if key in item_fields}
        for item in legacy["items"]
    ]
    return _normalized_payload(PlanCreate.model_validate(legacy)) == _normalized_payload(payload)


def _lock_id(user_id: str, key: UUID) -> int:
    scope = json.dumps([user_id, "/api/v1/plans", str(key)], ensure_ascii=False)
    digest = hashlib.sha256(scope.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


def _invalid_field(field: str, message: str, index: int | None = None) -> NoReturn:
    location: list[str | int] = ["body", field]
    if index is not None:
        location.append(index)
    raise AppError(
        422,
        "VALIDATION_ERROR",
        "요청 값을 확인해 주세요.",
        {"errors": [{"loc": location, "type": "value_error", "message": message}]},
    )


def _validate_vehicle(payload: PlanCreate, vehicle: VehicleDetail) -> None:
    if payload.passenger_count > vehicle.seat_capacity:
        _invalid_field("passenger_count", "탑승 인원이 차량 정원을 초과합니다.")
    for field in ("car_seats", "retained_seats"):
        for index, seat in enumerate(getattr(payload, field)):
            if seat not in vehicle.seat_codes:
                _invalid_field(field, "이 차량에서 지원하지 않는 좌석입니다.", index)
            if field == "car_seats" and seat == vehicle.driver_seat:
                _invalid_field(field, "운전석에는 카시트를 지정할 수 없습니다.", index)
    if payload.passenger_seats is not None:
        seats = set(payload.passenger_seats)
        if len(seats) != payload.passenger_count or vehicle.driver_seat not in seats:
            _invalid_field("passenger_seats", "운전석을 포함해 탑승 인원만큼 좌석을 지정해 주세요.")
        if not seats <= set(vehicle.seat_codes):
            _invalid_field("passenger_seats", "이 차량에서 지원하는 좌석을 지정해 주세요.")


def validate_plan_input(session: Session, payload: PlanCreate, user_id: str) -> None:
    vehicle = get_vehicle_catalog().get(payload.vehicle_id)
    if vehicle is None or session.get(Vehicle, payload.vehicle_id) is None:
        raise AppError(404, "VEHICLE_NOT_FOUND", "차량을 찾을 수 없습니다.")
    _validate_vehicle(payload, vehicle)
    saved_ids = {item.saved_item_id for item in payload.items if item.saved_item_id is not None}
    if saved_ids:
        owned_ids = set(
            session.scalars(
                select(SavedItem.saved_item_id).where(
                    SavedItem.saved_item_id.in_(saved_ids), SavedItem.user_id == user_id
                )
            )
        )
        if saved_ids != owned_ids:
            _invalid_field("items", "사용할 수 없는 내 짐 ID가 포함되어 있습니다.")


def _plan_data(plan: Plan, latest_calculation_id: UUID | None = None) -> PlanData:
    return PlanData(
        plan_id=plan.plan_id,
        vehicle_id=plan.vehicle_id,
        passenger_count=plan.passenger_count,
        passenger_seats=plan.passenger_seats,
        car_seats=sorted(seat.seat_code for seat in plan.car_seats),
        retained_seats=sorted(seat.seat_code for seat in plan.retained_seats),
        items=[
            PlanItemData.model_validate(item, from_attributes=True)
            for item in sorted(plan.items, key=lambda item: item.position)
            if item.is_active
        ],
        created_at=plan.created_at,
        updated_at=plan.updated_at,
        revision=plan.revision,
        latest_calculation_id=latest_calculation_id,
        selected_calculation_id=plan.selected_calculation_id,
        selected_arrangement_id=plan.selected_arrangement_id,
        name=plan.name,
        category=plan.category,
        memo=plan.memo,
        is_favorite=plan.is_favorite,
        favorited_at=plan.favorited_at,
    )


def get_plan_snapshot(plan: Plan) -> dict:
    return _plan_data(plan).model_dump(mode="json")


def _apply_metadata(plan: Plan, payload) -> None:
    for field in METADATA_FIELDS:
        if field not in payload.model_fields_set:
            continue
        value = getattr(payload, field)
        if field == "is_favorite" and value != plan.is_favorite:
            plan.favorited_at = datetime.now(UTC) if value else None
        setattr(plan, field, value)


def _new_plan(
    session: Session, payload: PlanCreate, user_id: str, *, key: UUID | None = None
) -> Plan:
    validate_plan_input(session, payload, user_id)
    plan = Plan(
        user_id=user_id,
        vehicle_id=payload.vehicle_id,
        passenger_count=payload.passenger_count,
        passenger_seats=payload.passenger_seats,
        idempotency_key=key,
        request_hash=_request_hash(payload) if key else None,
        items=[
            PlanItem(**item.model_dump(exclude={"item_id"}), position=index)
            for index, item in enumerate(payload.items, 1)
        ],
        car_seats=[PlanCarSeat(seat_code=seat) for seat in payload.car_seats],
        retained_seats=[PlanRetainedSeat(seat_code=seat) for seat in payload.retained_seats],
    )
    _apply_metadata(plan, payload)
    session.add(plan)
    session.flush()
    return plan


def _source_plan_input(source: dict) -> PlanInputUpdate:
    snapshot = source["input_snapshot"]
    values = {key: value for key, value in snapshot.items() if key in PlanInputUpdate.model_fields}
    candidate = source["candidate"]
    exclusions: Counter[str] = Counter()
    for excluded in candidate.get("exclusions", []):
        exclusions[str(excluded["item_id"])] += excluded["quantity"]
    placements = Counter(str(placement["item_id"]) for placement in candidate["placements"])
    items = []
    source_ids = {str(item["item_id"]) for item in snapshot["items"]}
    if not set(exclusions) <= source_ids or not set(placements) <= source_ids:
        raise AppError(422, "INVALID_ARRANGEMENT_SELECTION", "배치의 짐 구성이 올바르지 않습니다.")
    for original in snapshot["items"]:
        item = {key: value for key, value in original.items() if key in PlanItemData.model_fields}
        item_id = str(item["item_id"])
        remaining = item["quantity"] - exclusions[item_id]
        if (
            remaining < item.get("required_quantity", 0)
            or remaining < 0
            or placements[item_id] != remaining
        ):
            raise AppError(
                422, "INVALID_ARRANGEMENT_SELECTION", "배치 수량과 필수 조건을 확인해 주세요."
            )
        if remaining:
            item["quantity"] = remaining
            items.append(item)
    if not items:
        raise AppError(422, "INVALID_ARRANGEMENT_SELECTION", "최소 한 개의 짐이 필요합니다.")
    values["items"] = items
    return PlanInputUpdate.model_validate(values)


def _load_source(session: Session, user_id: str, payload, target_plan: Plan | None = None) -> dict:
    from app.services.calculations import load_candidate

    return load_candidate(
        session,
        user_id,
        source_calculation_id=payload.source_calculation_id,
        source_preview_id=payload.source_preview_id,
        arrangement_id=payload.selected_arrangement_id,
        target_plan=target_plan,
        expected_revision=getattr(payload, "expected_revision", None),
    )


def _apply_result(
    session: Session, plan: Plan, source: dict, input_payload: PlanInputUpdate
) -> UUID:
    from app.services.calculations import persist_applied_result

    active = sorted((item for item in plan.items if item.is_active), key=lambda item: item.position)
    item_map = {
        str(original.item_id): str(current.item_id)
        for original, current in zip(input_payload.items, active, strict=True)
    }
    result = persist_applied_result(session, plan, source, item_map)
    plan.selected_calculation_id = UUID(str(result["calculation_id"]))
    plan.selected_arrangement_id = UUID(str(result["arrangement_id"]))
    session.flush()
    return plan.selected_calculation_id


def create_plan(
    session: Session, payload: PlanCreate | PlanFromResult, user_id: str, key: UUID
) -> tuple[PlanData, bool]:
    scope = "/api/v1/plans"
    normalized = _normalized_payload(payload)
    with session.begin():
        replay = replay_request(session, user_id, scope, key, normalized)
        if replay is not None:
            return PlanData.model_validate(replay), False
        # 7단계에서 저장한 요청 키도 계속 재사용 방지 대상으로 유지합니다.
        previous = session.scalar(
            select(Plan)
            .where(Plan.user_id == user_id, Plan.idempotency_key == key)
            .options(*PLAN_LOAD)
        )
        if previous is not None:
            if not isinstance(payload, PlanCreate) or not (
                previous.request_hash == _request_hash(payload)
                or (
                    _is_legacy_input(payload)
                    and previous.request_hash == _legacy_request_hash(payload)
                )
            ):
                raise AppError(409, "IDEMPOTENCY_KEY_REUSED", "다른 입력에 사용한 요청 키입니다.")
            data = _plan_data(previous)
            save_response(session, user_id, scope, key, normalized, data)
            return data, False
        if isinstance(payload, PlanFromResult):
            source = _load_source(session, user_id, payload)
            inputs = _source_plan_input(source)
            plan = _new_plan(session, inputs, user_id)
            latest = _apply_result(session, plan, source, inputs)
            data = _plan_data(plan, latest)
        else:
            plan = _new_plan(session, payload, user_id, key=key)
            data = _plan_data(plan)
        data.model_dump_json()
        save_response(session, user_id, scope, key, normalized, data)
    return data, True


def _latest_calculation_id(plan_id, revision=None):
    statement = select(Calculation.calculation_id).where(Calculation.plan_id == plan_id)
    if revision is not None:
        statement = statement.where(Calculation.plan_revision == revision)
    return (
        statement.order_by(Calculation.created_at.desc(), Calculation.calculation_id.desc())
        .limit(1)
        .scalar_subquery()
    )


def list_plans(
    session: Session,
    user_id: str,
    limit: int,
    offset: int,
    *,
    sort: str = "latest",
    q: str | None = None,
    category: str | None = None,
    favorite: bool | None = None,
) -> PlanListData:
    filters = [Plan.user_id == user_id]
    if category is not None:
        filters.append(Plan.category == category)
    if favorite is not None:
        filters.append(Plan.is_favorite == favorite)
    if q:
        filters.append(Plan.name.icontains(q, autoescape=True))
    columns = (
        Plan.plan_id,
        Plan.vehicle_id,
        Plan.passenger_count,
        Plan.created_at,
        Plan.updated_at,
        Plan.revision,
        Plan.selected_calculation_id,
        Plan.selected_arrangement_id,
        Plan.name,
        Plan.category,
        Plan.memo,
        Plan.is_favorite,
        Plan.favorited_at,
    )
    ordering = [Plan.created_at.desc(), Plan.plan_id.desc()]
    if sort == "favorite":
        ordering.insert(0, Plan.favorited_at.desc().nullslast())
    page = (
        select(*columns)
        .where(*filters)
        .order_by(*ordering)
        .limit(limit)
        .offset(offset)
        .cte("plan_page")
    )
    total = select(func.count().label("total")).select_from(Plan).where(*filters).subquery()
    item_counts = (
        select(
            PlanItem.plan_id,
            func.count().label("item_type_count"),
            func.sum(PlanItem.quantity).label("item_total_quantity"),
            func.jsonb_agg(
                aggregate_order_by(
                    func.jsonb_build_object(
                        "item_id",
                        PlanItem.item_id,
                        "name",
                        PlanItem.name,
                        "quantity",
                        PlanItem.quantity,
                    ),
                    PlanItem.position,
                )
            ).label("item_summaries"),
        )
        .join(page, page.c.plan_id == PlanItem.plan_id)
        .where(PlanItem.is_active)
        .group_by(PlanItem.plan_id)
        .subquery()
    )
    outer_order = [page.c.created_at.desc(), page.c.plan_id.desc()]
    if sort == "favorite":
        outer_order.insert(0, page.c.favorited_at.desc().nullslast())
    statement = (
        select(
            total.c.total,
            page,
            Vehicle.name.label("vehicle_name"),
            func.coalesce(item_counts.c.item_type_count, 0).label("item_type_count"),
            func.coalesce(item_counts.c.item_total_quantity, 0).label("item_total_quantity"),
            func.coalesce(item_counts.c.item_summaries, text("'[]'::jsonb")).label(
                "item_summaries"
            ),
            _latest_calculation_id(page.c.plan_id, page.c.revision).label("latest_calculation_id"),
        )
        .select_from(
            total.outerjoin(page, true())
            .outerjoin(Vehicle, Vehicle.vehicle_id == page.c.vehicle_id)
            .outerjoin(item_counts, item_counts.c.plan_id == page.c.plan_id)
        )
        .order_by(*outer_order)
    )
    rows = session.execute(statement).mappings().all()
    return PlanListData(
        plans=[
            PlanSummary(**{key: value for key, value in row.items() if key != "total"})
            for row in rows
            if row["plan_id"] is not None
        ],
        limit=limit,
        offset=offset,
        total=rows[0]["total"],
    )


def get_plan(session: Session, user_id: str, plan_id: UUID) -> PlanData:
    row = session.execute(
        select(Plan, _latest_calculation_id(Plan.plan_id, Plan.revision))
        .where(Plan.plan_id == plan_id, Plan.user_id == user_id)
        .options(*PLAN_LOAD)
    ).one_or_none()
    if row is None:
        raise AppError(404, "PLAN_NOT_FOUND", "적재 계획을 찾을 수 없습니다.")
    return _plan_data(row[0], row[1])


def _locked_plan(session: Session, user_id: str, plan_id: UUID, revision: int) -> Plan:
    plan = session.scalar(
        select(Plan)
        .where(Plan.plan_id == plan_id, Plan.user_id == user_id)
        .with_for_update()
        .options(*PLAN_LOAD)
        .execution_options(populate_existing=True)
    )
    if plan is None:
        raise AppError(404, "PLAN_NOT_FOUND", "적재 계획을 찾을 수 없습니다.")
    if plan.revision != revision:
        raise AppError(409, "PLAN_REVISION_CONFLICT", "계획이 변경되었습니다. 다시 조회해 주세요.")
    return plan


def _preserve_creation_receipt(session: Session, plan: Plan, user_id: str) -> None:
    if (
        plan.idempotency_key is not None
        and session.get(OperationReceipt, (user_id, "/api/v1/plans", plan.idempotency_key)) is None
    ):
        snapshot = _plan_data(plan)
        original = snapshot.model_dump(include=set(PlanCreate.model_fields))
        original["items"] = [item.model_dump(exclude={"item_id"}) for item in snapshot.items]
        save_response(
            session,
            user_id,
            "/api/v1/plans",
            plan.idempotency_key,
            _normalized_payload(PlanCreate.model_validate(original)),
            snapshot,
        )


def _replace_input(
    session: Session,
    plan: Plan,
    payload: PlanInputUpdate,
    user_id: str,
    *,
    from_result: bool = False,
) -> None:
    from app.services.calculations import snapshot_before_edit

    validate_plan_input(session, payload, user_id)
    active = {item.item_id: item for item in plan.items if item.is_active}
    for item in payload.items:
        if not from_result and item.item_id is not None and item.item_id not in active:
            _invalid_field("items", "현재 계획에 없는 짐 ID입니다.")
    _preserve_creation_receipt(session, plan, user_id)
    snapshot_before_edit(session, plan)
    # 먼저 활성 인덱스에서 빼서 위치 교환과 삭제 시 기존 계산의 FK를 보존합니다.
    for item in active.values():
        item.is_active = False
    session.flush()
    for index, item in enumerate(payload.items, 1):
        values = item.model_dump(exclude={"item_id"})
        existing = active.get(item.item_id)
        if existing is None:
            plan.items.append(PlanItem(**values, position=index, is_active=True))
        else:
            for field, value in values.items():
                setattr(existing, field, value)
            existing.position = index
            existing.is_active = True
    session.execute(delete(PlanCarSeat).where(PlanCarSeat.plan_id == plan.plan_id))
    session.execute(delete(PlanRetainedSeat).where(PlanRetainedSeat.plan_id == plan.plan_id))
    session.expire(plan, ["car_seats", "retained_seats"])
    plan.car_seats.extend(PlanCarSeat(seat_code=seat) for seat in payload.car_seats)
    plan.retained_seats.extend(PlanRetainedSeat(seat_code=seat) for seat in payload.retained_seats)
    plan.vehicle_id = payload.vehicle_id
    plan.passenger_count = payload.passenger_count
    plan.passenger_seats = payload.passenger_seats
    plan.revision += 1
    plan.updated_at = datetime.now(UTC)
    plan.selected_calculation_id = None
    plan.selected_arrangement_id = None
    if not from_result:
        _apply_metadata(plan, payload)
    session.flush()


def update_plan(
    session: Session, payload: PlanUpdate, user_id: str, plan_id: UUID, key: UUID
) -> PlanData:
    scope = f"/api/v1/plans/{plan_id}"
    normalized = _normalized_payload(payload)
    with session.begin():
        replay = replay_request(session, user_id, scope, key, normalized)
        if replay is not None:
            return PlanData.model_validate(replay)
        plan = _locked_plan(session, user_id, plan_id, payload.expected_revision)
        source = None if payload.plan is not None else _load_source(session, user_id, payload, plan)
        inputs = payload.plan if payload.plan is not None else _source_plan_input(source)
        _replace_input(session, plan, inputs, user_id, from_result=source is not None)
        latest = _apply_result(session, plan, source, inputs) if source is not None else None
        data = _plan_data(plan, latest)
        save_response(session, user_id, scope, key, normalized, data)
    return data


def update_metadata(
    session: Session, payload: PlanMetadataUpdate, user_id: str, plan_id: UUID, key: UUID
) -> PlanData:
    scope = f"/api/v1/plans/{plan_id}/metadata"
    normalized = _normalized_payload(payload)
    with session.begin():
        replay = replay_request(session, user_id, scope, key, normalized)
        if replay is not None:
            return PlanData.model_validate(replay)
        plan = _locked_plan(session, user_id, plan_id, payload.expected_revision)
        _preserve_creation_receipt(session, plan, user_id)
        _apply_metadata(plan, payload)
        plan.updated_at = datetime.now(UTC)
        session.flush()
        latest = session.scalar(select(_latest_calculation_id(plan.plan_id, plan.revision)))
        data = _plan_data(plan, latest)
        save_response(session, user_id, scope, key, normalized, data)
    return data


def select_arrangement(
    session: Session, payload: PlanSelection, user_id: str, plan_id: UUID, key: UUID
) -> PlanSelectionData:
    scope = f"/api/v1/plans/{plan_id}/selection"
    with session.begin():
        replay = replay_request(session, user_id, scope, key, payload)
        if replay is not None:
            return PlanSelectionData.model_validate(replay)
        plan = _locked_plan(session, user_id, plan_id, payload.expected_revision)
        calculation = session.scalar(
            select(Calculation).where(
                Calculation.calculation_id == payload.calculation_id, Calculation.plan_id == plan_id
            )
        )
        if calculation is None:
            raise AppError(404, "CALCULATION_NOT_FOUND", "계산 결과를 찾을 수 없습니다.")
        result = calculation.result_payload or {}
        if calculation.plan_revision != plan.revision:
            raise AppError(409, "PLAN_REVISION_CONFLICT", "현재 계획의 계산 결과가 아닙니다.")
        valid = (
            result.get("result_code") == "LAYOUT_FOUND"
            and result.get("is_success") is True
            and any(
                str(candidate.get("arrangement_id")) == str(payload.arrangement_id)
                for candidate in result.get("arrangements", [])
            )
        )
        if not valid:
            raise AppError(422, "INVALID_ARRANGEMENT_SELECTION", "선택할 수 없는 배치안입니다.")
        plan.selected_calculation_id = payload.calculation_id
        plan.selected_arrangement_id = payload.arrangement_id
        session.flush()
        data = PlanSelectionData(
            plan_id=plan_id,
            revision=plan.revision,
            selected_calculation_id=payload.calculation_id,
            selected_arrangement_id=payload.arrangement_id,
        )
        save_response(session, user_id, scope, key, payload, data)
    return data
