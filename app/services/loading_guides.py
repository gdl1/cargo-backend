import re
from copy import deepcopy
from datetime import UTC, datetime
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.exceptions import AppError
from app.models.calculation import Calculation
from app.models.plan import Plan
from app.models.preview import CalculationPreview
from app.schemas.calculation import CalculationData
from app.schemas.loading_guide import (
    GeneratedGuide,
    LoadingGuideCreate,
    LoadingGuideData,
    LoadingGuideStep,
)
from app.schemas.packing import MAX_UNITS, PackingProblem
from app.services.calculations import owned_plan, result_payload
from app.services.llm import LlmUnavailable, ResponsesClient
from app.services.operations import (
    replay_request,
    request_fingerprint,
    request_guard,
    save_response,
)
from app.services.packing import cm_to_mm, oriented_size, placement_space_ids, validate_packing

SCOPE = "/ai/loading-guides"
FALLBACK_REPLY = "위 그림을 보고 배치해 주세요."

UNSUPPORTED_SUMMARY = re.compile(
    r"[a-zA-Z\d]|<[^>]+>|좌표|모서리|폴딩|회전 코드|치수|센티|미터|인치|"
    r"하중|무게|킬로|안전|보장|문제\s*없|안심|견딜|"
    r"왼쪽|오른쪽|입구|안쪽|좌우|앞뒤|위아래|눕혀|세워"
)


def _unavailable() -> AppError:
    return AppError(
        503,
        "CALCULATION_RESULT_UNAVAILABLE",
        "계산의 배치 정보를 확인할 수 없습니다. 새 계산을 요청해 주세요.",
    )


def _load_source(session: Session, user_id: str, payload: LoadingGuideCreate) -> dict:
    try:
        return _load_context(session, user_id, payload)
    except (ValidationError, ValueError, KeyError, TypeError) as exc:
        raise _unavailable() from exc


def _load_context(session: Session, user_id: str, payload: LoadingGuideCreate) -> dict:
    source_plan_id = payload.plan_id
    if payload.preview_id is not None:
        preview = session.scalar(
            select(CalculationPreview)
            .where(
                CalculationPreview.preview_id == payload.preview_id,
                CalculationPreview.user_id == user_id,
            )
            .execution_options(populate_existing=True)
        )
        if preview is None:
            raise AppError(404, "PREVIEW_NOT_FOUND", "계산 미리보기를 찾을 수 없습니다.")
        if preview.expires_at <= datetime.now(UTC):
            raise AppError(410, "PREVIEW_EXPIRED", "계산 미리보기가 만료되었습니다.")
        source_plan_id = preview.source_plan_id
        try:
            result = CalculationData.model_validate(preview.result_payload).model_dump(mode="json")
        except ValidationError as exc:
            raise _unavailable() from exc
    else:
        owned_plan(session, user_id, payload.plan_id)
        calculation = session.scalar(
            select(Calculation)
            .where(
                Calculation.plan_id == payload.plan_id,
                Calculation.calculation_id == payload.calculation_id,
            )
            .execution_options(populate_existing=True)
        )
        if calculation is None:
            raise AppError(404, "CALCULATION_NOT_FOUND", "계산 결과를 찾을 수 없습니다.")
        result = CalculationData.model_validate(result_payload(calculation)).model_dump(mode="json")

    matches = [
        row
        for row in result["arrangements"] + result["alternatives"]
        if row["arrangement_id"] == str(payload.arrangement_id)
    ]
    if not matches:
        raise AppError(404, "ARRANGEMENT_NOT_FOUND", "해당 계산의 배치안을 찾을 수 없습니다.")
    if len(matches) != 1:
        raise _unavailable()
    arrangement = deepcopy(matches[0])
    items = {str(item["item_id"]): item for item in result["input_snapshot"].get("items", [])}
    placements = sorted(arrangement["placements"], key=lambda row: row["load_order"])
    if not placements or len(placements) > MAX_UNITS:
        raise _unavailable()
    identifiers = {row["placement_id"] for row in placements}
    if len(identifiers) != len(placements) or [row["load_order"] for row in placements] != list(
        range(1, len(placements) + 1)
    ):
        raise _unavailable()
    spaces = {space["space_id"]: space for space in arrangement["packing_spaces"]}
    if len(spaces) != len(arrangement["packing_spaces"]):
        raise _unavailable()
    seen = set()
    for placement in placements:
        UUID(placement["placement_id"])
        if (
            placement["item_id"] not in items
            or not set(placement_space_ids(placement)) <= spaces.keys()
            or not set(placement["supporting_placement_ids"]) <= seen
        ):
            raise _unavailable()
        seen.add(placement["placement_id"])
    # 최신 차량 모델을 섞지 않고 계산 당시 공간 안의 배치인지 다시 확인합니다.
    validate_packing(
        PackingProblem.model_validate(
            {"spaces": list(spaces.values()), "items": list(items.values())}
        ),
        placements,
        stacking_assessments=arrangement.get("stacking_assessments", []),
        expected_units={(row["item_id"], row["unit_index"]) for row in placements},
    )
    fixed_ids = set()
    if result["packing_mode"] == "KEEP_EXISTING":
        base = session.scalar(
            select(Calculation)
            .join(Plan, Plan.plan_id == Calculation.plan_id)
            .where(
                Calculation.calculation_id == result["base_calculation_id"],
                Calculation.plan_id == source_plan_id,
                Plan.user_id == user_id,
            )
            .execution_options(populate_existing=True)
        )
        if base is None or not base.result_payload:
            raise _unavailable()
        base_arrangement = next(
            (
                candidate
                for candidate in base.result_payload.get("arrangements", [])
                if candidate["arrangement_id"] == result["base_arrangement_id"]
            ),
            None,
        )
        if base_arrangement is None:
            raise _unavailable()
        fixed_ids = {row["placement_id"] for row in base_arrangement["placements"]}
        if not fixed_ids <= identifiers:
            raise _unavailable()
    return {
        "vehicle_data_version": result["vehicle_data_version"],
        "packing_mode": result["packing_mode"],
        "current_folded_segments": result.get("current_folded_segments"),
        "target_folded_segments": arrangement["target_folded_segments"],
        "passenger_seats": arrangement["passenger_seats"],
        "exclusions": arrangement.get("exclusions", []),
        "items": list(items.values()),
        "spaces": list(spaces.values()),
        "placements": placements,
        "fixed_placement_ids": sorted(fixed_ids),
    }


