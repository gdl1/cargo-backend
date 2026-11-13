from uuid import UUID, uuid4

import pytest
from test_ai import ITEM, _post, change, intent, message
from test_ai import ai_client as ai_client

from app.models.ai import AiConversation
from app.models.plan import Plan
from app.schemas.plan import PlanInputUpdate
from app.services.llm import ResponsesClient, StackingEstimates

pytestmark = pytest.mark.integration

ADD_MESSAGE = "가로 깊이 높이가 각각 20cm인 포장된 상자 한 개도 실을 수 있나요?"


@pytest.fixture
def selected_plan(ai_client, monkeypatch):
    def interpret(self, name, model, instructions, data):
        if name == "estimate_stacking":
            return StackingEstimates(assessments=[])
        if data["message"] == ADD_MESSAGE:
            return intent(
                item_changes=[
                    change(
                        name="상자",
                        quantity=1,
                        width_cm=20,
                        depth_cm=20,
                        height_cm=20,
                        transport_state="PACKAGED",
                        evidence=ADD_MESSAGE,
                    )
                ]
            )
        return intent()

    monkeypatch.setattr(ResponsesClient, "call_function", interpret)
    return _create_selected_plan(ai_client)


def _create_selected_plan(client):
    response = _post(
        client,
        "/api/v1/plans",
        {"vehicle_id": "avante_demo", "passenger_count": 2, "items": [ITEM]},
    )
    assert response.status_code == 201, response.text
    plan = response.json()["data"]
    return plan, _calculate_and_select(client, plan)


