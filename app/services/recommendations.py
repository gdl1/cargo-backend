"""좌석 제약을 지키는 공간 조합을 계산하고 검증된 후보를 정렬합니다."""

from copy import deepcopy
from itertools import combinations
from uuid import uuid4

from pydantic import ValidationError

from app.core.exceptions import AppError
from app.schemas.packing import MAX_UNITS, ORIENTATIONS, PackingItem, PackingProblem
from app.services.packing import SearchBudget, SearchLimit, find_layout, validate_packing


def _error(code: str, message: str, status: int = 422) -> AppError:
    return AppError(status, code, message)


def normalize_vehicle_state(
    vehicle: dict, request: dict, plan: dict
) -> tuple[dict, dict, list | None, list | None]:
    seats = dict.fromkeys(vehicle["seat_codes"], "UNKNOWN")
    folds = dict.fromkeys((row["code"] for row in vehicle["fold_segments"]), "UNKNOWN")
    occupied, folded = request.get("occupied_seats"), request.get("current_folded_segments")
    for values, known in ((occupied, seats), (folded, folds)):
        if values is not None and (
            len(values) != len(set(values)) or not set(values) <= known.keys()
        ):
            raise _error("VALIDATION_ERROR", "지원하지 않거나 중복된 좌석/폴딩 코드입니다.")
    if occupied is not None:
        seats = {code: "OCCUPIED" if code in occupied else "EMPTY" for code in seats}
    if folded is not None:
        folds = {code: "FOLDED" if code in folded else "UNFOLDED" for code in folds}
    state = request.get("vehicle_state") or {}
    for entries, known, key, field in (
        (state.get("seats", []), seats, "seat_code", "occupancy"),
        (state.get("fold_segments", []), folds, "segment_code", "state"),
    ):
        seen = set()
        for entry in entries:
            code, value = entry[key], entry[field]
            if code not in known or code in seen:
                raise _error("VALIDATION_ERROR", "지원하지 않거나 중복된 차량 상태입니다.")
            seen.add(code)
            if known[code] != "UNKNOWN" and value != "UNKNOWN" and known[code] != value:
                raise _error("VEHICLE_STATE_CONFLICT", "차량 상태 입력이 서로 다릅니다.")
            if value != "UNKNOWN":
                known[code] = value
    actual = {seat for seat, state in seats.items() if state == "OCCUPIED"}
    if len(actual | {vehicle["driver_seat"]}) > plan["passenger_count"]:
        raise _error("VEHICLE_STATE_CONFLICT", "착석 상태가 계획의 탑승 인원과 다릅니다.")
    if plan.get("passenger_seats") is not None and not actual <= set(plan["passenger_seats"]):
        raise _error("VEHICLE_STATE_CONFLICT", "현재 착석 위치와 계획의 탑승 좌석이 다릅니다.")
    protected = actual | set(plan.get("car_seats", [])) | {vehicle["driver_seat"]}
    for segment in vehicle["fold_segments"]:
        if folds[segment["code"]] == "FOLDED" and protected & set(segment["seat_codes"]):
            raise _error("VEHICLE_STATE_CONFLICT", "착석 또는 카시트 좌석이 접힌 상태입니다.")
    occupied_result = (
        [code for code in seats if seats[code] == "OCCUPIED"]
        if "UNKNOWN" not in seats.values()
        else None
    )
    folded_result = (
        [code for code in folds if folds[code] == "FOLDED"]
        if "UNKNOWN" not in folds.values()
        else None
    )
    return seats, folds, occupied_result, folded_result


def _space_signature(space: dict) -> tuple:
    return tuple(
        space.get(field)
        for field in (
            "space_id",
            "kind",
            "width_cm",
            "depth_cm",
            "height_cm",
            "origin_cm",
            "seat_codes",
        )
    )


