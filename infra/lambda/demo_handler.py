"""Minimal demo handler: API Gateway proxy <-> DynamoDB item access.

Deliberately plumbing only: read one item by its request identifier
(GET) or store one item (PUT). The identifier is the table's verified
partition key ``pk`` (string, no sort key): reads use
``Key={"pk": identifier}`` and writes store it under ``pk``. The HTTP
path/query parameter stays ``id``. No business logic, no retries, no
secrets, no network calls beyond the DynamoDB table named by the
TABLE_NAME environment variable, which Terraform sets from the
provisioned table.

This file is project-owned demo material built reproducibly by
build_demo_artifact.py. It is never authored by the LLM.
"""

import json
import os

import boto3

_table_resource = None


def _table():
    global _table_resource
    if _table_resource is None:
        _table_resource = boto3.resource("dynamodb").Table(
            os.environ["TABLE_NAME"]
        )
    return _table_resource


def _response(status, body):
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body),
    }


def handler(event, context):
    """Lambda entry point. ``event`` is an API Gateway HTTP API payload."""
    _ = context
    method = (event.get("httpMethod") or event.get("requestContext", {})
              .get("http", {}).get("method", "") or "").upper()
    params = event.get("pathParameters") or {}
    item_id = params.get("id") or (event.get("queryStringParameters")
                                   or {}).get("id", "")
    if not item_id:
        return _response(400, {"error": "missing id"})

    table = _table()
    if method == "GET":
        result = table.get_item(Key={"pk": item_id})
        item = result.get("Item")
        if item is None:
            return _response(404, {"error": "not found"})
        return _response(200, item)
    if method in ("PUT", "POST"):
        try:
            body = json.loads(event.get("body") or "{}")
        except ValueError:
            return _response(400, {"error": "invalid JSON body"})
        if not isinstance(body, dict):
            return _response(400, {"error": "object body required"})
        body["pk"] = item_id
        table.put_item(Item=body)
        return _response(200, body)
    return _response(405, {"error": "method not allowed"})
