"""Orchestrator: stateful-ядро звонка. STT-события -> RAG -> LLM -> TTS + эскалации."""
import asyncio
import hashlib
import json
import logging
import os
import time
import uuid

import httpx
import yaml
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from context_window import ContextWindow
from policies import (
    build_system_prompt,
    is_policy_dispute,
    is_simple_explain_request,
    is_substantive_question,
    pick_filler,
)
from validator import ArticleValidator

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("orchestrator")

app = FastAPI()

CONFIG_PATH = os.environ.get("CONFIG_FILE", "/etc/caller/orchestrator.yaml")
CFG: dict = {}
HTTP: httpx.AsyncClient = None
VALIDATOR: ArticleValidator = None
CTX: ContextWindow = None

SESSIONS: dict = {}
ORCH_WS = None
ORCH_LOCK = asyncio.Lock()

OUTCOME_MAP = {
    "stt_low_confidence": "escalated_stt",
    "rag_no_match": "escalated_rag",
    "llm_low_confidence": "escalated_llm",
    "llm_unavailable": "escalated_llm",
    "citation_invalid": "escalated_citation",
    "clarification_limit": "escalated_clarification",
    "policy_dispute": "escalated_policy",
}


class LlmAnswer(BaseModel):
    answer_text: str
    articles_cited: list = []
    confidence: float = 0.0
    escalation_suggested: bool = False
    clarification_question: str | None = None


class CallSession:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.call_id = str(uuid.uuid4())
        self.lang = "ru"
        self.elder_mode = False
        self.low_conf_streak = 0
        self.clarify_attempts = 0
        self.simple_counts: dict = {}
        self.history: list = []
        self.summary: str | None = None
        self.rag_block: str | None = None
        self.active_topic_query: str | None = None
        self.escalated = False
        self.escalation_reason: str | None = None
        self.started_at = time.time()
        self.turns_count = 0
        self.lock = asyncio.Lock()


# ---------- startup / health ----------


@app.on_event("startup")
async def _startup():
    global CFG, HTTP, VALIDATOR, CTX
    with open(CONFIG_PATH, encoding="utf-8") as f:
        CFG = yaml.safe_load(f)
    HTTP = httpx.AsyncClient(timeout=httpx.Timeout(60))
    VALIDATOR = ArticleValidator(CFG)
    CTX = ContextWindow(CFG)
    asyncio.get_running_loop().create_task(_refresh_articles_loop())


async def _refresh_articles_loop():
    while True:
        try:
            await VALIDATOR.refresh()
        except Exception as e:  # noqa: BLE001
            log.warning("articles refresh failed: %s", e)
        await asyncio.sleep(600)


@app.get("/health")
def health():
    return {"status": "ok", "sessions": len(SESSIONS)}


# ---------- WS hub ----------


@app.websocket("/ws/media")
async def ws_media(ws: WebSocket):
    global ORCH_WS
    await ws.accept()
    ORCH_WS = ws
    log.info("media-gateway connected")
    try:
        async for msg in ws.iter_text():
            ev = json.loads(msg)
            sid = ev.get("session_id", "")
            session = SESSIONS.setdefault(sid, CallSession(sid))
            handler = {
                "call_started": _on_call_started,
                "call_ended": _on_call_ended,
                "utterance_final": _on_utterance,
                "dtmf": _on_dtmf,
                "gateway_reconnected": None,
            }.get(ev.get("event", ""))
            if handler:
                asyncio.get_running_loop().create_task(handler(session, ev))
    except WebSocketDisconnect:
        pass
    finally:
        ORCH_WS = None
        log.info("media-gateway disconnected")


async def send_command(command: str, session_id: str, data: dict | None = None):
    if ORCH_WS is None or ORCH_WS.closed:
        log.warning("no media WS — dropping command %s", command)
        return
    async with ORCH_LOCK:
        try:
            await ORCH_WS.send_json(
                {"command": command, "session_id": session_id, "data": data}
            )
        except Exception as e:  # noqa: BLE001
            log.warning("send command failed: %s", e)


async def speak(session, text: str, lang: str, interrupt: bool = True, speed: float | None = None):
    data = {"text": text, "lang": lang, "interrupt": interrupt}
    if speed is not None:
        data["speed"] = speed
    elif session.elder_mode:
        data["speed"] = CFG["elder_mode"]["tts_speed"]
    await send_command("speak", session.session_id, data)


