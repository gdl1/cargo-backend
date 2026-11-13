import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.core.exceptions import AppError
from app.models.operation import OperationReceipt


def _json_value(value):
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(f"Unsupported value: {type(value).__name__}")


def json_data(value):
    return json.loads(json.dumps(value, default=_json_value, ensure_ascii=False, allow_nan=False))


def request_fingerprint(payload) -> str:
    raw = json.dumps(json_data(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


@contextmanager
def request_guard(session: Session, user_id: str, scope: str, key: UUID) -> Iterator[None]:
    """외부 호출 중에도 같은 키의 중복 실행을 막고 연결 종료 시 잠금을 해제합니다."""
    bind = session.get_bind()
    engine = bind.engine if isinstance(bind, Connection) else bind
    digest = hashlib.sha256(json.dumps(["execution", user_id, scope, str(key)]).encode()).digest()
    lock_id = int.from_bytes(digest[:8], "big", signed=True)
    # 긴 LLM 요청의 잠금이 일반 조회·저장용 연결 풀을 소진하지 않게 분리합니다.
    guard_engine = create_engine(
        engine.url,
        poolclass=NullPool,
        isolation_level="AUTOCOMMIT",
        connect_args={"connect_timeout": 3},
    )
    with guard_engine.connect() as connection:
        acquired = connection.scalar(text("SELECT pg_try_advisory_lock(:id)"), {"id": lock_id})
        if not acquired:
            raise AppError(409, "REQUEST_IN_PROGRESS", "같은 요청을 처리 중입니다.")
        try:
            yield
        finally:
            try:
                connection.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": lock_id})
            except Exception:
                connection.invalidate()
                raise


def replay_request(session: Session, user_id: str, scope: str, key: UUID, payload) -> dict | None:
    """호출자의 트랜잭션 안에서 잠그고 완료 응답을 먼저 확인합니다."""
    digest = hashlib.sha256(json.dumps([user_id, scope, str(key)]).encode()).digest()
    lock_id = int.from_bytes(digest[:8], "big", signed=True)
    if not session.scalar(text("SELECT pg_try_advisory_xact_lock(:id)"), {"id": lock_id}):
        raise AppError(409, "REQUEST_IN_PROGRESS", "같은 요청을 처리 중입니다.")
    receipt = session.get(OperationReceipt, (user_id, scope, key))
    if receipt is None:
        return None
    if receipt.request_hash != request_fingerprint(payload):
        raise AppError(409, "IDEMPOTENCY_KEY_REUSED", "다른 입력에 사용한 요청 키입니다.")
    return receipt.response


def save_response(session: Session, user_id: str, scope: str, key: UUID, payload, response) -> None:
    session.add(
        OperationReceipt(
            user_id=user_id,
            scope=scope,
            key=key,
            request_hash=request_fingerprint(payload),
            response=json_data(response),
        )
    )
    session.flush()
