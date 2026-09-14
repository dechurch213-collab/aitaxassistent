from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchAny


class QdrantStore:
    def __init__(self, url: str, api_key, collection: str):
        kwargs = {"url": url}
        if api_key:
            kwargs["api_key"] = api_key
        self.client = QdrantClient(**kwargs)
        self.collection = collection

    def search(self, vector, limit: int, exclude_articles: list | None = None) -> list:
        qf = None
        if exclude_articles:
            qf = Filter(
                must_not=[
                    FieldCondition(
                        key="article_number", match=MatchAny(any=exclude_articles)
                    )
                ]
            )
        res = self.client.query_points(
            collection_name=self.collection,
            query=vector,
            limit=limit,
            query_filter=qf,
            with_payload=True,
        )
        return [
            {"id": str(p.id), "score": float(p.score), "payload": p.payload}
            for p in res.points
        ]

    def get_article_numbers(self) -> list:
        nums = set()
        next_offset = None
        while True:
            points, next_offset = self.client.scroll(
                self.collection,
                limit=256,
                offset=next_offset,
                with_payload=True,
                with_vectors=False,
            )
            for p in points:
                if p.payload.get("chunk_type", "article") == "article":
                    nums.add(p.payload["article_number"])
            if next_offset is None:
                break
        return sorted(nums)

    def get_article_text(self, article_number: str) -> str | None:
        if not article_number:
            return None
        points, _ = self.client.scroll(
            self.collection,
            limit=1,
            scroll_filter=Filter(
                must=[
                    FieldCondition(
                        key="article_number",
                        match=MatchAny(any=[article_number]),
                    )
                ]
            ),
            with_payload=True,
            with_vectors=False,
        )
        if not points:
            return None
        return points[0].payload.get("text")
