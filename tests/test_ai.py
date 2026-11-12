import json
from io import BytesIO
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy import func, select

from app.core.exceptions import AppError
from app.db.seed import seed_vehicles
from app.db.session import get_db
from app.models.ai import AiConversation, ProductCandidate
from app.models.plan import Plan
from app.schemas.ai import AiMessage
from app.schemas.products import ProductSearch
from app.services.ai import (
    CargoIntent,
    ItemChange,
    _required_questions,
    apply_answers,
    apply_intent,
)
from app.services.llm import (
    LlmUnavailable,
    ResponsesClient,
    StackingEstimates,
    StackingUnavailable,
    estimate_stacking,
)
from app.services.products import SourcedProducts, _confirmed_dimension


def intent(**updates):
    values = {field: None for field in CargoIntent.model_fields}
    values.update(
        item_changes=[], ambiguous_item_reference=False, ambiguous_required_quantity=False
    )
    values.update(updates)
    return CargoIntent.model_validate(values)


def change(**updates):
    values = {field: None for field in ItemChange.model_fields}
    values.update(operation="ADD", evidence="의자 두 개", name="의자", quantity=2)
    values.update(updates)
    return ItemChange.model_validate(values)


def message(**updates):
    values = {
        "message": "의자 두 개",
        "context": {
            "scenario": "CHECK_ITEM",
            "vehicle_id": "avante_demo",
            "cargo_state": "UNKNOWN",
        },
    }
    values.update(updates)
    return AiMessage.model_validate(values)


def state():
    return {
        "context": message().context.model_dump(mode="json"),
        "plan": {
            "vehicle_id": "avante_demo",
            "passenger_count": 2,
            "car_seats": [],
            "retained_seats": [],
            "items": [],
        },
        "options": {"packing_mode": "REPACK_ALL", "allow_exclusions": False},
        "questions": [],
    }


ITEM = {
    "name": "의자",
    "width_cm": 20,
    "depth_cm": 20,
    "height_cm": 60,
    "quantity": 2,
    "dimension_source": "AR",
    "transport_state": "FOLDED",
}


@pytest.mark.parametrize("scenario", ["EDIT_PLAN", "ADD_TO_LOAD"])
def test_existing_scenarios_require_plan_revision(scenario):
    with pytest.raises(ValidationError):
        message(context={"scenario": scenario, "vehicle_id": "avante_demo"})


@pytest.mark.parametrize(
    "updates",
    [
        {"conversation_id": str(uuid4())},
        {"conversation_version": 1},
        {"answers": [{"question_id": "item_dimensions", "answer": [ITEM]}]},
        {"extra": True},
    ],
)
def test_message_rejects_invalid_context(updates):
    with pytest.raises(ValidationError):
        message(**updates)


def test_duplicate_answers_rejected():
    with pytest.raises(ValidationError):
        message(
            conversation_id=uuid4(),
            conversation_version=1,
            answers=[{"question_id": "cargo_state", "answer": "EMPTY"}] * 2,
        )


def test_answer_requires_current_question():
    payload = message(
        conversation_id=uuid4(),
        conversation_version=1,
        answers=[{"question_id": "old_question", "answer": 2}],
    )
    with pytest.raises(AppError) as error:
        apply_answers(state(), payload)
    assert error.value.code == "VALIDATION_ERROR"


@pytest.mark.parametrize("value", ["2", True, 0, 10, [], None])
def test_passenger_answer_is_strict(value):
    draft = state()
    draft["questions"] = [{"question_id": "passenger_count"}]
    payload = message(
        conversation_id=uuid4(),
        conversation_version=1,
        answers=[{"question_id": "passenger_count", "answer": value}],
    )
    with pytest.raises(AppError):
        apply_answers(draft, payload)


def test_context_and_cargo_answer_cannot_disagree():
    draft = state()
    draft["questions"] = [{"question_id": "cargo_state"}]
    payload = message(
        conversation_id=uuid4(),
        conversation_version=1,
        answers=[{"question_id": "cargo_state", "answer": "EMPTY"}],
    )
    with pytest.raises(AppError):
        apply_answers(draft, payload)


