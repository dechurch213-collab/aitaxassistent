# ПОШАГОВАЯ ИНСТРУКЦИЯ ПО ЗАПУСКУ

Этот документ объясняет, как запустить голосового AI-ассистента с нуля.
Читайте по порядку, выполняйте команды точно так, как написано.
Команды для копирования выделены в отдельный блок.

Понадобится:
- SSH-доступ к вашему Debian 12 серверу (где стоит Asterisk/FreePBX)
- NVIDIA GPU (RTX 3050) с драйвером
- ~5-10 ГБ свободного места на диске
- Около 2-3 часов времени (большая часть — на загрузку моделей)

Если что-то не понятно — спрашивайте, не гадвайте.

---

## ШАГ 0: Проверка готовности сервера

Откройте терминал и подключитесь к серверу по SSH:

    ssh root@ваш-сервер

Введите пароль. Теперь вы на сервере.

Проверьте, что всё на месте. Выполните эти команды:

    nvidia-smi

Если видите карту RTX 3050 и драйвер — отлично. Если ошибка — сначала поставьте
NVIDIA-драйвер (без него STT не будет работать нормально).

    free -h

Посмотрите, что есть 24 ГБ RAM.

    docker --version
    docker compose version

Если docker не установлен — см. "Приложения" в конце этого файла.

    asterisk -rx "core show settings" | grep "Asterisk"

Убедитесь, что Asterisk работает.

---

## ШАГ 1: Скачать код проекта на сервер

У вас есть этот проект (папка "aitaxassistent") где-то локально. Нужно передать его на сервер.

Вариант А — если проект уже на сервере, пропустите этот шаг.

Вариант Б — с локальной машины на Windows:

    scp -r E:\AI\aitaxassistent root@ваш-сервер:~/aitaxassistent

Вариант В — через Git (если проект в репозитории):

    ssh root@ваш-сервер
    cd ~
    git clone https://github.com/dechurch213-collab/aitaxassistent

Проверьте, что файлы на месте:

    cd ~/aitaxassistent
    ls -la

Должны быть папки: services, config, shared, indexer, scripts, audit, tests, docs.

---

## ШАГ 2: Настроить секреты (ключи и пароли)

Создадим файл с паролями. Не показывайте этот файл никому.

    cd ~/aitaxassistent
    cp .env.example .env

Откройте его для редактирования:

    nano .env

Вы увидите пустые поля. Заполните их:

- LLM_API_KEY_PRIMARY=ваш ключ от провайдера LLM (например, Qwen/DashScope)
  Где взять: у вашего поставщика LLM-сервиса. Это ключ для доступа к модели.
- LLM_API_KEY_SECONDARY=второй ключ (резервный, если первый сломается)
- QDRANT_API_KEY=придумайте любой сложный пароль (это для внутренней БД)
- ARI_USER=ai_assistant_user (придумайте)
- ARI_PASSWORD=очень-сложный-пароль-123 (придумайте, запомните)
- AUDIT_DB_PASSWORD=ещё-один-сложный-пароль (для базы аудита)

Сохранить в nano: Ctrl+O, Enter, затем Ctrl+X.

---

## ШАГ 3: Скачать модели (AI-мозги)

Это самый долгий шаг (20-40 минут в зависимости от интернета).

Выполните:

    cd ~/aitaxassistent
    chmod +x scripts/fetch_models.sh
    bash scripts/fetch_models.sh

Скрипт скачает:
- Silero VAD (распознаёт, где речь, где тишина)
- BGE-M3 (понимает смысл вопросов)
- Qwen3-Reranker (ранжирует статьи)
- Piper голоса (русский, казахский)
- Whisper (распознаёт речь — базовую версию)

ВАЖНО: скрипт скачает БАЗОВУЮ версию Whisper. Вам нужно заменить её на ВАШУ
файнтюн-версию (под казахский/русский, 8 кГц).

