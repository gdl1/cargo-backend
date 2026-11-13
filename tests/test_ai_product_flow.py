from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.db.seed import seed_vehicles
from app.db.session import get_db
from app.models.ai import AiConversation, ProductCandidate
from app.models.plan import Plan
from app.models.preview import CalculationPreview
from app.services.ai import CargoIntent
from app.services.llm import ResponsesClient, StackingEstimates
from app.services.products import SourcedProducts

pytestmark = pytest.mark.integration
SOURCE = {"url": "https://example.com/chair", "title": "의자 제품 규격"}
CONTEXT = {"scenario": "CHECK_ITEM", "vehicle_id": "avante_demo", "cargo_state": "EMPTY"}
BOX = {"name": "상자", "width_cm": 10, "depth_cm": 10, "height_cm": 10, "quantity": 1}


@pytest.fixture
def product_client(application, model_session, monkeypatch):
    seed_vehicles(model_session)
    model_session.commit()
    application.dependency_overrides[get_db] = lambda: model_session

    def interpret(self, name, model, instructions, data):
        if name == "estimate_stacking":
            return StackingEstimates(assessments=[])
        assert name == "interpret_cargo_request"
        values = {field: None for field in CargoIntent.model_fields}
        values.update(
            item_changes=[], ambiguous_item_reference=False, ambiguous_required_quantity=False
        )
        if data["message"] == "혼자 타고 의자 제품을 검색해 줘":
            values.update(
                passenger_count=1,
                passenger_evidence="혼자",
                product_search_query="의자",
                product_search_evidence="의자 제품을 검색해 줘",
            )
        return CargoIntent.model_validate(values)

    def search(self, model, instructions, data):
        return SourcedProducts(
            candidates=[
                {
                    "name": "캠핑 의자",
                    "model": "A",
                    "variant": None,
                    "transport_state": "ASSEMBLED",
                    "width_cm": 20,
                    "depth_cm": 20,
                    "height_cm": None,
                    "sources": [SOURCE],
                },
                {
                    "name": "캠핑 의자",
                    "model": "A",
                    "variant": None,
                    "transport_state": "PACKAGED",
                    "width_cm": 22,
                    "depth_cm": 22,
                    "height_cm": 18,
                    "sources": [SOURCE],
                },
            ]
        ), {SOURCE["url"]}

    monkeypatch.setattr(ResponsesClient, "call_function", interpret)
    monkeypatch.setattr(ResponsesClient, "search", search)
    with TestClient(application) as client:
        yield client


def post(client, path, payload, key=None):
    return client.post(path, json=payload, headers={"Idempotency-Key": str(key or uuid4())})


def data(response, status=200):
    assert response.status_code == status, response.text
    return response.json()["data"]


def request(context, previous=None, **values):
    payload = {"context": context, "message": "확인했습니다", **values}
    if previous is not None:
        payload.update(
            conversation_id=previous["conversation_id"],
            conversation_version=previous["conversation_version"],
        )
    return payload


def search_products(client, context):
    result = data(
        post(
            client,
            "/api/v1/ai/messages",
            request(context, message="혼자 타고 의자 제품을 검색해 줘"),
        )
    )
    assert result["status"] == "NEEDS_INPUT"
    assert result["preview_id"] is None
    assert len(result["product_candidates"]) == 2
    return result


def confirmed_item(candidate, **updates):
    return {
        key: candidate[key]
        for key in ("name", "width_cm", "depth_cm", "height_cm", "transport_state")
    } | {"quantity": 2, "required_quantity": 2, "dimension_source": "PRODUCT_SOURCE", **updates}


