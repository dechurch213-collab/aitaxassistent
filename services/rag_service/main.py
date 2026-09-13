import os
import time

import yaml
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI()

CONFIG_PATH = os.environ.get("CONFIG_FILE", "/etc/aitaxassistent/rag.yaml")
CFG: dict = {}
embedder = None
reranker = None
store = None


class EmbedRequest(BaseModel):
    texts: list
    type: str = "document"  # document | query


class RetrievalRequest(BaseModel):
    session_id: str
    query: str
    language: str
    top_n_vector: int = 20
    top_n_final: int = 4
    exclude_articles: list = []
    hints: dict | None = None


@app.on_event("startup")
def _startup():
    global CFG, embedder, reranker, store
    with open(CONFIG_PATH, encoding="utf-8") as f:
        CFG = yaml.safe_load(f)
    from embedder import Embedder
    from qdrant_client import QdrantStore
    from reranker import Qwen3Reranker

    e = CFG.get("embedding", {})
    embedder = Embedder(
        e.get("model", "/models/bge-m3"),
        e.get("device", "cpu"),
        e.get("query_instruction", ""),
    )
    r = CFG.get("reranker", {})
    reranker = Qwen3Reranker(
        r.get("model_path", "/models/reranker"),
        r.get("device", "cpu"),
        int(r.get("max_length", 512)),
        r.get(
            "instruction",
            "Rank relevant articles of the Tax Code of the Republic of Kazakhstan (2026) for the given query.",
        ),
    )
    q = CFG.get("qdrant", {})
    store = QdrantStore(
        q.get("url", "http://qdrant:6333"),
        os.environ.get("QDRANT_API_KEY"),
        q.get("collection", "tax_code_2026"),
    )


@app.get("/health")
def health():
    return {
        "status": "ok",
        "embedder": embedder is not None,
        "reranker": reranker is not None,
    }


@app.post("/embed")
def embed(req: EmbedRequest):
    if embedder is None:
        raise HTTPException(503, "not ready")
    return {"vectors": embedder.embed(req.texts, req.type)}


@app.get("/articles")
def articles():
    if store is None:
        raise HTTPException(503, "not ready")
    return {"article_numbers": store.get_article_numbers()}


@app.post("/retrieval")
def retrieval(req: RetrievalRequest):
    if None in (embedder, reranker, store):
        raise HTTPException(503, "not ready")

    t0 = time.perf_counter()
    qv = embedder.embed([req.query], "query")[0]
    t_embed = int((time.perf_counter() - t0) * 1000)

    hits = store.search(qv, req.top_n_vector, req.exclude_articles)
    t_search = int((time.perf_counter() - t0) * 1000) - t_embed

    if not hits:
        return {
            "session_id": req.session_id,
            "query": req.query,
            "matched": False,
            "results": [],
            "retrieval_meta": {
                "vector_hits": 0,
                "embed_ms": t_embed,
                "search_ms": max(0, t_search),
                "rerank_ms": 0,
            },
        }

    scores = reranker.score(req.query, [h["payload"]["text"] for h in hits])
    t_rerank = int((time.perf_counter() - t0) * 1000) - t_embed - t_search

    ranked = sorted(zip(hits, scores), key=lambda x: x[1], reverse=True)
    ranked = ranked[: req.top_n_final]
    threshold = float(CFG.get("retrieval", {}).get("min_rerank_score", 0.30))

    # слияние section-чанков в уровень статьи: берём лучшую статью, текст — статьи целиком
    results = []
    seen = {}
    for h, s in ranked:
        p = h["payload"]
        key = p.get("article_number", h["id"])
        if key in seen:
            continue
        seen[key] = True
        article_text = p.get("text", "")
        if p.get("chunk_type") == "section":
            full = store.get_article_text(p.get("parent_article", key))
            if full:
                article_text = full
        results.append(
            {
                "doc_id": h["id"],
                "article_number": key,
                "chapter_number": p.get("chapter_number", ""),
                "chapter_title": p.get("chapter_title"),
                "article_title": p.get("title"),
                "text": article_text,
                "vector_score": round(float(h["score"]), 4),
                "rerank_score": round(float(s), 4),
                "cross_references": p.get("cross_references", []),
                "chapter_path": p.get("chapter_path"),
                "chunk_type": p.get("chunk_type", "article"),
            }
        )

    matched = bool(results) and results[0]["rerank_score"] >= threshold
    return {
        "session_id": req.session_id,
        "query": req.query,
        "matched": matched,
        "results": results,
        "retrieval_meta": {
            "vector_hits": len(hits),
            "embed_ms": t_embed,
            "search_ms": max(0, t_search),
            "rerank_ms": max(0, t_rerank),
        },
    }
