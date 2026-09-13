"""Валидация номеров статей: программная проверка на существование в БД."""
import logging
import re

import httpx

log = logging.getLogger("validator")

ARTICLE_RE = re.compile(
    r"(?:статья|статьи|статье|статью|статті|бап|бапы|бабы|бапта)\s+(\d{1,4}(?:-\d{1,4})?)",
    re.IGNORECASE,
)


class ArticleValidator:
    def __init__(self, cfg: dict):
        self.rag_url = cfg["services"]["rag"].rstrip("/")
        self.articles: set = set()

    async def refresh(self):
        async with httpx.AsyncClient(timeout=15) as cs:
            r = await cs.get(f"{self.rag_url}/articles")
            r.raise_for_status()
            self.articles = set(r.json().get("article_numbers", []))
        log.info("articles refreshed: %d", len(self.articles))

    def extract(self, text: str) -> list:
        return ARTICLE_RE.findall(text or "")

    def validate(self, cited: list) -> tuple:
        """(ok, missing). Пустой набор статей в БД = пропускаем (индекс ещё не построен)."""
        if not self.articles:
            log.warning("articles meta not loaded — skipping validation")
            return True, []
        missing = [c for c in cited if c not in self.articles]
        return len(missing) == 0, missing