Где лежит базовая версия:
    ~/aitaxassistent/models/whisper/whisper-large-v3-turbo-FT-kzru/

Как заменить:
1. Возьмите ваш файнтюн-файл Whisper (у вас он уже есть, раз вы его тренировали)
2. Загрузите его на сервер в эту папку
3. Структура папки должна быть такой:
   whisper-large-v3-turbo-FT-kzru/
   ├── model.bin (или config.json, tokenizer.json и т.д.)
   └── (все файлы от вашего файнтюна)

Если у вас нет файнтюна — система будет работать, но хуже понимать
непроясную речь (особенно казахский и пожилых пользователей).

Проверьте, что модели скачались:

    ls -la models/
    ls -la models/whisper/

Должны быть папки с файлами, не пустые.

---

## ШАГ 4: Подготовить текст Налогового кодекса

Системе нужен текст НК РК 2026 в специальном формате.

Формат (пример):

    РАЗДЕЛ 17. НАЛОГИ И СБОРЫ
    ГЛАВА 43. НАЛОГ НА ДОХОДЫ ФИЗИЧЕСКИХ ЛИЦ
    СТАТЬЯ 43. НАЛОГ НА ДОХОДЫ ФИЗИЧЕСКИХ ЛИЦ (ИПН)
    1. Текст первого пункта...
    2. Текст второго пункта...

СТАТЬЯ 43-1. ДРУГАЯ СТАТЬЯ
    1. Текст...

Вам нужно:
1. Взять текст НК РК 2026 (на казахском и русском)
2. Оформить его в этом формате
3. Сохранить как два файла:
   - ~/aitaxassistent/data/nk_2026_kz.txt (казахский)
   - ~/aitaxassistent/data/nk_2026_ru.txt (русский)

Если текст уже в другом формате — напишите, помогу его преобразовать.

Создайте папку:

    mkdir -p ~/aitaxassistent/data

---

## ШАГ 5: Настроить Asterisk

Аsterisk должен знать, что при звонке на определённый номер нужно передать
его нашему AI. Всё делается через терминал — готовый конфиг уже есть.

### 5.1. Добавить пользователя ARI

Это "пароль", по которому наш AI подключается к Asterisk.

    nano /etc/asterisk/ari.conf

Добавьте в конец файла:

    [ai_assistant_user]
    type = user
    password = ПАРОЛЬ_ИЗ_ENV

Вместо ПАРОЛЬ_ИЗ_ENV вставьте тот пароль, который вы задали в .env
в строке ARI_PASSWORD (ШАГ 2). Должны совпадать!

Сохранить: Ctrl+O, Enter, Ctrl+X.

    asterisk -rx "ari reload"

### 5.2. Добавить сценарий звонка (dialplan)

У нас есть готовый файл сценария. Скопируем его:

    cd ~/aitaxassistent
    cp telephony/asterisk_dialplan.conf /etc/asterisk/ai_assistant.conf

Откроем и проверим:

    nano /etc/asterisk/ai_assistant.conf

Убедитесь, что в секции [from-ai-escalation] указаны ваши реальные операторы.
Найдите строку:

    same => n,Dial(SIP/AGENT1|SIP/AGENT2,60)

И замените AGENT1|AGENT2 на ваши реальные SIP-агентов (через |, например:
SIP/101|SIP/102|SIP/103). Если операторов пока нет — оставьте как есть,
эскалация будет недоступна, но AI работать будет.

Сохранить: Ctrl+O, Enter, Ctrl+X.

Теперь подключим этот файл к Asterisk:

    nano /etc/asterisk/extensions.conf

Найдите строку:

    #include custom.conf

И добавьте под ней (или в секцию [general], если она есть):

    #include ai_assistant.conf

Сохранить: Ctrl+O, Enter, Ctrl+X.

Перезагрузите Asterisk:

    asterisk -rx "dialplan reload"