def _fixed(plan: dict, base: dict | None, request: dict) -> list[dict]:
    if request.get("packing_mode", "REPACK_ALL") != "KEEP_EXISTING":
        return []
    if base is None or request.get("cargo_state") != "SELECTED_ARRANGEMENT":
        raise _error("BASE_LAYOUT_STALE", "현재 선택한 배치가 필요합니다.", 409)
    items = {str(item["item_id"]): item for item in plan["items"]}
    original = {
        str(item["item_id"]): item for item in (base.get("input_snapshot") or {}).get("items", [])
    }
    for placement in base["placements"]:
        item_id = str(placement["item_id"])
        item, before = items.get(item_id), original.get(item_id)
        if item is None or placement["unit_index"] > item["quantity"]:
            raise _error("BASE_LAYOUT_STALE", "기존 짐을 삭제하거나 수량을 줄일 수 없습니다.", 409)
        if before and (
            item["quantity"] < before["quantity"]
            or any(item[field] != before[field] for field in ("width_cm", "depth_cm", "height_cm"))
            or set(item.get("allowed_orientations", ORIENTATIONS))
            != set(before.get("allowed_orientations", ORIENTATIONS))
        ):
            raise _error(
                "BASE_LAYOUT_STALE", "기존 짐의 치수나 허용 회전을 변경할 수 없습니다.", 409
            )
    return deepcopy(base["placements"])


def _rank(candidate: dict) -> tuple:
    changes = candidate["seat_change_count"]
    return (
        candidate["excluded_total_quantity"],
        changes if changes is not None else len(candidate["target_folded_segments"]),
        not candidate["all_in_trunk"],
        len(candidate["target_folded_segments"]),
        tuple(candidate["target_folded_segments"]),
        tuple(
            (row["item_id"], tuple(row["unit_indices"])) for row in candidate.get("exclusions", [])
        ),
        tuple(
            (
                row["item_id"],
                row["unit_index"],
                row["space_id"],
                row["x_cm"],
                row["y_cm"],
                row["z_cm"],
                row["orientation"],
            )
            for row in candidate["placements"]
        ),
    )


def _exclusion_counts(maxima: list[int], total: int, budget: SearchBudget):
    suffix = [0] * (len(maxima) + 1)
    for index in range(len(maxima) - 1, -1, -1):
        suffix[index] = suffix[index + 1] + maxima[index]

    def walk(index: int, remaining: int, counts: tuple[int, ...]):
        budget.check()
        if index == len(maxima):
            if remaining == 0:
                yield counts
            return
        for count in range(
            max(0, remaining - suffix[index + 1]), min(maxima[index], remaining) + 1
        ):
            yield from walk(index + 1, remaining - count, (*counts, count))

    yield from walk(0, total, ())


