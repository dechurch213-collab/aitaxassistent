# Голосовой AI-ассистент консультаций по Налоговому кодексу РК (2026)
## Техническая архитектура v1.0

Стек зафиксирован (не обсуждается):
- Телефония: Asterisk/FreePBX (Debian 12, провайдер КТК), ARI + ExternalMedia
- STT: Whisper large-v3-turbo (файнтюн kz/ru, 8кГц) + faster-whisper, RTX 3050 4GB
- TTS: Piper (CPU, self-hosted), отдельные голоса ru/kk
- RAG: Qdrant (self-hosted) + BGE-M3 или Qwen3-Embedding-8B + Qwen3-Reranker
- LLM: внешний API (Qwen-семейство), свой лёгкий gateway в духе LiteLLM, prompt caching
- Контекст: 32K токенов потолок на сессию, RAG-контекст по теме, история сохраняется

---

## 1. Диаграмма компонентов и потока данных

```
                                   Debian 12 (один сервер)
┌───────────────────────────────────────────────────────────────────────────────────────┐
│                                                                                       │
┌────────┐  SIP/PSTN  ┌───────────────────┐   ARI REST + ExternalMedia (WS)            │
│   КТК  │◄──────────►│  Asterisk/FreePBX │◄────────────────────────┐                  │
│абонент │            │  (systemd, не в   │                        │                  │
└────────┘            │   docker)         │                        │ L8/L16 8kHz      │
                      └─────────┬─────────┘                        │ 20ms frames      │
                                │ ARI: originate/hold              │                  │
                                ▼                                  ▼                  │
                      ┌───────────────────┐          ┌─────────────────────────┐       │
                      │ Операторы (SIP)   │◄─────────┤     media-gateway       │       │
                      │ (очередь эскала-  │  transfer │  VAD, endpointing,     │       │
                      │  ций, ручной до-  │          │  ресемплинг, barge-in,  │       │
                      │  звон)            │          │  буферизация, failover  │       │
                      └───────────────────┘          └───────┬─────────┬───────┘       │
                                                             │         │               │
                       WS-события                            │         │ HTTP POST      │
                    (utterance_final, etc.)                  │         │ /transcribe    │
                                                             ▼         │ L16 16kHz      │
                      ┌─────────────────────────┐            │         ▼               │
                      │      ORCHESTRATOR       │            │  ┌─────────────┐        │
                      │ • session state machine │            │  │     stt     │        │
                      │ • topic detection       │            │  │ faster-     │        │
                      │ • 32K ctx window mgmt   │            │  │ whisper     │        │
                      │ • escalation logic      │            │  │ large-v3-   │        │
                      │ • article validator     │            │  │ turbo (FT)  │        │
                      │ • audit writer          │            │  └─────────────┘        │
                      └──┬──────────┬───────────┘            │    GPU RTX 3050         │
                         │          │                        │                          │
              POST /retrieval       │ POST /v1/chat/completions (stream)                │
                         │          │ (OpenAI-compatible + cache headers)               │
                         ▼          ▼                        │                          │
              ┌───────────────┐  ┌──────────────────┐        │                          │
              │  rag-service  │  │   llm-gateway    │        │                          │
              │ BGE-M3 /      │  │ • failover       │        │                          │
              │ Qwen3-Emb-8B  │  │   (try/except    │        │                          │
              │ Qwen3-        │  │   по base_url)   │        │                          │
              │ Reranker      │  │ • prompt cache   │        │                          │
              └──┬────────────┘  │ • rate limits    │        │                          │
                 │               └────────┬─────────┘        │                          │
                 │ REST/gRPC              │ HTTP/SSE         │                          │
                 ▼                        ▼                  │                          │
          ┌─────────────┐        ┌───────────────────┐       │                          │
          │   qdrant    │        │ LLM API (Qwen/…)  │       │                          │
          │ tax_code_   │        │ (ВНЕШНЯЯ СЕТЬ)    │       │                          │
          │ 2026        │        └───────────────────┘       │                          │
          └─────────────┘                                    │                          │
                                                             │                          │
          LLM-ответ (текст) ──► TTS-запрос ──► POST /synthesize (chunked)               │
                                                             │                          │
          ┌─────────────┐        ┌──────────────────────────────────────────┐           │
          │  audit-db   │◄───────│ Postgres: audit turns, escalations,      │           │
          │ (Postgres)  │  audit │ csat, articles_meta (валидация цитат)    │           │
          └─────────────┘        └──────────────────────────────────────────┘           │
                                                                                       │
                      ┌─────────────┐                                                  │
                      │     tts     │  Piper (CPU), голоса ru_RU-*, kk_KZ-*           │
                      │             │  вход: текст+lang, выход: s16le 24kHz (chunked)  │
                      └──────┬──────┘                                                  │
                             │ audio chunks (WS)                                       │
                             └────────────────► media-gateway ──► Asterisk ──► КТК     │
└───────────────────────────────────────────────────────────────────────────────────────┘
```