def test_ar_answer_preserves_required_count_and_ignores_model_overwrite():
    draft = state()
    item_id = str(uuid4())
    draft["plan"]["items"] = [{**ITEM, "item_id": item_id, "required_quantity": 2}]
    draft["questions"] = [{"question_id": "item_dimensions"}]
    payload = message(
        conversation_id=uuid4(),
        conversation_version=1,
        answers=[{"question_id": "item_dimensions", "answer": [ITEM]}],
    )
    answered = apply_answers(draft, payload)
    apply_intent(
        draft,
        intent(
            item_changes=[
                change(operation="UPDATE", item_id=item_id, width_cm=999, required_quantity=0)
            ]
        ),
        payload.message,
        [],
        answered,
    )
    assert draft["plan"]["items"][0]["width_cm"] == 20
    assert draft["plan"]["items"][0]["required_quantity"] == 2
    assert draft["plan"]["items"][0]["dimension_source"] == "AR"


def test_existing_ar_dimension_is_not_replaced_by_model_guess():
    draft = state()
    item_id = str(uuid4())
    draft["plan"]["items"] = [{**ITEM, "item_id": item_id}]
    apply_intent(
        draft,
        intent(item_changes=[change(operation="UPDATE", item_id=item_id, width_cm=999)]),
        "의자 두 개",
        [],
        set(),
    )
    assert draft["plan"]["items"][0]["width_cm"] == 20


def test_required_item_cannot_be_removed_without_user_evidence():
    draft = state()
    item_id = str(uuid4())
    draft["plan"]["items"] = [{**ITEM, "item_id": item_id, "required_quantity": 2}]
    with pytest.raises(LlmUnavailable):
        apply_intent(
            draft,
            intent(
                item_changes=[change(operation="REMOVE", item_id=item_id, evidence="의자를 빼줘")]
            ),
            "의자 두 개는 꼭 가져갈게",
            [],
            set(),
        )
    assert len(draft["plan"]["items"]) == 1


def test_wrong_semantic_remove_cannot_bypass_structured_confirmation():
    draft = state()
    item_id = str(uuid4())
    draft["plan"]["items"] = [{**ITEM, "item_id": item_id, "required_quantity": 2}]
    text = "필수 의자는 꼭 가져갈게"
    parsed = intent(item_changes=[change(operation="REMOVE", item_id=item_id, evidence=text)])
    apply_intent(draft, parsed, text, [], set())
    assert len(draft["plan"]["items"]) == 1
    draft["questions"] = _required_questions(draft, parsed, set())
    assert "confirm_changes" in {question["question_id"] for question in draft["questions"]}
    payload = message(
        conversation_id=uuid4(),
        conversation_version=1,
        answers=[{"question_id": "confirm_changes", "answer": False}],
    )
    apply_answers(draft, payload)
    assert len(draft["plan"]["items"]) == 1
    assert not draft["pending_operations"]


def test_explicit_dimension_change_requires_structured_measurement():
    draft = state()
    item_id = str(uuid4())
    draft["plan"]["items"] = [{**ITEM, "item_id": item_id}]
    text = "의자 가로를 30cm로 수정해"
    parsed = intent(
        item_changes=[change(operation="UPDATE", item_id=item_id, width_cm=30, evidence=text)]
    )
    apply_intent(draft, parsed, text, [], set())
    assert draft["plan"]["items"][0]["width_cm"] == 20
    draft["questions"] = _required_questions(draft, parsed, set())
    assert "item_dimensions" in {question["question_id"] for question in draft["questions"]}
    payload = message(
        conversation_id=uuid4(),
        conversation_version=1,
        answers=[
            {
                "question_id": "item_dimensions",
                "answer": [{**ITEM, "item_id": item_id, "width_cm": 30}],
            }
        ],
    )
    apply_answers(draft, payload)
    assert draft["plan"]["items"][0]["width_cm"] == 30
    assert draft["dimension_confirmation_ids"] == []


def test_ambiguous_remove_is_preserved_until_target_and_confirmation():
    draft = state()
    item_id = str(uuid4())
    draft["plan"]["items"] = [{**ITEM, "item_id": item_id, "required_quantity": 2}]
    parsed = intent(ambiguous_item_reference=True)
    apply_intent(draft, parsed, "의자를 빼줘", [], set())
    draft["questions"] = _required_questions(draft, parsed, set())
    payload = message(
        conversation_id=uuid4(),
        conversation_version=1,
        message="이 의자요",
        answers=[{"question_id": "item_reference", "answer": [{"item_id": item_id}]}],
    )
    answered = apply_answers(draft, payload)
    assert draft["pending_reference_request"] == "의자를 빼줘"
    followup = intent(
        item_changes=[change(operation="REMOVE", item_id=item_id, evidence="이 의자요")]
    )
    apply_intent(draft, followup, "이 의자요", [], answered)
    assert len(draft["plan"]["items"]) == 1
    assert "pending_reference_request" not in draft
    draft["questions"] = _required_questions(draft, followup, answered)
    assert "confirm_changes" in {question["question_id"] for question in draft["questions"]}
    confirmation = message(
        conversation_id=uuid4(),
        conversation_version=2,
        answers=[{"question_id": "confirm_changes", "answer": True}],
    )
    apply_answers(draft, confirmation)
    assert draft["plan"]["items"] == []


