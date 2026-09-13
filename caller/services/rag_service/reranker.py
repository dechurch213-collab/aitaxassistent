class Qwen3Reranker:
    """Qwen3-Reranker (0.6B/4B/8B): cross-encoder, sigmoid(logit) = score."""

    def __init__(self, model_path: str, device: str, max_length: int, instruction: str):
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        self.torch = torch
        self.device = device
        self.max_length = max_length
        self.instruction = instruction
        self.tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
        self.model = (
            AutoModelForSequenceClassification.from_pretrained(
                model_path, trust_remote_code=True, num_labels=1
            )
            .to(device)
            .eval()
        )

    def score(self, query: str, docs: list) -> list:
        if not docs:
            return []
        pairs = [
            f"{self.instruction} Query: {query} Document: {d}" for d in docs
        ]
        inputs = self.tokenizer(
            pairs,
            padding=True,
            truncation=True,
            max_length=self.max_length,
            return_tensors="pt",
        ).to(self.device)
        with self.torch.no_grad():
            logits = self.model(**inputs).logits
            scores = self.torch.sigmoid(logits).squeeze(-1)
        return scores.cpu().tolist()
