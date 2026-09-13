"""Тесты контрактов: примеры -> JSON Schema валидация + логики оркестратора/индексатора."""
import json
import os
import sys

import jsonschema
from jsonschema import Draft7Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT7

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
SCHEMAS = os.path.join(ROOT, "shared", "schemas")

_registry = None


def _schema(name):
    with open(os.path.join(SCHEMAS, name), encoding="utf-8") as f:
        return json.load(f)


def _validate(sample, name, refs=()):
    global _registry
    if _registry is None:
        resources = []
        for fn in os.listdir(SCHEMAS):
            if fn.endswith(".schema.json"):
                res = Resource.from_contents(
                    _schema(fn), default_specification=DRAFT7
                )
                resources.append((f"urn:schema:{fn}", res))
                resources.append((fn, res))  # относительные $ref внутри схем
        _registry = Registry().with_resources(resources)
    schema = _schema(name)
    Draft7Validator(schema, registry=_registry).validate(sample)


def test_stt_result():
    sample = {
        "session_id": "ch-1",
        "turn_id": 1,
        "language": "ru",
        "language_confidence": 0.97,
        "text": "Как оплатить ИПН?",
        "confidence": 0.84,
        "no_speech_prob": 0.05,
        "words": [{"w": "Как", "c": 0.99, "t0": 0.0, "t1": 0.2}],
        "duration_ms": 4200,
        "model": "whisper-ft",
    }
    _validate(sample, "stt_result.schema.json")


def test_orch_events():
    sample = {
        "event": "utterance_final",
        "session_id": "ch-1",
        "turn_id": 1,
        "payload": {
            "session_id": "ch-1",
            "turn_id": 1,
            "language": "ru",
            "text": "t",
            "confidence": 0.9,
        },
    }
    _validate(sample, "orch_events.schema.json")
    _validate(
        {"event": "call_started", "session_id": "ch-1",
         "payload": {"caller": "7700", "uniqueid": "ch-1", "codec": "L16-8k"}},
        "orch_events.schema.json",
    )


def test_orch_commands():
    _validate(
        {
            "command": "speak",
            "session_id": "ch-1",
            "data": {"text": "привет", "lang": "ru", "interrupt": True},
        },
        "orch_commands.schema.json",
    )
    _validate(
        {
            "command": "transfer",
            "session_id": "ch-1",
            "data": {"reason": "rag_no_match", "queue": "OPERATOR_QUEUE"},
        },
        "orch_commands.schema.json",
    )


def test_rag_contracts():
    _validate(
        {
            "session_id": "ch-1",
            "query": "q",
            "language": "ru",
            "top_n_vector": 20,
            "top_n_final": 4,
            "exclude_articles": ["43"],
        },
        "rag_request.schema.json",
    )
    sample = {
        "session_id": "ch-1",
        "query": "q",
        "matched": True,
        "results": [
            {
                "doc_id": "art-259-001",
                "article_number": "259-1",
                "chapter_number": "259",
                "text": "text",
                "vector_score": 0.6,
                "rerank_score": 0.8,
                "cross_references": ["259-2"],
            }
        ],
        "retrieval_meta": {"vector_hits": 20, "embed_ms": 40, "search_ms": 30, "rerank_ms": 600},
    }
    _validate(sample, "rag_response.schema.json")


def test_llm_answer():
    _validate(
        {
            "answer_text": "Согласно статье 259...",
            "articles_cited": ["259"],
            "confidence": 0.93,
            "escalation_suggested": False,
            "clarification_question": None,
        },
        "llm_gateway_response.schema.json",
    )


def test_chunker():
    sys.path.insert(0, os.path.join(ROOT, "indexer"))
    from chunker import build_chunks, parse_tax_code

    sample_text = """
РАЗДЕЛ 17. НАЛОГИ
ГЛАВА 259. НДС
СТАТЬЯ 259. ОБЩИЕ ПОЛОЖЕНИЯ
1. НДС взимается с реализации товаров.
2. Налоговый кредит регулируется пунктом 3 статьи 259-3.
СТАТЬЯ 259-3. НАЛОГОВЫЙ КРЕДИТ
1. Налоговый кредит предоставляется.
"""
    arts = parse_tax_code(sample_text, "ru")
    assert len(arts) == 2, arts
    assert arts[0].number == "259"
    assert arts[0].chapter[0] == "259"

    chunks = build_chunks(arts, "ru")
    art_chunks = [c for c in chunks if c["chunk_type"] == "article"]
    assert len(art_chunks) == 2
    a259 = [c for c in art_chunks if c["article_number"] == "259"][0]
    assert "259-3" in a259["cross_references"]


def test_policies():
    sys.path.insert(0, os.path.join(ROOT, "services", "orchestrator"))
    from policies import (
        is_policy_dispute,
        is_simple_explain_request,
        is_substantive_question,
    )

    assert is_simple_explain_request("объясни проще, пожалуйста")
    assert not is_simple_explain_request("как оплатить налог?")
    assert is_substantive_question("Сколько платить НДС?")
    assert not is_substantive_question("да")
    assert is_policy_dispute("Мне пришло уведомление о доначислении", ["доначислен"])
    assert not is_policy_dispute("как подать декларацию", ["доначислен"])


def test_validator_extract():
    sys.path.insert(0, os.path.join(ROOT, "services", "orchestrator"))
    from validator import ArticleValidator

    v = ArticleValidator({"services": {"rag": "http://x"}})
    assert v.extract("Согласно статье 259 и пункту 1 статьи 259-3") == ["259", "259-3"]
    assert v.extract("Сәйкес бап 43 бойынша") == ["43"]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"OK {fn.__name__}")
    print("ALL TESTS PASSED")
