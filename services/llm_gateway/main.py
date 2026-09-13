"""Лёгкий LLM gateway в духе LiteLLM: OpenAI-compatible, failover по base_url.

- без сторонних агрегаторов; только прямые API-ключи
- try/except по списку провайдеров, health-marking при 5xx/таймауте
- prompt caching: для OpenAI-совместимых провайдеров (DashScope и др.)
  кэширование префикса выполняется на стороне провайдера автоматически;
  x_cache_keys пробрасываются в заголовки для метрик.
"""
import os
import time

import httpx
import yaml
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()

CONFIG_PATH = os.environ.get("CONFIG_FILE", "/etc/caller/llm-gateway.yaml")
CFG: dict = {}
PROVIDERS: list = []
DOWN: dict = {}


@app.on_event("startup")
def _startup():
    global CFG, PROVIDERS
    with open(CONFIG_PATH, encoding="utf-8") as f:
        CFG = yaml.safe_load(f)
    PROVIDERS = CFG.get("providers", [])
    CFG["_down_seconds"] = CFG.get("failover", {}).get("health_mark_down_seconds", 60)


def _ok(name: str) -> bool:
    return DOWN.get(name, 0) < time.time()


def _down(name: str):
    DOWN[name] = time.time() + CFG.get("_down_seconds", 60)


@app.get("/health")
def health():
    return {
        "status": "ok",
        "providers": [
            {"name": p["name"], "available": _ok(p["name"])} for p in PROVIDERS
        ],
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    stream = bool(body.get("stream", False))
    last_err = None

    for prov in PROVIDERS:
        name = prov["name"]
        if not _ok(name):
            last_err = f"{name}: down (cooldown)"
            continue
        key = os.environ.get(prov.get("api_key_env", ""), "")
        if not key:
            _down(name)
            last_err = f"{name}: api key missing"
            continue

        url = prov["base_url"].rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        ck = body.get("x_cache_keys") or {}
        if ck.get("system_hash"):
            headers["X-Cache-System-Hash"] = ck["system_hash"]
        if ck.get("rag_hash"):
            headers["X-Cache-Rag-Hash"] = ck["rag_hash"]

        # модель маршрутизирует сам gateway по порядку списка провайдеров
        payload = {k: v for k, v in body.items() if not k.startswith("x_")}
        payload["model"] = prov.get("model", payload.get("model"))
        timeout = prov.get("timeout_seconds", 15)

        try:
            async with httpx.AsyncClient() as client:
                if not stream:
                    r = await client.post(url, json=payload, headers=headers, timeout=timeout)
                    if r.status_code >= 500:
                        last_err = f"{name}: http {r.status_code}"
                        _down(name)
                        continue
                    r.raise_for_status()
                    data = r.json()
                    data["x_provider"] = name
                    return JSONResponse(data)
                req = client.build_request(
                    "POST", url, json=payload, headers=headers,
                    timeout=httpx.Timeout(timeout, read=120),
                )
                r = await client.send(req, stream=True)
                if r.status_code >= 500:
                    await r.aclose()
                    last_err = f"{name}: http {r.status_code}"
                    _down(name)
                    continue
                r.raise_for_status()
                return StreamingResponse(
                    _stream(r),
                    media_type="text/event-stream",
                    headers={
                        "X-Provider": name,
                        "X-Model": prov.get("model", ""),
                        "Cache-Control": "no-cache",
                    },
                )
        except (httpx.HTTPStatusError, httpx.TimeoutException,
                httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError) as e:
            last_err = f"{name}: {type(e).__name__}"
            _down(name)
            continue

    return JSONResponse(
        {"error": {"message": f"all providers failed: {last_err}", "type": "gateway_error"}},
        status_code=502,
    )


async def _stream(response):
    try:
        async for line in response.aiter_lines():
            if line:
                yield f"{line}\n"
    except Exception:  # noqa: BLE001 — mid-stream ошибка провайдера: просто закрываем
        pass
    finally:
        await response.aclose()
