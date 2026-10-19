"""POST /v1/score: validate a batch and store it, idempotently, in one transaction."""
import base64
import hashlib
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

from common import error, header, response, validate_body

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TABLE_NAME = os.environ["TABLE_NAME"]
dynamodb = boto3.client("dynamodb")   # module level: reused across warm invocations

IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
IDEMPOTENCY_TTL_SECONDS = 24 * 60 * 60


def handler(event, context):
    key = header(event, "Idempotency-Key")
    if not key or not IDEMPOTENCY_KEY_RE.match(key):
        return error(400, "invalid_idempotency_key",
                     message="Idempotency-Key header is required: 8-128 characters of [A-Za-z0-9_-]")

    try:
        raw = event.get("body") or ""
        if event.get("isBase64Encoded"):
            raw = base64.b64decode(raw).decode("utf-8")
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return error(400, "invalid_json", message="Request body must be valid JSON")

    details = validate_body(body)
    if details:
        return error(400, "validation_error", details=details)

    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    request_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    batch_id = str(uuid.uuid4())
    jobs = body["jobs"]
    response_body = json.dumps({"batch_id": batch_id, "status": "queued", "job_count": len(jobs)})
    now = time.time()
    created_at = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    items = [
        {
            "Put": {
                "TableName": TABLE_NAME,
                "Item": {
                    "PK": {"S": f"IDEMP#{key}"},
                    "SK": {"S": "IDEMP"},
                    "request_hash": {"S": request_hash},
                    "batch_id": {"S": batch_id},
                    "response_status": {"N": "202"},
                    "response_body": {"S": response_body},
                    "ttl": {"N": str(int(now) + IDEMPOTENCY_TTL_SECONDS)},
                },
                # TTL deletion can lag by days; treat an expired record as absent so expiry is strict.
                "ConditionExpression": "attribute_not_exists(PK) OR #ttl < :now",
                "ExpressionAttributeNames": {"#ttl": "ttl"},
                "ExpressionAttributeValues": {":now": {"N": str(int(now))}},
                "ReturnValuesOnConditionCheckFailure": "ALL_OLD",
            }
        },
        {
            "Put": {
                "TableName": TABLE_NAME,
                "Item": {
                    "PK": {"S": f"BATCH#{batch_id}"},
                    "SK": {"S": "META"},
                    "status": {"S": "queued"},
                    "created_at": {"S": created_at},
                    "job_count": {"N": str(len(jobs))},
                },
            }
        },
    ]
    for position, job in enumerate(jobs):
        url = job.get("url")
        items.append({
            "Put": {
                "TableName": TABLE_NAME,
                "Item": {
                    "PK": {"S": f"BATCH#{batch_id}"},
                    "SK": {"S": f"JOB#{job['job_id']}"},
                    "title": {"S": job["title"]},
                    "company": {"S": job["company"]},
                    "description": {"S": job["description"]},
                    "url": {"S": url} if url is not None else {"NULL": True},
                    "status": {"S": "queued"},
                    "score": {"NULL": True},
                    "position": {"N": str(position)},
                },
            }
        })

    try:
        dynamodb.transact_write_items(TransactItems=items)
    except ClientError as exc:
        old_item = _failed_idempotency_item(exc)
        if old_item is None:
            logger.exception("TransactWriteItems failed for batch %s", batch_id)
            return error(500, "internal_error")
        if old_item["request_hash"]["S"] != request_hash:
            return error(409, "idempotency_key_conflict")
        logger.info("Idempotent replay of batch %s", old_item["batch_id"]["S"])
        return _stored_response(old_item)

    logger.info("Queued batch %s with %d jobs", batch_id, len(jobs))
    return _stored_response({
        "batch_id": {"S": batch_id},
        "response_status": {"N": "202"},
        "response_body": {"S": response_body},
    })


def _failed_idempotency_item(exc):
    """If the transaction was cancelled because the idempotency key already exists, return the old item."""
    if exc.response.get("Error", {}).get("Code") != "TransactionCanceledException":
        return None
    reasons = exc.response.get("CancellationReasons") or []
    if not reasons or reasons[0].get("Code") != "ConditionalCheckFailed":
        return None
    return reasons[0].get("Item")


def _stored_response(item):
    batch_id = item["batch_id"]["S"]
    return response(
        int(item["response_status"]["N"]),
        item["response_body"]["S"],
        headers={"Location": f"/v1/batches/{batch_id}"},
    )
