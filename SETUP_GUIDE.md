# ПОШАГОВАЯ ИНСТРУКЦИЯ ПО ЗАПУСКУ

Этот документ объясняет, как запустить голосового AI-ассистента консультаций
по Налоговому кодексу РК (2026) с нуля. Читайте по порядку, команды для
копирования выделены в отдельные блоки и выполняются точно как написано.

Понадобится:
- SSH-доступ к серверу Debian 12 (где уже стоит Asterisk/FreePBX)
- NVIDIA GPU (RTX 3050 4 ГБ) с драйвером
- ~25 ГБ свободного места (модели ~15 ГБ + Docker-образы + данные)
- ~24 ГБ RAM
- Около 2–3 часов (большая часть — загрузка моделей)

Если что-то не понятно — спрашивайте, не гадайте.

---

## Что именно запускается

9 контейнеров (сеть `aitaxassistent_net`):

| Сервис | Порт | Роль |
|---|---|---|
| media-gateway | 8081 | ARI/ExternalMedia, VAD, endpointing, barge-in |
| stt | 8091 | faster-whisper large-v3-turbo (GPU) |
| orchestrator | 8090 | state-машина звонка, эскалации, контекст-окно |
| rag-service | 8093 | embedding (BGE-M3) + reranker (Qwen3) + Qdrant |
| qdrant | 6333 | векторная БД статей НК РК |
| llm-gateway | 8094 | OpenAI-compatible прокси, failover по провайдерам |
| tts | 8095 | Piper (ru/kk) |
| audit-db | 5432 | Postgres: звонки, туры, метрики |
| audit-worker | 8096 | асинхронная запись аудита |

Asterisk/FreePBX остаётся вне Docker (systemd, уже развёрнут). Конфиги сервисов
лежат в `config/` и монтируются в контейнеры как `/etc/aitaxassistent/*.yaml`.

---

## ШАГ 0: Проверка готовности сервера

Подключитесь по SSH:

    ssh root@ваш-сервер

Проверьте железо и софт:

    nvidia-smi
    # должна видна карта RTX 3050 и драйвер. Если ошибка — поставьте драйвер
    # (см. «Приложения» в конце).

    free -h
    # должно быть ~24 ГБ RAM

    docker --version
    docker compose version
    # если нет — см. «Приложения».

    asterisk -rx "core show settings" | grep Asterisk
    # Asterisk должен работать.

---

## ШАГ 1: Скачать код проекта на сервер

Проект (папка `aitaxassistent`) нужно передать на сервер.

Вариант А — проект уже на сервере: пропустите шаг.

Вариант Б — с локальной машины Windows:

    scp -r E:\AI\aitaxassistent root@ваш-сервер:~/aitaxassistent

Вариант В — через Git:

    ssh root@ваш-сервер
    cd ~
    git clone ваша-ссылка-на-репозиторий aitaxassistent

Проверьте, что файлы на месте:

    cd ~/aitaxassistent
    ls -la

Должны быть папки: `services`, `config`, `shared`, `indexer`, `scripts`, `audit`,
`tests`, `docs`, `telephony` и файлы `docker-compose.yml`, `.env.example`.

---

## ШАГ 2: Секреты (ключи и пароли)

    cd ~/aitaxassistent
    cp .env.example .env
    nano .env

Заполните все поля:

- `LLM_API_KEY_PRIMARY=` ключ от провайдера LLM (DashScope/Qwen). Основной.
- `LLM_API_KEY_SECONDARY=` резервный ключ (другой base_url, см. `config/llm-gateway.yaml`).
- `QDRANT_API_KEY=` любой сложный пароль для внутренней векторной БД Qdrant.
- `ARI_USER=` `ai_assistant_user` (придумайте; создадим его в Asterisk на ШАГЕ 5).
- `ARI_PASSWORD=` сложный пароль для ARI (запомните, он же пойдёт в `ari.conf`).
- `AUDIT_DB_PASSWORD=` пароль пользователя БД аудита (пользователь `aitaxassistent`).
- `RAG_RERANKER_PROFILE=` `gpu-4b` (по умолчанию; см. `config/rag.yaml`).

