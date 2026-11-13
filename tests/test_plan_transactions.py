import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event, func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command
from app.core.config import Settings
from app.db.seed import seed_vehicles
from app.db.session import build_engine, get_db
from app.main import create_app
from app.models import Plan, PlanCarSeat, PlanItem, PlanRetainedSeat
from app.services import plans

pytestmark = pytest.mark.integration

PLAN_INPUT = {
    "vehicle_id": "avante_demo",
    "passenger_count": 1,
    "car_seats": ["ROW_2_RIGHT"],
    "retained_seats": ["ROW_2_LEFT"],
    "items": [{"name": "상자", "width_cm": 30, "depth_cm": 41.5, "height_cm": 20, "quantity": 2}],
}


@pytest.fixture
def committed_engine() -> Iterator[Engine]:
    if os.getenv("RUN_DB_TESTS") != "1":
        pytest.skip("실제 DB 검사는 RUN_DB_TESTS=1일 때 실행합니다.")
    engine = build_engine(Settings(_env_file=None))
    schema = f"test_plan_transactions_{uuid4().hex}"
    try:
        with engine.begin() as connection:
            connection.execute(CreateSchema(schema))
            connection.execute(
                text("SELECT set_config('search_path', :schema, true)"), {"schema": schema}
            )
            config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
        test_engine = engine.execution_options(schema_translate_map={None: schema})
        with Session(test_engine) as session, session.begin():
            seed_vehicles(session)
        yield test_engine
    finally:
        try:
            with engine.begin() as connection:
                connection.execute(DropSchema(schema, cascade=True, if_exists=True))
        finally:
            engine.dispose()


def use_independent_sessions(application: FastAPI, engine: Engine) -> None:
    def override_db() -> Iterator[Session]:
        with Session(engine) as session:
            yield session

    application.dependency_overrides[get_db] = override_db


@pytest.fixture
def transaction_client(application: FastAPI, committed_engine: Engine) -> Iterator[TestClient]:
    use_independent_sessions(application, committed_engine)
    with TestClient(application, raise_server_exceptions=False) as client:
        yield client


def plan_counts(engine: Engine) -> tuple[int, ...]:
    with Session(engine) as session:
        return tuple(
            session.scalar(select(func.count()).select_from(model))
            for model in (Plan, PlanItem, PlanCarSeat, PlanRetainedSeat)
        )


def test_in_progress_lock_from_another_connection_releases_for_retry(
    transaction_client: TestClient, committed_engine: Engine, settings: Settings
) -> None:
    key = uuid4()
    headers = {"Idempotency-Key": str(key)}
    with Session(committed_engine) as holder, holder.begin():
        assert holder.scalar(
            text("SELECT pg_try_advisory_xact_lock(:lock_id)"),
            {"lock_id": plans._lock_id(settings.demo_user_id, key)},
        )
        response = transaction_client.post("/api/v1/plans", json=PLAN_INPUT, headers=headers)
        assert response.status_code == 409
        assert response.json()["code"] == "REQUEST_IN_PROGRESS"
        assert plan_counts(committed_engine) == (0, 0, 0, 0)

    first = transaction_client.post("/api/v1/plans", json=PLAN_INPUT, headers=headers)
    retry = transaction_client.post("/api/v1/plans", json=PLAN_INPUT, headers=headers)
    assert first.status_code == 201
    assert retry.status_code == 200
    assert retry.json() == first.json()
    assert plan_counts(committed_engine) == (1, 1, 1, 1)


