import importlib.util
import json
import os
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

ROOT = Path(__file__).resolve().parent.parent
TABLE_NAME = "jobs-test"
TOKEN_PARAMETER = "/job-scoring-api/api-token"

os.environ.update({
    "AWS_DEFAULT_REGION": "us-east-2",
    "AWS_ACCESS_KEY_ID": "testing",
    "AWS_SECRET_ACCESS_KEY": "testing",
    "AWS_SESSION_TOKEN": "testing",
    "TABLE_NAME": TABLE_NAME,
    "TOKEN_PARAMETER": TOKEN_PARAMETER,
})
sys.path.insert(0, str(ROOT / "src" / "batches"))


def load_module(name, path):
    """Import a handler fresh, so its module-level boto3 client is created inside the moto mock."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def aws():
    with mock_aws():
        yield


@pytest.fixture
def table(aws):
    client = boto3.client("dynamodb")
    client.create_table(
        TableName=TABLE_NAME,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
        ],
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
    )
    return client


@pytest.fixture
def submit(table):
    return load_module("submit", ROOT / "src" / "batches" / "submit.py")


@pytest.fixture
def get_batch(table):
    return load_module("get_batch", ROOT / "src" / "batches" / "get_batch.py")


def post_event(body, key="key-12345678", raw=None):
    headers = {"content-type": "application/json"}
    if key is not None:
        headers["idempotency-key"] = key
    return {
        "headers": headers,
        "body": raw if raw is not None else json.dumps(body),
        "isBase64Encoded": False,
    }


def get_event(batch_id):
    return {"headers": {}, "pathParameters": {"id": batch_id}}


def job(job_id="j1", **overrides):
    return {
        "job_id": job_id,
        "title": "Backend Engineer",
        "company": "Acme",
        "description": "Build APIs.",
        "url": f"https://example.com/{job_id}",
        **overrides,
    }
