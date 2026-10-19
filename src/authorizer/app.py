"""HTTP API Lambda authorizer (payload 2.0, simple responses): checks a static bearer token."""
import hmac
import logging
import os

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TOKEN_PARAMETER = os.environ["TOKEN_PARAMETER"]
ssm = boto3.client("ssm")   # module level: reused across warm invocations

_expected = None   # "Bearer <token>", fetched once per cold start. Never log it.


def _expected_header():
    global _expected
    if _expected is None:
        param = ssm.get_parameter(Name=TOKEN_PARAMETER, WithDecryption=True)
        _expected = f"Bearer {param['Parameter']['Value']}"
    return _expected


def handler(event, context):
    provided = ""
    for key, value in (event.get("headers") or {}).items():
        if key.lower() == "authorization":
            provided = value or ""
            break

    authorized = hmac.compare_digest(provided.encode("utf-8"), _expected_header().encode("utf-8"))
    if not authorized:
        logger.info("Rejected request %s", event.get("requestContext", {}).get("requestId"))
    return {"isAuthorized": authorized}
