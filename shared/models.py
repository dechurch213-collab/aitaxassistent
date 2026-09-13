"""Pydantic-модели по shared/schemas/*.json. Используются всеми сервисами."""
from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field


# C1: STT
class SttWord(BaseModel):
    w: str
    c: float
    t0: Optional[float] = None
    t1: Optional[float] = None


class SttResult(BaseModel):
    session_id: str
    turn_id: int
    language: Literal["ru", "kk", "unknown"]
    language_confidence: float = 0.0
    text: str
    confidence: float
    no_speech_prob: float = 0.0
    words: List[SttWord] = []
    duration_ms: int = 0
    model: Optional[str] = None


# C2: события media-gateway -> orchestrator
class OrchEvent(BaseModel):
    event: Literal[
        "utterance_final", "call_started", "call_ended", "dtmf", "gateway_reconnected"
    ]
    session_id: str
    turn_id: Optional[int] = None
    payload: Optional[dict] = None


# C3: команды orchestrator -> media-gateway
class OrchCommand(BaseModel):
    command: Literal[
        "speak", "clear_queue", "transfer", "hangup", "play_tone", "set_state"
    ]
    session_id: str
    data: Optional[dict] = None


# C4: RAG
class RagQueryResult(BaseModel):
    doc_id: str
    article_number: str
    chapter_number: str
    chapter_title: Optional[str] = None
    article_title: Optional[str] = None
    text: str
    vector_score: Optional[float] = None
    rerank_score: float
    cross_references: List[str] = []
    chapter_path: Optional[str] = None
    chunk_type: Literal["article", "section"] = "article"


class RagRetrievalRequest(BaseModel):
    session_id: str
    query: str
    language: Literal["ru", "kk"]
    top_n_vector: int = 20
    top_n_final: int = 4
    exclude_articles: List[str] = []
    hints: Optional[dict] = None


class RetrievalMeta(BaseModel):
    vector_hits: int
    embed_ms: int
    search_ms: int
    rerank_ms: int


class RagRetrievalResponse(BaseModel):
    session_id: str
    query: str
    matched: bool
    results: List[RagQueryResult]
    retrieval_meta: RetrievalMeta


# C6: LLM gateway
class LlmChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class LlmChatRequest(BaseModel):
    model: str
    messages: List[LlmChatMessage]
    stream: bool = True
    temperature: float = 0.1
    max_tokens: Optional[int] = None
    response_format: Optional[dict] = None
    x_session_id: Optional[str] = None
    x_cache_keys: Optional[dict] = None


class LlmAnswer(BaseModel):
    answer_text: str
    articles_cited: List[str] = []
    confidence: float = 0.0
    escalation_suggested: bool = False
    clarification_question: Optional[str] = None


# C7: TTS
class TtsRequest(BaseModel):
    session_id: str
    text: str
    language: Literal["ru", "kk"]
    voice_id: Optional[str] = None
    speed: float = 1.0
