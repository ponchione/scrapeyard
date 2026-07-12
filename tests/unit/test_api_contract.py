from __future__ import annotations

from scrapeyard.main import app


def _schema_for(response: dict) -> dict:
    return response["content"]["application/json"]["schema"]


def test_openapi_has_concrete_success_and_error_contracts_for_every_operation():
    schema = app.openapi()
    for path, path_item in schema["paths"].items():
        for method, operation in path_item.items():
            if method not in {"get", "post", "put", "delete", "patch"}:
                continue
            success_responses = [
                response
                for status, response in operation["responses"].items()
                if 200 <= int(status) < 300
            ]
            assert success_responses, f"{method.upper()} {path} has no success response"
            for response in success_responses:
                if response.get("description") == "Successful Response" and not response.get(
                    "content"
                ):
                    # 204 endpoints intentionally have no entity schema.
                    continue
                assert _schema_for(response), f"{method.upper()} {path} has an empty schema"

            if path.startswith("/health"):
                continue
            for status in (
                400,
                401,
                403,
                404,
                405,
                409,
                413,
                415,
                422,
                429,
                500,
                503,
                504,
            ):
                response = operation["responses"][str(status)]
                assert _schema_for(response) == {
                    "$ref": "#/components/schemas/ErrorEnvelope"
                }


def test_openapi_documents_conditional_results_and_pagination_headers():
    schema = app.openapi()
    scrape = schema["paths"]["/scrape"]["post"]["responses"]
    results = schema["paths"]["/results/{job_id}"]["get"]["responses"]
    assert _schema_for(scrape["200"])
    assert _schema_for(scrape["202"]) == {
        "$ref": "#/components/schemas/QueuedSubmissionResponse"
    }
    assert _schema_for(results["200"])
    assert _schema_for(results["202"]) == {
        "$ref": "#/components/schemas/QueuedSubmissionResponse"
    }

    for path in ("/jobs", "/errors"):
        headers = schema["paths"][path]["get"]["responses"]["200"]["headers"]
        assert set(headers) == {
            "X-Scrapeyard-Limit",
            "X-Scrapeyard-Offset",
            "X-Scrapeyard-Item-Count",
            "X-Scrapeyard-Has-More",
            "X-Scrapeyard-Next-Offset",
        }


def test_openapi_models_exclude_secret_and_local_storage_fields():
    schemas = app.openapi()["components"]["schemas"]
    public_properties = {
        property_name
        for model in schemas.values()
        for property_name in model.get("properties", {})
    }
    assert "file_path" not in public_properties
    assert "proxy_url" not in public_properties
    assert "webhook_headers" not in public_properties
    assert "api_key" not in public_properties
