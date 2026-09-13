"""Индексация НК РК 2026: текст -> Qdrant (векторы) + articles_meta (Postgres).

Запуск:
  python tax_code_ingest.py --input nk_2026_kz.txt --lang kk --rag-url http://localhost:8093
  python tax_code_ingest.py --input nk_2026_ru.txt --lang ru --rag-url http://localhost:8093

Опции:
  --db-url      PostgreSQL для articles_meta (по умолчанию из env AUDIT_DB_URL)
  --collection  имя коллекции (по умолчанию tax_code_2026)
  --dim         размерность вектора (1024 для BGE-M3, 4096 для Qwen3-Emb-8B)
"""
import argparse
import asyncio
import json
import os
import sys

import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from chunker import build_chunks, parse_tax_code  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--lang", default="kk", choices=["ru", "kk"])
    ap.add_argument("--rag-url", default="http://localhost:8093")
    ap.add_argument("--db-url", default=os.environ.get("AUDIT_DB_URL", ""))
    ap.add_argument("--collection", default="tax_code_2026")
    ap.add_argument("--dim", type=int, default=1024)
    ap.add_argument("--long-article-split", type=int, default=3000)
    args = ap.parse_args()

    with open(args.input, encoding="utf-8") as f:
        raw = f.read()

    articles = parse_tax_code(raw, args.lang)
    print(f"Parsed articles: {len(articles)}")
    chunks = build_chunks(articles, args.lang, args.long_article_split)
    print(f"Chunks: {len(chunks)}")

    # 1. Векторизация через rag-service
    vectors = []
    batch_size = 16
    for i in range(0, len(chunks), batch_size):
        batch = [c["text"] for c in chunks[i : i + batch_size]]
        r = httpx.post(
            f"{args.rag_url.rstrip('/')}/embed",
            json={"texts": batch, "type": "document"},
            timeout=300,
        )
        r.raise_for_status()
        vectors.extend(r.json()["vectors"])
        print(f"  embedded {min(i + batch_size, len(chunks))}/{len(chunks)}")

    # 2. Запись в Qdrant
    from qdrant_client import QdrantClient

    qd = QdrantClient(url=os.environ.get("QDRANT_URL", "http://localhost:6333"),
                      api_key=os.environ.get("QDRANT_API_KEY") or None)
    if not qd.collection_exists(args.collection):
        from qdrant_client.models import Distance, VectorParams

        qd.create_collection(
            args.collection,
            vectors_config=VectorParams(size=args.dim, distance=Distance.COSINE),
        )
        print(f"Collection {args.collection} created (dim={args.dim})")

    points = []
    for c, v in zip(chunks, vectors):
        from qdrant_client.models import PointStruct

        points.append(
            PointStruct(
                id=c["id"],
                vector=v,
                payload={
                    "article_number": c["article_number"],
                    "chapter_number": c["chapter_number"],
                    "chapter_title": c["chapter_title"],
                    "title": c["title"],
                    "text": c["text"],
                    "chapter_path": c["chapter_path"],
                    "cross_references": c["cross_references"],
                    "lang": c["lang"],
                    "chunk_type": c["chunk_type"],
                    "parent_article": c["parent_article"],
                    "effective_date": "2026-01-01",
                },
            )
        )
    for i in range(0, len(points), 64):
        qd.upsert(args.collection, points=points[i : i + 64])
    print(f"Qdrant upserted: {len(points)}")

    # 3. articles_meta в Postgres (для валидации цитат)
    if args.db_url:
        import asyncpg

        async def _write_meta():
            conn = await asyncpg.connect(args.db_url)
            try:
                for a in articles:
                    cross = build_chunks([a], args.lang)[0]["cross_references"]
                    await conn.execute(
                        """INSERT INTO articles_meta
                           (article_number, chapter_number, chapter_title, title, effective_date, cross_refs, lang)
                           VALUES ($1,$2,$3,$4,'2026-01-01',$5,$6)
                           ON CONFLICT (article_number) DO UPDATE SET
                             chapter_number=EXCLUDED.chapter_number,
                             chapter_title=EXCLUDED.chapter_title,
                             title=EXCLUDED.title,
                             cross_refs=EXCLUDED.cross_refs""",
                        a.number,
                        a.chapter[0] if a.chapter else "",
                        a.chapter[1] if a.chapter else None,
                        a.title,
                        cross,
                        args.lang,
                    )
            finally:
                await conn.close()

        asyncio.run(_write_meta())
        print(f"articles_meta upserted: {len(articles)}")
    else:
        print("WARN: no --db-url, articles_meta skipped")


if __name__ == "__main__":
    main()