Сохранить: `Ctrl+O`, `Enter`, `Ctrl+X`.

> Файл `.env` не показывайте никому и не коммитьте. Ключи LLM уходят только в
> `llm-gateway`, наружу не пробрасываются.

---

## ШАГ 3: Скачать модели (AI-мозги)

Самый долгий шаг (20–40 минут). Сначала поставим загрузчик моделей:

    pip3 install huggingface_hub
    # если pip3 нет: apt install -y python3-pip

Запускаем загрузку:

    cd ~/aitaxassistent
    chmod +x scripts/fetch_models.sh
    bash scripts/fetch_models.sh

Скрипт скачает:
- `models/silero_vad.onnx` — Silero VAD (детектор речи)
- `models/bge-m3/` — BGE-M3 (embedding, ~2.3 ГБ)
- `models/reranker/` — Qwen3-Reranker-4B (~9 ГБ)
- `models/piper/` — голоса Piper: `ru_RU-aidar-medium`, `ru_RU-dmitri-medium`,
  `kk_KZ-astana-medium`, `kk_KZ-almaty-medium`
- `models/whisper/whisper-large-v3-turbo-FT-kzru/` — Whisper large-v3-turbo
  (базовая версия, ~1.6 ГБ)

ВАЖНО: скрипт качает **базовый** Whisper. Для продакшена замените его на ВАШ
файнтюн (kz/ru, телефонное качество 8 кГц). Положите ваш чекпоинт в ту же папку:

    ~/aitaxassistent/models/whisper/whisper-large-v3-turbo-FT-kzru/

Структура (пример):
```
whisper-large-v3-turbo-FT-kzru/
├── config.json
├── tokenizer.json
├── model.bin (или эквивалент от вашего файнтюна)
└── ...остальные файлы чекпоинта
```

Если файнтюна нет — система будет работать, но хуже понимать казахский и
невнятную речь (особенно пожилых абонентов).

Проверьте, что всё скачалось:

    ls -la models/
    ls -la models/whisper/whisper-large-v3-turbo-FT-kzru/

Папки должны быть непустыми.

> Если `kk_KZ-astana-medium.onnx` не скачался (нет в публичном репо) — подложите
> свой казахский голос Piper в `models/piper/` и пропишите его в `config/tts.yaml`.
> Без казахского голоса TTS будет отдавать 503 на языке `kk`.

---

## ШАГ 4: Подготовить текст Налогового кодекса РК 2026

Системе нужен текст НК в специальном формате. Пример (русский):

    РАЗДЕЛ 17. НАЛОГИ И СБОРЫ
    ГЛАВА 43. НАЛОГ НА ДОХОДЫ ФИЗИЧЕСКИХ ЛИЦ
    СТАТЬЯ 43. НАЛОГ НА ДОХОДЫ ФИЗИЧЕСКИХ ЛИЦ (ИПН)
    1. Текст первого пункта...
    2. Текст второго пункта...

    СТАТЬЯ 43-1. ДРУГАЯ СТАТЬЯ
    1. Текст...

Для казахского заголовки: `BÖLIM` (раздел), `ТАРАУ` (глава), `БАП` (статья).
Формат разбора см. в `indexer/chunker.py`.

Сохраните два файла:

    ~/aitaxassistent/data/nk_2026_kz.txt   (казахский)
    ~/aitaxassistent/data/nk_2026_ru.txt   (русский)

Создайте папку:

    mkdir -p ~/aitaxassistent/data

---

## ШАГ 5: Настроить Asterisk

Asterisk должен: при звонке на нужный номер передавать его в AI, а при обрыве/
эскалации — переводить на операторов. Готовый dialplan уже в
`telephony/asterisk_dialplan.conf`.

### 5.1. Включить ARI и создать пользователя

    nano /etc/asterisk/http.conf

