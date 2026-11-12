from uuid import UUID, uuid4

import pytest
from test_ai import ITEM, answer_item_reference, change, intent, message, state

from app.services.ai import _required_questions, apply_answers, apply_intent


def mixed_reference_request():
    draft = state()
    draft["context"]["cargo_state"] = "EMPTY"
    first, second, other = [str(uuid4()) for _ in range(3)]
    draft["plan"]["items"] = [
        {**ITEM, "item_id": first},
        {**ITEM, "item_id": second, "name": "배낭"},
        {**ITEM, "item_id": other, "name": "상자"},
    ]
    update = change(
        operation="UPDATE", item_id=first, name=None, quantity=3, evidence="의자는 세 개로"
    )
    remove = change(operation="REMOVE", name=None, quantity=None, evidence="배낭은 빼줘")
    apply_intent(
        draft,
        intent(item_changes=[update, remove], ambiguous_item_reference=True),
        "의자는 세 개로 하고 배낭은 빼줘",
        [],
        set(),
    )
    return draft, first, second, other, update, remove


def confirm_changes(draft):
    draft["questions"] = _required_questions(draft, intent(), set())
    return apply_answers(
        draft,
        message(
            conversation_id=uuid4(),
            conversation_version=2,
            answers=[{"question_id": "confirm_changes", "answer": True}],
        ),
    )


@pytest.mark.parametrize("reverse_selection", [False, True])
@pytest.mark.parametrize("repeat_known_change", [False, True])
def test_mixed_reference_only_removes_the_resolved_target(reverse_selection, repeat_known_change):
    draft, first, second, other, update, remove = mixed_reference_request()
    selected = [second, first] if reverse_selection else [first, second]
    answered = answer_item_reference(draft, selected)
    changes = [remove.model_copy(update={"item_id": UUID(second)})]
    if repeat_known_change:
        changes.insert(0, update)
    apply_intent(draft, intent(item_changes=changes), "선택한 짐이에요", [], answered)

    assert [item["quantity"] for item in draft["plan"]["items"]] == [3, 2, 2]
    assert [operation["item_id"] for operation in draft["pending_operations"]] == [second]
    assert "pending_reference_request" not in draft
    confirmed = confirm_changes(draft)
    assert [item["item_id"] for item in draft["plan"]["items"]] == [first, other]
    assert draft["plan"]["items"][0]["quantity"] == 3
    assert _required_questions(draft, intent(), confirmed) == []


@pytest.mark.parametrize(
    "followup", ["empty", "known_only", "changed_action", "known_target", "unselected_target"]
)
def test_ambiguous_mixed_reference_asks_before_removing_and_can_resume(followup):
    draft, first, second, other, update, remove = mixed_reference_request()
    answered = answer_item_reference(draft, [first, second])
    changes = [] if followup == "empty" else [update]
    if followup == "changed_action":
        changes.append(remove.model_copy(update={"item_id": UUID(second), "operation": "UPDATE"}))
    if followup == "known_target":
        changes.append(remove.model_copy(update={"item_id": UUID(first)}))
    if followup == "unselected_target":
        changes.append(remove.model_copy(update={"item_id": UUID(other)}))
    apply_intent(draft, intent(item_changes=changes), "선택한 짐이에요", [], answered)

    assert [item["quantity"] for item in draft["plan"]["items"]] == [2, 2, 2]
    assert not draft.get("pending_operations")
    reference = next(
        question
        for question in _required_questions(draft, intent(), answered)
        if question["question_id"] == "item_reference"
    )
    assert remove.evidence in reference["message"]

    answered = answer_item_reference(draft, [second])
    apply_intent(draft, intent(), "선택한 짐이에요", [], answered)
    assert [item["quantity"] for item in draft["plan"]["items"]] == [3, 2, 2]
    assert [operation["item_id"] for operation in draft["pending_operations"]] == [second]
    assert "pending_reference_request" not in draft
    confirmed = confirm_changes(draft)
    assert [item["item_id"] for item in draft["plan"]["items"]] == [first, other]
    assert _required_questions(draft, intent(), confirmed) == []


def test_single_ambiguous_removal_still_accepts_multiple_targets_with_a_known_update():
    draft, first, second, other, _, _ = mixed_reference_request()
    answered = answer_item_reference(draft, [other, second])
    apply_intent(draft, intent(), "선택한 짐이에요", [], answered)

    assert draft["plan"]["items"][0]["quantity"] == 3
    assert {operation["item_id"] for operation in draft["pending_operations"]} == {second, other}
    confirmed = confirm_changes(draft)
    assert [item["item_id"] for item in draft["plan"]["items"]] == [first]
    assert _required_questions(draft, intent(), confirmed) == []