### 5.3. Привязать телефонный номер

Теперь нужно сказать Asterisk: "если звонят на номер X — запускай AI".

Вариант 1 (через FreePBX, проще):
1. Откройте браузер, войдите в FreePBX (адрес вашего сервера)
2. Inbound Routes -> Add Inbound Route
3. Заполните:
   - Description: AI Tax Assistant
   - DID Number: ваш-номер-для-консультаций
   - Set Destination to: Extension or Feature
   - Extension: ai-assist
4. Submit Changes

Вариант 2 (через терминал, если FreePBX не используете):

    nano /etc/asterisk/extensions.conf

Добавьте новый контекст:

    [from-pstn-ai]
    exten => ВАШ-НОМЕР,1,Goto(ai-assistant,ai-assist,1)
    exten => i,1,Goto(ai-assistant,ai-assist,1)

Где ВАШ-НОМЕР — номер, на который будут звонить (например, 8800 или 2590).

    asterisk -rx "dialplan reload"

### 5.4. Проверить, что Asterisk видит всё

    asterisk -rx "dialplan show ai-assistant"

Должна появиться схема с ai-assist. Если пусто — проверьте ШАГ 5.2.

    asterisk -rx "ari show users"

Должен быть пользователь ai_assistant_user.

---

## ШАГ 6: Запустить все сервисы

Теперь самое интересное — запуск!

    cd ~/aitaxassistent
    docker compose up -d --build

Это займёт 5-10 минут (docker соберёт контейнеры).

Если всё прошло успешно, увидите что-то вроде:

    [+] Running 9/9
     ✔ Container aitaxassistent-media-gateway-1  Started
     ✔ Container aitaxassistent-stt-1           Started
     ...

Если видите ошибки — прокрутите вверх, посмотрите, где проблема.
Самые частые:
- "port already in use" — освободите порт
- "no space left on device" — мало места на диске
- "cannot allocate memory" — мало RAM

Проверьте, что все сервисы запущены:

    docker compose ps

Все должны быть в статусе "running" или "Up".

---

## ШАГ 7: Проверить, что всё работает

Проверим каждый сервис. Выполните эти команды:

    # Media gateway
    curl http://localhost:8081/health
    # Должно быть: {"status":"ok",...}

    # STT
    curl http://localhost:8091/health
    # Должно быть: {"status":"ok","model_loaded":true}
    # Если model_loaded: false — модель не загрузилась, см. troubleshooting

    # Orchestrator
    curl http://localhost:8090/health
    # Должно быть: {"status":"ok",...}

    # RAG
    curl http://localhost:8093/health
    # Должно быть: {"status":"ok",...}

    # LLM Gateway
    curl http://localhost:8094/health
    # Должно быть: {"status":"ok","providers":[...]}

    # TTS
    curl http://localhost:8095/health
    # Должно быть: {"status":"ok","voices_available":[...]}

Если все проверки прошли — поздравляю, сервисы работают!

---

## ШАГ 8: Наполнить базу статей НК

Теперь нужно, чтобы система "прочитала" Налоговый кодекс.

Сначала установим нужные библиотеки (один раз):

    pip3 install httpx qdrant-client asyncpg

Если pip3 нет:

    apt install -y python3-pip

    cd ~/aitaxassistent

    # Для казахского языка
    python3 indexer/tax_code_ingest.py \
      --input data/nk_2026_kz.txt \
      --lang kk \
      --rag-url http://localhost:8093 \
      --db-url postgresql://aitaxassistent:ВАШ_ПАРОЛЬ_АУДИТ@localhost:5432/audit \
      --dim 1024

    # Для русского языка
    python3 indexer/tax_code_ingest.py \
      --input data/nk_2026_ru.txt \
      --lang ru \
      --rag-url http://localhost:8093 \
      --db-url postgresql://aitaxassistent:ВАШ_ПАРОЛЬ_АУДИТ@localhost:5432/audit \
      --dim 1024

