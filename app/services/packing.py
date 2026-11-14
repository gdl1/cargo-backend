"""정수 mm를 사용하는 제한 시간 내 직육면체 배치 탐색."""

import math
from collections.abc import Sequence
from dataclasses import dataclass, replace
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
    space_ids: tuple[str, ...]
    position: tuple[int, ...]
    size: tuple[int, ...]
    orientation: str
    placement_id: str
    supporting_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class PackingRegion:
    space_ids: tuple[str, ...]
    minimum: tuple[int, ...]
    maximum: tuple[int, ...]
    opening: tuple[int, int] | None
    boundary_y: int | None = None

    def start_limits(self, size: tuple[int, ...]) -> tuple[tuple[int, ...], tuple[int, ...]] | None:
        if self.opening and (size[0] > self.opening[0] or size[2] > self.opening[1]):
            return None
        x, y, z = self.minimum
        right, front, top = (end - dim for end, dim in zip(self.maximum, size, strict=True))
        if self.boundary_y is not None:
            # 연결 배치는 경계 양쪽을 모두 점유해야 합니다. 계산 좌표의 최소 단위는 1mm입니다.
            y = max(y, self.boundary_y - size[1] + 1)
            front = min(front, self.boundary_y - 1)
        if x > right or y > front or z > top:
            return None
        return (x, y, z), (right, front, top)


def _packing_regions(problem: PackingProblem) -> dict[tuple[str, ...], PackingRegion]:
    spaces = sorted(problem.spaces, key=lambda space: (space.kind != "TRUNK", space.space_id))
    regions = {}
    for space in spaces:
        origin = space.origin_cm
        # 공간의 차량 기준 원점은 음수도 허용합니다.
        minimum = tuple(int(value * 10) for value in (origin.x_cm, origin.y_cm, origin.z_cm))
        maximum = tuple(
            start + cm_to_mm(size)
            for start, size in zip(
                minimum, (space.width_cm, space.depth_cm, space.height_cm), strict=True
            )
        )
        opening = space.opening
        entry = (cm_to_mm(opening.width_cm), cm_to_mm(opening.height_cm)) if opening else None
        ids = (space.space_id,)
        regions[ids] = PackingRegion(ids, minimum, maximum, entry)

    for space in spaces:
        if space.connected_to_space_id is None:
            continue
        trunk = regions[(space.connected_to_space_id,)]
        rear = regions[(space.space_id,)]
        entries = [region.opening for region in (trunk, rear) if region.opening is not None]
        entry = None
        if entries:
            entry = (min(size[0] for size in entries), min(size[1] for size in entries))
        ids = trunk.space_ids + rear.space_ids
        # 인접한 두 공간의 공통 폭·높이 안에서만 한 짐이 경계를 넘을 수 있습니다.
        regions[ids] = PackingRegion(
            ids,
            (max(trunk.minimum[0], rear.minimum[0]), trunk.minimum[1], trunk.minimum[2]),
            (
                min(trunk.maximum[0], rear.maximum[0]),
                rear.maximum[1],
                min(trunk.maximum[2], rear.maximum[2]),
            ),
            entry,
            boundary_y=rear.minimum[1],
        )
    return regions


def placement_space_ids(placement: dict) -> tuple[str, ...]:
    anchor = placement["space_id"]
    if "spanning_space_id" in placement:
        return (anchor, placement["spanning_space_id"])
    return (anchor,)


def _overlaps(first: Box, second: Box) -> bool:
    return any(space_id in second.space_ids for space_id in first.space_ids) and all(
        start < other + other_size and other < start + size
        for start, size, other, other_size in zip(
            first.position, first.size, second.position, second.size, strict=True
        )
    )


def _support(
    box: Box, placed: Sequence[Box], allowed: set[tuple[str, str]], floor: int
) -> tuple[str, ...] | None:
    x, y, z = box.position
    width, depth, _ = box.size
    if z == floor:
        return ()
    rectangles: list[tuple[int, int, int, int]] = []
    supports: list[str] = []
    for lower in placed:
        lx, ly, lz = lower.position
        lw, ld, lh = lower.size
        if lz + lh != z or not any(space_id in lower.space_ids for space_id in box.space_ids):
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


