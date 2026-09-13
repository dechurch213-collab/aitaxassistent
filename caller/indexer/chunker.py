"""Парсер НК РК (2026): текст -> статьи с иерархией, cross-refs, под-чанками.

Ожидаемый формат входного текста (страницы, пустые строки не важны):

  РАЗДЕЛ 17. НАЛОГИ И СБОРЫ
  ГЛАВА 43. НАЛОГ НА ДОХОДЫ ФИЗИЧЕСКИХ ЛИЦ
  СТАТЬЯ 43. НАЛОГ НА ДОХОДЫ ФИЗИЧЕСКИХ ЛИЦ (ИПН)
  1. text paragraph...
  2. text paragraph...

Поддерживаются заголовки на казахском: BÖLIM, ТАРАУ, БАП (и варианты).
"""
import re

SECTION_RE = re.compile(
    r"^\s*(?:РАЗДЕЛ|BÖLIM|БӨЛІМ|БОЛІМ)\s*\.?\s*(\d+)\.?\s*(.*?)\s*$", re.IGNORECASE
)
CHAPTER_RE = re.compile(
    r"^\s*(?:ГЛАВА|ТАРАУ)\s*\.?\s*(\d+)\.?\s*(.*?)\s*$", re.IGNORECASE
)
ARTICLE_RE = re.compile(
    r"^\s*(?:СТАТЬЯ|СТАТ'Я|БАП)\s*\.?\s*(\d{1,4}(?:-\d{1,4})*)\.?\s*(.*?)\s*$", re.IGNORECASE
)
XREF_RE = re.compile(
    r"(?:статья|статьи|статье|статью|статті|бап|бапы|бабы|бапта)\s*(\d{1,4}(?:-\d{1,4})?)",
    re.IGNORECASE,
)
NUMBERED_PARA_RE = re.compile(r"^\s*\d{1,3}\.\s")


class ArticleDoc:
    def __init__(self, number, title, text, section, chapter, chapter_path):
        self.number = number
        self.title = title
        self.text = text
        self.section = section  # (number, title) or None
        self.chapter = chapter  # (number, title) or None
        self.chapter_path = chapter_path

    @property
    def word_count(self):
        return len(self.text.split())


def parse_tax_code(text: str, lang: str = "ru") -> list:
    section = None
    chapter = None
    chapter_path = None
    cur = None
    lines_out = []

    for line in text.splitlines():
        m = SECTION_RE.match(line)
        if m:
            section = (m.group(1), m.group(2) or "")
            chapter = None
            chapter_path = f"Раздел {section[0]}"
            continue
        m = CHAPTER_RE.match(line)
        if m:
            chapter = (m.group(1), m.group(2) or "")
            chapter_path = f"Раздел {section[0]} / Глава {chapter[0]}" if section else f"Глава {chapter[0]}"
            continue
        m = ARTICLE_RE.match(line)
        if m:
            cur = ArticleDoc(m.group(1), m.group(2) or "", "", section, chapter, chapter_path)
            lines_out.append(cur)
            continue
        if cur is not None and line.strip():
            cur.text = (cur.text + " " + line.strip()) if cur.text else line.strip()

    for a in lines_out:
        a.text = a.text.strip()
    return [a for a in lines_out if a.text]


def build_chunks(articles: list, lang: str, long_threshold: int = 3000) -> list:
    """1 статья = 1 точка; длинные статьи -> section-под-чанки с parent_article."""
    chunks = []
    for a in articles:
        cross_refs = sorted(
            set(XREF_RE.findall(a.text)) - {a.number}, key=_xref_sort_key
        )
        if a.word_count <= long_threshold:
            chunks.append(
                {
                    "id": f"art-{a.number}",
                    "text": a.text,
                    "article_number": a.number,
                    "chapter_number": a.chapter[0] if a.chapter else "",
                    "chapter_title": a.chapter[1] if a.chapter else None,
                    "title": a.title,
                    "chapter_path": a.chapter_path,
                    "cross_references": cross_refs,
                    "lang": lang,
                    "chunk_type": "article",
                    "parent_article": None,
                }
            )
            continue
        # длинные: статья целиком + под-чанки по параграфам (N.)
        chunks.append(
            {
                "id": f"art-{a.number}",
                "text": a.text,
                "article_number": a.number,
                "chapter_number": a.chapter[0] if a.chapter else "",
                "chapter_title": a.chapter[1] if a.chapter else None,
                "title": a.title,
                "chapter_path": a.chapter_path,
                "cross_references": cross_refs,
                "lang": lang,
                "chunk_type": "article",
                "parent_article": None,
            }
        )
        parts = _split_numbered_paragraphs(a.text)
        for i, part in enumerate(parts):
            if len(part.split()) < 30:
                continue
            chunks.append(
                {
                    "id": f"art-{a.number}-{i:02d}",
                    "text": part,
                    "article_number": a.number,
                    "chapter_number": a.chapter[0] if a.chapter else "",
                    "chapter_title": a.chapter[1] if a.chapter else None,
                    "title": a.title,
                    "chapter_path": a.chapter_path,
                    "cross_references": cross_refs,
                    "lang": lang,
                    "chunk_type": "section",
                    "parent_article": a.number,
                }
            )
    return chunks


def _split_numbered_paragraphs(text: str) -> list:
    out = []
    buf = ""
    for seg in text.split(". "):
        if re.match(r"^\d{1,3}\.\s", seg + ". "):
            if buf:
                out.append(buf)
            buf = seg + ". "
        else:
            buf = (buf + " " + seg + ". ").strip()
    if buf:
        out.append(buf)
    return [p.strip() for p in out if p.strip()]


def _xref_sort_key(x: str):
    parts = x.split("-")
    return (int(parts[0]), int(parts[1]) if len(parts) > 1 else 0)