Вместо ВАШ_ПАРОЛЬ_АУДИТ вставьте пароль из .env (строка AUDIT_DB_PASSWORD).

Если rag-service ещё не поднял (медленно стартует), подождите минуту и повторите.

Это займёт 5-15 минут. Вы увидите, сколько статей было обработано.

Проверьте:

    docker compose exec audit-db psql -U aitaxassistent -d audit -c "SELECT count(*) FROM articles_meta;"

Должно быть 100+ статей.

---

## ШАГ 9: Тестовый звонок

Всё готово! Теперь можно звонить.

1. Возьмите телефон
2. Позвоните на номер, который вы настроили в ШАГЕ 5.2
3. Подскажите, что вы хотите спросить о налогах
4. AI ответит голосом

Пример диалога:
- Вы: "Как оплатить налог на доход?"
- AI: "Для оплаты налога на доход физических лиц (ИПН) вы можете..."

Если AI не понимает или отвечает неправильно:
- Проверьте, что модели загружены (ШАГ 7)
- Проверьте, что НК загружен (ШАГ 8)
- Посмотрите логи: `docker compose logs -f orchestrator`

---

## ЧТО ДЕЛАТЬ, ЕСЛИ ЧТО-ТО СЛОМАЛОСЬ

### STT не загружает модель

Проверьте:
    nvidia-smi
    # GPU должен быть виден

    docker compose logs stt | tail -20
    # Посмотрите, что пишет STT

Частые причины:
- Неверный путь к модели
- Модель повреждена при скачивании
- Нет NVIDIA-драйвера

Решение:
    # Перескачайте модель
    rm -rf models/whisper/whisper-large-v3-turbo-FT-kzru
    bash scripts/fetch_models.sh

### LLM не отвечает

Проверьте ключ:
    nano .env
    # Убедитесь, что LLM_API_KEY_PRIMARY заполнен

Проверьте логи:
    docker compose logs llm-gateway | tail -20

Если ошибка 401 - неверный ключ.
Если ошибка 404 - неверный URL провайдера.

### RAG не находит статьи

Проверьте, что НК загружен:
    docker compose exec audit-db psql -U aitaxassistent -d audit -c "SELECT count(*) FROM articles_meta;"

Если 0 - перезапустите ШАГ 8.

### Звонки не приходят

Проверьте Asterisk:
    asterisk -rx "core show channels"

Проверьте, что номер настроен правильно (ШАГ 5.2).

### Высокая задержка

Проверьте ресурсы:
    nvidia-smi
    free -h
    docker stats

Если GPU загружен на 100% - это нормально во время разговора.
Если RAM почти закончилась - может быть проблема.

---

## ПРИЛОЖЕНИЯ

### Если Docker не установлен

    # Установить Docker
    curl -fsSL https://get.docker.com | sh

    # Установить Docker Compose
    curl -L "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64" \
      -o /usr/local/bin/docker-compose
    chmod +x /usr/local/bin/docker-compose

### Если NVIDIA-драйвер не установлен

    # Для Debian 12
    sudo apt update
    sudo apt install -y nvidia-driver-535
    sudo reboot

    # После перезагрузки проверьте:
    nvidia-smi

### Полезные команды

    # Посмотреть логи всех сервисов
    docker compose logs -f

    # Перезапустить всё
    docker compose restart

    # Остановить всё
    docker compose down

    # Проверить использование ресурсов
    docker stats

---

## ГОТОВО!

Поздравляю! Голосовой AI-ассистент запущен.

Теперь он:
- Принимает звонки по вашему номеру
- Понимает вопросы на русском и казахском
- Ищет ответы в Налоговом кодексе РК 2026
- Отвечает голосом со ссылкой на статью
- Передаёт сложные вопросы живому оператору

Если что-то не работает или есть вопросы - обращайтесь!
