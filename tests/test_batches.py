import base64
import json
import time
import uuid

from conftest import TABLE_NAME, get_event, job, post_event


def body_of(result):
    return json.loads(result["body"])


# ---------- POST /v1/score: request checks ----------

def test_missing_idempotency_key(submit):
    result = submit.handler(post_event({"jobs": [job()]}, key=None), None)
    assert result["statusCode"] == 400
    assert body_of(result)["error"] == "invalid_idempotency_key"


def test_malformed_idempotency_key(submit):
    for key in ["short", "x" * 129, "has space!"]:
        result = submit.handler(post_event({"jobs": [job()]}, key=key), None)
        assert result["statusCode"] == 400, key
        assert body_of(result)["error"] == "invalid_idempotency_key"


def test_invalid_json(submit):
    result = submit.handler(post_event(None, raw="{not json"), None)
    assert result["statusCode"] == 400
    assert body_of(result)["error"] == "invalid_json"


def test_validation_returns_all_errors(submit):
    body = {
        "jobs": [
            job("dup", title=""),
            job("dup", description="x" * 10_001),
            {"job_id": "j3", "title": 42},
            "not an object",
        ],
        "extra": True,
    }
    result = submit.handler(post_event(body), None)
    assert result["statusCode"] == 400
    payload = body_of(result)
    assert payload["error"] == "validation_error"
    errors = {(d["field"], d["message"]) for d in payload["details"]}
    assert errors == {
        ("extra", "unknown field"),
        ("jobs[0].title", "must not be empty"),
        ("jobs[1].description", "must be at most 10000 characters"),
        ("jobs[1].job_id", "duplicate job_id in batch"),
        ("jobs[2].title", "must be a string"),
        ("jobs[2].company", "is required"),
        ("jobs[2].description", "is required"),
        ("jobs[3]", "must be an object"),
    }


def test_job_count_limits(submit):
    for jobs in ([], [job(f"j{i}") for i in range(21)]):
        result = submit.handler(post_event({"jobs": jobs}), None)
        assert result["statusCode"] == 400
        assert {"field": "jobs", "message": "must contain between 1 and 20 jobs"} in body_of(result)["details"]


def test_body_must_be_object(submit):
    result = submit.handler(post_event([job()]), None)
    assert body_of(result)["details"] == [{"field": "body", "message": "must be a JSON object"}]


def test_invalid_request_is_not_recorded(submit, table):
    submit.handler(post_event({"jobs": []}), None)
    assert table.scan(TableName=TABLE_NAME)["Count"] == 0


# ---------- POST /v1/score: success and idempotency ----------

def test_submit_success(submit, table):
    result = submit.handler(post_event({"jobs": [job("a"), job("b", url=None)]}), None)

    assert result["statusCode"] == 202
    payload = body_of(result)
    batch_id = payload["batch_id"]
    uuid.UUID(batch_id)
    assert payload == {"batch_id": batch_id, "status": "queued", "job_count": 2}
    assert result["headers"]["Location"] == f"/v1/batches/{batch_id}"

    items = table.scan(TableName=TABLE_NAME)["Items"]
    keys = {(i["PK"]["S"], i["SK"]["S"]) for i in items}
    assert keys == {
        ("IDEMP#key-12345678", "IDEMP"),
        (f"BATCH#{batch_id}", "META"),
        (f"BATCH#{batch_id}", "JOB#a"),
        (f"BATCH#{batch_id}", "JOB#b"),
    }
    idem = next(i for i in items if i["SK"]["S"] == "IDEMP")
    assert idem["batch_id"]["S"] == batch_id
    assert idem["response_status"]["N"] == "202"
    assert int(idem["ttl"]["N"]) > 0


def test_base64_body(submit):
    event = post_event({"jobs": [job()]})
    event["body"] = base64.b64encode(event["body"].encode()).decode()
    event["isBase64Encoded"] = True
    assert submit.handler(event, None)["statusCode"] == 202


def test_idempotent_replay_returns_same_batch(submit, table):
    first = submit.handler(post_event({"jobs": [job("a"), job("b")]}), None)
    # Same content, different key order and whitespace: same request_hash.
    raw = '{ "jobs": [ %s, %s ] }' % (
        json.dumps(dict(reversed(list(job("a").items())))),
        json.dumps(job("b")),
    )
    second = submit.handler(post_event(None, raw=raw), None)

    assert second["statusCode"] == 202
    assert second["body"] == first["body"]
    assert second["headers"]["Location"] == first["headers"]["Location"]
    metas = [i for i in table.scan(TableName=TABLE_NAME)["Items"] if i["SK"]["S"] == "META"]
    assert len(metas) == 1


def test_same_key_different_body_conflicts(submit):
    submit.handler(post_event({"jobs": [job("a")]}), None)
    result = submit.handler(post_event({"jobs": [job("b")]}), None)
    assert result["statusCode"] == 409
    assert body_of(result) == {"error": "idempotency_key_conflict"}


def test_expired_key_not_yet_deleted_creates_new_batch(submit, table):
    old_batch_id = str(uuid.uuid4())
    table.put_item(TableName=TABLE_NAME, Item={
        "PK": {"S": "IDEMP#key-12345678"},
        "SK": {"S": "IDEMP"},
        "request_hash": {"S": "0" * 64},   # differs from the new body's hash
        "batch_id": {"S": old_batch_id},
        "response_status": {"N": "202"},
        "response_body": {"S": json.dumps({"batch_id": old_batch_id, "status": "queued", "job_count": 1})},
        "ttl": {"N": str(int(time.time()) - 60)},   # expired, but TTL sweeper hasn't run
    })

    result = submit.handler(post_event({"jobs": [job("new")]}), None)

    assert result["statusCode"] == 202
    new_batch_id = body_of(result)["batch_id"]
    assert new_batch_id != old_batch_id
    idem = table.get_item(TableName=TABLE_NAME,
                          Key={"PK": {"S": "IDEMP#key-12345678"}, "SK": {"S": "IDEMP"}})["Item"]
    assert idem["batch_id"]["S"] == new_batch_id
    assert int(idem["ttl"]["N"]) > time.time()


def test_unexpected_dynamodb_error_returns_500(submit, monkeypatch):
    from botocore.exceptions import ClientError

    def boom(**kwargs):
        raise ClientError({"Error": {"Code": "InternalServerError", "Message": "secret detail"}},
                          "TransactWriteItems")

    monkeypatch.setattr(submit.dynamodb, "transact_write_items", boom)
    result = submit.handler(post_event({"jobs": [job()]}), None)
    assert result["statusCode"] == 500
    assert body_of(result) == {"error": "internal_error"}


# ---------- GET /v1/batches/{id} ----------

def test_get_batch(submit, get_batch):
    jobs = [job("z"), job("a"), job("m")]
    batch_id = body_of(submit.handler(post_event({"jobs": jobs}), None))["batch_id"]

    result = get_batch.handler(get_event(batch_id), None)
    assert result["statusCode"] == 200
    payload = body_of(result)
    assert payload["batch_id"] == batch_id
    assert payload["status"] == "queued"
    assert payload["job_count"] == 3
    assert payload["created_at"].endswith("Z")
    assert payload["jobs"] == [
        {"job_id": j["job_id"], "title": j["title"], "company": j["company"], "status": "queued", "score": None}
        for j in jobs   # submission order, not job_id order
    ]


def test_get_batch_not_found(get_batch):
    for batch_id in [str(uuid.uuid4()), "not-a-uuid"]:
        result = get_batch.handler(get_event(batch_id), None)
        assert result["statusCode"] == 404
        assert body_of(result) == {"error": "not_found"}