### Поток данных (happy path, один turn)

1. **Телефония**: абонент звонит → Asterisk → dialplan поднимает ExternalMedia → media-gateway подключается по WebSocket, получает кадры L16 8kHz (20 мс/кадр).
2. **VAD/endpointing** (media-gateway): Silero-VAD или webrtcvad; конец реплики = 500–600 мс тишины + минимальная длина речи 300 мс. Пока идёт ответ TTS — barge-in (отмена воспроизведения при новой реплике).
3. **STT** (media-gateway → `stt`): 8k→16k ресемплинг → POST `/transcribe` (faster-whisper large-v3-turbo, GPU) → `{text, language, confidence, words[]}`.
4. **Событие** (media-gateway → orchestrator, WS): `utterance_final` с результатами STT.
5. **Оркестратор**:
   - проверяет пороги STT (уверенность, пустой текст) → ветка уточнения (п. 5.1);
   - определяет тему: cos(query, active_topic_query) > 0.82 → та же тема (RAG-контекст сохраняется), иначе **новый RAG-запрос**;
   - строит промпт: system (~450 ток.) + RAG-блок (2–4К) + история + текущий вопрос;
   - при смене темы — RAG-блок заменяется, история сохраняется; при переполнении 32K — суммаризация старых туров.
6. **RAG** (orchestrator → `rag-service` → qdrant): embed(query) → top-20 (vector) → Qwen3-Reranker → top-3–5 → ответ с article_number, иерархией, cross-refs.
7. **LLM** (orchestrator → `llm-gateway` → внешний API): OpenAI-compatible, streaming; prompt cache по хэшу system+RAG-блока; structured output `{answer_text, articles_cited, confidence, escalation_suggested}`.
8. **Валидация** (orchestrator): regex-извлечение «Статья N» → проверка в `articles_meta` (Postgres). Не валидно → 1 retry с исправляющим промптом → ещё нет → эскалация.
9. **TTS** (orchestrator → `tts`): голос по `lang` (ru/kk) → чанки s16le 24kHz → media-gateway ресемплирует в L16 8kHz → Asterisk → абонент.
10. **Аудит**: каждый turn асинхронно пишется в audit-db (тексты, скоринги, модель, токены, задержки, цитаты, валидация).

### Ветки эскалации (оркестратор → media-gateway → ARI)

| Условие | Действие |
|---|---|
| STT: пустой/низкая уверенность, попытки исчерпаны (2, или 4–5 в elder-режиме) | `transfer`, reason=`stt_low_confidence` |
| RAG: max rerank_score < τ_rag или 0 результатов | `transfer`, reason=`rag_no_match` |
| LLM: confidence < τ_llm или `escalation_suggested=true` | `transfer`, reason=`llm_low_confidence` |
| LLM API: все провайдеры gateway упали / таймаут | `transfer`, reason=`llm_unavailable` (заготовленная фраза ожидания) |
| Валидация: статья не найдена после retry | `transfer`, reason=`citation_invalid` |
| Счётчик «объясни проще» > 3 на один вопрос | `transfer`, reason=`clarification_limit` |
| Ключевые слова споров/доначислений (детерминированный классификатор до RAG) | `transfer`, reason=`policy_dispute` |
| media-gateway потерял WS | failover dialplan Asterisk → операторы (без участия SW) |

