import os
import re

import yaml

ENV_RE = re.compile(r"\$\{(\w+)\}")


def _expand_env(obj):
    if isinstance(obj, dict):
        return {k: _expand_env(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_env(v) for v in obj]
    if isinstance(obj, str):
        return ENV_RE.sub(lambda m: os.environ.get(m.group(1), ""), obj)
    return obj


def load() -> dict:
    with open(os.environ.get("CONFIG_FILE", "/etc/caller/media-gateway.yaml"), encoding="utf-8") as f:
        return _expand_env(yaml.safe_load(f))