async def audit_event(event_type: str, payload: dict):
    try:
        await HTTP.post(
            f"{CFG['services']['audit_worker'].rstrip('/')}/ingest",
            json={"event_type": event_type, "payload": payload},
            timeout=httpx.Timeout(5),
        )
    except Exception as e:  # noqa: BLE001
        log.warning("audit ingest failed: %s", e)


# ---------- события ----------


async def _on_call_started(session, ev):
    await audit_event(
        "call_started",
        {
            "call_id": session.call_id,
            "caller": ev.get("payload", {}).get("caller"),
            "language": "unknown",
        },
    )


async def _on_dtmf(session, ev):
    log.info("dtmf %s on %s", ev.get("payload", {}).get("digit"), session.session_id)


async def _on_call_ended(session, ev):
    outcome = "completed"
    if session.escalated:
        outcome = OUTCOME_MAP.get(session.escalation_reason, "abandoned")
    elif ev.get("payload", {}).get("reason") in ("ws_closed", "hangup"):
        outcome = "abandoned"
    await audit_event(
        "call_ended",
        {
            "call_id": session.call_id,
            "outcome": outcome,
            "duration_s": int(time.time() - session.started_at),
            "turns_count": session.turns_count,
            "elder_mode": session.elder_mode,
        },
    )
    SESSIONS.pop(session.session_id, None)
    log.info("call ended: %s outcome=%s", session.call_id, outcome)


# ---------- основной поток ----------


async def _on_utterance(session, ev):
    if session.escalated:
        return
    async with session.lock:
        if session.escalated:
            return
        stt = ev.get("payload", {}) or {}
        text = (stt.get("text") or "").strip()
        lang = stt.get("language", "unknown")
        conf = float(stt.get("confidence", 0.0))
        rag = None
        if lang in ("ru", "kk"):
            session.lang = lang

        # elder-режим: N последовательных низкоуверенных STT
        ae = CFG["elder_mode"]["auto_enable"]
        if conf < ae["low_confidence_threshold"]:
            session.low_conf_streak += 1
        else:
            session.low_conf_streak = 0
        if session.low_conf_streak >= ae["consecutive_low_confidence"]:
            session.elder_mode = True
            log.info("elder mode ON for %s", session.call_id)

        # 1. STT-уверенность: уточняющий вопрос ДО RAG
        if not text or conf < CFG["stt"]["min_confidence"]:
            session.clarify_attempts += 1
            limit = (
                CFG["clarification"]["max_attempts_elder"]
                if session.elder_mode
                else CFG["clarification"]["max_attempts_default"]
            )
            if session.clarify_attempts >= limit:
                await _escalate(session, "stt_low_confidence")
                return
            if text:
                phrase = f"Правильно ли я понял: «{text}»?"
            else:
                phrase = (
                    "Простите, я вас не расслышал. Повторите, пожалуйста, ваш вопрос."
                )
            await speak(session, phrase, session.lang)
            await _audit_turn(session, stt, None, None, None, None, None, None)
            return
        session.clarify_attempts = 0

        # 2. Споры/доначисления — детерминированный фильтр до RAG/LLM
        if is_policy_dispute(
            text, CFG["escalation"]["policy_dispute_keywords"]
        ):
            await speak(
                session,
                "Этот вопрос требует уточнения специалиста. Соединяю вас с оператором.",
                session.lang,
            )
            await _escalate(session, "policy_dispute")
            return

        # 3. «Объясни проще» — счётчик на вопрос
        if is_simple_explain_request(text) and session.rag_block is not None:
            qid = hashlib.sha1(text.lower().encode()).hexdigest()[:12]
            session.simple_counts[qid] = session.simple_counts.get(qid, 0) + 1
            if (
                session.simple_counts[qid]
                > CFG["simple_explain"]["max_per_question"]
            ):
                await _escalate(session, "clarification_limit")
                return
            ans, meta = await _llm_answer(session, text, simplify=True)
            if ans is None:
                await _escalate(session, "llm_unavailable")
                return
            await speak(session, ans.answer_text, session.lang)
            session.turns_count += 1
            await _audit_turn(
                session, stt, None, ans, meta, None, None, None
            )
            return

        # 4. Существенный вопрос -> RAG (per spec: на каждый запрос, без стат. кэша)
        if is_substantive_question(text) or session.rag_block is None:
            await speak(session, pick_filler(session.lang, CFG), session.lang, interrupt=False)
            rag = await _rag_retrieve(session, text, lang)
            if rag is None:
                await _escalate(session, "rag_no_match")
                return
            if not rag.get("matched"):
                await _escalate(session, "rag_no_match")
                return
            session.rag_block = _render_rag_block(rag["results"])
            session.active_topic_query = text

        # 5. LLM
        ans, meta = await _llm_answer(session, text)
        if ans is None:
            await _escalate(session, "llm_unavailable")
            return
        if ans.clarification_question:
            await speak(session, ans.clarification_question, session.lang)
            session.turns_count += 1
            await _audit_turn(session, stt, rag.get("results"), ans, meta, None, None, None)
            return
        if ans.escalation_suggested or ans.confidence < CFG["llm"]["min_confidence"]:
            await _escalate(session, "llm_low_confidence")
            return

        # 6. Валидация цитат
        cited = ans.articles_cited or VALIDATOR.extract(ans.answer_text)
        ok, missing = VALIDATOR.validate(cited)
        if CFG["citation"]["validate"] and not ok:
            for _ in range(CFG["citation"]["retry_on_invalid"]):
                log.info("invalid citations %s — retry", missing)
                ans2, meta2 = await _llm_answer(session, text, fix_citations=cited)
                if ans2 is None:
                    await _escalate(session, "llm_unavailable")
                    return
                cited2 = ans2.articles_cited or VALIDATOR.extract(ans2.answer_text)
                ok2, _ = VALIDATOR.validate(cited2)
                ans, meta = ans2, meta2
                if ok2:
                    break
            if not ok2:
                await _escalate(session, "citation_invalid")
                return

        # 7. Ответ абоненту
        await speak(session, ans.answer_text, session.lang)
        session.history.append({"role": "user", "content": text})
        session.history.append({"role": "assistant", "content": ans.answer_text})
        session.turns_count += 1

        # 8. Контекст-окно + аудит
        sys_text = build_system_prompt(session.lang, session.elder_mode, False, None)
        if CTX.should_compress(
            sys_text, session.rag_block or "", session.summary or "", session.history
        ):
            await _compress_history(session)
        await _audit_turn(
            session, stt, rag.get("results") if rag else None,
            ans, meta, cited, ok, missing,
        )