Transfer-механика: ARI `channels/{id}/actions` → `hold` → `originate` на SIP-очередь операторов (extension из конфига) → media-gateway отключается от канала.

---

## 2. Разбивка по сервисам / контейнерам

### На текущем Debian-сервере (docker-compose)

| Сервис | Ресурсы | Комментарий |
|---|---|---|
| `media-gateway` | 2 vCPU, 0.5GB | Python/aiohttp; WS-клиент ARI; Silero-VAD; soxr; бари-ин; watchdog |
| `stt` | GPU: 4GB VRAM, 4GB RAM | faster-whisper, `compute_type=int8_float16`; volume с моделью (FT-чекпоинт) |
| `orchestrator` | 2 vCPU, 1GB | Python; stateful per-session; все бизнес-правила |
| `rag-service` | 4 vCPU, 6–9GB | BGE-M3 (CPU, ~3GB) или Qwen3-Emb-8B (GPU); Qwen3-Reranker* |
| `qdrant` | 1 vCPU, 2GB | volume для storage; collection `tax_code_2026` |
| `llm-gateway` | 1 vCPU, 0.5GB | без состояния; конфиг провайдеров; health marking |
| `tts` | 2 vCPU, 2GB | Piper; модели голосов в volume; lazy-load по voice_id |
| `audit-db` | 1 vCPU, 1GB | PostgreSQL 16 |

\* **Размерность Qwen3-Reranker на 4GB VRAM**: 8B int8 ≈ 9GB — не влезает вместе со Whisper. Варианты (конфигурируемо, стек не меняется):
- старт: **Qwen3-Reranker-4B** на GPU (int8 ≈ 4.5GB, shared с Whisper по очереди задач) или **8B на CPU** (20 docs ≈ 1.5–3 с — укладывается в бюджет RAG);
- выбор через `RAG_RERANKER_PROFILE=gpu-4b | cpu-8b | cpu-4b`.

### Вызовы по сети (внешние)

- **LLM API** (Qwen/DashScope или аналог): только через `llm-gateway`. API-ключи — в `.env` → gateway, наружу не попадают.
- Всё остальное — внутри `docker network` (127.0.0.1 только через compose-порты, наружу не пробрасывать).

### Asterisk (вне docker)

- ARI-приложение `ai_assistant` (user/pass в env), ExternalMedia на порту WS (напр. 8081, только localhost-сеть).
- Dialplan (FreePBX Extension or Feature):
  ```
  exten => ai-assist,1,NoOp(Inbound AI consultation)
  same, n,Answer()
  same, n,ExternalMedia(${EXTEN},ws://media-gateway:8081/stream/${UNIQUEID},linear8)
  same, n,GotoIf($?["0"]?ai-failover:hangup)
  same, n(ai-failover),Set(CALLERID(num)=8800)
  same, n,Dial(SIP/OPERATOR_QUEUE,60)      ; failover при смерти gateway
  same, n,Hangup()
  ```
- Эскалация: очередь `OPERATOR_QUEUE` (SIP-агенты), ARI originate с заголовком `X-Escalation-Reason`.

---

## 3. Контракты между компонентами (JSON)

Полные JSON Schema-файлы: `shared/schemas/*.schema.json`. Ниже — суть каждого контракта.

### C1. STT → media-gateway → orchestrator: `SttResult`

```json
{
  "session_id": "ch-2026-09-13-0001",
  "turn_id": 3,
  "language": "ru",
  "language_confidence": 0.97,
  "text": "Скажите, как мне оплатить ИПН за второй квартал?",
  "confidence": 0.84,
  "no_speech_prob": 0.05,
  "words": [
    {"w": "Скажите", "c": 0.99, "t0": 0.0, "t1": 0.42},
    {"w": "как", "c": 0.71, "t0": 0.5, "t1": 0.6}
  ],
  "duration_ms": 4200,
  "model": "whisper-large-v3-turbo-FT-kzru-v2"
}
```