def _box(item: PackingItem, placement: dict, origin: tuple[int, ...]) -> Box:
    orientation = placement["orientation"]
    if orientation not in item.allowed_orientations:
        raise ValueError("허용하지 않은 회전입니다.")
    return Box(
        item.item_id,
        placement["unit_index"],
        placement_space_ids(placement),
        tuple(
            start + cm_to_mm(placement[axis])
            for start, axis in zip(origin, ("x_cm", "y_cm", "z_cm"), strict=True)
        ),
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
    regions = _packing_regions(problem)
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
    allowed = _allowed_pairs(stacking_assessments)
    for row in placements:
        item = by_item[str(row["item_id"])]
        if type(row["unit_index"]) is not int or not 1 <= row["unit_index"] <= item.quantity:
            raise ValueError("낱개 번호가 수량 범위를 벗어났습니다.")
        region = regions.get(placement_space_ids(row))
        if region is None:
            raise ValueError("등록되지 않은 공간 또는 연결입니다.")
        box = _box(item, row, regions[(row["space_id"],)].minimum)
        limits = region.start_limits(box.size)
        if limits is None or any(
            not lower <= position <= upper
            for position, lower, upper in zip(box.position, *limits, strict=True)
        ):
            raise ValueError("공간 경계나 입구 규격을 벗어난 배치입니다.")
        if any(_overlaps(box, other) for other in checked):
            raise ValueError("짐이 겹칩니다.")
        supports = _support(box, checked, allowed, region.minimum[2])
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
    regions = _packing_regions(problem)
    placed = [
        _box(items[str(row["item_id"])], row, regions[(row["space_id"],)].minimum)
        for row in fixed_placements
    ]
    remaining = wanted - fixed_units
    allowed = _allowed_pairs(stacking_assessments)
    space_sizes = [
        tuple(cm_to_mm(value) for value in (space.width_cm, space.depth_cm, space.height_cm))
        for space in problem.spaces
    ]
    sizes = {
        item.item_id: (cm_to_mm(item.width_cm), cm_to_mm(item.depth_cm), cm_to_mm(item.height_cm))
        for item in problem.items
    }
    if sum(math.prod(sizes[item_id]) for item_id, _ in wanted) > sum(
        math.prod(size) for size in space_sizes
    ):
        return None
    possible_sizes = {}
    for item_id, item in items.items():
        possible_sizes[item_id] = [
            shape
            for orientation in item.allowed_orientations
            if (shape := oriented_size(*sizes[item_id], orientation))
            and any(region.start_limits(shape) is not None for region in regions.values())
        ]
    if any(not possible_sizes[item_id] for item_id, _ in wanted):
        return None
    if not allowed and sum(
        min(shape[0] * shape[1] for shape in possible_sizes[item_id]) for item_id, _ in wanted
    ) > sum(size[0] * size[1] for size in space_sizes):
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
                for region in regions.values():
                    budget.check()
                    limits = region.start_limits(size)
                    if limits is None:
                        continue
                    lower, upper = limits
                    neighbors = [
                        box
                        for box in placed
                        if any(code in box.space_ids for code in region.space_ids)
                    ]
                    axes = []
                    for axis in (0, 1):
                        points = {lower[axis], upper[axis]}
                        for other in neighbors:
                            start, end = (
                                other.position[axis],
                                other.position[axis] + other.size[axis],
                            )
                            points.update((start, end, start - size[axis], end - size[axis]))
                        axes.append(sorted(p for p in points if lower[axis] <= p <= upper[axis]))
                    heights = sorted(
                        {lower[2], *(box.position[2] + box.size[2] for box in neighbors)}
                    )
                    for z, y, x in product(heights, axes[1], axes[0]):
                        budget.check(visit=True)
                        if not lower[2] <= z <= upper[2]:
                            continue
                        box = Box(
                            item_id,
                            unit_index,
                            region.space_ids,
                            (x, y, z),
                            size,
                            orientation,
                            str(uuid4()),
                        )
                        if any(_overlaps(box, other) for other in neighbors):
                            continue
                        support = _support(box, neighbors, allowed, lower[2])
                        if support is None:
                            continue
                        placed.append(replace(box, supporting_ids=support))
                        if search(unplaced - {(item_id, unit_index)}):
                            return True
                        placed.pop()
        return False

    if not search(remaining):
        return None
    budget.check()
    result = []
    for index, box in enumerate(placed, start=1):
        space_id = box.space_ids[0]
        origin = regions[(space_id,)].minimum
        x, y, z = (
            centimeters(value - start) for value, start in zip(box.position, origin, strict=True)
        )
        placement = {
            "placement_id": box.placement_id,
            "item_id": box.item_id,
            "unit_index": box.unit_index,
            "space_id": space_id,
            "x_cm": x,
            "y_cm": y,
            "z_cm": z,
            "orientation": box.orientation,
            "load_order": index,
            "supporting_placement_ids": list(box.supporting_ids),
        }
        if len(box.space_ids) == 2:
            placement["spanning_space_id"] = box.space_ids[1]
        result.append(placement)
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