Убедитесь, что HTTP-сервер Asterisk слушает адрес, доступный из Docker (не только
127.0.0.1). Минимум:

    [general]
    enabled=yes
    bindaddr=0.0.0.0
    bindport=8088

Перезагрузите: `asterisk -rx "http reload"`.

    nano /etc/asterisk/ari.conf

Добавьте в конец (пароль = `ARI_PASSWORD` из `.env`):

    [ai_assistant_user]
    type = user
    password = ВАШ_ПАРОЛЬ_ИЗ_ENV
    read_only = no

    ; приложение, которое «забирает» звонок:
    [general]
    enabled = yes
    pretty = no
    allowed_origins = *

Сохранить: `Ctrl+O`, `Enter`, `Ctrl+X`.

    asterisk -rx "ari reload"
    asterisk -rx "ari show users"
    # должен быть ai_assistant_user.

### 5.2. Подключить dialplan

    cd ~/aitaxassistent
    cp telephony/asterisk_dialplan.conf /etc/asterisk/ai_assistant.conf
    nano /etc/asterisk/ai_assistant.conf

В секции `[from-ai-escalation]` замените `AGENT1|AGENT2` на ваши реальные SIP-агенты:

    same => n,Dial(SIP/101|SIP/102|SIP/103,60)

Если операторов нет — оставьте как есть (эскалация будет недоступна, но AI
ответы отдаёт). Номер абонента (`?caller=${CALLERID(num)}`) передаётся в
media-gateway и маскируется в аудите; `CALLERID` НЕ перезаписывается, чтобы
оператор видел реальный номер.

Подключите файл к extensions:

    nano /etc/asterisk/extensions.conf

Найдите `#include custom.conf` и добавьте под ней:

    #include ai_assistant.conf

    asterisk -rx "dialplan reload"
    asterisk -rx "dialplan show ai-assistant"
    # должна появиться схема с ai-assist.

### 5.3. Привязать входящий номер к AI

Вариант 1 (FreePBX, проще): Inbound Routes → Add →
- Description: AI Tax Assistant
- DID Number: ваш-номер-для-консультаций
- Destination: Extension/Feature → `ai-assist`

Вариант 2 (терминал):

    nano /etc/asterisk/extensions.conf

Добавьте контекст:

    [from-pstn-ai]
    exten => ВАШ-НОМЕР,1,Goto(ai-assistant,ai-assist,1)
    exten => i,1,Goto(ai-assistant,ai-assist,1)

    asterisk -rx "dialplan reload"

> Asterisk на хосте стучится на ExternalMedia по `ws://127.0.0.1:8081` — этот
> порт проброшен контейнером `media-gateway` только на localhost (см.
> `docker-compose.yml`). ARI ходит в обратную сторону: контейнер → хост через
> `host.docker.internal` (настроено в compose через `extra_hosts`).

---

## ШАГ 6: Запустить все сервисы

    cd ~/aitaxassistent
    docker compose up -d --build

Сборка идёт 5–10 минут. Успешный запуск выглядит примерно так:

    [+] Running 9/9
     ✔ Container aitaxassistent-media-gateway-1  Started
     ✔ Container aitaxassistent-stt-1           Started
     ✔ Container aitaxassistent-rag-service-1    Started
     ✔ Container aitaxassistent-qdrant-1         Started
     ✔ Container aitaxassistent-llm-gateway-1    Started
     ✔ Container aitaxassistent-tts-1           Started
     ✔ Container aitaxassistent-audit-db-1       Started
     ✔ Container aitaxassistent-audit-worker-1   Started
     ✔ Container aitaxassistent-orchestrator-1   Started

Частые ошибки:
- `port already in use` — освободите порт (особенно 5432/8081 на хосте).
- `no space left on device` — мало места.
- `cannot allocate memory` — мало RAM.

Проверьте статус:

    docker compose ps

Все сервисы должны быть `Up`. Колонка `health` со временем станет `healthy`
(stt и rag стартуют дольше — до минуты).

---

## ШАГ 7: Проверить, что всё работает