def test_response_failure_after_flush_rolls_back_all_rows_and_releases_key(
    transaction_client: TestClient, committed_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_after_flush(plan: Plan) -> None:
        assert plan.plan_id is not None
        assert plan.items[0].item_id is not None
        raise RuntimeError("private-response-construction-detail")

    headers = {"Idempotency-Key": str(uuid4())}
    with monkeypatch.context() as patch:
        patch.setattr(plans, "_plan_data", fail_after_flush)
        response = transaction_client.post("/api/v1/plans", json=PLAN_INPUT, headers=headers)
    assert response.status_code == 500
    assert response.json()["code"] == "INTERNAL_SERVER_ERROR"
    assert "private-response" not in response.text
    assert plan_counts(committed_engine) == (0, 0, 0, 0)
    retry = transaction_client.post("/api/v1/plans", json=PLAN_INPUT, headers=headers)
    assert retry.status_code == 201
    assert plan_counts(committed_engine) == (1, 1, 1, 1)


def test_simulated_commit_failure_rolls_back_all_rows_and_releases_key(
    application: FastAPI, transaction_client: TestClient, committed_engine: Engine
) -> None:
    def fail_commit(session: Session) -> None:
        raise OperationalError("COMMIT", {}, RuntimeError("private-commit-failure"))

    def override_db() -> Iterator[Session]:
        with Session(committed_engine) as session:
            event.listen(session, "before_commit", fail_commit)
            yield session

    application.dependency_overrides[get_db] = override_db
    headers = {"Idempotency-Key": str(uuid4())}
    response = transaction_client.post("/api/v1/plans", json=PLAN_INPUT, headers=headers)
    assert response.status_code == 500
    assert response.json()["code"] == "INTERNAL_SERVER_ERROR"
    assert "private-commit" not in response.text
    assert "COMMIT" not in response.text
    assert plan_counts(committed_engine) == (0, 0, 0, 0)

    use_independent_sessions(application, committed_engine)
    retry = transaction_client.post("/api/v1/plans", json=PLAN_INPUT, headers=headers)
    assert retry.status_code == 201
    assert plan_counts(committed_engine) == (1, 1, 1, 1)


def test_simultaneous_clients_create_one_plan_with_same_key(
    committed_engine: Engine, settings: Settings
) -> None:
    mobile_app = create_app(settings)
    pleos_app = create_app(settings)
    use_independent_sessions(mobile_app, committed_engine)
    use_independent_sessions(pleos_app, committed_engine)
    headers = {"Idempotency-Key": str(uuid4())}
    barrier = Barrier(2)

    def request(client: TestClient):
        barrier.wait(timeout=5)
        return client.post("/api/v1/plans", json=PLAN_INPUT, headers=headers)

    with (
        TestClient(mobile_app, raise_server_exceptions=False) as mobile,
        TestClient(pleos_app, raise_server_exceptions=False) as pleos,
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        futures = [pool.submit(request, client) for client in (mobile, pleos)]
        responses = [future.result(timeout=10) for future in futures]
        assert sorted(response.status_code for response in responses) in (
            [200, 201],
            [201, 409],
        )
        created = next(response for response in responses if response.status_code == 201)
        for response in responses:
            if response.status_code == 409:
                assert response.json()["code"] == "REQUEST_IN_PROGRESS"
            else:
                assert response.json() == created.json()
        retry = mobile.post("/api/v1/plans", json=PLAN_INPUT, headers=headers)
        assert retry.status_code == 200
        assert retry.json() == created.json()
    assert plan_counts(committed_engine) == (1, 1, 1, 1)


def test_concurrent_updates_use_one_revision(committed_engine: Engine, settings: Settings) -> None:
    apps = [create_app(settings) for _ in range(2)]
    for app in apps:
        use_independent_sessions(app, committed_engine)
    barrier = Barrier(2)
    with TestClient(apps[0]) as first, TestClient(apps[1]) as second:
        original = first.post(
            "/api/v1/plans", json=PLAN_INPUT, headers={"Idempotency-Key": str(uuid4())}
        ).json()["data"]
        path = f"/api/v1/plans/{original['plan_id']}"
        plan_input = {
            key: original[key]
            for key in ("vehicle_id", "passenger_count", "car_seats", "retained_seats", "items")
        }

        def edit(client: TestClient, quantity: int):
            payload = {**plan_input, "items": [{**plan_input["items"][0], "quantity": quantity}]}
            barrier.wait(timeout=5)
            return client.put(
                path,
                json={"expected_revision": 1, "plan": payload},
                headers={"Idempotency-Key": str(uuid4())},
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(edit, first, 1), pool.submit(edit, second, 3)]
            responses = [future.result(timeout=15) for future in futures]
        assert sorted(response.status_code for response in responses) == [200, 409]
        rejected = next(response for response in responses if response.status_code == 409)
        assert rejected.json()["code"] == "PLAN_REVISION_CONFLICT"
        assert first.get(path).json()["data"]["revision"] == 2


def test_parallel_metadata_changes_preserve_distinct_fields(
    committed_engine: Engine, settings: Settings
) -> None:
    apps = [create_app(settings) for _ in range(2)]
    for app in apps:
        use_independent_sessions(app, committed_engine)
    barrier = Barrier(2)
    with TestClient(apps[0]) as first, TestClient(apps[1]) as second:
        original = first.post(
            "/api/v1/plans", json=PLAN_INPUT, headers={"Idempotency-Key": str(uuid4())}
        ).json()["data"]
        path = f"/api/v1/plans/{original['plan_id']}"

        def edit(client: TestClient, fields: dict):
            barrier.wait(timeout=5)
            return client.patch(
                f"{path}/metadata",
                json={"expected_revision": 1, **fields},
                headers={"Idempotency-Key": str(uuid4())},
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(edit, first, {"name": "새 이름"}),
                pool.submit(edit, second, {"is_favorite": True}),
            ]
            responses = [future.result(timeout=15) for future in futures]
        assert [response.status_code for response in responses] == [200, 200]
        final = first.get(path).json()["data"]
        assert final["revision"] == 1 and final["name"] == "새 이름" and final["is_favorite"]
