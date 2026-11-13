import json
from collections.abc import Sequence
from functools import lru_cache
from importlib.resources import files

from app.schemas.vehicle import PackingProfile, SeatCode, VehicleDetail


@lru_cache(maxsize=1)
def _load_vehicle_data() -> tuple[VehicleDetail, ...]:
    # 공유 자료의 트렁크 깊이 110·높이 50cm와 조수석 방석 폭 48cm를 참고합니다.
    # 나머지 치수·6:4 좌우 대응은 더미이며, 간격 0도 1차 계산용 가정입니다.
    path = files("app.data.vehicles").joinpath("avante-demo-v3.json")
    vehicle = VehicleDetail.model_validate(json.loads(path.read_text(encoding="utf-8")))
    return (vehicle,)


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
