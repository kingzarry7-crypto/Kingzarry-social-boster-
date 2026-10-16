import os
import tempfile

_db = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
os.environ["DATABASE_PATH"] = _db.name
os.environ["APP_SECRET"] = "test-secret-that-is-long-enough-for-ci"

from fastapi.testclient import TestClient
from app import app

client = TestClient(app)

def test_health_and_platform_status():
    assert client.get("/health").json()["status"] == "ok"
    status = client.get("/api/status").json()
    assert "Instagram" in status["platforms"]
    assert status["features"]["publishing"] is False

def test_dashboard_requires_login():
    client.post("/api/auth/logout")
    assert client.get("/api/dashboard").status_code == 401

def test_register_generate_and_save_content():
    email = "test-social-booster@example.com"
    response = client.post("/api/auth/register", json={"email": email, "password": "StrongPassword123"})
    assert response.status_code == 200
    generated = client.post("/api/ai/generate", json={"platform":"Instagram","kind":"caption","topic":"healthy meal prep"})
    assert generated.status_code == 200
    assert generated.json()["content"]
    saved = client.post("/api/content", json={"platform":"Instagram","kind":"caption","topic":"healthy meal prep","body":generated.json()["content"]})
    assert saved.status_code == 200
    assert saved.json()["status"] == "draft"
    assert len(client.get("/api/content").json()["items"]) == 1

def test_campaign_creation():
    response = client.post("/api/campaigns", json={"name":"First campaign","platform":"YouTube","goal":"increase tutorial watch time","audience":"new creators"})
    assert response.status_code == 200
    assert response.json()["platform"] == "YouTube"
