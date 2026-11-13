"""Unit tests for the hand-publishing script: each scenario sends what it promises."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.messaging.events import TaskUpdate
from scripts.publish_update import SCENARIOS, build_messages

TASK, TENANT = uuid4(), uuid4()


def parsed(scenario: str) -> list[TaskUpdate]:
    return [TaskUpdate.model_validate_json(m.body) for m in build_messages(scenario, TASK, TENANT)]


@pytest.mark.parametrize("status", ["in_progress", "done", "failed"])
def test_single_update_is_a_valid_task_update_for_the_task(status: str) -> None:
    (update,) = parsed(status)

    assert (update.task_id, update.tenant_id, update.status) == (TASK, TENANT, status)
    assert (update.error is not None) == (status == "failed")


def test_duplicate_sends_one_message_twice() -> None:
    first, second = build_messages("duplicate", TASK, TENANT)

    assert first == second  # same message_id, same bytes


def test_stale_sends_done_then_in_progress() -> None:
    assert [u.status for u in parsed("stale")] == ["done", "in_progress"]


def test_poison_is_not_a_task_update() -> None:
    (message,) = build_messages("poison", None, None)

    with pytest.raises(ValidationError):
        TaskUpdate.model_validate_json(message.body)


def test_unknown_task_is_well_formed_but_names_another_task() -> None:
    (message,) = build_messages("unknown-task", None, None)

    update = TaskUpdate.model_validate_json(message.body)
    assert update.task_id != TASK


def test_every_scenario_is_buildable() -> None:
    for scenario in SCENARIOS:
        assert build_messages(scenario, TASK, TENANT)