def _fallback(payload: LoadingGuideCreate) -> LoadingGuideData:
    return LoadingGuideData(
        **payload.model_dump(), source="FALLBACK", reply=FALLBACK_REPLY, steps=[]
    )


def _space_name(space: dict) -> str:
    if space["kind"] == "TRUNK":
        return "트렁크"
    rows = {seat.split("_")[1] for seat in space["seat_codes"]}
    seat = "좌석"
    if len(rows) == 1:
        seat = {"1": "앞좌석", "2": "뒷좌석", "3": "맨 뒷좌석"}.get(rows.pop(), seat)
    return f"접힌 {seat}" if space["kind"] == "FOLDED_SEAT" else seat


def _near_edge(start: int, size: int, limit: int, near: str, far: str) -> str:
    end_gap = limit - start - size
    if end_gap > start and (start == 0 or (end_gap - start) * 5 >= limit):
        return near
    if start > end_gap and (end_gap == 0 or (start - end_gap) * 5 >= limit):
        return far
    return ""


def _floor_location(placement: dict, space: dict, width: int, depth: int) -> str:
    if space["kind"] != "TRUNK":
        return f"그림에 표시된 {_space_name(space)} 자리에"
    # 트렁크의 y=0은 차 뒤쪽, 즉 트렁크 입구 쪽입니다.
    front_back = _near_edge(
        cm_to_mm(placement["y_cm"]), depth, cm_to_mm(space["depth_cm"]), "입구 쪽", "안쪽"
    )
    left_right = _near_edge(
        cm_to_mm(placement["x_cm"]), width, cm_to_mm(space["width_cm"]), "왼쪽", "오른쪽"
    )
    return " ".join(filter(None, ("트렁크", front_back, left_right, "바닥에")))


def _direction(size: tuple[int, ...], orientation: str) -> str:
    width, depth, height = size
    if height > max(width, depth):
        return "긴 쪽을 세워 "
    direction = ""
    if width > max(depth, height):
        direction = "긴 쪽이 좌우로 향하게 "
    elif depth > max(width, height):
        direction = "긴 쪽이 앞뒤로 향하게 "
    posture = "" if orientation[-1] == "H" else "옆으로 눕혀 "
    return posture + direction


def _object_phrase(name: str) -> str:
    last = ord(name[-1])
    if 0xAC00 <= last <= 0xD7A3:
        particle = "을" if (last - 0xAC00) % 28 else "를"
        return name + particle
    return f"‘{name}’ 짐을"


