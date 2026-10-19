"""Helpers shared by the submit and get-batch handlers."""
import json

MAX_JOBS = 20
MAX_DESCRIPTION = 10_000
MAX_JOB_ID = 128
MAX_TEXT = 500
MAX_URL = 2_048

ALLOWED_TOP_LEVEL = {"jobs"}
REQUIRED_JOB_FIELDS = (
    ("job_id", MAX_JOB_ID),
    ("title", MAX_TEXT),
    ("company", MAX_TEXT),
    ("description", MAX_DESCRIPTION),
)


def response(status, body, headers=None):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", **(headers or {})},
        "body": body if isinstance(body, str) else json.dumps(body),
    }


def error(status, code, **extra):
    return response(status, {"error": code, **extra})


def header(event, name):
    # HTTP API (payload 2.0) lowercases header names; be defensive anyway.
    name = name.lower()
    for key, value in (event.get("headers") or {}).items():
        if key.lower() == name:
            return value
    return None


def validate_body(body):
    """Return a list of {"field", "message"} errors; empty means valid."""
    if not isinstance(body, dict):
        return [{"field": "body", "message": "must be a JSON object"}]

    errors = []
    for key in sorted(body.keys() - ALLOWED_TOP_LEVEL):
        errors.append({"field": key, "message": "unknown field"})

    jobs = body.get("jobs")
    if "jobs" not in body:
        errors.append({"field": "jobs", "message": "is required"})
        return errors
    if not isinstance(jobs, list):
        errors.append({"field": "jobs", "message": "must be an array"})
        return errors
    if not 1 <= len(jobs) <= MAX_JOBS:
        errors.append({"field": "jobs", "message": f"must contain between 1 and {MAX_JOBS} jobs"})

    seen_ids = set()
    for i, job in enumerate(jobs):
        prefix = f"jobs[{i}]"
        if not isinstance(job, dict):
            errors.append({"field": prefix, "message": "must be an object"})
            continue

        for name, max_len in REQUIRED_JOB_FIELDS:
            field = f"{prefix}.{name}"
            value = job.get(name)
            if value is None:
                errors.append({"field": field, "message": "is required"})
            elif not isinstance(value, str):
                errors.append({"field": field, "message": "must be a string"})
            elif not value.strip():
                errors.append({"field": field, "message": "must not be empty"})
            elif len(value) > max_len:
                errors.append({"field": field, "message": f"must be at most {max_len} characters"})

        url = job.get("url")
        if url is not None:
            if not isinstance(url, str):
                errors.append({"field": f"{prefix}.url", "message": "must be a string"})
            elif len(url) > MAX_URL:
                errors.append({"field": f"{prefix}.url", "message": f"must be at most {MAX_URL} characters"})

        job_id = job.get("job_id")
        if isinstance(job_id, str) and job_id.strip():
            if job_id in seen_ids:
                errors.append({"field": f"{prefix}.job_id", "message": "duplicate job_id in batch"})
            seen_ids.add(job_id)

    return errors