@pytest.mark.parametrize("edit_existing", [False, True])
def test_product_confirmation_calculation_and_explicit_plan_save(
    product_client, model_session, edit_existing
):
    client = product_client
    context = dict(CONTEXT)
    original = None
    if edit_existing:
        original = data(
            post(
                client,
                "/api/v1/plans",
                {"vehicle_id": "avante_demo", "passenger_count": 1, "items": [BOX]},
            ),
            201,
        )
        context.update(
            scenario="EDIT_PLAN", plan_id=original["plan_id"], plan_revision=original["revision"]
        )
    searched = search_products(client, context)
    candidate = searched["product_candidates"][0 if edit_existing else 1]
    selected = data(
        post(
            client,
            "/api/v1/ai/messages",
            request(context, searched, selected_product_candidate_id=candidate["candidate_id"]),
        )
    )
    assert selected["status"] == "NEEDS_INPUT"
    assert selected["preview_id"] is None
    assert [question["question_id"] for question in selected["questions"]] == ["item_dimensions"]
    item = confirmed_item(candidate)
    if edit_existing:
        assert item["height_cm"] is None
        item.update(height_cm=35, dimension_source="AR")
    payload = request(
        context, selected, answers=[{"question_id": "item_dimensions", "answer": [item]}]
    )
    key = uuid4()
    response = post(client, "/api/v1/ai/messages", payload, key)
    ready = data(response)
    assert ready["status"] == "READY", response.text
    assert ready["calculation"]["result_code"] == "LAYOUT_FOUND"
    assert post(client, "/api/v1/ai/messages", payload, key).json() == response.json()
    assert model_session.scalar(select(func.count()).select_from(CalculationPreview)) == 1
    assert model_session.scalar(select(func.count()).select_from(Plan)) == int(edit_existing)
    model_session.commit()
    proposed = next(row for row in ready["proposed_plan"]["items"] if row["name"] == item["name"])
    assert all(proposed[field] == value for field, value in item.items())
    save = {
        "source_preview_id": ready["preview_id"],
        "selected_arrangement_id": ready["calculation"]["arrangements"][0]["arrangement_id"],
    }
    if edit_existing:
        path = f"/api/v1/plans/{original['plan_id']}"
        assert data(client.get(path))["items"] == original["items"]
        model_session.commit()
        save["expected_revision"] = original["revision"]
        saved = data(client.put(path, json=save, headers={"Idempotency-Key": str(uuid4())}))
        assert saved["revision"] == original["revision"] + 1
        assert saved["plan_id"] == original["plan_id"]
    else:
        saved = data(post(client, "/api/v1/plans", save), 201)
    stored_item = next(row for row in saved["items"] if row["name"] == item["name"])
    assert all(stored_item[field] == value for field, value in item.items())
    result = data(
        client.get(
            f"/api/v1/plans/{saved['plan_id']}/calculations/{saved['selected_calculation_id']}"
        )
    )
    placements = result["arrangements"][0]["placements"]
    assert sum(row["item_id"] == stored_item["item_id"] for row in placements) == 2


def test_switching_candidate_requires_new_dimensions_and_rejects_missing_height(
    product_client, model_session
):
    client = product_client
    searched = search_products(client, CONTEXT)
    assembled, packaged = searched["product_candidates"]
    selected = data(
        post(
            client,
            "/api/v1/ai/messages",
            request(CONTEXT, searched, selected_product_candidate_id=packaged["candidate_id"]),
        )
    )
    switched = data(
        post(
            client,
            "/api/v1/ai/messages",
            request(CONTEXT, selected, selected_product_candidate_id=assembled["candidate_id"]),
        )
    )
    assert switched["status"] == "NEEDS_INPUT"
    assert switched["product_candidates"][0]["transport_state"] == "ASSEMBLED"
    assert switched["product_candidates"][0]["height_cm"] is None
    assert switched["preview_id"] is None
    item = confirmed_item(assembled)
    payload = request(
        CONTEXT, switched, answers=[{"question_id": "item_dimensions", "answer": [item]}]
    )
    invalid = post(client, "/api/v1/ai/messages", payload)
    assert invalid.status_code == 422
    assert invalid.json()["code"] == "VALIDATION_ERROR"
    conversation = model_session.get(AiConversation, UUID(switched["conversation_id"]))
    assert conversation.version == switched["conversation_version"]
    assert model_session.scalar(select(func.count()).select_from(CalculationPreview)) == 0
    model_session.commit()
    item.update(height_cm=35, dimension_source="AR")
    ready = data(post(client, "/api/v1/ai/messages", payload))
    assert ready["status"] == "READY"
    assert len(ready["proposed_plan"]["items"]) == 1
    confirmed = ready["proposed_plan"]["items"][0]
    assert confirmed["height_cm"] == 35
    assert confirmed["transport_state"] == "ASSEMBLED"
    assert confirmed["dimension_source"] == "AR"


@pytest.mark.parametrize("foreign_candidate", [False, True])
def test_product_candidate_lookup_rejects_missing_or_foreign_candidate_without_changing_state(
    product_client, model_session, foreign_candidate
):
    searched = search_products(product_client, CONTEXT)
    candidate_id = uuid4()
    if foreign_candidate:
        candidate_id = UUID(searched["product_candidates"][0]["candidate_id"])
        candidate = model_session.get(ProductCandidate, candidate_id)
        candidate.user_id = "another-user"
        model_session.commit()
    response = post(
        product_client,
        "/api/v1/ai/messages",
        request(CONTEXT, searched, selected_product_candidate_id=str(candidate_id)),
    )
    assert response.status_code == 404
    assert response.json()["code"] == "PRODUCT_CANDIDATE_NOT_FOUND"
    conversation = model_session.get(AiConversation, UUID(searched["conversation_id"]))
    assert conversation.version == searched["conversation_version"]
    assert "selected_product_candidate" not in conversation.state
    assert model_session.scalar(select(func.count()).select_from(CalculationPreview)) == 0
