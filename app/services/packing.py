"""정수 mm를 사용하는 제한 시간 내 직육면체 배치 탐색."""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from itertools import product
from time import monotonic
from uuid import uuid4

from app.schemas.packing import (
    ORIENTATIONS,
    PackingItem,
    PackingProblem,
    PackingResult,
    centimeters,
)


def cm_to_mm(value: Decimal | int | float) -> int:
    if isinstance(value, bool) or not isinstance(value, (Decimal, int, float)):
        raise ValueError("치수는 숫자여야 합니다.")
    value = Decimal(str(value))
    if not value.is_finite() or value < 0 or value * 10 != (value * 10).to_integral_value():
        raise ValueError("치수는 0 이상이며 cm 소수 첫째 자리까지 허용합니다.")
    return int(value * 10)


def oriented_size(width: int, depth: int, height: int, orientation: str) -> tuple[int, ...]:
    if orientation not in ORIENTATIONS:
        raise ValueError("허용되지 않은 회전 코드입니다.")
    axes = {"W": width, "D": depth, "H": height}
    return tuple(axes[axis] for axis in orientation)


class SearchLimit(Exception):
    pass


@dataclass
class SearchBudget:
    deadline: float
    maximum: int = 200_000
    evaluated: int = 0

    @classmethod
    def create(cls, seconds: float, maximum: int = 200_000) -> "SearchBudget":
        if isinstance(seconds, bool) or not math.isfinite(seconds) or seconds < 0:
            raise ValueError("제한 시간은 0 이상 유한한 숫자여야 합니다.")
        if type(maximum) is not int or maximum < 0:
            raise ValueError("탐색 한도는 0 이상 정수여야 합니다.")
        return cls(monotonic() + seconds, maximum)

    def check(self, *, visit: bool = False) -> None:
        if monotonic() >= self.deadline or self.evaluated >= self.maximum:
            raise SearchLimit
        self.evaluated += int(visit)


@dataclass(frozen=True)
class Box:
    item_id: str
    unit_index: int
    space_id: str
    position: tuple[int, ...]
    size: tuple[int, ...]
    orientation: str
    placement_id: str
    supporting_ids: tuple[str, ...] = ()


def _overlaps(first: Box, second: Box) -> bool:
    return first.space_id == second.space_id and all(
        start < other + other_size and other < start + size
        for start, size, other, other_size in zip(
            first.position, first.size, second.position, second.size, strict=True
        )
    )


def _support(
    box: Box, placed: Sequence[Box], allowed: set[tuple[str, str]]
) -> tuple[str, ...] | None:
    x, y, z = box.position
    width, depth, _ = box.size
    if z == 0:
        return ()
    rectangles: list[tuple[int, int, int, int]] = []
    supports: list[str] = []
    for lower in placed:
        lx, ly, lz = lower.position
        lw, ld, lh = lower.size
        if lower.space_id != box.space_id or lz + lh != z:
            continue
        left, right, front, back = (
            max(x, lx),
            min(x + width, lx + lw),
            max(y, ly),
            min(y + depth, ly + ld),
        )
        if left >= right or front >= back:
            continue
        if (lower.item_id, box.item_id) not in allowed:
            return None
        rectangles.append((left, right, front, back))
        supports.append(lower.placement_id)
    # 같은 높이 받침들의 합집합이 밑면 전체를 덮어야 합니다.
    boundaries = sorted({x, x + width, *(edge for rect in rectangles for edge in rect[:2])})
    area = 0
    for left, right in zip(boundaries, boundaries[1:], strict=False):
        intervals = sorted(
            (a, b) for start, end, a, b in rectangles if start <= left and right <= end
        )
        end, length = y, 0
        for front, back in intervals:
            length += max(0, back - max(front, end))
            end = max(end, back)
        area += (right - left) * length
    return tuple(sorted(supports)) if area == width * depth else None


def _allowed_pairs(assessments: Sequence[dict]) -> set[tuple[str, str]]:
    return {
        (str(row["lower_item_id"]), str(row["upper_item_id"]))
        for row in assessments
        if row.get("allowed") is True
    }


def _box(item: PackingItem, placement: dict) -> Box:
    orientation = placement["orientation"]
    if orientation not in item.allowed_orientations:
        raise ValueError("허용하지 않은 회전입니다.")
    return Box(
        item.item_id,
        placement["unit_index"],
        placement["space_id"],
        tuple(cm_to_mm(placement[axis]) for axis in ("x_cm", "y_cm", "z_cm")),
        oriented_size(
            cm_to_mm(item.width_cm),
            cm_to_mm(item.depth_cm),
            cm_to_mm(item.height_cm),
            orientation,
        ),
        orientation,
        str(placement["placement_id"]),
        tuple(str(value) for value in placement.get("supporting_placement_ids", [])),
    )