def recommend(
    plan: dict,
    vehicle: dict,
    request: dict,
    *,
    base_arrangement: dict | None = None,
    stacking_assessments: list[dict] | None = None,
    timeout_seconds: float = 2.0,
    max_candidates: int = 20,
) -> dict:
    """조회·저장과 분리된 계산. 제한시간 도달 시 발견한 후보도 반환하지 않습니다."""
    if type(max_candidates) is not int or max_candidates < 1:
        raise ValueError("반환 후보 수는 양수여야 합니다.")
    plan, request = deepcopy(plan), deepcopy(request)
    try:
        if not 1 <= len(plan["items"]) <= MAX_UNITS:
            raise ValueError
        validated_items = [
            PackingItem.model_validate({**item, "item_id": str(item["item_id"])})
            for item in plan["items"]
        ]
        if (
            not validated_items
            or sum(item.quantity for item in validated_items) > MAX_UNITS
            or len({item.item_id for item in validated_items}) != len(validated_items)
        ):
            raise ValueError
        for item, validated in zip(plan["items"], validated_items, strict=True):
            item["item_id"] = validated.item_id
    except (ValueError, KeyError) as exc:
        raise _error(
            "VALIDATION_ERROR", f"계산할 짐은 1~{MAX_UNITS}개이며 치수가 필요합니다."
        ) from exc
    seats, folds, occupied, folded = normalize_vehicle_state(vehicle, request, plan)
    fixed = _fixed(plan, base_arrangement, request)
    assessments = deepcopy(stacking_assessments or [])
    output = {
        "is_success": False,
        "result_code": "NO_LAYOUT_FOUND",
        "search_complete": True,
        "current_folded_segments": folded,
        "occupied_seats": occupied,
        "vehicle_data_version": vehicle["data_version"],
        "packing_mode": request.get("packing_mode", "REPACK_ALL"),
        "base_calculation_id": request.get("base_calculation_id"),
        "base_arrangement_id": request.get("base_arrangement_id"),
        "arrangements": [],
        "alternatives": [],
        "required_inputs": [],
        "warnings": ["STACKING_ASSESSMENT_UNAVAILABLE"] if stacking_assessments is None else [],
    }
    if request.get("cargo_state") == "UNKNOWN":
        output.update(
            result_code="STATE_REQUIRED",
            search_complete=False,
            required_inputs=[
                {
                    "field": "cargo_state",
                    "reason": "현재 짐이 없는지, 선택한 배치로 실려 있는지 확인해 주세요.",
                }
            ],
        )
        return output
    budget = SearchBudget.create(timeout_seconds)
    segments = {row["code"]: set(row["seat_codes"]) for row in vehicle["fold_segments"]}
    actual = {code for code, value in seats.items() if value == "OCCUPIED"} | {
        vehicle["driver_seat"]
    }
    protected = actual | set(plan.get("car_seats", [])) | set(plan.get("retained_seats", []))
    selected_passengers = plan.get("passenger_seats")
    if selected_passengers is not None:
        selected = set(selected_passengers)
        if (
            len(selected) != len(selected_passengers)
            or len(selected) != plan["passenger_count"]
            or vehicle["driver_seat"] not in selected
            or not selected <= set(vehicle["seat_codes"])
        ):
            raise _error("VALIDATION_ERROR", "선택한 탑승 좌석과 인원이 일치해야 합니다.")
        if not actual <= selected:
            raise _error("VEHICLE_STATE_CONFLICT", "현재 착석 위치와 계획의 탑승 좌석이 다릅니다.")
        actual = selected
        protected |= selected
    if not protected <= set(vehicle["seat_codes"]) or not 1 <= plan["passenger_count"] <= len(
        vehicle["seat_codes"]
    ):
        raise _error("VALIDATION_ERROR", "차량 좌석이나 탑승 인원을 확인해 주세요.")
    fixed_spaces = {row["space_id"] for row in fixed}
    base_spaces = {
        row["space_id"]: row for row in (base_arrangement or {}).get("packing_spaces", [])
    }
    if fixed and base_arrangement:
        fixed_seats = set().union(
            *(
                set(base_spaces[code].get("seat_codes", []))
                for code in fixed_spaces
                if code in base_spaces
            )
        )
        base_target = set(base_arrangement["target_folded_segments"])
        conflicts = []
        for code, covered_seats in segments.items():
            expected = "FOLDED" if code in base_target else "UNFOLDED"
            if covered_seats & fixed_seats and folds[code] not in {"UNKNOWN", expected}:
                conflicts.append(
                    {
                        "field": "vehicle_state.fold_segments",
                        "segment_code": code,
                        "reason": "관측한 폴딩 상태와 선택한 배치가 달라 확인이 필요합니다.",
                    }
                )
        for seat in fixed_seats:
            if seats[seat] == "OCCUPIED":
                conflicts.append(
                    {
                        "field": "vehicle_state.seats",
                        "seat_code": seat,
                        "reason": "짐을 놓은 좌석의 착석 상태를 확인해 주세요.",
                    }
                )
        if conflicts:
            output.update(
                result_code="STATE_REQUIRED", search_complete=False, required_inputs=conflicts
            )
            return output
    needed: dict[tuple, dict] = {}
    variants: list[tuple[dict, list, list]] = []
    unknown_variants: list[tuple[dict, list, set]] = []
    structurally_valid = False
    compatible_base = not fixed
    try:
        for profile in vehicle["packing_profiles"]:
            budget.check()
            target = set(profile["target_folded_segments"])
            if not target <= segments.keys():
                raise _error("VEHICLE_MODEL_UNAVAILABLE", "폴딩 조합을 확인할 수 없습니다.", 503)
            folded_seats = set().union(*(segments[code] for code in target))
            if protected & folded_seats:
                continue
            available = [seat for seat in vehicle["seat_codes"] if seat not in folded_seats]
            if len(available) < plan["passenger_count"] or not actual <= set(available):
                continue
            structurally_valid = True
            established_folded_seats = set().union(
                *(
                    segments[code]
                    for code in target
                    & set((base_arrangement or {}).get("target_folded_segments", []))
                    if folds[code] != "UNFOLDED"
                )
            )
            unknown_folding_seats = [
                seat
                for seat in folded_seats
                if seats[seat] == "UNKNOWN" and seat not in established_folded_seats
            ]
            spaces = profile.get("spaces") or profile.get("packing_spaces")
            if not spaces:
                if profile.get("packing_space"):
                    spaces = [
                        {
                            "space_id": "TRUNK",
                            "kind": "TRUNK",
                            "seat_codes": [],
                            "origin_cm": {"x_cm": 0, "y_cm": 0, "z_cm": 0},
                            "opening": vehicle.get("opening"),
                            **profile["packing_space"],
                        }
                    ]
                else:
                    raise _error(
                        "VEHICLE_MODEL_UNAVAILABLE", "계산 공간이 등록되지 않았습니다.", 503
                    )
            by_space = {row["space_id"]: row for row in spaces}
            if fixed and any(
                space_id not in by_space
                or space_id not in base_spaces
                or _space_signature(by_space[space_id]) != _space_signature(base_spaces[space_id])
                for space_id in fixed_spaces
            ):
                continue
            compatible_base = True
            spare = [seat for seat in available if seat not in actual]
            for extra in combinations(spare, plan["passenger_count"] - len(actual)):
                passengers = actual | set(extra)
                usable = []
                potential = []
                for space in spaces:
                    space_seats = set(space.get("seat_codes", []))
                    if space_seats & (protected | passengers):
                        continue
                    potential.append(space)
                    unknown = [seat for seat in space_seats if seats[seat] == "UNKNOWN"]
                    if unknown and space["space_id"] not in fixed_spaces:
                        continue
                    usable.append(space)
                if (
                    potential
                    and fixed_spaces <= {row["space_id"] for row in potential}
                    and (unknown_folding_seats or len(potential) != len(usable))
                ):
                    unknown_variants.append((profile, potential, set(unknown_folding_seats)))
                if unknown_folding_seats:
                    continue
                if not fixed_spaces <= {row["space_id"] for row in usable} or not usable:
                    continue
                variants.append(
                    (
                        profile,
                        usable,
                        [seat for seat in vehicle["seat_codes"] if seat in passengers],
                    )
                )
        if not compatible_base:
            raise _error(
                "BASE_LAYOUT_STALE", "현재 차량 공간에서 기준 배치를 보존할 수 없습니다.", 409
            )
        all_units = {
            (row["item_id"], index)
            for row in plan["items"]
            for index in range(1, row["quantity"] + 1)
        }
        fixed_units = {(str(row["item_id"]), row["unit_index"]) for row in fixed}

        def evaluate(selected: set[tuple[str, int]]) -> list[dict]:
            candidates = []
            for profile, spaces, passengers in variants:
                budget.check()
                problem = PackingProblem.model_validate({"spaces": spaces, "items": plan["items"]})
                try:
                    placements = None
                    trunks = [space for space in spaces if space["kind"] == "TRUNK"]
                    if trunks and fixed_spaces <= {space["space_id"] for space in trunks}:
                        trunk_problem = PackingProblem.model_validate(
                            {"spaces": trunks, "items": plan["items"]}
                        )
                        placements = find_layout(
                            trunk_problem,
                            budget=budget,
                            selected_units=selected,
                            fixed_placements=fixed,
                            stacking_assessments=assessments,
                        )
                    if placements is None and len(trunks) != len(spaces):
                        placements = find_layout(
                            problem,
                            budget=budget,
                            selected_units=selected,
                            fixed_placements=fixed,
                            stacking_assessments=assessments,
                        )
                except (ValueError, KeyError) as exc:
                    if fixed:
                        raise _error(
                            "BASE_LAYOUT_STALE", "기준 배치의 치수나 받침이 일치하지 않습니다.", 409
                        ) from exc
                    raise
                if not placements:
                    continue
                target = sorted(profile["target_folded_segments"])
                changes = len(set(folded) ^ set(target)) if folded is not None else None
                kinds = {space["space_id"]: space["kind"] for space in spaces}
                in_trunk = all(kinds[row["space_id"]] == "TRUNK" for row in placements)
                excluded = all_units - selected
                reasons = ["FEWER_EXCLUSIONS"] if excluded else []
                if changes is not None:
                    reasons.append("NO_SEAT_CHANGE" if changes == 0 else "FEWER_SEAT_CHANGES")
                if in_trunk:
                    reasons.append("ALL_IN_TRUNK")
                candidate = {
                    "arrangement_id": str(uuid4()),
                    "rank": 0,
                    "passenger_seats": passengers,
                    "target_folded_segments": target,
                    "seat_change_count": changes,
                    "packing_space": {
                        key: spaces[0][key] for key in ("width_cm", "depth_cm", "height_cm")
                    }
                    if len(spaces) == 1
                    else None,
                    "profile_id": profile.get("profile_id", "TRUNK_ONLY"),
                    "packing_spaces": [
                        {
                            key: value
                            for key, value in space.items()
                            if key
                            in {
                                "space_id",
                                "kind",
                                "width_cm",
                                "depth_cm",
                                "height_cm",
                                "origin_cm",
                                "seat_codes",
                            }
                        }
                        for space in spaces
                    ],
                    "score": None,
                    "placements": placements,
                    "all_in_trunk": in_trunk,
                    "excluded_total_quantity": len(excluded),
                    "reason_codes": reasons,
                    "stacking_assessments": assessments,
                }
                if excluded:
                    candidate["exclusions"] = [
                        {
                            "item_id": item["item_id"],
                            "name": item["name"],
                            "quantity": len(indices),
                            "unit_indices": indices,
                            "reason_code": "SPACE_CONSTRAINT",
                        }
                        for item in plan["items"]
                        if (
                            indices := sorted(
                                index for item_id, index in excluded if item_id == item["item_id"]
                            )
                        )
                    ]
                candidates.append(candidate)
            return candidates

        candidates = evaluate(all_units)
        if not candidates:
            for _, spaces, folding_seats in unknown_variants:
                budget.check()
                problem = PackingProblem.model_validate({"spaces": spaces, "items": plan["items"]})
                placements = find_layout(
                    problem,
                    budget=budget,
                    selected_units=all_units,
                    fixed_placements=fixed,
                    stacking_assessments=assessments,
                )
                if placements:
                    used = {row["space_id"] for row in placements}
                    unknown_seats = folding_seats | {
                        seat
                        for space in spaces
                        if space["space_id"] in used and space["space_id"] not in fixed_spaces
                        for seat in space.get("seat_codes", [])
                        if seats[seat] == "UNKNOWN"
                    }
                    for seat in sorted(unknown_seats):
                        needed[("seat", seat)] = {
                            "field": "vehicle_state.seats",
                            "seat_code": seat,
                            "reason": "이 배치에 사용할 좌석이 비어 있는지 확인해 주세요.",
                        }
                    if needed:
                        break
        alternatives = []
        if not candidates and not needed and request.get("allow_exclusions", False):
            removable = []
            for item in plan["items"]:
                fixed_indices = {
                    index for item_id, index in fixed_units if item_id == item["item_id"]
                }
                optional = [
                    index for index in range(1, item["quantity"] + 1) if index not in fixed_indices
                ]
                maximum = min(len(optional), item["quantity"] - item.get("required_quantity", 0))
                removable.append((item["item_id"], optional, maximum))
            for excluded_count in range(1, len(all_units)):
                budget.check()
                for counts in _exclusion_counts(
                    [row[2] for row in removable], excluded_count, budget
                ):
                    budget.check()
                    excluded = (
                        {
                            (item_id, index)
                            for (item_id, indices, _), count in zip(removable, counts, strict=True)
                            for index in indices[len(indices) - count :]
                        }
                        if any(counts)
                        else set()
                    )
                    alternatives.extend(evaluate(all_units - excluded))
                if alternatives:
                    break
        budget.check()
        destination = "arrangements" if candidates else "alternatives"
        values = sorted(candidates or alternatives, key=_rank)[:max_candidates]
        for index, candidate in enumerate(values, start=1):
            candidate["rank"] = index
        budget.check()
        output[destination] = values
        if candidates:
            output.update(is_success=True, result_code="LAYOUT_FOUND")
        elif alternatives:
            output["result_code"] = "EXCLUSION_OPTIONS_FOUND"
        elif needed:
            output.update(
                result_code="STATE_REQUIRED",
                search_complete=False,
                required_inputs=list(needed.values()),
            )
        elif not structurally_valid:
            output["result_code"] = "NO_VALID_SEAT_CONFIGURATION"
        return output
    except SearchLimit:
        output.update(
            result_code="TIME_LIMIT", search_complete=False, arrangements=[], alternatives=[]
        )
        return output
    except ValidationError as exc:
        raise _error("VALIDATION_ERROR", "짐과 공간 입력을 확인해 주세요.") from exc


