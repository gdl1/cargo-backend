from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import AppError
from app.db.session import get_db
from app.models import Vehicle
from app.schemas.common import ApiResponse, ErrorResponse
from app.schemas.vehicle import VehicleDetail, VehicleListData, VehicleSummary
from app.services.vehicles import get_vehicle_catalog

router = APIRouter(prefix="/vehicles", tags=["차량"])


@router.get("", response_model=ApiResponse[VehicleListData], summary="차량 목록 조회")
def list_vehicles(session: Annotated[Session, Depends(get_db)]) -> ApiResponse[VehicleListData]:
    catalog = get_vehicle_catalog()
    rows = session.scalars(
        select(Vehicle).where(Vehicle.vehicle_id.in_(list(catalog))).order_by(Vehicle.vehicle_id)
    ).all()
    vehicles = [
        VehicleSummary(
            vehicle_id=row.vehicle_id,
            name=row.name,
            seat_capacity=catalog[row.vehicle_id].seat_capacity,
            data_version=catalog[row.vehicle_id].data_version,
            data_source=catalog[row.vehicle_id].data_source,
        )
        for row in rows
    ]
    return ApiResponse(
        code="SUCCESS",
        message="차량 목록을 조회했습니다.",
        data=VehicleListData(vehicles=vehicles),
    )


@router.get(
    "/{vehicle_id}",
    response_model=ApiResponse[VehicleDetail],
    responses={404: {"model": ErrorResponse, "description": "차량 없음"}},
    summary="차량 상세 조회",
)
def get_vehicle(
    vehicle_id: str, session: Annotated[Session, Depends(get_db)]
) -> ApiResponse[VehicleDetail]:
    detail = get_vehicle_catalog().get(vehicle_id)
    if detail is None:
        raise AppError(404, "VEHICLE_NOT_FOUND", "차량을 찾을 수 없습니다.")
    row = session.get(Vehicle, vehicle_id)
    if row is None:
        raise AppError(404, "VEHICLE_NOT_FOUND", "차량을 찾을 수 없습니다.")
    return ApiResponse(
        code="SUCCESS",
        message="차량 정보를 조회했습니다.",
        data=VehicleDetail.model_validate({**detail.model_dump(), "name": row.name}),
    )
