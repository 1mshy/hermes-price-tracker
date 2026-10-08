"""Ensure the installed watchlist and assets coexist with the API and MCP."""
from fastapi.testclient import TestClient

from pricewatch.main import app


def test_watchlist_routes_and_bundled_assets():
    client = TestClient(app)
    for path in ("/", "/watchlist"):
        response = client.get(path)
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert 'id="add-form"' in response.text
    for asset, kind in (("watchlist.js", "javascript"), ("watchlist.css", "text/css")):
        response = client.get(f"/assets/{asset}")
        assert response.status_code == 200
        assert kind in response.headers["content-type"]
    assert client.get("/api/health").status_code == 200
    assert client.get("/assets/../settings.py").status_code == 404
