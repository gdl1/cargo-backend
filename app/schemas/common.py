from typing import Any

from pydantic import BaseModel


class ApiResponse[T](BaseModel):
    code: str
    message: str
    data: T | None = None


class ErrorResponse(BaseModel):
    code: str
    message: str
    data: dict[str, Any] | None = None
