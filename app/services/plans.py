import hashlib
import json
from decimal import Decimal
from typing import NoReturn
from uuid import UUID

from sqlalchemy import func, select, text, true
from sqlalchemy.orm import Session, selectinload

from app.core.exceptions import AppError
from app.models import Calculation, Plan, PlanCarSeat, PlanItem, PlanRetainedSeat, Vehicle
from app.schemas.plan import PlanCreate, PlanData, PlanItemData, PlanListData, PlanSummary
from app.schemas.vehicle import VehicleDetail
from app.services.vehicles import get_vehicle_catalog


def _decimal_text(value: Decimal) -> str:
    value_text = format(value, "f")
    return value_text.rstrip("0").rstrip(".") if "." in value_text else value_text


def _request_hash(payload: PlanCreate) -> str:
    values = payload.model_dump()
    for field in ("car_seats", "retained_seats"):
        values[field] = sorted(values[field])
    normalized = json.dumps(
        values,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=_decimal_text,
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


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


def _plan_data(plan: Plan, latest_calculation_id: UUID | None = None) -> PlanData:
    return PlanData(
        plan_id=plan.plan_id,
        vehicle_id=plan.vehicle_id,
        passenger_count=plan.passenger_count,
        car_seats=sorted(seat.seat_code for seat in plan.car_seats),
        retained_seats=sorted(seat.seat_code for seat in plan.retained_seats),
        items=[
            PlanItemData(
                item_id=item.item_id,
                name=item.name,
                width_cm=item.width_cm,
                depth_cm=item.depth_cm,
                height_cm=item.height_cm,
                quantity=item.quantity,
            )
            for item in sorted(plan.items, key=lambda item: item.position)
        ],
        created_at=plan.created_at,
        # 저장·재시도에는 기본값 None, 조회에는 최신 계산 ID를 전달합니다.
        latest_calculation_id=latest_calculation_id,
    )


def create_plan(
    session: Session, payload: PlanCreate, user_id: str, key: UUID
) -> tuple[PlanData, bool]:
    request_hash = _request_hash(payload)
    with session.begin():
        locked = session.scalar(
            text("SELECT pg_try_advisory_xact_lock(:lock_id)"),
            {"lock_id": _lock_id(user_id, key)},
        )
        if not locked:
            raise AppError(
                409, "REQUEST_IN_PROGRESS", "같은 요청을 처리 중입니다. 잠시 후 다시 요청해 주세요."
            )

        plan = session.scalar(
            select(Plan)
            .where(Plan.user_id == user_id, Plan.idempotency_key == key)
            .options(
                selectinload(Plan.items),
                selectinload(Plan.car_seats),
                selectinload(Plan.retained_seats),
            )
        )
        created = plan is None
        if plan is not None:
            if plan.request_hash != request_hash:
                raise AppError(409, "IDEMPOTENCY_KEY_REUSED", "다른 입력에 사용한 요청 키입니다.")
        else:
            vehicle = get_vehicle_catalog().get(payload.vehicle_id)
            if vehicle is None or session.get(Vehicle, payload.vehicle_id) is None:
                raise AppError(404, "VEHICLE_NOT_FOUND", "차량을 찾을 수 없습니다.")
            _validate_vehicle(payload, vehicle)
            plan = Plan(
                user_id=user_id,
                vehicle_id=payload.vehicle_id,
                passenger_count=payload.passenger_count,
                idempotency_key=key,
                request_hash=request_hash,
                items=[
                    PlanItem(**item.model_dump(), position=index)
                    for index, item in enumerate(payload.items, start=1)
                ],
                car_seats=[PlanCarSeat(seat_code=seat) for seat in payload.car_seats],
                retained_seats=[
                    PlanRetainedSeat(seat_code=seat) for seat in payload.retained_seats
                ],
            )
            session.add(plan)
            session.flush()

        data = _plan_data(plan)
        data.model_dump_json()
    return data, created


def _latest_calculation_id(plan_id):
    return (
        select(Calculation.calculation_id)
        .where(Calculation.plan_id == plan_id)
        .order_by(Calculation.created_at.desc(), Calculation.calculation_id.desc())
        .limit(1)
        .scalar_subquery()
    )


def list_plans(session: Session, user_id: str, limit: int, offset: int) -> PlanListData:
    page = (
        select(Plan.plan_id, Plan.vehicle_id, Plan.passenger_count, Plan.created_at)
        .where(Plan.user_id == user_id)
        .order_by(Plan.created_at.desc(), Plan.plan_id.desc())
        .limit(limit)
        .offset(offset)
        .cte("plan_page")
    )
    total = (
        select(func.count().label("total"))
        .select_from(Plan)
        .where(Plan.user_id == user_id)
        .subquery()
    )
    item_counts = (
        select(
            PlanItem.plan_id,
            func.count().label("item_type_count"),
            func.sum(PlanItem.quantity).label("item_total_quantity"),
        )
        .join(page, page.c.plan_id == PlanItem.plan_id)
        .group_by(PlanItem.plan_id)
        .subquery()
    )
    # 개수와 페이지를 한 SQL에서 읽어 동시 저장 중에도 같은 시점의 결과를 반환합니다.
    statement = (
        select(
            total.c.total,
            page,
            Vehicle.name.label("vehicle_name"),
            func.coalesce(item_counts.c.item_type_count, 0).label("item_type_count"),
            func.coalesce(item_counts.c.item_total_quantity, 0).label("item_total_quantity"),
            _latest_calculation_id(page.c.plan_id).label("latest_calculation_id"),
        )
        .select_from(
            total.outerjoin(page, true())
            .outerjoin(Vehicle, Vehicle.vehicle_id == page.c.vehicle_id)
            .outerjoin(item_counts, item_counts.c.plan_id == page.c.plan_id)
        )
        .order_by(page.c.created_at.desc(), page.c.plan_id.desc())
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
        select(Plan, _latest_calculation_id(Plan.plan_id))
        .where(Plan.plan_id == plan_id, Plan.user_id == user_id)
        .options(
            selectinload(Plan.items),
            selectinload(Plan.car_seats),
            selectinload(Plan.retained_seats),
        )
    ).one_or_none()
    if row is None:
        raise AppError(404, "PLAN_NOT_FOUND", "적재 계획을 찾을 수 없습니다.")
    return _plan_data(row[0], latest_calculation_id=row[1])