# ---------- RAG / LLM / контекст ----------


async def _rag_retrieve(session, query, lang):
    body = {
        "session_id": session.session_id,
        "query": query,
        "language": lang if lang in ("ru", "kk") else "ru",
        "top_n_vector": CFG["rag"]["top_n_vector"],
        "top_n_final": CFG["rag"]["top_n_final"],
        "exclude_articles": [],
    }
    try:
        r = await HTTP.post(
            f"{CFG['services']['rag'].rstrip('/')}/retrieval", json=body
        )
        if r.status_code >= 500:
            log.error("rag http %s", r.status_code)
            return None
        return r.json()
    except Exception as e:  # noqa: BLE001
        log.error("rag error: %s", e)
        return None


def _render_rag_block(results: list) -> str:
    parts = []
    for r in results or []:
        line = (
            f"Статья {r['article_number']}"
            + (f" ({r['chapter_path']})" if r.get("chapter_path") else "")
            + (f". {r['article_title']}." if r.get("article_title") else "")
            + f"\n{r['text']}"
        )
        if r.get("cross_references"):
            line += f"\nСвязанные статьи: {', '.join(r['cross_references'])}"
        parts.append(line)
    return "\n\n---\n\n".join(parts)


async def _llm_answer(session, text, simplify=False, fix_citations=None):
    sys_text = build_system_prompt(
        session.lang, session.elder_mode, simplify, fix_citations
    )
    messages = [{"role": "system", "content": sys_text}]
    if session.rag_block:
        messages.append(
            {"role": "system", "content": f"Доступные статьи (RAG):\n{session.rag_block}"}
        )
    if session.summary:
        messages.append(
            {
                "role": "system",
                "content": "Краткое содержание предыдущего диалога:\n" + session.summary,
            }
        )
    history_budget = (
        CTX.max_tokens
        - CTX.reserve
        - CTX.est_tokens(sys_text)
        - CTX.est_tokens(session.rag_block or "")
        - CTX.est_tokens(session.summary or "")
    )
    messages.extend(CTX.build_history(session.history, history_budget))
    messages.append({"role": "user", "content": text})

    body = {
        "model": CFG["llm"].get("model_alias", "caller"),
        "messages": messages,
        "stream": False,
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
        "x_session_id": session.call_id,
        "x_cache_keys": {
            "system_hash": hashlib.sha256(sys_text.encode()).hexdigest()[:16],
            "rag_hash": hashlib.sha256(
                (session.rag_block or "").encode()
            ).hexdigest()[:16],
        },
    }
    t0 = time.time()
    try:
        r = await HTTP.post(
            f"{CFG['services']['llm_gateway'].rstrip('/')}/v1/chat/completions",
            json=body,
        )
        if r.status_code == 502:
            return None, None
        r.raise_for_status()
        data = r.json()
        content = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})
        ans = LlmAnswer.model_validate_json(content)
        meta = {
            "llm_model": data.get("x_model", ""),
            "llm_provider": data.get("x_provider", ""),
            "llm_in_tokens": usage.get("prompt_tokens"),
            "llm_out_tokens": usage.get("completion_tokens"),
            "llm_latency_ms": int((time.time() - t0) * 1000),
        }
        return ans, meta
    except Exception as e:  # noqa: BLE001
        log.warning("LLM error: %s", e)
        return None, None