`confidence` = `exp(avg_logprob)` сегмента; для слов — word-level probabilities. Пороги на orchestrator.

### C2. media-gateway → orchestrator: WS-события (envelope)

```json
{"event": "utterance_final", "session_id": "...", "turn_id": 3, "payload": { ...SttResult... }}
{"event": "call_started",    "session_id": "...", "payload": {"caller": "7xxx", "uniqueid": "...", "codec": "L16-8k"}}
{"event": "call_ended",      "session_id": "...", "payload": {"reason": "hangup_normal"}}
{"event": "dtmf",            "session_id": "...", "payload": {"digit": "1", "ts": 1726183200.1}}
```

### C3. orchestrator → media-gateway: команды

```json
{"command": "speak",     "session_id": "...",
 "data": {"text": "Согласно статье 259 НК РК...", "lang": "ru", "voice_id": "ru_RU-aidar-medium",
          "speed": 1.0, "interrupt": true}}
{"command": "clear_queue", "session_id": "...", "data": {}}
{"command": "transfer",  "session_id": "...",
 "data": {"reason": "llm_low_confidence", "queue": "OPERATOR_QUEUE", "announce": "Соединяю с оператором"}}
{"command": "hangup",    "session_id": "...", "data": {"reason": "completed"}}
```

### C4. orchestrator → rag-service: `/retrieval`

Request:
```json
{
  "session_id": "...",
  "query": "Оплата ИПН за второй квартал для самозанятых",
  "language": "ru",
  "top_n_vector": 20,
  "top_n_final": 4,
  "exclude_articles": ["43"],
  "hints": {"entity": "ИПН", "taxpayer_type": "individual"}
}
```

Response:
```json
{
  "session_id": "...",
  "query": "...",
  "matched": true,
  "results": [
    {
      "doc_id": "art-43-017",
      "article_number": "43-17",
      "chapter_number": "43",
      "chapter_title": "Налог на доходы физических лиц",
      "article_title": "Удержание и уплату налога",
      "text": "...полный текст статьи...",
      "vector_score": 0.62,
      "rerank_score": 0.81,
      "cross_references": ["43-5", "306-1"],
      "chapter_path": "Раздел 17 / Глава 43"
    }
  ],
  "retrieval_meta": {"vector_hits": 20, "embed_ms": 48, "search_ms": 35, "rerank_ms": 610}
}
```

`matched=false`, если max `rerank_score` < τ (поиск выполнен, релевантного нет — orchestrator решает эскалацию).

### C5. Qdrant-документ (индексация)

Collection: `tax_code_2026`, vector 1024 (BGE-M3) или 4096 (Qwen3-Emb-8B).

```json
{
  "id": "art-259-003",
  "vector": [ ... ],
  "payload": {
    "article_number": "259-3",
    "chapter_number": "259",
    "section_number": "3",
    "title": "Налоговый кредит",
    "text": "...",
    "chapter_path": "Раздел 18 / Глава 259",
    "cross_references": ["259-1", "259-2", "260-4"],
    "effective_date": "2026-01-01",
    "lang": "kz",
    "chunk_type": "article"
  }
}
```

Индексация: **1 статья = 1 точка** (основной режим, как зафиксировано). Для статей > 3000 слов дополнительно создаются под-чанки `chunk_type=section` с `parent_article` — поиск идёт по всем, результат аггрегируется на уровне статьи; в промпт уходит статья целиком. Иерархия глав + cross-refs хранятся в payload и используются LLM для навигации по связанным нормам. **Retrieval — на каждый релевантный запрос, без статического кэширования на сессию.**

### C6. orchestrator → llm-gateway: `POST /v1/chat/completions`

OpenAI-compatible + расширения gateway:

```json
{
  "model": "qwen-max",
  "stream": true,
  "temperature": 0.1,
  "response_format": {"type": "json_object"},
  "messages": [
    {"role": "system", "content": "<POLICY + правила цитирования + elder-mode flags>"},
    {"role": "system", "content": "<RAG-БЛОК: статьи, 2–4К токенов>"},
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": "..."}
  ],
  "x_session_id": "...",
  "x_cache_keys": {"system_hash": "sha256:…", "rag_hash": "sha256:…"}
}
```

