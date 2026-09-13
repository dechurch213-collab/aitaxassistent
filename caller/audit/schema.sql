-- Аудит и метрики голосового AI-ассистента НК РК
-- БД: audit-db (PostgreSQL 16)
-- Запись асинхронная через audit-worker; голосовой контур не блокируется.

CREATE TYPE call_outcome AS ENUM (
    'completed',
    'escalated_stt',
    'escalated_rag',
    'escalated_llm',
    'escalated_policy',
    'escalated_citation',
    'escalated_clarification',
    'abandoned'
);

CREATE TYPE escalation_reason AS ENUM (
    'stt_low_confidence',
    'rag_no_match',
    'llm_low_confidence',
    'llm_unavailable',
    'citation_invalid',
    'clarification_limit',
    'policy_dispute'
);

-- Звонки
CREATE TABLE calls (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at            TIMESTAMPTZ,
    caller_masked       TEXT,                -- 7***, маскируется при записи
    language            TEXT CHECK (language IN ('ru','kk','mixed','unknown')),
    outcome             call_outcome NOT NULL DEFAULT 'completed',
    duration_s          INTEGER,
    turns_count         INTEGER NOT NULL DEFAULT 0,
    elder_mode          BOOLEAN NOT NULL DEFAULT FALSE,
    csat_score          SMALLINT CHECK (csat_score BETWEEN 1 AND 5),
    csat_comment        TEXT
);
CREATE INDEX idx_calls_started_at ON calls (started_at);
CREATE INDEX idx_calls_outcome ON calls (outcome);

-- Поверки (по одному на реплику абонента)
CREATE TABLE turns (
    id                  BIGSERIAL PRIMARY KEY,
    call_id             UUID NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
    turn_n              INTEGER NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- STT
    stt_text            TEXT,
    stt_confidence      NUMERIC(5,4),
    stt_lang            TEXT,
    stt_duration_ms     INTEGER,

    -- RAG
    rag_query           TEXT,
    rag_results         JSONB,               -- полный массив results из RagRetrievalResponse
    rag_matched         BOOLEAN,
    rag_meta            JSONB,               -- embed_ms/search_ms/rerank_ms

    -- LLM
    llm_model           TEXT,
    llm_provider        TEXT,                -- из failover gateway
    llm_in_tokens       INTEGER,
    llm_out_tokens      INTEGER,
    llm_latency_ms      INTEGER,
    llm_confidence      NUMERIC(5,4),
    llm_answer          TEXT,
    articles_cited      JSONB,
    citation_valid      BOOLEAN,
    llm_retries         INTEGER NOT NULL DEFAULT 0,

    -- TTS
    tts_voice           TEXT,
    tts_duration_ms     INTEGER,

    -- Эскалация
    escalated           BOOLEAN NOT NULL DEFAULT FALSE,
    escalation_reason   escalation_reason,

    UNIQUE (call_id, turn_n)
);
CREATE INDEX idx_turns_call_id ON turns (call_id);
CREATE INDEX idx_turns_created_at ON turns (created_at);

-- События эскалации
CREATE TABLE escalations (
    id                  BIGSERIAL PRIMARY KEY,
    call_id             UUID NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
    turn_id             BIGINT REFERENCES turns(id) ON DELETE SET NULL,
    reason              escalation_reason NOT NULL,
    queue               TEXT NOT NULL,
    agent               TEXT,                -- SIP-ID оператора (после обработки)
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    handled_at          TIMESTAMPTZ
);
CREATE INDEX idx_escalations_reason ON escalations (reason, created_at);
CREATE INDEX idx_escalations_created_at ON escalations (created_at);

-- Выборочный аудит точности (3-5% звонков)
CREATE TABLE audit_samples (
    id                  BIGSERIAL PRIMARY KEY,
    call_id             UUID NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
    turn_id             BIGINT NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
    reviewer            TEXT NOT NULL,
    correct             BOOLEAN NOT NULL,
    error_type          TEXT,                -- wrong_article, hallucination, stale_text, wrong_lang, other
    note                TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_audit_samples_correct ON audit_samples (correct);

-- Метаданные статей НК (для валидации цитат)
-- Заполняется indexer/tax_code_ingest.py при индексации
CREATE TABLE articles_meta (
    article_number      TEXT PRIMARY KEY,    -- напр. '259-3'
    chapter_number      TEXT NOT NULL,
    chapter_title       TEXT,
    title               TEXT,
    effective_date      DATE,
    cross_refs          TEXT[] DEFAULT '{}',
    lang                TEXT CHECK (lang IN ('kz','ru'))
);

-- Dead-letter очередь аудита (F11)
CREATE TABLE audit_dlq (
    id                  BIGSERIAL PRIMARY KEY,
    event_type          TEXT NOT NULL,
    payload             JSONB NOT NULL,
    error               TEXT,
    retries             INTEGER NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Утилита: доля эскалаций по дням
CREATE OR REPLACE VIEW v_escalations_daily AS
SELECT
    date_trunc('day', created_at)::date AS day,
    reason,
    COUNT(*) AS cnt
FROM escalations
GROUP BY 1, 2
ORDER BY 1, 2;