def test_unresolved_reference_never_silently_completes():
    draft = state()
    item_id = str(uuid4())
    draft["plan"]["items"] = [{**ITEM, "item_id": item_id}]
    parsed = intent(ambiguous_item_reference=True)
    apply_intent(draft, parsed, "의자를 빼줘", [], set())
    draft["questions"] = _required_questions(draft, parsed, set())
    answered = apply_answers(
        draft,
        message(
            conversation_id=uuid4(),
            conversation_version=1,
            answers=[{"question_id": "item_reference", "answer": [{"item_id": item_id}]}],
        ),
    )
    apply_intent(draft, intent(), "이 의자요", [], answered)
    assert "item_reference" in {
        question["question_id"] for question in _required_questions(draft, intent(), answered)
    }


def test_dimension_confirmation_only_clears_the_selected_duplicate_name():
    draft = state()
    first, second = str(uuid4()), str(uuid4())
    draft["plan"]["items"] = [{**ITEM, "item_id": first}, {**ITEM, "item_id": second}]
    draft["dimension_confirmation_ids"] = [first, second]
    draft["questions"] = [{"question_id": "item_dimensions"}]
    apply_answers(
        draft,
        message(
            conversation_id=uuid4(),
            conversation_version=1,
            answers=[
                {
                    "question_id": "item_dimensions",
                    "answer": [{**ITEM, "item_id": second, "width_cm": 30}],
                }
            ],
        ),
    )
    assert draft["dimension_confirmation_ids"] == [first]


def test_required_count_and_rotation_relaxation_wait_for_confirmation():
    draft = state()
    item_id = str(uuid4())
    draft["plan"]["items"] = [
        {**ITEM, "item_id": item_id, "required_quantity": 2, "allowed_orientations": ["WDH"]}
    ]
    text = "의자 필수 수량을 1개로 바꾸고 눕혀도 돼"
    parsed = intent(
        item_changes=[
            change(
                operation="UPDATE",
                item_id=item_id,
                evidence=text,
                required_quantity=1,
                allowed_orientations=["WDH", "DWH"],
            )
        ]
    )
    apply_intent(draft, parsed, text, [], set())
    assert draft["plan"]["items"][0]["required_quantity"] == 2
    assert draft["plan"]["items"][0]["allowed_orientations"] == ["WDH"]
    assert draft["pending_operations"]


def test_ambiguous_required_quantity_does_not_modify_items():
    draft = state()
    apply_intent(
        draft,
        intent(item_changes=[change(required_quantity=2)], ambiguous_required_quantity=True),
        "의자 두 개",
        [],
        set(),
    )
    assert draft["plan"]["items"] == []


def test_explicit_exclusion_choice_and_mode_are_preserved_without_changes():
    draft = state()
    draft["options"] = {"packing_mode": "KEEP_EXISTING", "allow_exclusions": True}
    apply_intent(draft, intent(), "다시 확인해줘", [], set())
    assert draft["options"] == {"packing_mode": "KEEP_EXISTING", "allow_exclusions": True}


@pytest.mark.parametrize(
    "value,expected",
    [(0, None), (-3, None), (41.55, None), (41.5, 41.5), (None, None), (float("inf"), None)],
)
def test_product_dimensions_never_round_unknown_values(value, expected):
    assert _confirmed_dimension(value) == expected


def test_product_state_duplicates_rejected():
    with pytest.raises(ValidationError):
        ProductSearch(query="의자", transport_states=["FOLDED", "FOLDED"])