def _guide_steps(context: dict, spaces: dict) -> list[LoadingGuideStep]:
    items = {item["item_id"]: item for item in context["items"]}
    fixed = set(context["fixed_placement_ids"])
    names = {}
    steps = []
    for placement in context["placements"]:
        placement_id = placement["placement_id"]
        item = items[placement["item_id"]]
        name = item["name"]
        if item["quantity"] > 1:
            name = f"{name} {placement['unit_index']}번"
        names[placement_id] = name
        cargo = _object_phrase(name)
        if placement_id in fixed:
            text = f"이미 실은 {cargo} 현재 위치와 방향 그대로 두세요."
        else:
            size = oriented_size(
                cm_to_mm(item["width_cm"]),
                cm_to_mm(item["depth_cm"]),
                cm_to_mm(item["height_cm"]),
                placement["orientation"],
            )
            supports = placement["supporting_placement_ids"]
            spanning = placement.get("spanning_space_id")
            span_location = (
                f"트렁크에서 {_space_name(spaces[spanning])}까지 걸치도록" if spanning else ""
            )
            if len(supports) == 1:
                location = f"{names[supports[0]]} 위에"
            elif supports:
                location = "그림에 표시된 받침 짐 위에"
            else:
                location = span_location or _floor_location(
                    placement, spaces[placement["space_id"]], *size[:2]
                )
            if supports and span_location:
                location += f" {span_location}"
            direction = _direction(size, placement["orientation"])
            text = f"{cargo} {location} {direction}놓아 주세요."
        steps.append(
            LoadingGuideStep(order=placement["load_order"], placement_id=placement_id, text=text)
        )
    return steps


def _generate(payload: LoadingGuideCreate, context: dict, settings: Settings) -> LoadingGuideData:
    try:
        spaces = {space["space_id"]: space for space in context["spaces"]}
        used_spaces = {
            _space_name(spaces[space_id])
            for placement in context["placements"]
            for space_id in placement_space_ids(placement)
        }
        generated = ResponsesClient(settings).call_function(
            "create_loading_guide",
            GeneratedGuide,
            "차량 화면에서 읽어줄 적재 안내의 첫 문장을 한국어로 작성하세요. "
            "제공된 적재 공간과 쌓기 여부를 바탕으로 사용자가 그림을 보며 할 일을 "
            "짧은 한 문장으로 말해 주세요. "
            "예를 들어 트렁크만 쓰는 경우 ‘그림에 나온 순서대로 트렁크에 짐을 실어 주세요.’라고 "
            "말합니다. "
            "짐마다 놓을 자리와 방향, 좌석 상태와 제외한 짐은 서버가 따로 안내합니다. "
            "좌표, 치수, 숫자, 영문 코드, 좌석 조작, 새 배치 제안이나 안전 판단을 넣지 마세요.",
            {
                "적재 공간": sorted(used_spaces),
                "쌓는 짐이 있음": any(p["supporting_placement_ids"] for p in context["placements"]),
            },
        )
        reply = GeneratedGuide.model_validate(generated).reply.strip()
        if not reply or any(ord(char) < 32 for char in reply) or UNSUPPORTED_SUMMARY.search(reply):
            raise LlmUnavailable("Unsupported guide summary")
        steps = _guide_steps(context, spaces)
        if context["packing_mode"] == "KEEP_EXISTING":
            reply = "기존 짐은 현재 위치와 방향을 유지하고 그림에 표시된 새 짐만 추가해 주세요."
        current_folds = context["current_folded_segments"]
        target_folds = set(context["target_folded_segments"])
        if target_folds != set(current_folds or []):
            reply += " 좌석은 그림에 나온 상태인지 확인해 주세요."
        if context["exclusions"]:
            names = ", ".join(f"{row['name']} {row['quantity']}개" for row in context["exclusions"])
            reply += f" 이번 배치에서는 {names}를 빼 주세요."
        return LoadingGuideData(**payload.model_dump(), source="AI", reply=reply, steps=steps)
    except (LlmUnavailable, ValidationError, ValueError, TypeError, KeyError, TimeoutError):
        return _fallback(payload)


def create_loading_guide(
    session: Session,
    payload: LoadingGuideCreate,
    user_id: str,
    key: UUID,
    settings: Settings,
) -> LoadingGuideData:
    request = payload.model_dump(mode="json")
    with request_guard(session, user_id, SCOPE, key):
        with session.begin():
            context = _load_source(session, user_id, payload)
            replay = replay_request(session, user_id, SCOPE, key, request)
            if replay is not None:
                return LoadingGuideData.model_validate(replay)
        data = _generate(payload, context, settings)
        with session.begin():
            current = _load_source(session, user_id, payload)
            if request_fingerprint(current) != request_fingerprint(context):
                raise AppError(
                    409, "GUIDE_SOURCE_CHANGED", "배치 정보가 변경되었습니다. 다시 요청해 주세요."
                )
            replay = replay_request(session, user_id, SCOPE, key, request)
            if replay is not None:
                return LoadingGuideData.model_validate(replay)
            save_response(session, user_id, SCOPE, key, request, data)
        return data
