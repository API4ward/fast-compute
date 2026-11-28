"""Homepage product advisor (/sales/chat), access requests (/sales/lead) and the homepage."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import auth, make_user
from dockerserver.api import create_app
from dockerserver.config import Settings
from dockerserver.sales import SYSTEM_PROMPT, clean_messages, SalesError, faq_answer


def ask(client, text, **extra):
    return client.post("/sales/chat", json={"messages": [{"role": "user", "content": text}], **extra})


def test_homepage_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "home.js?v=" in r.text and "home.css?v=" in r.text
    js = client.get("/static/home.js").text
    assert "/sales/chat" in js and "AWS Lambda" in js
    assert client.get("/sales/info").json()["mode"] == "faq"


def test_faq_mode_without_llm(client):
    r = ask(client, "How much does it cost?")
    assert r.status_code == 200
    assert r.json()["mode"] == "faq" and "credit-hour" in r.json()["reply"]
    zh = ask(client, "支持 AWS Lambda 吗？").json()["reply"]
    assert "Lambda" in zh and "开发中" in zh
    assert faq_answer("hello") and faq_answer("你好", "zh") != faq_answer("hello")


def test_bad_requests(client):
    assert client.post("/sales/chat", json={"messages": []}).status_code == 400
    assert client.post("/sales/chat", json={"messages": [{"role": "system", "content": "x"}]}).status_code == 400
    assert client.post("/sales/chat", content=b"not json",
                       headers={"Content-Type": "application/json"}).status_code == 400
    with pytest.raises(SalesError):
        clean_messages([{"role": "assistant", "content": "hi"}])
    long = clean_messages([{"role": "user", "content": "x" * 10000}])
    assert len(long[0]["content"]) == 1500


def test_rate_limit(tmp_path):
    s = Settings(data_dir=tmp_path)
    s.logs_dir.mkdir(parents=True, exist_ok=True)
    s.sales_rate_limit = 2
    with TestClient(create_app(s, recover=False, enable_mcp=False)) as c:
        assert ask(c, "a").status_code == 200
        assert ask(c, "b").status_code == 200
        assert ask(c, "c").status_code == 429


def _llm_app(tmp_path, handler):
    s = Settings(data_dir=tmp_path)
    s.logs_dir.mkdir(parents=True, exist_ok=True)
    s.sales_api_key, s.sales_model, s.sales_base_url = "sk-test", "test-model", "https://llm.example/v1"
    app = create_app(s, recover=False, enable_mcp=False)
    app.state.sales._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return app


def test_llm_mode(tmp_path):
    seen = []

    def handler(req: httpx.Request):
        body = json.loads(req.content)
        seen.append((str(req.url), req.headers["authorization"], body))
        return httpx.Response(200, json={"choices": [{"message": {"content": "  LLM says hi  "}}]})

    with TestClient(_llm_app(tmp_path, handler)) as c:
        assert c.get("/sales/info").json()["mode"] == "llm"
        r = c.post("/sales/chat", json={"messages": [
            {"role": "assistant", "content": "Hello!"},       # leading greeting is dropped
            {"role": "user", "content": "What is it?"},
            {"role": "assistant", "content": "A platform."},
            {"role": "user", "content": "Price?"}]})
        assert r.json() == {"reply": "LLM says hi", "mode": "llm"}
    url, authz, body = seen[0]
    assert url == "https://llm.example/v1/chat/completions" and authz == "Bearer sk-test"
    assert body["model"] == "test-model" and body["messages"][0] == {"role": "system", "content": SYSTEM_PROMPT}
    assert [m["role"] for m in body["messages"][1:]] == ["user", "assistant", "user"]


def test_llm_max_completion_tokens_retry_and_failure_fallback(tmp_path):
    calls = []

    def handler(req):
        body = json.loads(req.content)
        calls.append(body)
        if "max_tokens" in body:
            return httpx.Response(400, json={"error": {"message": "Use 'max_completion_tokens' instead."}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    with TestClient(_llm_app(tmp_path, handler)) as c:
        assert ask(c, "hi").json()["reply"] == "ok"
    assert "max_completion_tokens" in calls[1]

    def broken(req):
        return httpx.Response(500, text="upstream down")

    with TestClient(_llm_app(tmp_path / "b", broken)) as c:
        r = ask(c, "pricing?")
        assert r.status_code == 200 and r.json()["mode"] == "faq"


def test_leads(client, admin_token):
    assert client.post("/sales/lead", json={"name": "Ann"}).status_code == 400
    r = client.post("/sales/lead", json={"name": "Ann", "contact": "ann@example.com",
                                         "message": "run simulations", "lang": "en"})
    assert r.status_code == 201
    assert client.get("/admin/leads").status_code == 401
    u = make_user(client, admin_token, "bob")
    tok = client.post(f"/admin/users/{u['id']}/tokens", json={}, headers=auth(admin_token)).json()["token"]
    assert client.get("/admin/leads", headers=auth(tok)).status_code == 403
    leads = client.get("/admin/leads", headers=auth(admin_token)).json()
    assert leads[0]["contact"] == "ann@example.com" and leads[0]["message"] == "run simulations"
    for _ in range(5):
        client.post("/sales/lead", json={"contact": "x"})
    assert client.post("/sales/lead", json={"contact": "x"}).status_code == 429