Gateway: `x_cache_keys` → provider-native prompt caching (cache_control / context-cache по хэшу префикса); failover try/except по списку провайдеров; health-marking 60 с при 5xx/таймауте.

Финальный (собранный из SSE) structured-ответ LLM:

```json
{
  "answer_text": "В соответствии со статьёй 259 Налогового кодекса РК …",
  "articles_cited": ["259"],
  "confidence": 0.93,
  "escalation_suggested": false,
  "clarification_question": null
}
```

### C7. orchestrator → tts: `POST /synthesize` (chunked)

Request:
```json
{"session_id": "...", "text": "...", "lang": "kk", "voice_id": "kk_KZ-astana-medium", "speed": 0.95}
```

Response: `200 OK`, `Content-Type: audio/pcm`, `X-Format: s16le`, `X-Sample-Rate: 24000`, `X-Channels: 1`; тело — бинарный PCM chunked-потоком (первые чанки ≤ 400 мс для старта воспроизведения). Media-gateway ресемплирует 24k→8k и пушит в ExternalMedia.

---

## 4. Управление контекстным окном (потолок 32K на сессию)

Бюджет: `system ≈ 450` + `rag_block 2–4К` + `history ≤ 32768 − 450 − 4096 − 2048 (резерв)`.

Алгоритм на каждый turn:
1. **Topic detection**: `cos(embed(current_query), embed(active_topic_query))`.
   - `> 0.82` → тема не сменилась: RAG-блок переиспользуется, если новый запрос не содержит нового вопроса (классификатор «новый вопрос?»).
   - `≤ 0.82` или новый вопрос → **новый retrieval**, RAG-блок заменяется, история сохраняется.
2. **Сборка**: `system + RAG + summary (если есть) + history (последние K туров) + current`.
3. **Переполнение** (total > 32K): вызов LLM «суммаризируй предыдущие туры в ≤ 200 токенов» → `summary` заменяет старые туры; последние 6 туров остаются дословно.
4. Суммаризация считается «бесплатным» вызовом: temperature 0, max_tokens 300.

---

## 5. Логика эскалации и переспросов (state machine orchestrator)

```
utterance_final
  │
  ├─ text пуст или confidence < 0.60 ─────────────┐
  ├─ clarification_question от LLM ───────────────┤  clarify_attempts++
  │                                               │   elder_mode? 5 попыток : 2
  │   «Правильно ли я понял, вы спрашиваете про «{text}»?»
  │   (до RAG-поиска!)  ── retry STT/подтверждение
  │                                               └─ исчерпано → TRANSFER(stt_low_confidence)
  │
  ├─ policy-фильтр (споры/доначисления, детерминированный классификатор)
  │      → TRANSFER(policy_dispute)   [до RAG/LLM]
  │
  ├─ intent «объясни проще» → simple_count[question_id]++
  │      → перегенерация (проще, без терминов)
  │      → simple_count > 3 → TRANSFER(clarification_limit)
  │
  ├─ RAG retrieval
  │      max_rerank < 0.30 или matched=false → TRANSFER(rag_no_match)
  │
  ├─ LLM → structured answer
  │      confidence < 0.70 или escalation_suggested → TRANSFER(llm_low_confidence)
  │
  ├─ Валидация цитат: каждое «Статья N» ∈ articles_meta?
  │      нет → 1 retry (промпт-исправление) → нет → TRANSFER(citation_invalid)
  │
  └─ TTS → ответ абоненту; audit write
```

**Elder-режим** (авто-флаг сессии): включается при ≥ 2 последовательных STT с confidence < 0.65, длинных паузах (> 4 с) перед ответом, или повторении фраз. Эффекты: порог попыток 4–5, filler-фразы медленнее, TTS speed 0.9, более длинные подтверdings «Правильно ли я вас услышал…».

**Filler-фразы** (снимают восприятие задержки 4–7 с): «Одно мгновение, я уточняю в справочнике…», «Спасибо, проверяю норму кодекса…» — воспроизводятся параллельно с RAG/LLM.

