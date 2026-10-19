import boto3
import pytest

from conftest import ROOT, TOKEN_PARAMETER, load_module

TOKEN = "s3cret-token-value"


@pytest.fixture
def authorizer(aws):
    boto3.client("ssm").put_parameter(Name=TOKEN_PARAMETER, Value=TOKEN, Type="SecureString")
    return load_module("authorizer_app", ROOT / "src" / "authorizer" / "app.py")


def event(authorization):
    headers = {} if authorization is None else {"authorization": authorization}
    return {"headers": headers, "requestContext": {"requestId": "r1"}}


def test_accepts_correct_token(authorizer):
    assert authorizer.handler(event(f"Bearer {TOKEN}"), None) == {"isAuthorized": True}


@pytest.mark.parametrize("value", [
    None,
    "",
    TOKEN,                      # missing "Bearer "
    f"Bearer {TOKEN}x",
    "Bearer wrong",
    f"bearer {TOKEN}",
])
def test_rejects_bad_token(authorizer, value):
    assert authorizer.handler(event(value), None) == {"isAuthorized": False}


def test_token_fetched_once_per_cold_start(authorizer, monkeypatch):
    calls = []
    original = authorizer.ssm.get_parameter

    def counting(**kwargs):
        calls.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(authorizer.ssm, "get_parameter", counting)
    for _ in range(3):
        authorizer.handler(event(f"Bearer {TOKEN}"), None)
    assert calls == [{"Name": TOKEN_PARAMETER, "WithDecryption": True}]


def test_token_never_logged(authorizer, caplog):
    caplog.set_level("INFO")   # the Lambda log level
    authorizer.handler(event(f"Bearer {TOKEN}"), None)
    authorizer.handler(event("Bearer wrong"), None)
    assert TOKEN not in caplog.text
