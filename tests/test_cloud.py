"""Cloud hand-off tests. None of these spend money: the client is faked, or the key is fake."""
from types import SimpleNamespace as NS

import anthropic
import httpx

import pocket_agent as pa


def fake_client(response=None, error=None, seen=None):
    class Messages:
        def create(self, **kw):
            if seen is not None:
                seen.update(kw)
            if error:
                raise error
            return response

    return lambda **kw: NS(messages=Messages())


def test_offline_falls_back_to_local(monkeypatch):
    monkeypatch.setattr(pa, "online", lambda *a, **k: False)
    out = pa.ask_big_brain("plan a trip")
    assert out.startswith("CLOUD UNREACHABLE") and "best-effort" in out


def test_request_uses_env_model_and_records_usage(monkeypatch):
    seen = {}
    resp = NS(content=[NS(type="thinking", thinking=""), NS(type="text", text="Day 1: temple")],
              usage=NS(input_tokens=30, output_tokens=12), stop_reason="end_turn")
    monkeypatch.setattr(pa, "online", lambda *a, **k: True)
    monkeypatch.setattr(pa, "CLOUD_MODEL", "claude-haiku-5-5")
    monkeypatch.setattr(anthropic, "Anthropic", fake_client(resp, seen=seen))
    pa._cloud_usage.clear()
    assert pa.ask_big_brain("plan a trip") == "Day 1: temple"
    assert seen["model"] == "claude-haiku-5-5"
    assert seen["output_config"] == {"effort": "low"}
    assert "temperature" not in seen  # rejected by current API versions
    usage = pa._cloud_usage.pop()
    assert (usage["who"], usage["tokens_in"], usage["tokens_out"]) == ("cloud", 30, 12)


def test_refusal_falls_back(monkeypatch):
    resp = NS(content=[], usage=NS(input_tokens=5, output_tokens=0), stop_reason="refusal")
    monkeypatch.setattr(pa, "online", lambda *a, **k: True)
    monkeypatch.setattr(anthropic, "Anthropic", fake_client(resp))
    assert pa.ask_big_brain("x").startswith("CLOUD DECLINED")
    pa._cloud_usage.clear()


def test_auth_error_falls_back(monkeypatch):
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    err = anthropic.AuthenticationError("bad key", response=httpx.Response(401, request=req), body=None)
    monkeypatch.setattr(pa, "online", lambda *a, **k: True)
    monkeypatch.setattr(anthropic, "Anthropic", fake_client(error=err))
    assert pa.ask_big_brain("x").startswith("CLOUD UNAVAILABLE: the API key was rejected")


def test_connection_error_falls_back(monkeypatch):
    err = anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))
    monkeypatch.setattr(pa, "online", lambda *a, **k: True)
    monkeypatch.setattr(anthropic, "Anthropic", fake_client(error=err))
    assert pa.ask_big_brain("x").startswith("CLOUD UNREACHABLE")
