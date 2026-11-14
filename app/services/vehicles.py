from collections.abc import Sequence
from functools import lru_cache
from importlib.resources import files

from app.schemas.vehicle import PackingProfile, SeatCode, VehicleDetail

SPACE_SNAPSHOT_FIELDS = (
    "space_id",
    "kind",
    "width_cm",
    "depth_cm",
    "height_cm",
    "origin_cm",
    "seat_codes",
    "opening",
    "connected_to_space_id",
)


def space_matches_snapshot(space: dict, snapshot: dict) -> bool:
    return all(
        space.get(field) == snapshot.get(field)
        for field in SPACE_SNAPSHOT_FIELDS
        # 이전 계산 결과에는 입구가 저장되지 않았습니다.
        if field != "opening" or field in snapshot
    )


@lru_cache(maxsize=1)
def _load_vehicle_data() -> tuple[VehicleDetail, ...]:
    directory = files("app.data.vehicles")
    return tuple(
        VehicleDetail.model_validate_json(directory.joinpath(name).read_text(encoding="utf-8"))
        for name in ("avante-demo-v3.json", "tucson-hybrid-v1.json")
    )


def get_vehicle_catalog() -> dict[str, VehicleDetail]:
    return {vehicle.vehicle_id: vehicle.model_copy(deep=True) for vehicle in _load_vehicle_data()}


def get_compatible_profiles(
    vehicle: VehicleDetail,
    passenger_count: int,
    car_seats: Sequence[SeatCode] = (),
) -> tuple[PackingProfile, ...]:
    """탑승 인원과 카시트 좌석을 유지할 수 있는 등록된 조합을 반환합니다."""
    if type(passenger_count) is not int or not 1 <= passenger_count <= vehicle.seat_capacity:
        raise ValueError("탑승 인원은 운전자를 포함한 차량 정원 안에서 지정해 주세요.")
    protected = set(car_seats)
    if len(protected) != len(car_seats) or not protected <= set(vehicle.seat_codes):
        raise ValueError("카시트 좌석은 지원 좌석이며 중복되지 않아야 합니다.")
    if vehicle.driver_seat in protected:
        raise ValueError("운전석에는 카시트를 지정할 수 없습니다.")

    segments = {segment.code: set(segment.seat_codes) for segment in vehicle.fold_segments}
    compatible = []
    for profile in vehicle.packing_profiles:
        unavailable = set().union(*(segments[code] for code in profile.target_folded_segments))
        if (
            not unavailable & protected
            and vehicle.seat_capacity - len(unavailable) >= passenger_count
        ):
            compatible.append(profile)
    return tuple(compatible)
