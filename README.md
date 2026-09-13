# Голосовой AI-ассистент консультаций по НК РК (2026)

Голосовой ассистент для колл-центра: звонки от налогоплательщиков, ответы по
Налоговому кодексу РК (kz/ru), RAG-поиск по статьям, эскалация сложных вопросов
на живого оператора.

Документация: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

📖 **Не разбираетесь в Docker/Asterisk? Читайте [SETUP_GUIDE.md](SETUP_GUIDE.md) —
пошаговая инструкция с примерами команд от и до.**

## Состав

| Сервис | Порт | Роль |
|---|---|---|
| media-gateway | 8081 | ARI/ExternalMedia, VAD, endpointing, barge-in |
| stt | 8091 | faster-whisper large-v3-turbo (GPU) |
| orchestrator | 8090 | state-машина звонка, эскалации, ctx-окно |
| rag-service | 8093 | embedding + reranker + Qdrant |
| qdrant | 6333 | векторная БД НК РК |
| llm-gateway | 8094 | OpenAI-compatible, failover по провайдерам |
| tts | 8095 | Piper (ru/kk) |
| audit-db | 5432 | Postgres: звонки, туры, метрики |
| audit-worker | 8096 | асинхронная запись аудита |

Asterisk/FreePBX — вне docker (уже развёрнут), см. `telephony/asterisk_dialplan.conf`.

## Развёртывание (Debian 12, RTX 3050)

```bash
# 1. Секреты
cp .env.example .env && vim .env

# 2. Модели (Whisper-FT — ВАШ файнтюн; скрипт тянет остальное)
bash scripts/fetch_models.sh
#    → models/whisper/whisper-large-v3-turbo-FT-kzru заменить на свой чекпоинт

# 3. Тексты НК РК 2026 (формат: РАЗДЕЛ/ГЛАВА/СТАТЬЯ — см. indexer/chunker.py)
#    и индексация:
python indexer/tax_code_ingest.py --input /data/nk_2026_kz.txt --lang kk \
  --rag-url http://localhost:8093 --db-url postgresql://aitaxassistent:.../audit --dim 1024
python indexer/tax_code_ingest.py --input /data/nk_2026_ru.txt --lang ru \
  --rag-url http://localhost:8093 --db-url postgresql://aitaxassistent:.../audit --dim 1024

# 4. Asterisk: применить dialplan из telephony/asterisk_dialplan.conf,
#    создать ARI-приложение ai_assistant (user/pass -> .env)

# 5. Запуск
docker compose up -d --build
docker compose logs -f orchestrator media-gateway
```

## Проверка

```bash
# контракты + логики (локально, без docker)
python tests/test_contracts.py

# сервисы
curl localhost:8090/health    # orchestrator
curl localhost:8091/health    # stt (model_loaded: true)
curl localhost:8093/health    # rag
curl localhost:8094/health    # llm-gateway (providers available)
curl localhost:8095/health    # tts (voices)
```

## Метрики (Postgres)

- эскалации по дням/причинам: `SELECT * FROM v_escalations_daily;`
- точность по выборке: `SELECT correct::int::float / count(*) FROM audit_samples;`
- CSAT: `SELECT avg(csat_score) FROM calls;`

## Ключевые настройки

Все пороги и бюджеты — в `config/orchestrator.yaml` (без пересборки):
- `stt.min_confidence`, `clarification.max_attempts_elder` (4–5 для пожилых)
- `rag.min_rerank_score` (ниже → эскалация, не «додумывание»)
- `llm.min_confidence`, `simple_explain.max_per_question`
- `session.context.max_tokens` (32K на сессию)