Сначала тесты контрактов (без Docker, на хосте):

    cd ~/aitaxassistent
    python3 tests/test_contracts.py
    # ожидаем: ALL TESTS PASSED

Затем health-чеки. Порты 8081, 6333, 8093, 5432 проброшены на localhost —
можно дёрнуть напрямую с хоста:

    curl -s http://localhost:8081/health     # media-gateway
    curl -s http://localhost:6333/healthz   # qdrant
    curl -s http://localhost:8093/health    # rag-service

Внутренние сервисы (8090/8091/8094/8095) не проброшены наружу — проверяем
изнутри контейнеров (curl в них уже установлен):

    docker compose exec orchestrator curl -s localhost:8090/health
    docker compose exec stt          curl -s localhost:8091/health   # "model_loaded": true
    docker compose exec llm-gateway  curl -s localhost:8094/health  # список провайдеров
    docker compose exec tts          curl -s localhost:8095/health   # voices_available

Если все вернули `{"status":"ok",...}` — сервисы работают.

> `stt` /health показывает `model_loaded: true` только после загрузки Whisper
> (несколько секунд после старта). Если `false` — см. troubleshooting.

---

## ШАГ 8: Наполнить базу статей НК (индексация)

Система должна «прочитать» Налоговый кодекс: векторизовать статьи в Qdrant и
записать метаданные в Postgres для валидации цитат.

Поставим зависимости индексатора (один раз):

    pip3 install httpx qdrant-client asyncpg

Перед запуском загрузим секрет Qdrant из `.env` в окружение (индексатор читает
`QDRANT_API_KEY` и `QDRANT_URL` из среды):

    cd ~/aitaxassistent
    set -a; . ./.env; set +a

Запускаем индексацию (rag-service и qdrant должны быть `healthy` — см. ШАГ 7).

Казахский корпус (`--lang kk`):

    python3 indexer/tax_code_ingest.py \
      --input data/nk_2026_kz.txt \
      --lang kk \
      --rag-url http://localhost:8093 \
      --db-url postgresql://aitaxassistent:ВАШ_ПАРОЛЬ_АУДИТ@localhost:5432/audit \
      --dim 1024

Русский корпус:

    python3 indexer/tax_code_ingest.py \
      --input data/nk_2026_ru.txt \
      --lang ru \
      --rag-url http://localhost:8093 \
      --db-url postgresql://aitaxassistent:ВАШ_ПАРОЛЬ_АУДИТ@localhost:5432/audit \
      --dim 1024

Где `ВАШ_ПАРОЛЬ_АУДИТ` — значение `AUDIT_DB_PASSWORD` из `.env`. `--dim 1024`
соответствует BGE-M3 (см. `config/rag.yaml`).

Процесс: парсинг → embedding через `/embed` rag-service → upsert в Qdrant
(коллекция `tax_code_2026`) → запись `articles_meta` в Postgres. Занимает
5–15 минут на язык.

Проверка:

    docker compose exec audit-db psql -U aitaxassistent -d audit -c "SELECT count(*) FROM articles_meta;"
    # 100+ строк.

    curl -s http://localhost:8093/articles | python3 -m json.tool
    # список article_numbers из Qdrant.

---

## ШАГ 9: Тестовый звонок

Всё готово.

1. Позвоните на номер из ШАГА 5.3.
2. Задайте вопрос по налогам (русский или казахский).
3. AI ответит голосом со ссылкой на статью.

Пример:
- Вы: «Как оплатить ИПН за второй квартал?»
- AI: «Согласно статье 43 НК РК, ИПН уплачивается не позднее 25 числа...»

Сложные вопросы (споры, доначисления, обжалование) AI deterministically
переводит на оператора («Соединяю вас с оператором...»).

Логи в реальном времени:

    docker compose logs -f orchestrator media-gateway

Если AI не понимает или отвечает неверно — проверьте модели (ШАГ 7), индексацию
(ШАГ 8) и логи.

---

## ЧТО ДЕЛАТЬ, ЕСЛИ ЧТО-ТО СЛОМАЛОСЬ

