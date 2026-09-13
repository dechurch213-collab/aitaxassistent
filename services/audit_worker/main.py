"""Audit worker: POST /ingest -> asyncpg -> Postgres. Асинхронно, не блокирует голосовой контур.

События: call_started, call_ended, turn, escalation, csat
При 3 неудачах записи -> audit_dlq.
"""
import asyncio
import json
import os

import asyncpg
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI()

DB_URL = os.environ.get("AUDIT_DB_URL", "")
pool = None
queue: asyncio.Queue = None


class IngestEvent(BaseModel):
    event_type: str
    payload: dict


def _mask_caller(number: str | None) -> str | None:
    if not number or len(number) < 3:
        return number
    return number[:2] + "***"


def _prepare(ev_type: str, p: dict) -> tuple:
    """Возвращает (sql, params) для события."""
    if ev_type == "call_started":
        return (
            "INSERT INTO calls (id, started_at, caller_masked, language) VALUES ($1, now(), $2, $3)",
            [p["call_id"], _mask_caller(p.get("caller")), p.get("language", "unknown")],
        )
    if ev_type == "call_ended":
        return (
            """UPDATE calls SET ended_at=now(), outcome=$2, duration_s=$3,
               turns_count=$4, elder_mode=$5 WHERE id=$1""",
            [p["call_id"], p.get("outcome", "completed"), p.get("duration_s"),
             p.get("turns_count", 0), p.get("elder_mode", False)],
        )
    if ev_type == "turn":
        return (
            """INSERT INTO turns (call_id, turn_n, stt_text, stt_confidence, stt_lang,
                  stt_duration_ms, rag_query, rag_results, rag_matched, rag_meta,
                  llm_model, llm_provider, llm_in_tokens, llm_out_tokens, llm_latency_ms,
                  llm_confidence, llm_answer, articles_cited, citation_valid, llm_retries,
                  tts_voice, tts_duration_ms, escalated, escalation_reason)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22,$23,$24)
               ON CONFLICT (call_id, turn_n) DO NOTHING""",
            [p["call_id"], p["turn_n"], p.get("stt_text"), p.get("stt_confidence"),
             p.get("stt_lang"), p.get("stt_duration_ms"), p.get("rag_query"),
             json.dumps(p.get("rag_results")), p.get("rag_matched"),
             json.dumps(p.get("rag_meta")), p.get("llm_model"), p.get("llm_provider"),
             p.get("llm_in_tokens"), p.get("llm_out_tokens"), p.get("llm_latency_ms"),
             p.get("llm_confidence"), p.get("llm_answer"),
             json.dumps(p.get("articles_cited")), p.get("citation_valid"),
             p.get("llm_retries", 0), p.get("tts_voice"), p.get("tts_duration_ms"),
             p.get("escalated", False), p.get("escalation_reason")],
        )
    if ev_type == "escalation":
        return (
            """INSERT INTO escalations (call_id, turn_id, reason, queue)
               VALUES ($1,$2,$3,$4)""",
            [p["call_id"], p.get("turn_id"), p["reason"], p.get("queue", "OPERATOR_QUEUE")],
        )
    if ev_type == "csat":
        return (
            "UPDATE calls SET csat_score=$2, csat_comment=$3 WHERE id=$1",
            [p["call_id"], p.get("score"), p.get("comment")],
        )
    raise ValueError(f"unknown event_type: {ev_type}")


async def _worker():
    retries = {}
    while True:
        ev = await queue.get()
        ev_type, p = ev
        try:
            sql, params = _prepare(ev_type, p)
            async with pool.acquire() as conn:
                await conn.execute(sql, *params)
            retries.pop(ev, None)
        except Exception as e:  # noqa: BLE001
            n = retries.get(ev, 0) + 1
            retries[ev] = n
            if n >= 3:
                retries.pop(ev, None)
                try:
                    async with pool.acquire() as conn:
                        await conn.execute(
                            "INSERT INTO audit_dlq (event_type, payload, error) VALUES ($1,$2,$3)",
                            ev_type, json.dumps(p), str(e),
                        )
                except Exception:  # noqa: BLE001
                    pass
            else:
                queue.put_nowait(ev)


@app.on_event("startup")
def _startup():
    pass


@app.on_event("startup")
async def _startup_async():
    global pool, queue
    pool = await asyncpg.create_pool(DB_URL, min_size=1, max_size=4)
    queue = asyncio.Queue()
    asyncio.get_event_loop().create_task(_worker())


@app.post("/ingest")
async def ingest(ev: IngestEvent):
    try:
        _prepare(ev.event_type, ev.payload)  # валидация формы
    except ValueError as e:
        raise HTTPException(400, str(e))
    queue.put_nowait((ev.event_type, ev.payload))
    return {"status": "queued"}


@app.get("/health")
def health():
    return {"status": "ok", "db": pool is not None, "queue_size": queue.qsize() if queue else -1}
