"""Structured errors for the employee mobile API.

The shipped mobile app string-matches the ``detail`` field of error
responses, so ``detail`` must stay a plain English sentence. These errors
add machine-readable fields *alongside* it: a stable ``code``, localized
human messages, and a ``retryable`` flag, plus ``X-Error-Code`` /
``X-Error-Retryable`` headers. Old clients keep working; new clients act on
the code instead of the wording.
"""

from __future__ import annotations

from fastapi import HTTPException


class MobileApiError(HTTPException):
    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        message_zh: str,
        message_es: str | None = None,
        retryable: bool,
        retry_after_seconds: int | None = None,
    ) -> None:
        headers = {
            "X-Error-Code": code,
            "X-Error-Retryable": "true" if retryable else "false",
        }
        if retry_after_seconds is not None:
            headers["Retry-After"] = str(max(1, int(retry_after_seconds)))
        super().__init__(status_code=status_code, detail=message, headers=headers)
        self.code = code
        self.message_zh = message_zh
        self.message_es = message_es
        self.retryable = retryable

    def response_body(self) -> dict:
        messages = {"en": self.detail, "zh": self.message_zh}
        if self.message_es:
            messages["es"] = self.message_es
        return {
            "detail": self.detail,
            "code": self.code,
            "message": messages,
            "retryable": self.retryable,
        }