def _calculate_and_select(client, plan):
    response = _post(
        client,
        f"/api/v1/plans/{plan['plan_id']}/calculations",
        {
            "expected_revision": plan["revision"],
            "current_folded_segments": [],
            "occupied_seats": [],
        },
    )
    assert response.status_code == 201, response.text
    calculation = response.json()["data"]
    base = {
        "base_calculation_id": calculation["calculation_id"],
        "base_arrangement_id": calculation["arrangements"][0]["arrangement_id"],
    }
    selected = client.put(
        f"/api/v1/plans/{plan['plan_id']}/selection",
        json={
            "expected_revision": plan["revision"],
            "calculation_id": base["base_calculation_id"],
            "arrangement_id": base["base_arrangement_id"],
        },
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert selected.status_code == 200, selected.text
    return base


def _context(plan, base=None):
    return {
        "scenario": "CHECK_ITEM",
        "vehicle_id": plan["vehicle_id"],
        "plan_id": plan["plan_id"],
        "plan_revision": plan["revision"],
        "cargo_state": "SELECTED_ARRANGEMENT" if base else "UNKNOWN",
        **(base or {}),
    }


def _start(client, plan, base=None):
    response = _post(
        client,
        "/api/v1/ai/messages",
        message(message=ADD_MESSAGE, context=_context(plan, base)).model_dump(mode="json"),
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    if base is None:
        assert data["status"] == "NEEDS_INPUT"
        assert [question["question_id"] for question in data["questions"]] == ["cargo_state"]
    return data


def _followup(client, first, plan, base, *, answer=True):
    return _post(
        client,
        "/api/v1/ai/messages",
        message(
            message="현재 선택한 배치의 짐이 실려 있어요.",
            conversation_id=first["conversation_id"],
            conversation_version=first["conversation_version"],
            context=_context(plan, base),
            answers=(
                [{"question_id": "cargo_state", "answer": "SELECTED_ARRANGEMENT"}] if answer else []
            ),
        ).model_dump(mode="json"),
    )


def _assert_rejected(response, first, session, status, code):
    assert response.status_code == status, response.text
    assert response.json()["code"] == code
    stored = session.get(AiConversation, UUID(first["conversation_id"]))
    assert stored.version == first["conversation_version"]
    session.commit()


def _update_plan(client, plan):
    inputs = {key: plan[key] for key in PlanInputUpdate.model_fields if key in plan}
    response = client.put(
        f"/api/v1/plans/{plan['plan_id']}",
        json={"expected_revision": plan["revision"], "plan": inputs},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_check_item_can_attach_first_base_to_existing_plan(ai_client, selected_plan, model_session):
    plan, base = selected_plan
    first = _start(ai_client, plan)

    response = _followup(ai_client, first, plan, base)

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["status"] == "READY", response.text
    assert data["preview_id"] is not None
    assert data["conversation_id"] == first["conversation_id"]
    assert data["conversation_version"] == first["conversation_version"] + 1
    assert data["questions"] == []
    assert [item["name"] for item in data["proposed_plan"]["items"]] == ["의자", "상자"]
    assert data["proposed_plan"]["items"][0]["item_id"] == plan["items"][0]["item_id"]
    for field, value in base.items():
        assert data["calculation"][field] == value
    stored = model_session.get(AiConversation, UUID(first["conversation_id"]))
    for field, value in _context(plan, base).items():
        assert stored.state["context"][field] == value
    model_session.commit()
    saved = ai_client.get(f"/api/v1/plans/{plan['plan_id']}").json()["data"]
    assert saved["revision"] == plan["revision"]
    assert saved["items"] == plan["items"]


def test_first_base_requires_the_current_cargo_state_answer(
    ai_client, selected_plan, model_session
):
    plan, base = selected_plan
    first = _start(ai_client, plan)

    response = _followup(ai_client, first, plan, base, answer=False)

    _assert_rejected(response, first, model_session, 422, "VALIDATION_ERROR")


def test_existing_base_cannot_be_replaced_in_same_conversation(
    ai_client, selected_plan, model_session
):
    plan, base = selected_plan
    first = _start(ai_client, plan, base)
    replacement = _calculate_and_select(ai_client, plan)

    response = _followup(ai_client, first, plan, replacement, answer=False)

    _assert_rejected(response, first, model_session, 422, "VALIDATION_ERROR")


def test_first_base_cannot_attach_a_different_plan(ai_client, selected_plan, model_session):
    plan, _ = selected_plan
    first = _start(ai_client, plan)
    other_plan, other_base = _create_selected_plan(ai_client)

    response = _followup(ai_client, first, other_plan, other_base)

    _assert_rejected(response, first, model_session, 422, "VALIDATION_ERROR")


def test_first_base_cannot_change_conversation_plan_revision(
    ai_client, selected_plan, model_session
):
    plan, _ = selected_plan
    first = _start(ai_client, plan)
    updated = _update_plan(ai_client, plan)
    base = _calculate_and_select(ai_client, updated)

    response = _followup(ai_client, first, updated, base)

    _assert_rejected(response, first, model_session, 422, "VALIDATION_ERROR")


def test_first_base_rejects_stale_plan_revision(ai_client, selected_plan, model_session):
    plan, base = selected_plan
    first = _start(ai_client, plan)
    _update_plan(ai_client, plan)

    response = _followup(ai_client, first, plan, base)

    _assert_rejected(response, first, model_session, 409, "PLAN_REVISION_CONFLICT")


def test_first_base_must_still_be_selected(ai_client, selected_plan, model_session):
    plan, base = selected_plan
    first = _start(ai_client, plan)
    _calculate_and_select(ai_client, plan)

    response = _followup(ai_client, first, plan, base)

    _assert_rejected(response, first, model_session, 409, "BASE_LAYOUT_STALE")


@pytest.mark.parametrize(
    ("target", "code"),
    [("plan", "PLAN_NOT_FOUND"), ("conversation", "CONVERSATION_NOT_FOUND")],
)
def test_first_base_checks_ownership(ai_client, selected_plan, model_session, target, code):
    plan, base = selected_plan
    first = _start(ai_client, plan)
    row = (
        model_session.get(Plan, UUID(plan["plan_id"]))
        if target == "plan"
        else model_session.get(AiConversation, UUID(first["conversation_id"]))
    )
    row.user_id = "other-user"
    model_session.commit()

    response = _followup(ai_client, first, plan, base)

    _assert_rejected(response, first, model_session, 404, code)
