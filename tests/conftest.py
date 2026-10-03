import pytest
from starlette.testclient import TestClient

from fact_manager.catalog import Catalog
from fact_manager.server import make_app


@pytest.fixture
def catalog(tmp_path):
    return Catalog(
        {
            "state_dir": str(tmp_path),
            "llm_model": "deepseek-flash",
            "llm_url": "https://api.deepseek.com",
            "ragflow_url": "http://ragflow",
            "public_host": "facts.example.test",
        }
    )


@pytest.fixture
def web(catalog, monkeypatch):
    monkeypatch.setenv("FACT_ADMIN_TOKEN", "a" * 64)
    with TestClient(
        make_app(catalog, catalog.config), base_url="https://facts.example.test"
    ) as client:
        response = client.post("/api/login", json={"token": "a" * 64})
        assert response.status_code == 200
        client.headers["X-Fact-CSRF"] = response.json()["csrf"]
        yield client
