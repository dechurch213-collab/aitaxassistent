class Embedder:
    """BGE-M3 или Qwen3-Embedding-8B через sentence-transformers."""

    def __init__(self, model_path: str, device: str, query_instruction: str):
        from sentence_transformers import SentenceTransformer

        self.model = SentenceTransformer(model_path, device=device)
        self.query_instruction = query_instruction or ""
        # BGE-M3 рекомендация: query instruction
        if not self.query_instruction and "bge" in model_path.lower():
            self.query_instruction = "Represent this query for retrieval: "

    def embed(self, texts: list, type: str) -> list:
        if type == "query" and self.query_instruction:
            texts = [self.query_instruction + t for t in texts]
        vectors = self.model.encode(
            list(texts),
            normalize_embeddings=True,
            batch_size=8,
            show_progress_bar=False,
        )
        return vectors.tolist()
