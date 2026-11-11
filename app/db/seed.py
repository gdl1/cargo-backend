from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.session import build_engine
from app.models import Vehicle
from app.services.vehicles import get_vehicle_catalog


def seed_vehicles(session: Session) -> int:
    catalog = get_vehicle_catalog()
    rows = [
        {"vehicle_id": vehicle_id, "name": vehicle.name} for vehicle_id, vehicle in catalog.items()
    ]
    if not rows:
        return 0
    statement = insert(Vehicle).values(rows)
    session.execute(
        statement.on_conflict_do_update(
            index_elements=[Vehicle.vehicle_id], set_={"name": statement.excluded.name}
        )
    )
    return len(rows)


def main() -> None:
    engine = build_engine(Settings())
    try:
        with Session(engine) as session, session.begin():
            count = seed_vehicles(session)
        print(f"차량 {count}개를 등록하거나 갱신했습니다.")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