def test_responses_request_uses_approved_model_tool_and_no_storage(monkeypatch, settings):
    settings.openai_api_key = SecretStr("test-key-never-send")
    captured = {}

    def fake_urlopen(request, timeout):
        captured.update(url=request.full_url, body=json.loads(request.data), timeout=timeout)
        return BytesIO(
            json.dumps(
                {
                    "status": "completed",
                    "output": [
                        {
                            "type": "function_call",
                            "name": "interpret_cargo_request",
                            "arguments": intent().model_dump_json(),
                        }
                    ],
                }
            ).encode()
        )

    monkeypatch.setattr("app.services.llm.urlopen", fake_urlopen)
    result = ResponsesClient(settings).call_function(
        "interpret_cargo_request", CargoIntent, "지침", {}
    )
    assert result.item_changes == []
    assert captured["url"] == "https://api.openai.com/v1/responses"
    assert captured["body"]["model"] == "gpt-6.1-terra"
    assert captured["body"]["reasoning"] == {"effort": "none"}
    assert captured["body"]["store"] is False
    assert captured["body"]["tools"][0]["strict"] is True
    assert captured["timeout"] == settings.llm_timeout_seconds


@pytest.mark.parametrize(
    "output",
    [
        {"status": "incomplete", "output": []},
        {"status": "completed", "output": []},
        {
            "status": "completed",
            "output": [{"type": "function_call", "name": "delete_plan", "arguments": "{}"}],
        },
        {
            "status": "completed",
            "output": [
                {"type": "function_call", "name": "interpret_cargo_request", "arguments": "{}"}
            ],
        },
    ],
)
def test_incomplete_or_unapproved_function_response_fails(monkeypatch, settings, output):
    settings.openai_api_key = SecretStr("test-key")
    monkeypatch.setattr(
        "app.services.llm.urlopen", lambda *a, **k: BytesIO(json.dumps(output).encode())
    )
    with pytest.raises(LlmUnavailable):
        ResponsesClient(settings).call_function("interpret_cargo_request", CargoIntent, "지침", {})


def test_no_api_key_never_calls_network(monkeypatch, settings):
    monkeypatch.setattr(
        "app.services.llm.urlopen", lambda *a, **k: pytest.fail("network was called")
    )
    with pytest.raises(LlmUnavailable):
        ResponsesClient(settings).call_function("interpret_cargo_request", CargoIntent, "지침", {})


@pytest.mark.parametrize(
    "assessment",
    [
        {"lower_item_id": "unknown", "upper_item_id": "a", "allowed": True, "reason": "평평함"},
        {"lower_item_id": "a", "upper_item_id": "a", "allowed": True, "reason": "20kg까지 가능"},
    ],
)
def test_stacking_rejects_unknown_items_and_invented_load(monkeypatch, settings, assessment):
    monkeypatch.setattr(
        ResponsesClient,
        "call_function",
        lambda *a, **k: StackingEstimates(assessments=[assessment]),
    )
    with pytest.raises(StackingUnavailable):
        estimate_stacking([{**ITEM, "item_id": "a"}], settings)


def test_stacking_unknown_is_not_approval(monkeypatch, settings):
    monkeypatch.setattr(
        ResponsesClient,
        "call_function",
        lambda *a, **k: StackingEstimates(
            assessments=[
                {
                    "lower_item_id": "a",
                    "upper_item_id": "a",
                    "allowed": None,
                    "reason": "운반 상태가 불명확합니다.",
                }
            ]
        ),
    )
    result = estimate_stacking([{**ITEM, "item_id": "a"}], settings)
    assert result[0]["allowed"] is None
    assert result[0]["basis"] == "LLM_ESTIMATE"


@pytest.fixture
def ai_client(application, model_session):
    seed_vehicles(model_session)
    model_session.commit()
    application.dependency_overrides[get_db] = lambda: model_session
    with TestClient(application) as client:
        yield client


