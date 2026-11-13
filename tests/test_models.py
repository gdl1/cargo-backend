from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import JSON, func, inspect, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, configure_mappers

from alembic import command
from app.models import (
    Arrangement,
    Calculation,
    Placement,
    Plan,
    PlanCarSeat,
    PlanItem,
    PlanRetainedSeat,
    Vehicle,
)


def test_model_relationships_configure() -> None:
    configure_mappers()


@pytest.fixture
def stored_plan(model_session: Session) -> Plan:
    plan = Plan(
        user_id="demo-user",
        vehicle=Vehicle(vehicle_id="avante_demo", name="아반떼 데모"),
        passenger_count=2,
        items=[
            PlanItem(
                name="두 번째 짐", width_cm=30, depth_cm=40, height_cm=20, quantity=1, position=2
            ),
            PlanItem(
                name="첫 번째 짐",
                width_cm=Decimal("41.5"),
                depth_cm=40,
                height_cm=20,
                quantity=2,
                position=1,
            ),
        ],
        car_seats=[PlanCarSeat(seat_code="ROW_2_RIGHT")],
        retained_seats=[
            PlanRetainedSeat(seat_code="ROW_1_LEFT"),
            PlanRetainedSeat(seat_code="ROW_2_LEFT"),
        ],
    )
    model_session.add(plan)
    model_session.flush()
    model_session.expire_all()
    return plan


@pytest.fixture
def stored_arrangement(model_session: Session, stored_plan: Plan) -> Arrangement:
    item = stored_plan.items[0]
    calculation = Calculation(plan=stored_plan, is_success=True, current_folded_segments=[])
    arrangement = Arrangement(
        calculation=calculation,
        rank=1,
        passenger_seats=["ROW_1_LEFT", "ROW_1_RIGHT"],
    )
    arrangement.placements = [
        Placement(item=item, x_cm=Decimal("41.5"), y_cm=0, z_cm=0, orientation="WDH", load_order=2),
        Placement(item=item, x_cm=0, y_cm=0, z_cm=0, orientation="WDH", load_order=1),
    ]
    model_session.add(calculation)
    model_session.flush()
    model_session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    model_session.execute(text("SET CONSTRAINTS ALL DEFERRED"))
    model_session.expire_all()
    return arrangement


