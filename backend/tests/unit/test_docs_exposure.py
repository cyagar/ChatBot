from app.main import interactive_docs_urls


def test_interactive_docs_are_served_in_development():
    urls = interactive_docs_urls("development")
    assert urls == {"docs_url": "/docs", "redoc_url": "/redoc", "openapi_url": "/openapi.json"}


def test_interactive_docs_are_disabled_everywhere_else():
    for env in ("production", "staging", "test", ""):
        assert interactive_docs_urls(env) == {"docs_url": None, "redoc_url": None, "openapi_url": None}


def test_a_production_app_serves_no_docs_routes():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI(**interactive_docs_urls("production"))
    client = TestClient(app)
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404
    assert app.openapi()["info"]["title"], "programmatic schema export must still work"