def validate_candidate(
    plan: dict, vehicle: dict, candidate: dict, request: dict | None = None
) -> None:
    """저장된 후보를 다시 사용할 때 입력·좌석·공간·배치를 함께 검증합니다."""
    passengers = candidate["passenger_seats"]
    if (
        len(passengers) != len(set(passengers))
        or len(passengers) != plan["passenger_count"]
        or vehicle["driver_seat"] not in passengers
        or not set(passengers) <= set(vehicle["seat_codes"])
        or (
            plan.get("passenger_seats") is not None
            and set(passengers) != set(plan["passenger_seats"])
        )
    ):
        raise ValueError("탑승 좌석이 계획과 일치하지 않습니다.")
    profile = next(
        (
            row
            for row in vehicle["packing_profiles"]
            if row["profile_id"] == candidate["profile_id"]
        ),
        None,
    )
    if profile is None or set(profile["target_folded_segments"]) != set(
        candidate["target_folded_segments"]
    ):
        raise ValueError("차량 폴딩 조합이 일치하지 않습니다.")
    protected = (
        set(passengers) | set(plan.get("car_seats", [])) | set(plan.get("retained_seats", []))
    )
    if request is not None:
        seats, _, _, _ = normalize_vehicle_state(vehicle, request, plan)
        occupied = {seat for seat, state in seats.items() if state == "OCCUPIED"}
        if not occupied <= set(passengers):
            raise ValueError("현재 착석 좌석을 유지해야 합니다.")
        protected |= occupied
    for segment in vehicle["fold_segments"]:
        if (
            segment["code"] in candidate["target_folded_segments"]
            and set(segment["seat_codes"]) & protected
        ):
            raise ValueError("보호한 좌석을 접을 수 없습니다.")
    known_spaces = {row["space_id"]: row for row in profile["spaces"]}
    snapshots = candidate["packing_spaces"]
    if len({row["space_id"] for row in snapshots}) != len(snapshots):
        raise ValueError("공간을 중복 사용할 수 없습니다.")
    selected_spaces = []
    for space in snapshots:
        known = known_spaces.get(space["space_id"])
        if known is None or _space_signature(known) != _space_signature(space):
            raise ValueError("차량 공간이 변경되었습니다.")
        if set(space.get("seat_codes", [])) & protected:
            raise ValueError("보호 좌석에 짐을 놓을 수 없습니다.")
        selected_spaces.append(known)
    problem = PackingProblem.model_validate({"spaces": selected_spaces, "items": plan["items"]})
    units = {
        (str(item["item_id"]), index)
        for item in plan["items"]
        for index in range(1, item["quantity"] + 1)
    }
    excluded: set[tuple[str, int]] = set()
    for row in candidate.get("exclusions", []):
        values = {(str(row["item_id"]), index) for index in row["unit_indices"]}
        if len(values) != row["quantity"] or excluded & values or not values <= units:
            raise ValueError("제외 수량이나 낱개 번호가 일치하지 않습니다.")
        excluded |= values
    if len(excluded) != candidate.get("excluded_total_quantity", 0) or excluded == units:
        raise ValueError("전체 짐을 제외할 수 없습니다.")
    for item in plan["items"]:
        remaining = sum(item_id == str(item["item_id"]) for item_id, _ in units - excluded)
        if remaining < item.get("required_quantity", 0):
            raise ValueError("필수 수량을 제외할 수 없습니다.")
    validate_packing(
        problem,
        candidate["placements"],
        stacking_assessments=candidate.get("stacking_assessments", []),
        expected_units=units - excluded,
    )
    kinds = {space["space_id"]: space["kind"] for space in selected_spaces}
    if candidate["all_in_trunk"] != all(
        kinds[row["space_id"]] == "TRUNK" for row in candidate["placements"]
    ):
        raise ValueError("트렁크 적재 여부가 배치와 일치하지 않습니다.")