async def _compress_history(session):
    keep = 2 * CTX.keep_last
    old = session.history[:-keep] if len(session.history) > keep else []
    lines = [f"{m['role']}: {m['content']}" for m in old]
    if session.summary:
        lines.insert(0, "Текущее резюме: " + session.summary)
    if not lines:
        return
    body = {
        "model": CFG["llm"].get("model_alias", "caller"),
        "messages": [
            {
                "role": "user",
                "content": (
                    "Сжми диалог в резюме (до 150 слов), сохрани: темы, упомянутые "
                    "статьи, данные абонента. Только резюме.\n\n" + "\n".join(lines)
                ),
            }
        ],
        "stream": False,
        "temperature": 0,
        "max_tokens": 300,
    }
    try:
        r = await HTTP.post(
            f"{CFG['services']['llm_gateway'].rstrip('/')}/v1/chat/completions",
            json=body,
        )
        r.raise_for_status()
        session.summary = r.json()["choices"][0]["message"]["content"].strip()
        session.history = session.history[-keep:]
    except Exception as e:  # noqa: BLE001 — F10: отбрасываем старые туры
        log.warning("history compression failed, truncating: %s", e)
        session.history = session.history[-keep:]


# ---------- эскалация / аудит ----------


async def _escalate(session, reason: str):
    session.escalated = True
    session.escalation_reason = reason
    esc = CFG["escalation"]
    await speak(
        session, esc["announce_phrase"], session.lang, interrupt=True
    )
    await send_command(
        "transfer",
        session.session_id,
        {"reason": reason, "queue": esc["queue"], "announce": esc["announce_phrase"]},
    )
    await audit_event(
        "escalation",
        {
            "call_id": session.call_id,
            "reason": reason,
            "queue": esc["queue"],
        },
    )
    log.info("escalated call %s: %s", session.call_id, reason)


async def _audit_turn(session, stt, rag_results, ans, meta, cited, citation_ok, missing):
    await audit_event(
        "turn",
        {
            "call_id": session.call_id,
            "turn_n": session.turns_count,
            "stt_text": stt.get("text"),
            "stt_confidence": stt.get("confidence"),
            "stt_lang": stt.get("language"),
            "stt_duration_ms": stt.get("duration_ms"),
            "rag_query": session.active_topic_query,
            "rag_results": rag_results,
            "rag_matched": None if rag_results is None else (rag_results is not None),
            "llm_model": (meta or {}).get("llm_model"),
            "llm_provider": (meta or {}).get("llm_provider"),
            "llm_in_tokens": (meta or {}).get("llm_in_tokens"),
            "llm_out_tokens": (meta or {}).get("llm_out_tokens"),
            "llm_latency_ms": (meta or {}).get("llm_latency_ms"),
            "llm_confidence": ans.confidence if ans else None,
            "llm_answer": ans.answer_text if ans else None,
            "articles_cited": cited,
            "citation_valid": citation_ok,
            "escalated": session.escalated,
            "escalation_reason": session.escalation_reason,
        },
    )