def validate_packing(
    problem: PackingProblem,
    placements: Sequence[dict],
    *,
    stacking_assessments: Sequence[dict] = (),
    expected_units: set[tuple[str, int]] | None = None,
) -> None:
    """탐색과 별도로 수량·회전·경계·입구·겹침·받침 참조를 검증합니다."""
    by_item = {item.item_id: item for item in problem.items}
    spaces = {space.space_id: space for space in problem.spaces}
    units = [(str(row["item_id"]), row["unit_index"]) for row in placements]
    if expected_units is None:
        expected_units = {
            (item.item_id, index) for item in problem.items for index in range(1, item.quantity + 1)
        }
    if len(set(units)) != len(units) or set(units) != expected_units:
        raise ValueError("배치 낱개 목록이 계산 입력과 일치하지 않습니다.")
    if len({str(row["placement_id"]) for row in placements}) != len(placements):
        raise ValueError("배치 ID는 중복할 수 없습니다.")
    if [row["load_order"] for row in placements] != list(range(1, len(placements) + 1)):
        raise ValueError("적재 순서는 1부터 연속되어야 합니다.")
    checked: list[Box] = []
    for row in placements:
        item = by_item[str(row["item_id"])]
        if type(row["unit_index"]) is not int or not 1 <= row["unit_index"] <= item.quantity:
            raise ValueError("낱개 번호가 수량 범위를 벗어났습니다.")
        box = _box(item, row)
        if box.space_id not in spaces:
            raise ValueError("등록되지 않은 공간입니다.")
        space = spaces[box.space_id]
        bounds = tuple(
            cm_to_mm(value) for value in (space.width_cm, space.depth_cm, space.height_cm)
        )
        if any(
            p + size > limit for p, size, limit in zip(box.position, box.size, bounds, strict=True)
        ):
            raise ValueError("공간 경계를 벗어난 배치입니다.")
        if space.opening and (
            box.size[0] > cm_to_mm(space.opening.width_cm)
            or box.size[2] > cm_to_mm(space.opening.height_cm)
        ):
            raise ValueError("입구 폭이나 높이를 초과합니다.")
        if any(_overlaps(box, other) for other in checked):
            raise ValueError("짐이 겹칩니다.")
        supports = _support(box, checked, _allowed_pairs(stacking_assessments))
        if supports is None or set(supports) != set(box.supporting_ids):
            raise ValueError("밑면 받침 또는 쌓기 조건을 충족하지 않습니다.")
        checked.append(box)


