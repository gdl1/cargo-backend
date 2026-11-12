from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.exceptions import AppError
from app.models.preview import CalculationPreview
from app.schemas.calculation import PreviewCreate, PreviewData
from app.services.calculations import (
    check_revision,
    complete_payload,
    get_base_arrangement,
    owned_plan,
    run_calculation,
    vehicle_snapshot,
)
from app.services.operations import replay_request, request_guard, save_response


def create_preview(
    session: Session,
    payload: PreviewCreate,
    user_id: str,
    key: UUID,
    settings: Settings,
    *,
    commit: bool = True,
    stacking_assessments: list[dict] | None = None,
) -> tuple[PreviewData, bool]:
    guard = (
        request_guard(session, user_id, "/calculations/preview", key) if commit else nullcontext()
    )
    with guard:
        return _create_preview(
            session,
            payload,
            user_id,
            key,
            settings,
            commit=commit,
            stacking_assessments=stacking_assessments,
        )


def _create_preview(
    session: Session,
    payload: PreviewCreate,
    user_id: str,
    key: UUID,
    settings: Settings,
    *,
    commit: bool = True,
    stacking_assessments: list[dict] | None = None,
) -> tuple[PreviewData, bool]:
    scope = "/calculations/preview"
    request = payload.model_dump(mode="json")
    with session.begin() if commit else nullcontext():
        replay = replay_request(session, user_id, scope, key, request)
        if replay is not None:
            return PreviewData.model_validate(replay), False
        source = (
            owned_plan(session, user_id, payload.source_plan_id) if payload.source_plan_id else None
        )
        if source is not None:
            check_revision(source, payload.expected_revision)
            if (
                source.vehicle_id != payload.plan.vehicle_id
                and payload.calculation_options.base_calculation_id
            ):
                raise AppError(
                    409, "BASE_LAYOUT_STALE", "다른 차량에서 기준 배치를 사용할 수 없습니다."
                )
        from app.services.plans import validate_plan_input

        validate_plan_input(session, payload.plan, user_id)
        supplied_ids = {item.item_id for item in payload.plan.items if item.item_id is not None}
        source_ids = {item.item_id for item in source.items if item.is_active} if source else set()
        if not supplied_ids <= source_ids:
            raise AppError(
                422, "VALIDATION_ERROR", "기존 짐 ID는 원본 계획의 항목만 사용할 수 있습니다."
            )
        snapshot = payload.plan.model_dump(mode="json")
        for item in snapshot["items"]:
            if item.get("item_id") is None:
                item["item_id"] = str(uuid4())
        vehicle = vehicle_snapshot(snapshot["vehicle_id"])
        base = get_base_arrangement(session, user_id, source, payload.calculation_options, vehicle)
    result = run_calculation(
        snapshot,
        vehicle,
        payload.calculation_options,
        settings,
        base_arrangement=base,
        stacking_assessments=stacking_assessments,
    )
    now = datetime.now(UTC)
    calculation = complete_payload(
        result,
        snapshot,
        vehicle,
        payload.calculation_options,
        plan_id=None,
        revision=None,
        calculation_id=None,
        created_at=now,
    )
    data = PreviewData(
        preview_id=uuid4(),
        expires_at=now + timedelta(seconds=settings.preview_ttl_seconds),
        source_plan_id=payload.source_plan_id,
        calculation=calculation,
    )
    with session.begin() if commit else nullcontext():
        replay = replay_request(session, user_id, scope, key, request)
        if replay is not None:
            return PreviewData.model_validate(replay), False
        if payload.source_plan_id is not None:
            source = owned_plan(session, user_id, payload.source_plan_id, lock=True)
            check_revision(source, payload.expected_revision)
            get_base_arrangement(session, user_id, source, payload.calculation_options, vehicle)
        if vehicle_snapshot(snapshot["vehicle_id"])["data_version"] != vehicle["data_version"]:
            raise AppError(
                409, "BASE_LAYOUT_STALE", "차량 정보가 변경되었습니다. 다시 계산해 주세요."
            )
        session.add(
            CalculationPreview(
                preview_id=data.preview_id,
                user_id=user_id,
                source_plan_id=payload.source_plan_id,
                source_plan_revision=payload.expected_revision,
                vehicle_data_version=vehicle["data_version"],
                input_snapshot=calculation["input_snapshot"],
                result_payload=calculation,
                created_at=now,
                expires_at=data.expires_at,
            )
        )
        session.flush()
        save_response(session, user_id, scope, key, request, data)
    return data, True