@pytest.mark.integration
def test_ai_ar_followup_preview_replay_and_no_automatic_plan_save(
    ai_client, model_session, monkeypatch
):
    calls = []

    def fake_function(self, name, model, instructions, data):
        calls.append(name)
        if name == "estimate_stacking":
            return StackingEstimates(assessments=[])
        if len([call for call in calls if call == "interpret_cargo_request"]) == 1:
            return intent(item_changes=[change()], passenger_count=2, passenger_evidence="두 명")
        return intent()

    monkeypatch.setattr(ResponsesClient, "call_function", fake_function)
    payload = message(message="두 명이 타고 의자 두 개를 가져갈게").model_dump(mode="json")
    headers = {"Idempotency-Key": str(uuid4())}
    first = ai_client.post("/api/v1/ai/messages", json=payload, headers=headers)
    assert first.status_code == 200, first.text
    first_data = first.json()["data"]
    assert first_data["status"] == "NEEDS_INPUT"
    assert {question["question_id"] for question in first_data["questions"]} == {
        "cargo_state",
        "item_dimensions",
    }
    assert (
        ai_client.post("/api/v1/ai/messages", json=payload, headers=headers).json() == first.json()
    )
    assert calls == ["interpret_cargo_request"]
    second_payload = message(
        conversation_id=first_data["conversation_id"],
        conversation_version=1,
        message="측정값입니다",
        context={"scenario": "CHECK_ITEM", "vehicle_id": "avante_demo", "cargo_state": "EMPTY"},
        answers=[
            {"question_id": "cargo_state", "answer": "EMPTY"},
            {"question_id": "item_dimensions", "answer": [ITEM]},
        ],
    ).model_dump(mode="json")
    second_headers = {"Idempotency-Key": str(uuid4())}
    second = ai_client.post("/api/v1/ai/messages", json=second_payload, headers=second_headers)
    assert second.status_code == 200, second.text
    second_data = second.json()["data"]
    assert second_data["status"] == "READY", second.text
    assert second_data["preview_id"] is not None
    assert second_data["calculation"]["input_snapshot"]["items"][0]["dimension_source"] == "AR"
    assert second_data["conversation_version"] == 2
    assert model_session.scalar(select(func.count()).select_from(Plan)) == 0
    model_session.commit()
    assert (
        ai_client.post("/api/v1/ai/messages", json=second_payload, headers=second_headers).json()
        == second.json()
    )
    stale = ai_client.post(
        "/api/v1/ai/messages", json=second_payload, headers={"Idempotency-Key": str(uuid4())}
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "CONVERSATION_VERSION_CONFLICT"


@pytest.mark.integration
def test_ai_conversation_ownership(ai_client, model_session):
    conversation = AiConversation(
        conversation_id=uuid4(), user_id="other-user", version=1, state=state()
    )
    model_session.add(conversation)
    model_session.commit()
    payload = message(
        conversation_id=conversation.conversation_id, conversation_version=1
    ).model_dump(mode="json")
    model_session.commit()
    response = ai_client.post(
        "/api/v1/ai/messages", json=payload, headers={"Idempotency-Key": str(uuid4())}
    )
    assert response.status_code == 404
    assert response.json()["code"] == "CONVERSATION_NOT_FOUND"


@pytest.mark.integration
def test_product_search_sources_null_precision_and_idempotency(
    ai_client, model_session, monkeypatch
):
    source = {"url": "https://example.com/chair", "title": "제품 규격"}
    candidates = [
        {
            "name": "의자",
            "model": "A",
            "variant": None,
            "transport_state": "FOLDED",
            "width_cm": 41.55,
            "depth_cm": 20,
            "height_cm": None,
            "sources": [source],
        },
        {
            "name": "허위 출처",
            "model": None,
            "variant": None,
            "transport_state": "PACKAGED",
            "width_cm": 50,
            "depth_cm": 40,
            "height_cm": 20,
            "sources": [{"url": "https://not-retrieved.example.com", "title": "확인 안 됨"}],
        },
    ]
    calls = []

    def fake_search(*args, **kwargs):
        calls.append(True)
        return SourcedProducts(candidates=candidates), {source["url"]}

    monkeypatch.setattr(ResponsesClient, "search", fake_search)
    headers = {"Idempotency-Key": str(uuid4())}
    response = ai_client.post("/api/v1/products/search", json={"query": "의자"}, headers=headers)
    assert response.status_code == 200, response.text
    data = response.json()["data"]["candidates"]
    assert len(data) == 1
    assert data[0]["width_cm"] is None
    assert data[0]["depth_cm"] == 20
    assert data[0]["requires_confirmation"] is True
    assert (
        ai_client.post("/api/v1/products/search", json={"query": "의자"}, headers=headers).json()
        == response.json()
    )
    assert len(calls) == 1
    assert model_session.scalar(select(func.count()).select_from(ProductCandidate)) == 1
    model_session.commit()
    reused = ai_client.post("/api/v1/products/search", json={"query": "다른 의자"}, headers=headers)
    assert reused.status_code == 409


@pytest.mark.integration
def test_product_search_failure_stays_separate_from_ai_failure(ai_client, monkeypatch):
    monkeypatch.setattr(
        ResponsesClient,
        "call_function",
        lambda *a, **k: intent(
            product_search_query="제품 의자", product_search_evidence="제품 의자"
        ),
    )

    def fail(*args, **kwargs):
        raise LlmUnavailable("offline")

    monkeypatch.setattr(ResponsesClient, "search", fail)
    response = ai_client.post(
        "/api/v1/ai/messages",
        json=message(message="제품 의자 규격 찾아줘").model_dump(mode="json"),
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert response.status_code == 503
    assert response.json()["code"] == "PRODUCT_SEARCH_UNAVAILABLE"


@pytest.mark.integration
def test_ai_key_missing_reports_unavailable(ai_client):
    response = ai_client.post(
        "/api/v1/ai/messages",
        json=message().model_dump(mode="json"),
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert response.status_code == 503
    assert response.json()["code"] == "AI_UNAVAILABLE"


def _post(client, path, payload):
    return client.post(path, json=payload, headers={"Idempotency-Key": str(uuid4())})


@pytest.mark.integration
def test_edit_plan_previews_changes_without_mutating_saved_plan(
    ai_client, model_session, monkeypatch
):
    created = _post(
        ai_client,
        "/api/v1/plans",
        {"vehicle_id": "avante_demo", "passenger_count": 2, "items": [ITEM]},
    )
    assert created.status_code == 201, created.text
    plan = created.json()["data"]
    text = "탑승 인원을 세 명으로 바꿔줘"

    def interpret(self, name, model, instructions, data):
        if name == "estimate_stacking":
            return StackingEstimates(assessments=[])
        return intent(passenger_count=3, passenger_evidence=text)

    monkeypatch.setattr(ResponsesClient, "call_function", interpret)
    request = message(
        message=text,
        context={
            "scenario": "EDIT_PLAN",
            "vehicle_id": "avante_demo",
            "plan_id": plan["plan_id"],
            "plan_revision": plan["revision"],
            "cargo_state": "UNKNOWN",
        },
    )
    result = _post(ai_client, "/api/v1/ai/messages", request.model_dump(mode="json"))
    assert result.status_code == 200, result.text
    assert result.json()["data"]["status"] == "READY", result.text
    assert result.json()["data"]["proposed_plan"]["passenger_count"] == 3
    stored = ai_client.get(f"/api/v1/plans/{plan['plan_id']}").json()["data"]
    assert stored["passenger_count"] == 2
    assert stored["revision"] == plan["revision"]


@pytest.mark.integration
def test_add_to_selected_load_preserves_original_placements(ai_client, model_session, monkeypatch):
    created = _post(
        ai_client,
        "/api/v1/plans",
        {"vehicle_id": "avante_demo", "passenger_count": 2, "items": [ITEM]},
    )
    assert created.status_code == 201, created.text
    plan = created.json()["data"]
    calculation = _post(
        ai_client,
        f"/api/v1/plans/{plan['plan_id']}/calculations",
        {
            "expected_revision": plan["revision"],
            "current_folded_segments": [],
            "occupied_seats": [],
        },
    )
    assert calculation.status_code == 201, calculation.text
    result = calculation.json()["data"]
    base = result["arrangements"][0]
    selected = ai_client.put(
        f"/api/v1/plans/{plan['plan_id']}/selection",
        json={
            "expected_revision": plan["revision"],
            "calculation_id": result["calculation_id"],
            "arrangement_id": base["arrangement_id"],
        },
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert selected.status_code == 200, selected.text
    text = "기존 짐은 그대로 두고 상자 1개를 추가해줘. 가로 깊이 높이는 각각 20cm야."

    def interpret(self, name, model, instructions, data):
        if name == "estimate_stacking":
            return StackingEstimates(assessments=[])
        return intent(
            item_changes=[
                change(
                    name="상자", quantity=1, width_cm=20, depth_cm=20, height_cm=20, evidence=text
                )
            ],
            packing_mode="KEEP_EXISTING",
            mode_evidence="기존 짐은 그대로 두고",
        )

    monkeypatch.setattr(ResponsesClient, "call_function", interpret)
    request = message(
        message=text,
        context={
            "scenario": "ADD_TO_LOAD",
            "vehicle_id": "avante_demo",
            "plan_id": plan["plan_id"],
            "plan_revision": plan["revision"],
            "base_calculation_id": result["calculation_id"],
            "base_arrangement_id": base["arrangement_id"],
            "cargo_state": "SELECTED_ARRANGEMENT",
        },
    )
    response = _post(ai_client, "/api/v1/ai/messages", request.model_dump(mode="json"))
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["status"] == "READY", response.text
    placements = data["calculation"]["arrangements"][0]["placements"]
    for original in base["placements"]:
        current = next(
            placement
            for placement in placements
            if (placement["item_id"], placement["unit_index"])
            == (original["item_id"], original["unit_index"])
        )
        assert current == original
    assert len(data["proposed_plan"]["items"]) == 2
