"""One bearer check for every HTTP surface (#241)."""

from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from malkuth.http_auth import presented_token, require_token


def app_with(expected: str | None) -> TestClient:
    app = FastAPI()

    @app.get("/guarded", dependencies=[Depends(require_token(expected, realm="test token"))])
    async def guarded() -> dict[str, str]:
        return {"ok": "yes"}

    return TestClient(app)


def test_the_right_token_passes():
    assert (
        app_with("s3cret").get("/guarded", headers={"authorization": "Bearer s3cret"}).status_code
        == 200
    )


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"authorization": "Bearer wrong"},
        {"authorization": "s3cret"},
        {"authorization": "Basic s3cret"},
        {"authorization": "Bearer "},
    ],
)
def test_anything_else_is_401(headers):
    response = app_with("s3cret").get("/guarded", headers=headers)

    assert response.status_code == 401
    assert response.headers.get("www-authenticate") == "Bearer"


def test_no_expected_token_means_no_check():
    """켜는 것은 조립하는 쪽의 결정이다 — 미설정이면 통과."""
    assert app_with(None).get("/guarded").status_code == 200


def test_the_rejection_names_the_realm_not_the_token():
    """사유에 토큰 값이 섞이면 로그로 샌다."""
    body = app_with("s3cret").get("/guarded", headers={"authorization": "Bearer nope"}).json()

    assert body["detail"] == "invalid test token"
    assert "nope" not in body["detail"] and "s3cret" not in body["detail"]


class _Req:
    def __init__(self, value: str | None) -> None:
        self.headers = {} if value is None else {"authorization": value}


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("Bearer abc", "abc"),
        ("bearer abc", None),
        ("Bearer ", None),
        (None, None),
        ("Token abc", None),
    ],
)
def test_presented_token_is_strict_about_the_scheme(header, expected):
    assert presented_token(_Req(header)) == expected  # type: ignore[arg-type]
