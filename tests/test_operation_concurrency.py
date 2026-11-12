from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.core.exceptions import AppError
from app.services.operations import request_guard


def test_duplicate_execution_is_blocked_and_released(model_session: Session) -> None:
    key = uuid4()
    with request_guard(model_session, "demo-user", "/calculation", key):
        with pytest.raises(AppError) as error:
            with request_guard(model_session, "demo-user", "/calculation", key):
                pytest.fail("같은 키의 계산을 동시에 실행했습니다.")
        assert error.value.code == "REQUEST_IN_PROGRESS"
        with request_guard(model_session, "demo-user", "/calculation", uuid4()):
            pass
    with request_guard(model_session, "demo-user", "/calculation", key):
        pass


def test_failed_execution_releases_lock(model_session: Session) -> None:
    key = uuid4()
    with pytest.raises(RuntimeError):
        with request_guard(model_session, "demo-user", "/calculation", key):
            raise RuntimeError("실행 실패")
    with request_guard(model_session, "demo-user", "/calculation", key):
        pass
