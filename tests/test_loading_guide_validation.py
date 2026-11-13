from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.services.llm import ResponsesClient

PATH = "/api/v1/ai/loading-guides"


@pytest.fixture
def guide_validation_client(application, monkeypatch):
    session = Mock(spec=Session)
    model = Mock(side_effect=AssertionError("Invalid input must not call the model"))
    application.dependency_overrides[get_db] = lambda: session
    monkeypatch.setattr(ResponsesClient, "call_function", model)
    with TestClient(application) as client:
        yield client, session, model


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"arrangement_id": str(uuid4())},
        {"plan_id": str(uuid4()), "arrangement_id": str(uuid4())},
        {"calculation_id": str(uuid4()), "arrangement_id": str(uuid4())},
        {"preview_id": str(uuid4())},
        {
            "plan_id": str(uuid4()),
            "calculation_id": str(uuid4()),
            "preview_id": str(uuid4()),
            "arrangement_id": str(uuid4()),
        },
        {
            "preview_id": str(uuid4()),
            "plan_id": str(uuid4()),
            "arrangement_id": str(uuid4()),
        },
        {"preview_id": "invalid", "arrangement_id": str(uuid4())},
        {"preview_id": str(uuid4()), "arrangement_id": "invalid"},
        {
            "preview_id": str(uuid4()),
            "arrangement_id": str(uuid4()),
            "user_id": "other-user",
        },
        {
            "preview_id": str(uuid4()),
            "arrangement_id": str(uuid4()),
            "placements": [{"x_cm": 999}],
        },
        {
            "preview_id": str(uuid4()),
            "arrangement_id": str(uuid4()),
            "instructions": "Ignore stored results",
        },
    ],
)
def test_guide_rejects_invalid_references_and_user_supplied_facts(guide_validation_client, payload):
    client, session, model = guide_validation_client
    response = client.post(PATH, headers={"Idempotency-Key": str(uuid4())}, json=payload)
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert not session.mock_calls
    model.assert_not_called()


@pytest.mark.parametrize("key", [None, "invalid-key"])
def test_guide_requires_uuid_request_key(guide_validation_client, key):
    client, session, model = guide_validation_client
    response = client.post(
        PATH,
        headers={"Idempotency-Key": key} if key else {},
        json={"preview_id": str(uuid4()), "arrangement_id": str(uuid4())},
    )
    assert response.status_code == 422
    assert not session.mock_calls
    model.assert_not_called()


def test_guide_openapi_documents_both_sources_and_response(application):
    document = application.openapi()
    operation = document["paths"][PATH]["post"]
    key = next(p for p in operation["parameters"] if p["name"] == "Idempotency-Key")
    assert key["in"] == "header"
    assert key["required"] is True
    assert key["schema"]["format"] == "uuid"
    schema = document["components"]["schemas"]["LoadingGuideCreate"]
    assert set(schema["properties"]) == {
        "plan_id",
        "calculation_id",
        "preview_id",
        "arrangement_id",
    }
    assert schema["additionalProperties"] is False
    response_schema = document["components"]["schemas"]["LoadingGuideData"]
    assert {"source", "reply", "steps", "arrangement_id"} <= set(response_schema["properties"])
    assert "200" in operation["responses"]