**Пороги** — в `config/orchestrator.yaml` (см. файл), все меняются без кода.

---

## 6. Аудит и метрики (схема хранения)

Postgres: `audit-db`. Полный SQL: `audit/schema.sql`.

- **calls**: `id, started_at, ended_at, caller_masked, language, outcome enum, duration_s, turns_count, elder_mode, csat_score, csat_comment`
- **turns**: `id, call_id, turn_n, stt_text, stt_confidence, stt_lang, rag_query, rag_results jsonb, rag_matched bool, llm_model, llm_in_tokens, llm_out_tokens, llm_latency_ms, llm_confidence, llm_answer, articles_cited jsonb, citation_valid bool, tts_voice, tts_duration_ms, escalated bool, escalation_reason, created_at`
- **escalations**: `id, call_id, turn_id, reason, queue, agent, handled_at`
- **audit_samples**: `id, call_id, turn_id, reviewer, correct bool, error_type, note, created_at` (выборочная разметка для точности)
- **articles_meta**: `article_number pk, chapter_number, title, effective_date, cross_refs text[]` — для валидации цитат (заполняется индексатором)

Метрики (агрегацией по таблицам, не UI):
- доля эскалаций по reason: `SELECT reason, count(*) FROM escalations GROUP BY 1`
- точность: `correct=true / total` по `audit_samples` (выборка 3–5% звонков)
- CSAT: среднее `csat_score` (IVR-голосовое 1–5 или SMS-ссылка после звонка)
- latency P50/P95 по полям `*_ms`

Запись асинхронная (queue → worker), не блокирует голосовой контур.

---

## 7. Точки отказа и обработка

| # | Отказ | Детекция | Обработка |
|---|---|---|---|
| F1 | LLM API недоступен (5xx/таймаут > 10 с) | gateway: try/except по списку провайдеров | переключение на следующий base_url; все упали → health-mark 60 с → orchestrator: заготовленная фраза + `TRANSFER(llm_unavailable)`; session помечена для повторного RAG-ответа при восстановлении |
| F2 | LLM выдал невалидный/маловероятный ответ | confidence < 0.70, escalation_suggested, нарушен JSON-формат | 1 retry с исправляющим промптом → нет → эскалация (не «додумываем») |
| F3 | RAG не нашёл статью | matched=false, max_rerank < 0.30 | `TRANSFER(rag_no_match)`; в audit — полный log запроса для пополнения корпуса |
| F4 | Qdrant / rag-service упали | healthcheck HTTP, ошибки 5xx | orchestrator ловит → эскалация; **ответ без RAG-контекста запрещён** (политика: только со ссылками на статьи) |
| F5 | STT не распознал / невнятная речь | text пуст, confidence < 0.60 | уточняющий вопрос до RAG; elder-режим 4–5 попыток; затем эскалация |
| F6 | STT-сервис упал (GPU crash) | healthcheck, таймаут > 5 с | media-gateway: 2 retry; нет → orchestrator: эскалация со звуком «оператор» |
| F7 | TTS упал | таймаут первого чанка > 2 с | fallback-голос (второй Piper-профиль того же языка); нет → записанный WAV «мы перезвоним» + эскалация в очередь callback |
| F8 | media-gateway упал / WS разорван | Asterisk: код ExternalMedia / отсутствие heartbeats 5 с | failover в dialplan → `Dial(SIP/OPERATOR_QUEUE)`; звонок не теряется |
| F9 | GPU недоступен частично | faster-whisper init error | старт STT на CPU int8 (degraded, +2–4 с задержки) + алерт; если и CPU не хватает — F6 |
| F10 | Переполнение контекста | total_tokens > 32K | суммаризация истории (алгоритм п. 4); при ошибке суммаризации — отбрасывание старейших туров |
| F11 | Асинхронная ошибка аудита | worker exception | retry x3 → dead-letter очередь (таблица `audit_dlq`); голосовой контур не затрагивается |

**Общий принцип**: любой отказ в цепочке «STT→RAG→LLM→TTS» деградирует в **человека**, а не в «додуманный» ответ. Налоговая тема допускает только цитируемые ответы.

