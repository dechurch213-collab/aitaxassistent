"""Управление контекстным окном: потолок 32K токенов на сессию."""


class ContextWindow:
    def __init__(self, cfg: dict):
        c = cfg.get("session", {}).get("context", {})
        self.max_tokens = int(c.get("max_tokens", 32768))
        self.system_max = int(c.get("system_prompt_max_tokens", 500))
        self.rag_max = int(c.get("rag_block_max_tokens", 4096))
        self.reserve = int(c.get("history_reserve_tokens", 2048))
        self.keep_last = int(c.get("keep_last_turns_verbatim", 6))
        self.chars_per_token = float(c.get("chars_per_token", 2.0))

    def est_tokens(self, text: str) -> int:
        return int(len(text or "") / self.chars_per_token)

    def total_tokens(self, sys_text: str, rag_text: str, summary: str, history) -> int:
        t = (
            self.est_tokens(sys_text)
            + self.est_tokens(rag_text)
            + self.est_tokens(summary)
        )
        for m in history or []:
            t += self.est_tokens(m.get("content", ""))
        return t

    def should_compress(self, sys_text, rag_text, summary, history) -> bool:
        return self.total_tokens(sys_text, rag_text, summary, history) > (
            self.max_tokens - self.reserve
        )

    def build_history(self, history, budget_tokens: int) -> list:
        """История с конца: пока умещается в бюджет."""
        out = []
        budget = budget_tokens
        for m in reversed(history or []):
            t = self.est_tokens(m.get("content", ""))
            if t > budget:
                break
            out.append(m)
            budget -= t
        return list(reversed(out))
