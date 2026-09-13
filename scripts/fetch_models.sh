#!/usr/bin/env bash
# Загрузка моделей. Запускать на сервере (Debian 12).
# Зависимости: python3, huggingface_hub, ctranslate2 (для конвертации Whisper).
# Установка зависимостей:
#   pip3 install huggingface_hub ctranslate2 transformers[torch] torch
set -euo pipefail
cd "$(dirname "$0")"

# Конвертер Whisper: hf safetensors -> CTranslate2 (format faster-whisper)
command -v ct2-transformers-converter >/dev/null 2>&1 || {
  echo "ERROR: ct2-transformers-converter не найден."
  echo "Установите: pip3 install ctranslate2 transformers[torch] torch"
  exit 1
}

mkdir -p models/whisper models/bge-m3 models/reranker models/piper

echo "=== Silero VAD ==="
if [ ! -f models/silero_vad.onnx ]; then
  wget -q -O models/silero_vad.onnx \
    https://github.com/snakers4/silero-vad/raw/master/src/silero_vad/data/silero_vad.onnx
fi

echo "=== BGE-M3 (embedding, ~2.3GB) ==="
if [ ! -f models/bge-m3/config.json ]; then
  python3 -c "
from huggingface_hub import snapshot_download
snapshot_download('BAAI/bge-m3', local_dir='models/bge-m3')
"
fi

echo "=== Qwen3-Reranker-4B (~9GB int8-friendly) ==="
if [ ! -f models/reranker/config.json ]; then
  python3 -c "
from huggingface_hub import snapshot_download
snapshot_download('Qwen/Qwen3-Reranker-4B', local_dir='models/reranker')
"
fi

echo "=== Piper voices ==="
# ru
wget -q -O models/piper/ru_RU-aidar-medium.onnx \
  https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/ru/ru_RU/aidar/medium/ru_RU-aidar-medium.onnx
wget -q -O models/piper/ru_RU-aidar-medium.onnx.json \
  https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/ru/ru_RU/aidar/medium/ru_RU-aidar-medium.onnx.json
wget -q -O models/piper/ru_RU-dmitri-medium.onnx \
  https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/ru/ru_RU/dmitri/medium/ru_RU-dmitri-medium.onnx
wget -q -O models/piper/ru_RU-dmitri-medium.onnx.json \
  https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/ru/ru_RU/dmitri/medium/ru_RU-dmitri-medium.onnx.json
# kk (если голосов kk в публичном репо нет — заменить на свои надеренные веса)
wget -q -O models/piper/kk_KZ-astana-medium.onnx \
  https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/kk/kk_KZ/astana/medium/kk_KZ-astana-medium.onnx || echo "WARN: kk_KZ-astana не найден — подложите свой голос"
wget -q -O models/piper/kk_KZ-astana-medium.onnx.json \
  https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/kk/kk_KZ/astana/medium/kk_KZ-astana-medium.onnx.json || true
wget -q -O models/piper/kk_KZ-almaty-medium.onnx \
  https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/kk/kk_KZ/almaty/medium/kk_KZ-almaty-medium.onnx || true
wget -q -O models/piper/kk_KZ-almaty-medium.onnx.json \
  https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/kk/kk_KZ/almaty/medium/kk_KZ-almaty-medium.onnx.json || true

echo "=== Kazakh Whisper large-v3-turbo (файнтюн shyngys879, ~1.6GB) ==="
echo "Скачивание safetensors -> конвертация в формат CTranslate2 (int8_float16)."
echo "Модель оптимизирована под казахский; русский работает через base-архитектуру."
# faster-whisper требует веса в формате CTranslate2 (model.bin), а в HF лежит
# Transformers/Safetensors. ct2-transformers-converter тянет репо и конвертирует.
if [ ! -f models/whisper/whisper-large-v3-turbo-FT-kzru/model.bin ]; then
  ct2-transformers-converter \
    --model shyngys879/kazakh-whisper-large-v3-turbo \
    --output_dir models/whisper/whisper-large-v3-turbo-FT-kzru \
    --quantization int8_float16
fi

echo "Done."