@pytest.mark.integration
class TestPostgresqlModels:
    def test_migration_roundtrip(self, model_session: Session) -> None:
        config = model_session.info["migration_config"]
        connection = config.attributes["connection"]
        expected = {
            "vehicle",
            "plan",
            "plan_item",
            "plan_car_seat",
            "plan_retained_seat",
            "calculation",
            "arrangement",
            "placement",
            "saved_item",
            "calculation_preview",
            "operation_receipt",
            "ai_conversation",
            "product_candidate",
            "push_device",
            "cargo_reminder",
            "push_delivery",
        }
        assert set(inspect(connection).get_table_names()) - {"alembic_version"} == expected
        command.downgrade(config, "base")
        assert set(inspect(connection).get_table_names()) <= {"alembic_version"}
        command.upgrade(config, "head")
        assert set(inspect(connection).get_table_names()) - {"alembic_version"} == expected
        command.check(config)

    def test_existing_plan_survives_upgrade(self, model_session: Session) -> None:
        config = model_session.info["migration_config"]
        connection = config.attributes["connection"]
        command.downgrade(config, "0001")
        connection.execute(
            text("INSERT INTO vehicle (vehicle_id, name) VALUES ('existing', '기존 차량')")
        )
        plan_id = connection.scalar(
            text(
                "INSERT INTO plan (user_id, vehicle_id, passenger_count) "
                "VALUES ('existing-user', 'existing', 2) RETURNING plan_id"
            )
        )
        connection.execute(
            text(
                "INSERT INTO plan_item "
                "(plan_id, name, width_cm, depth_cm, height_cm, quantity, position) "
                "VALUES (:plan_id, '기존 짐', 41.5, 30, 20, 2, 1)"
            ),
            {"plan_id": plan_id},
        )
        connection.execute(
            text("INSERT INTO plan_car_seat VALUES (:plan_id, 'ROW_2_RIGHT')"),
            {"plan_id": plan_id},
        )
        command.upgrade(config, "head")
        plan = model_session.get(Plan, plan_id)
        assert plan.user_id == "existing-user"
        assert plan.vehicle_id == "existing"
        assert plan.passenger_count == 2
        assert plan.items[0].width_cm == Decimal("41.5")
        assert plan.items[0].quantity == 2
        assert [seat.seat_code for seat in plan.car_seats] == ["ROW_2_RIGHT"]
        assert plan.retained_seats == []
        assert plan.idempotency_key is None
        assert plan.request_hash is None
        assert plan.revision == 1
        assert plan.passenger_seats is None
        assert plan.items[0].is_active is True
        assert plan.items[0].required_quantity == 0
        assert set(plan.items[0].allowed_orientations) == {"WDH", "WHD", "DWH", "DHW", "HWD", "HDW"}

    def test_idempotency_key_unique_for_each_user(
        self, model_session: Session, stored_plan: Plan
    ) -> None:
        key = uuid4()
        stored_plan.idempotency_key = key
        stored_plan.request_hash = "a" * 64
        model_session.add(
            Plan(
                user_id="other-user",
                vehicle_id=stored_plan.vehicle_id,
                passenger_count=1,
                idempotency_key=key,
                request_hash="a" * 64,
            )
        )
        model_session.flush()
        assert model_session.scalar(select(func.count()).select_from(Plan)) == 2
        model_session.add(
            Plan(
                user_id=stored_plan.user_id,
                vehicle_id=stored_plan.vehicle_id,
                passenger_count=1,
                idempotency_key=key,
                request_hash="b" * 64,
            )
        )
        with pytest.raises(IntegrityError):
            model_session.flush()

    @pytest.mark.parametrize(
        ("key", "request_hash"),
        [
            (uuid4(), None),
            (None, "a" * 64),
            (uuid4(), ""),
            (uuid4(), "A" * 64),
            (uuid4(), "g" * 64),
            (uuid4(), "a" * 63),
            (uuid4(), "a" * 65),
        ],
    )
    def test_invalid_idempotency_pair_rejected(
        self,
        model_session: Session,
        stored_plan: Plan,
        key: UUID | None,
        request_hash: str | None,
    ) -> None:
        stored_plan.idempotency_key = key
        stored_plan.request_hash = request_hash
        with pytest.raises(IntegrityError):
            model_session.flush()

    def test_duplicate_retained_seat_rejected(
        self, model_session: Session, stored_plan: Plan
    ) -> None:
        with pytest.raises(IntegrityError):
            model_session.execute(
                PlanRetainedSeat.__table__.insert().values(
                    plan_id=stored_plan.plan_id, seat_code="ROW_1_LEFT"
                )
            )

    def test_decimal_values_and_item_order(self, stored_plan: Plan) -> None:
        assert [item.name for item in stored_plan.items] == ["첫 번째 짐", "두 번째 짐"]
        assert stored_plan.items[0].width_cm == Decimal("41.5")
        assert isinstance(stored_plan.items[0].width_cm, Decimal)
        assert stored_plan.created_at.tzinfo is not None
        assert stored_plan.user_id == "demo-user"

    def test_unknown_state_and_mutable_lists(
        self, model_session: Session, stored_arrangement: Arrangement
    ) -> None:
        known = stored_arrangement.calculation
        unknown = Calculation(plan=known.plan, is_success=False, current_folded_segments=None)
        model_session.add(unknown)
        model_session.flush()
        model_session.expire_all()
        assert unknown.current_folded_segments is None
        assert known.current_folded_segments == []
        assert (
            model_session.scalar(
                select(func.count())
                .select_from(Calculation)
                .where(Calculation.current_folded_segments.is_(None))
            )
            == 1
        )

        known.current_folded_segments.append("REAR_40")
        stored_arrangement.target_folded_segments.append("REAR_40")
        stored_arrangement.passenger_seats.append("ROW_2_LEFT")
        model_session.flush()
        model_session.expire_all()
        assert known.current_folded_segments == ["REAR_40"]
        assert stored_arrangement.target_folded_segments == ["REAR_40"]
        assert stored_arrangement.passenger_seats == ["ROW_1_LEFT", "ROW_1_RIGHT", "ROW_2_LEFT"]

    def test_quantity_and_recalculation_preserve_original(
        self, model_session: Session, stored_arrangement: Arrangement
    ) -> None:
        previous = stored_arrangement.calculation
        plan = previous.plan
        original = (plan.plan_id, plan.created_at, plan.items[0].item_id, plan.items[0].width_cm)
        placements = stored_arrangement.placements
        assert [placement.load_order for placement in placements] == [1, 2]
        assert {placement.item_id for placement in placements} == {plan.items[0].item_id}
        assert len({placement.placement_id for placement in placements}) == 2
        assert len(placements) == plan.items[0].quantity

        next_calculation = Calculation(plan=plan, is_success=False, current_folded_segments=None)
        model_session.add(next_calculation)
        model_session.flush()
        model_session.expire_all()
        assert (
            plan.plan_id,
            plan.created_at,
            plan.items[0].item_id,
            plan.items[0].width_cm,
        ) == original
        assert len(plan.calculations) == 2
        assert next_calculation.calculation_id != previous.calculation_id
        assert previous.is_success is True
        assert len(previous.arrangements[0].placements) == 2

    @pytest.mark.parametrize("target", ["item", "calculation"])
    def test_cross_plan_references_rejected(
        self, model_session: Session, stored_arrangement: Arrangement, target: str
    ) -> None:
        other = Plan(
            user_id="demo-user",
            vehicle=stored_arrangement.calculation.plan.vehicle,
            passenger_count=1,
            items=[
                PlanItem(
                    name="다른 계획 짐", width_cm=1, depth_cm=1, height_cm=1, quantity=1, position=1
                )
            ],
            calculations=[Calculation(is_success=False, current_folded_segments=None)],
        )
        model_session.add(other)
        model_session.flush()
        with pytest.raises(IntegrityError):
            if target == "item":
                stored_arrangement.placements[0].item_id = other.items[0].item_id
            else:
                stored_arrangement.calculation_id = other.calculations[0].calculation_id
            model_session.flush()
            model_session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))

    @pytest.mark.parametrize("target", ["position", "rank", "load_order"])
    def test_ordering_uniqueness(
        self, model_session: Session, stored_arrangement: Arrangement, target: str
    ) -> None:
        with pytest.raises(IntegrityError):
            if target == "position":
                stored_arrangement.calculation.plan.items[1].position = 1
            elif target == "rank":
                model_session.add(
                    Arrangement(
                        calculation=stored_arrangement.calculation,
                        rank=1,
                        passenger_seats=["ROW_1_LEFT"],
                    )
                )
            else:
                stored_arrangement.placements[1].load_order = 1
            model_session.flush()

    @pytest.mark.parametrize(
        ("entity", "field", "value"),
        [
            ("plan", "passenger_count", 0),
            ("plan", "user_id", " "),
            ("item", "width_cm", Decimal("0")),
            ("item", "width_cm", Decimal("-1")),
            ("item", "width_cm", Decimal("NaN")),
            ("item", "width_cm", Decimal("Infinity")),
            ("item", "quantity", 0),
            ("item", "position", 0),
            ("car_seat", "seat_code", "UNKNOWN"),
            ("car_seat", "seat_code", "ROW_1_LEFT"),
            ("retained_seat", "seat_code", "UNKNOWN"),
            ("calculation", "current_folded_segments", [1]),
            ("arrangement", "rank", 0),
            ("arrangement", "passenger_seats", ["ROW_1_RIGHT"]),
            ("arrangement", "passenger_seats", ["ROW_1_LEFT", "UNKNOWN"]),
            ("arrangement", "target_folded_segments", [1]),
            ("arrangement", "score", Decimal("NaN")),
            ("arrangement", "score", Decimal("Infinity")),
            ("placement", "orientation", "UNKNOWN"),
            ("placement", "load_order", 0),
            ("placement", "x_cm", Decimal("-1")),
            ("placement", "y_cm", Decimal("NaN")),
            ("placement", "z_cm", Decimal("Infinity")),
        ],
    )
    def test_invalid_values_rejected(
        self,
        model_session: Session,
        stored_arrangement: Arrangement,
        entity: str,
        field: str,
        value: object,
    ) -> None:
        calculation = stored_arrangement.calculation
        entities = {
            "plan": calculation.plan,
            "item": calculation.plan.items[0],
            "car_seat": calculation.plan.car_seats[0],
            "retained_seat": calculation.plan.retained_seats[0],
            "calculation": calculation,
            "arrangement": stored_arrangement,
            "placement": stored_arrangement.placements[0],
        }
        with pytest.raises(IntegrityError):
            setattr(entities[entity], field, value)
            model_session.flush()

    @pytest.mark.parametrize(
        ("target", "field", "value"),
        [
            ("calculation", "current_folded_segments", {"REAR_40": True}),
            ("calculation", "current_folded_segments", JSON.NULL),
            ("arrangement", "passenger_seats", {"ROW_1_LEFT": True}),
            ("arrangement", "target_folded_segments", JSON.NULL),
        ],
    )
    def test_non_array_json_rejected_by_database(
        self,
        model_session: Session,
        stored_arrangement: Arrangement,
        target: str,
        field: str,
        value: object,
    ) -> None:
        row = stored_arrangement.calculation if target == "calculation" else stored_arrangement
        table = row.__table__
        primary_key = next(iter(table.primary_key.columns))
        statement = (
            update(table)
            .where(primary_key == getattr(row, primary_key.name))
            .values({field: value})
        )
        with pytest.raises(IntegrityError):
            model_session.execute(statement)

    @pytest.mark.parametrize("target", ["item", "vehicle"])
    def test_referenced_row_deletion_rejected(
        self, model_session: Session, stored_arrangement: Arrangement, target: str
    ) -> None:
        plan = stored_arrangement.calculation.plan
        row = plan.items[0] if target == "item" else plan.vehicle
        assert len(plan.items[0].placements) == 2
        assert plan.vehicle.plans == [plan]
        with pytest.raises(IntegrityError):
            model_session.delete(row)
            model_session.flush()
            model_session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))

    def test_plan_deletion_cascades_with_loaded_relationships(
        self, model_session: Session, stored_arrangement: Arrangement
    ) -> None:
        plan = stored_arrangement.calculation.plan
        assert len(plan.items) == 2
        assert len(plan.car_seats) == 1
        assert len(plan.retained_seats) == 2
        assert len(plan.calculations) == 1
        assert len(plan.calculations[0].arrangements) == 1
        assert len(stored_arrangement.placements) == 2
        assert len(plan.items[0].placements) == 2
        model_session.delete(plan)
        model_session.flush()
        model_session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        for model in (
            Plan,
            PlanItem,
            PlanCarSeat,
            PlanRetainedSeat,
            Calculation,
            Arrangement,
            Placement,
        ):
            assert model_session.scalar(select(func.count()).select_from(model)) == 0
        assert model_session.scalar(select(func.count()).select_from(Vehicle)) == 1
