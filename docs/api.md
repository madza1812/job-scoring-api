# Job Scoring API — v1 contract

Base URL: the `ApiUrl` stack output, e.g. `https://<api-id>.execute-api.us-east-2.amazonaws.com`.

All request and response bodies are JSON (`Content-Type: application/json`).

## Authentication

Every route except `GET /v1/hello` requires:

```
Authorization: Bearer <token>
```

The token is stored in SSM Parameter Store as the SecureString `/job-scoring-api/api-token`.
API Gateway caches the authorizer decision per `Authorization` value for 300 seconds.

| Situation                       | Status | Body (set by API Gateway)       |
|---------------------------------|--------|---------------------------------|
| `Authorization` header missing  | 401    | `{"message": "Unauthorized"}`   |
| Wrong token                     | 403    | `{"message": "Forbidden"}`      |

## Throttling

Stage-wide default route settings: rate 5 requests/second, burst 10. Over the limit,
API Gateway returns `429 {"message": "Too Many Requests"}`.

---

## `GET /v1/hello`

Public health check (no auth).

**200**

```json
{"message": "hello from Lambda", "request_id": "<lambda request id>"}
```

---

## `POST /v1/score`

Submit a batch of jobs for scoring. Scoring itself is asynchronous; this call only queues the batch.

### Headers

| Header            | Required | Rules                                         |
|-------------------|----------|-----------------------------------------------|
| `Authorization`   | yes      | `Bearer <token>`                              |
| `Idempotency-Key` | yes      | 8–128 characters, `[A-Za-z0-9_-]` only        |

### Body

```json
{
  "jobs": [
    {
      "job_id": "abc-123",
      "title": "Backend Engineer",
      "company": "Acme",
      "description": "Build APIs ...",
      "url": "https://example.com/jobs/abc-123"
    }
  ]
}
```

### Validation rules

| Field                    | Rule                                                              |
|--------------------------|-------------------------------------------------------------------|
| body                     | must be a JSON object                                             |
| top-level fields         | only `jobs` is allowed; any other top-level field is rejected     |
| `jobs`                   | required array with 1–20 elements                                 |
| `jobs[i]`                | must be an object                                                 |
| `jobs[i].job_id`         | required non-empty string, ≤ 128 chars, unique within the batch   |
| `jobs[i].title`          | required non-empty string, ≤ 500 chars                            |
| `jobs[i].company`        | required non-empty string, ≤ 500 chars                            |
| `jobs[i].description`    | required non-empty string, ≤ 10,000 chars                         |
| `jobs[i].url`            | optional; if present must be a string ≤ 2,048 chars, or `null`    |

Unknown fields *inside* a job object are ignored (not stored).
All validation errors are returned together in one response.

### Responses

**202 Accepted** — batch queued.

Header: `Location: /v1/batches/{batch_id}`

```json
{"batch_id": "3f2b8c1e-...", "status": "queued", "job_count": 1}
```

**400** — missing or malformed `Idempotency-Key` (checked first):

```json
{"error": "invalid_idempotency_key", "message": "Idempotency-Key header is required: 8-128 characters of [A-Za-z0-9_-]"}
```

**400** — body is not valid JSON:

```json
{"error": "invalid_json", "message": "Request body must be valid JSON"}
```

**400** — validation failed:

```json
{
  "error": "validation_error",
  "details": [
    {"field": "jobs[0].title", "message": "is required"},
    {"field": "jobs[1].description", "message": "must be at most 10000 characters"},
    {"field": "extra", "message": "unknown field"}
  ]
}
```

`field` uses dotted / indexed paths (`jobs`, `jobs[2]`, `jobs[2].job_id`); errors about the body
as a whole use `field: "body"`.

**409** — the `Idempotency-Key` was already used with a different body:

```json
{"error": "idempotency_key_conflict"}
```

**500** — unexpected failure:

```json
{"error": "internal_error"}
```

### Idempotency

- Keys expire after 24 hours. After that, the same key is treated as new: the request creates a new
  batch regardless of its body, even if the old record has not been physically deleted yet.
- The request fingerprint is the SHA-256 of the parsed body re-serialized with sorted keys and
  compact separators, so whitespace and key order do not matter.
- Same key + same fingerprint: the original `202` response (status, body, `Location`) is replayed;
  no new batch is created.
- Same key + different fingerprint: `409`.
- Requests that fail validation are not recorded, so the key can be reused after fixing the body.

---

## `GET /v1/batches/{batch_id}`

### Responses

**200**

```json
{
  "batch_id": "3f2b8c1e-...",
  "status": "queued",
  "created_at": "2026-10-19T14:03:07Z",
  "job_count": 1,
  "jobs": [
    {"job_id": "abc-123", "title": "Backend Engineer", "company": "Acme", "status": "queued", "score": null}
  ]
}
```

Jobs are returned in the order they were submitted.

`status` (batch and job) is one of `queued | scoring | done | partial`. Only `queued` is produced
in this phase; `score` is always `null` until scoring is implemented.

**404** — unknown batch id (including ids that are not UUIDs):

```json
{"error": "not_found"}
```

---

## Storage (DynamoDB, single table)

| Item        | PK              | SK               | Attributes                                                         |
|-------------|-----------------|------------------|--------------------------------------------------------------------|
| Batch       | `BATCH#<id>`    | `META`           | `status`, `created_at` (ISO-8601 UTC), `job_count`                 |
| Job         | `BATCH#<id>`    | `JOB#<job_id>`   | `title`, `company`, `description`, `url`, `status`, `score`, `position` |
| Idempotency | `IDEMP#<key>`   | `IDEMP`          | `request_hash`, `batch_id`, `response_status`, `response_body`, `ttl` |

`ttl` (epoch seconds) is the table's TTL attribute; idempotency items expire 24 h after creation.
DynamoDB may delete expired items late, so the idempotency write uses the condition
`attribute_not_exists(PK) OR ttl < now` and overwrites an expired record.
`position` is the job's index in the submitted array, used to keep response order.