---

## 8. Структура репозитория

```
caller/
├── docker-compose.yml
├── .env.example
├── config/
│   ├── orchestrator.yaml          # пороги, budgets, elder-mode
│   ├── llm-gateway.yaml           # провайдеры, failover, cache
│   ├── rag.yaml                   # модели, top-N, reranker profile
│   ├── stt.yaml                   # модель, device, compute_type
│   ├── tts.yaml                   # голоса ru/kk, скорость
│   └── media-gateway.yaml         # VAD, endpointing, barge-in
├── shared/
│   ├── schemas/
│   │   ├── stt_result.schema.json
│   │   ├── orch_events.schema.json
│   │   ├── orch_commands.schema.json
│   │   ├── rag_request.schema.json
│   │   ├── rag_response.schema.json
│   │   ├── llm_gateway_request.schema.json
│   │   ├── llm_gateway_response.schema.json
│   │   └── tts_request.schema.json
│   └── models.py                  # Pydantic-модели по этим схемам
├── services/
│   ├── media_gateway/
│   │   ├── main.py                # WS-клиент ARI, event loop
│   │   ├── vad.py
│   │   ├── resample.py
│   │   └── Dockerfile
│   ├── stt/
│   │   ├── main.py                # FastAPI /transcribe
│   │   ├── engine.py              # faster-whisper wrapper
│   │   └── Dockerfile
│   ├── orchestrator/
│   │   ├── main.py                # FastAPI + WS-хаб
│   │   ├── session.py             # state, counters
│   │   ├── context_window.py      # 32K mgmt, topic, summary
│   │   ├── escalation.py          # state machine п. 5
│   │   ├── validator.py           # валидация статей
│   │   ├── policies.py            # system prompt, filler-фразы
│   │   └── Dockerfile
│   ├── rag_service/
│   │   ├── main.py                # FastAPI /retrieval
│   │   ├── embedder.py
│   │   ├── reranker.py
│   │   ├── qdrant_client.py
│   │   └── Dockerfile
│   ├── llm_gateway/
│   │   ├── main.py                # OpenAI-compatible proxy
│   │   ├── providers/             # adapter per base_url
│   │   ├── cache.py
│   │   └── Dockerfile
│   ├── tts/
│   │   ├── main.py                # FastAPI /synthesize (chunked)
│   │   └── Dockerfile
│   └── audit_worker/
│       ├── main.py                # consumer → Postgres
│       └── Dockerfile
├── indexer/
│   ├── tax_code_ingest.py         # парсинг НК РК 2026 → Qdrant + articles_meta
│   ├── chunker.py                 # статья/секция, иерархия, cross-refs
│   └── run_indexing.sh
├── audit/
│   └── schema.sql
├── tests/
│   ├── contracts/                 # schema-validation tests
│   └── e2e/                       # запись WAV → полный пайплайн (mock ARI)
└── docs/
    └── ARCHITECTURE.md
```

---

## 9. Бюджеты производительности (цели)

| Этап | P95 цель |
|---|---|
| STT (тур 5–10 с речи) | ≤ 1.5 с |
| RAG (embed+search+rerank) | ≤ 1.0 с |
| LLM first token | ≤ 2.0 с |
| LLM полный ответ | ≤ 6 с |
| TTS первый чанк | ≤ 0.4 с |
| **Восприятие: конец речи → старт ответа** | 4–7 с (filler закрывает паузу) |

Бюджет RAM (24GB): stt 4 + rag 7 + qdrant 2 + piper 2 + postgres 1 + orch 1 + media-gw 0.5 + misc ≈ 17.5 GB. VRAM 4GB: whisper int8_float16 (~3.3GB) + (опционально) reranker 4B по очереди.

## 10. Безопасность (минимально необходимое)

- API-ключи LLM — только в `.env` → gateway; наружу не пробрасываются.
- Нумерация вызовов маскируется в audit (7***).
- Все внутренние порты — только в compose-сети; наружу: WS ARI (ограничено по IP), ARI REST.
- TLS для наружного WS — reverse-proxy (Caddy) при необходимости.