def find_layout(
    problem: PackingProblem,
    *,
    budget: SearchBudget,
    selected_units: set[tuple[str, int]] | None = None,
    fixed_placements: Sequence[dict] = (),
    stacking_assessments: Sequence[dict] = (),
) -> list[dict] | None:
    """면 끝점 후보를 되돌아가며 탐색합니다. 전역 최적해를 보장하지 않습니다."""
    budget.check()
    items = {item.item_id: item for item in problem.items}
    wanted = (
        selected_units
        if selected_units is not None
        else {
            (item.item_id, index) for item in problem.items for index in range(1, item.quantity + 1)
        }
    )
    fixed_units = {(str(row["item_id"]), row["unit_index"]) for row in fixed_placements}
    if not wanted or not fixed_units <= wanted:
        return None
    validate_packing(
        problem,
        fixed_placements,
        stacking_assessments=stacking_assessments,
        expected_units=fixed_units,
    )
    placed = [_box(items[str(row["item_id"])], row) for row in fixed_placements]
    remaining = wanted - fixed_units
    allowed = _allowed_pairs(stacking_assessments)
    spaces = sorted(problem.spaces, key=lambda space: (space.kind != "TRUNK", space.space_id))
    bounds = {
        space.space_id: tuple(
            cm_to_mm(value) for value in (space.width_cm, space.depth_cm, space.height_cm)
        )
        for space in spaces
    }
    sizes = {
        item.item_id: (cm_to_mm(item.width_cm), cm_to_mm(item.depth_cm), cm_to_mm(item.height_cm))
        for item in problem.items
    }
    if sum(math.prod(sizes[item_id]) for item_id, _ in wanted) > sum(
        math.prod(size) for size in bounds.values()
    ):
        return None
    possible_sizes = {}
    for item_id, item in items.items():
        possible_sizes[item_id] = [
            shape
            for orientation in item.allowed_orientations
            if (shape := oriented_size(*sizes[item_id], orientation))
            and any(
                all(
                    value <= limit
                    for value, limit in zip(shape, bounds[space.space_id], strict=True)
                )
                and (
                    space.opening is None
                    or (
                        shape[0] <= cm_to_mm(space.opening.width_cm)
                        and shape[2] <= cm_to_mm(space.opening.height_cm)
                    )
                )
                for space in spaces
            )
        ]
    if any(not possible_sizes[item_id] for item_id, _ in wanted):
        return None
    if not allowed and sum(
        min(shape[0] * shape[1] for shape in possible_sizes[item_id]) for item_id, _ in wanted
    ) > sum(size[0] * size[1] for size in bounds.values()):
        return None

    def search(unplaced: set[tuple[str, int]]) -> bool:
        budget.check()
        if not unplaced:
            return True
        # 같은 물건의 낱개 번호만 바꾼 가지는 생략합니다.
        next_units: dict[str, int] = {}
        for item_id, index in sorted(unplaced):
            next_units.setdefault(item_id, index)
        order = sorted(next_units, key=lambda key: (-math.prod(sizes[key]), key))
        for item_id in order:
            unit_index = next_units[item_id]
            seen_sizes: set[tuple[int, ...]] = set()
            for orientation in items[item_id].allowed_orientations:
                size = oriented_size(*sizes[item_id], orientation)
                if size in seen_sizes:
                    continue
                seen_sizes.add(size)
                for space in spaces:
                    budget.check()
                    limits = bounds[space.space_id]
                    if any(a > b for a, b in zip(size, limits, strict=True)) or (
                        space.opening
                        and (
                            size[0] > cm_to_mm(space.opening.width_cm)
                            or size[2] > cm_to_mm(space.opening.height_cm)
                        )
                    ):
                        continue
                    neighbors = [box for box in placed if box.space_id == space.space_id]
                    axes = []
                    for axis in (0, 1):
                        points = {0, limits[axis] - size[axis]}
                        for other in neighbors:
                            start, end = (
                                other.position[axis],
                                other.position[axis] + other.size[axis],
                            )
                            points.update((start, end, start - size[axis], end - size[axis]))
                        axes.append(
                            sorted(p for p in points if 0 <= p <= limits[axis] - size[axis])
                        )
                    heights = sorted({0, *(box.position[2] + box.size[2] for box in neighbors)})
                    for z, y, x in product(heights, axes[1], axes[0]):
                        budget.check(visit=True)
                        if z + size[2] > limits[2]:
                            continue
                        box = Box(
                            item_id,
                            unit_index,
                            space.space_id,
                            (x, y, z),
                            size,
                            orientation,
                            str(uuid4()),
                        )
                        if any(_overlaps(box, other) for other in neighbors):
                            continue
                        support = _support(box, neighbors, allowed)
                        if support is None:
                            continue
                        placed.append(
                            Box(
                                item_id,
                                unit_index,
                                space.space_id,
                                (x, y, z),
                                size,
                                orientation,
                                box.placement_id,
                                support,
                            )
                        )
                        if search(unplaced - {(item_id, unit_index)}):
                            return True
                        placed.pop()
        return False

    if not search(remaining):
        return None
    budget.check()
    result = [
        {
            "placement_id": box.placement_id,
            "item_id": box.item_id,
            "unit_index": box.unit_index,
            "space_id": box.space_id,
            "x_cm": centimeters(box.position[0]),
            "y_cm": centimeters(box.position[1]),
            "z_cm": centimeters(box.position[2]),
            "orientation": box.orientation,
            "load_order": index,
            "supporting_placement_ids": list(box.supporting_ids),
        }
        for index, box in enumerate(placed, start=1)
    ]
    validate_packing(
        problem, result, stacking_assessments=stacking_assessments, expected_units=wanted
    )
    return result


def pack(
    problem: PackingProblem,
    *,
    timeout_seconds: float = 2.0,
    max_candidates: int = 200_000,
    stacking_assessments: Sequence[dict] = (),
) -> PackingResult:
    budget = SearchBudget.create(timeout_seconds, max_candidates)
    try:
        placements = find_layout(problem, budget=budget, stacking_assessments=stacking_assessments)
        return PackingResult(
            result_code="LAYOUT_FOUND" if placements else "NO_LAYOUT_FOUND",
            search_complete=True,
            placements=placements or [],
            evaluated_candidates=budget.evaluated,
        )
    except SearchLimit:
        return PackingResult(
            result_code="TIME_LIMIT", search_complete=False, evaluated_candidates=budget.evaluated
        )