### STT не загружает модель (`model_loaded: false`)

    nvidia-smi                  # GPU виден?
    docker compose logs stt | tail -30

Частые причины: неверный путь к модели, повреждённая модель, нет драйвера.

    rm -rf models/whisper/whisper-large-v3-turbo-FT-kzru
    bash scripts/fetch_models.sh

### LLM не отвечает (ошибка 502 от llm-gateway)

    nano .env                   # LLM_API_KEY_PRIMARY заполнен?
    docker compose logs llm-gateway | tail -30

- `401` — неверный ключ.
- `404` — неверный `base_url` в `config/llm-gateway.yaml`.

### RAG не находит статьи

    docker compose exec audit-db psql -U aitaxassistent -d audit -c "SELECT count(*) FROM articles_meta;"
    # если 0 — повторите ШАГ 8.

### Звонки не доходят до AI

    asterisk -rx "core show channels"
    asterisk -rx "dialplan show ai-assistant"
    curl -s http://localhost:8081/health
    docker compose logs media-gateway | tail -30

Проверьте, что ARI-пользователь есть (`ari show users`) и `http.conf` слушает
`0.0.0.0:8088` (контейнер reachает хост через `host.docker.internal`).

### caller_masked всегда пустой в аудите

dialplan должен передавать номер: `.../stream/${UNIQUEID}?caller=${CALLERID(num)}`
(см. `telephony/asterisk_dialplan.conf`). Не перезаписывайте `CALLERID`.

### Высокая задержка / GPU 100%

    nvidia-smi
    free -h
    docker stats

100% GPU во время разговора — нормально. RAM на пределе — проблема.

### Индексация падает на записи в Postgres

- Проверьте пароль в `--db-url` (=`AUDIT_DB_PASSWORD`).
- Проверьте, что `audit-db` `healthy`: `docker compose ps audit-db`.
- `articles_meta.lang` принимает `kk`/`ru` — не используйте `kz`.

---

## ПРИЛОЖЕНИЯ

### Если Docker не установлен

    curl -fsSL https://get.docker.com | sh
    systemctl enable --now docker
    # compose уже входит в состав Docker (команда `docker compose`).

### Если NVIDIA-драйвер не установлен (Debian 12)

    apt update
    apt install -y nvidia-driver-535
    reboot
    # после перезагрузки: nvidia-smi

Контейнерам нужен NVIDIA Container Toolkit, чтобы видеть GPU:

    curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
      | gpg --dearmor -o /usr/share/keyrings/nvidia.gpg
    curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
      | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia.gpg] https://#g' \
      > /etc/apt/sources.list.d/nvidia-container-toolkit.list
    apt update
    apt install -y nvidia-container-toolkit
    nvidia-ctk runtime configure
    systemctl restart docker
    # проверка: docker run --rm --gpus all nvidia/cuda:12.1.1-base-ubuntu22.04 nvidia-smi

### Полезные команды

    docker compose logs -f                # логи всех сервисов
    docker compose logs -f orchestrator   # логи одного сервиса
    docker compose restart                # перезапуск
    docker compose down                   # остановить всё
    docker compose ps                     # статус + health
    docker stats                          # ресурсы (CPU/RAM)
    docker compose exec audit-db psql -U aitaxassistent -d audit   # консоль БД

### Метрики (Postgres)

    docker compose exec audit-db psql -U aitaxassistent -d audit \
      -c "SELECT * FROM v_escalations_daily ORDER BY day DESC LIMIT 14;"

---

## ГОТОВО

Голосовой AI-ассистент запущен. Он:
- принимает звонки на ваш номер;
- понимает вопросы на русском и казахском;
- ищет ответы в Налоговом кодексе РК 2026 (RAG + rerank);
- отвечает голосом со ссылкой на статью;
- передаёт споры/доначисления и нераспознанные вопросы живому оператору.

Все пороги (уверенность STT/LLM, число переспросов, elder-режим, бюджеты
контекста) меняются без кода в `config/orchestrator.yaml`.
