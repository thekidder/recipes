"""Validate the authoritative contract and synthetic exchanges; no server/data access."""
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from jsonschema import Draft202012Validator, FormatChecker
from openapi_spec_validator import validate_spec
from referencing import Registry, Resource
import yaml


ROOT = Path(__file__).resolve().parent
spec = yaml.safe_load((ROOT.parent / "openapi.yaml").read_text())
validate_spec(spec)


def deref(value):
    while "$ref" in value:
        pointer = value["$ref"]
        assert pointer.startswith("#/"), f"Nonlocal reference: {pointer}"
        value = spec
        for part in pointer[2:].split("/"):
            value = value[part.replace("~1", "/").replace("~0", "~")]
    return value


def check_refs(value):
    if isinstance(value, dict):
        if "$ref" in value:
            deref(value)
        for child in value.values():
            check_refs(child)
    elif isinstance(value, list):
        for child in value:
            check_refs(child)


check_refs(spec)
operations = {}
for path, methods in spec["paths"].items():
    for method, operation in methods.items():
        name = operation["operationId"]
        assert name not in operations, f"Duplicate operation ID: {name}"
        operations[name] = (path, method.upper(), operation)
        if path.startswith("/control/v1/"):
            assert operation["security"] == []

assert set(spec["components"]["securitySchemes"]) == {"BearerAuth"}
assert spec["security"] == [{"BearerAuth": []}]

uri = "urn:erecipe:contract"
resource = Resource.from_contents({"$schema": "https://json-schema.org/draft/2020-12/schema", **spec})
registry = Registry().with_resource(uri, resource)


def validator(schema):
    # The full document supplies local references for a standalone subschema.
    return Draft202012Validator(
        {"$id": uri, "components": spec["components"], **schema},
        registry=registry,
        format_checker=FormatChecker(),
    )


index = json.loads((ROOT / "examples/index.json").read_text())
assert index["contract_version"] == spec["info"]["version"]
count = 0
for entry in index["scenarios"]:
    case = json.loads((ROOT / "examples" / entry["file"]).read_text())
    path, method, operation = operations[case["operation_id"]]
    assert case["path_template"] == path and case["method"] == method
    response = case["response"]
    assert response["status"] == entry["status"]
    request = case.get("request", {})
    if path.startswith("/control/"):
        proxy = case["proxy_context"]
        assert proxy["ingress"] == "tailscale_serve"
        assert proxy["tailnet_access_permitted"] is True
        assert proxy["injected_headers"] == {}  # Dashboard fixtures need no user identity.
        assert "Tailscale-User-Login" not in request["headers"]
    if path.startswith("/control/") and method == "POST":
        assert request["headers"]["X-eRecipe-Control"] == "1"
        assert request["headers"]["Origin"] == "https://" + request["headers"]["Host"]
    elif path.startswith("/api/") and case["operation_id"] != "redeemEnrollment":
        assert request["headers"]["Authorization"].startswith("Bearer ")
    if "body" in request:
        schema = operation["requestBody"]["content"][request["content_type"]]["schema"]
        validator(schema).validate(request["body"])
    elif "requestBody" in operation:
        raise AssertionError(f"Missing required body in {entry['file']}")
    for parameter in operation["parameters"]:
        parameter = deref(parameter)
        if parameter["in"] == "header" and parameter.get("required"):
            validator(parameter["schema"]).validate(request["headers"][parameter["name"]])
    expected = deref(operation["responses"][str(response["status"])])
    validator(expected["content"]["application/json"]["schema"]).validate(response["body"])
    for name, value in response["headers"].items():
        if name in expected.get("headers", {}):
            validator(expected["headers"][name]["schema"]).validate(value)
    if "error" in response["body"]:
        assert response["body"]["request_id"] == response["headers"]["X-Request-ID"]
    if "enrollment_url" in response["body"]:
        link = urlsplit(response["body"]["enrollment_url"])
        assert (link.scheme, link.netloc, link.path, link.fragment) == ("erecipe", "enroll", "", "")
        query = parse_qs(link.query, strict_parsing=True)
        assert set(query) == {"v", "server", "code"}
        assert all(len(values) == 1 for values in query.values())
        assert query["v"] == ["1"]
        assert urlsplit(query["server"][0]).scheme == "https"
        validator(spec["components"]["schemas"]["RedeemEnrollment"]).validate({"code": query["code"][0]})
    count += 1

# Guard against accepting combinations that would mislead client state machines.
invalid = [
    ("RedeemEnrollment", {"code": "short"}),
    ("TextImport", {"kind": "text", "text": "x", "user_id": "spoof"}),
    ("EditRevision", {"markdown": "draft"}),
    ("PublishPage", {"render_set_id": "render", "page_number": 0}),
    ("RenderResult", {"status": "ready", "revision_id": "r", "job_id": None, "render_set_id": None}),
    ("RecipesPage", {"items": [], "next_cursor": None, "has_more": True}),
    ("RevisionAccepted", {"recipe_id": "r", "revision_id": "v", "lock_version": 2,
                          "render_job_id": None, "render_set_id": None}),
    ("ErrorDetail", {"code": "revision_conflict", "message": "Changed", "retryable": False,
                     "details": {}}),
]
for name, value in invalid:
    assert not validator({"$ref": "#/components/schemas/" + name}).is_valid(value), name

print(f"Valid OpenAPI 3.1: {len(spec['paths'])} paths, {len(operations)} operations, "
      f"{len(spec['components']['schemas'])} schemas; {count} exchanges and {len(invalid)} rejection checks passed.")
