"""GET /v1/batches/{id}: one strongly consistent Query over the batch's partition."""
import logging
import os
import uuid

import boto3

from common import error, response

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TABLE_NAME = os.environ["TABLE_NAME"]
dynamodb = boto3.client("dynamodb")   # module level: reused across warm invocations

PROJECTED = ("SK", "status", "created_at", "job_count", "title", "company", "score", "position")


def handler(event, context):
    batch_id = (event.get("pathParameters") or {}).get("id", "")
    try:
        uuid.UUID(batch_id)
    except ValueError:
        return error(404, "not_found")

    items = []
    kwargs = {
        "TableName": TABLE_NAME,
        "KeyConditionExpression": "PK = :pk",
        "ExpressionAttributeValues": {":pk": {"S": f"BATCH#{batch_id}"}},
        # Skip descriptions: they are not returned and are the bulk of each item.
        # Alias every name so DynamoDB reserved words (status, position, ...) never bite.
        "ProjectionExpression": ", ".join(f"#{name}" for name in PROJECTED),
        "ExpressionAttributeNames": {f"#{name}": name for name in PROJECTED},
        "ConsistentRead": True,
    }
    while True:
        page = dynamodb.query(**kwargs)
        items.extend(page["Items"])
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    meta = next((item for item in items if item["SK"]["S"] == "META"), None)
    if meta is None:
        return error(404, "not_found")

    jobs = sorted(
        (item for item in items if item["SK"]["S"].startswith("JOB#")),
        key=lambda item: int(item["position"]["N"]),
    )
    return response(200, {
        "batch_id": batch_id,
        "status": meta["status"]["S"],
        "created_at": meta["created_at"]["S"],
        "job_count": int(meta["job_count"]["N"]),
        "jobs": [
            {
                "job_id": item["SK"]["S"][len("JOB#"):],
                "title": item["title"]["S"],
                "company": item["company"]["S"],
                "status": item["status"]["S"],
                "score": _number_or_none(item.get("score")),
            }
            for item in jobs
        ],
    })


def _number_or_none(attr):
    if not attr or "N" not in attr:
        return None
    value = float(attr["N"])
    return int(value) if value.is_integer() else value
