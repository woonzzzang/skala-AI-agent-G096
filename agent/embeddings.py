"""Small-corpus retrieval embedding providers (no vector database)."""

import os

import httpx
from langsmith import traceable


class OpenAIEmbedder:
    def __init__(self, model: str = "text-embedding-3-small"):
        self.model = model
        self.api_key = os.environ.get("OPENAI_API_KEY", "")

    @traceable(run_type="embedding", name="openai.embed_runbooks")
    def embed(self, texts: list[str]) -> tuple[list[list[float]], dict]:
        if not texts:
            return [], {"tokens": 0, "usd": 0.0, "requests": 0}
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY가 설정되지 않았습니다.")
        response = httpx.post(
            "https://api.openai.com/v1/embeddings",
            json={"model": self.model, "input": texts, "encoding_format": "float"},
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=60.0,
        )
        response.raise_for_status()
        data = response.json()
        indexed = {item["index"]: item["embedding"] for item in data["data"]}
        if set(indexed) != set(range(len(texts))):
            raise ValueError("Embedding 응답의 문서 인덱스가 요청과 다릅니다.")
        tokens = data.get("usage", {}).get("total_tokens") or 0
        # 앱의 cost_usd와 마찬가지로 추정치다. 다른 모델은 별도 단가 설정이 필요하다.
        per_million = float(os.environ.get("OPENAI_EMBEDDING_USD_PER_1M", "0.02"))
        return [indexed[i] for i in range(len(texts))], {
            "model": self.model, "tokens": tokens,
            "usd": round(tokens / 1_000_000 * per_million, 8), "requests": 1,
        }


class OllamaEmbedder:
    def __init__(self, model: str = "embeddinggemma"):
        self.model = model

    @traceable(run_type="embedding", name="ollama.embed_runbooks")
    def embed(self, texts: list[str]) -> tuple[list[list[float]], dict]:
        if not texts:
            return [], {"tokens": 0, "usd": 0.0, "requests": 0}
        response = httpx.post(
            "http://127.0.0.1:11434/api/embed",
            json={"model": self.model, "input": texts, "truncate": False},
            timeout=120.0,
        )
        response.raise_for_status()
        data = response.json()
        vectors = data["embeddings"]
        if len(vectors) != len(texts):
            raise ValueError("Embedding 응답의 벡터 수가 요청과 다릅니다.")
        return vectors, {"model": self.model,
                         "tokens": data.get("prompt_eval_count") or 0,
                         "usd": 0.0, "requests": 1}
