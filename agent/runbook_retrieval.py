"""Small-corpus Runbook retrieval; authoritative contents still come from MCP."""

import hashlib
import math
from pathlib import Path
from typing import Any

RUNBOOK_DIR = Path(__file__).resolve().parent.parent / "data" / "runbooks"


class RunbookRetriever:
    def __init__(self, directory: Path = RUNBOOK_DIR, min_score: float = 0.55,
                 top_k: int = 3, embedder: Any = None,
                 embedding_min_score: float = 0.35,
                 embedding_margin: float = 0.06):
        self.directory = directory
        self.min_score = min_score
        self.top_k = top_k
        self.embedder = embedder
        self.embedding_min_score = embedding_min_score
        self.embedding_margin = embedding_margin
        self._vectors: dict[str, tuple[str, list[float]]] = {}

    @property
    def mode(self) -> str:
        return "embedding" if self.embedder is not None else "llm"

    def catalog(self) -> list[dict[str, str]]:
        """Read current files so adding a Runbook needs no code or static registry edit."""
        docs = []
        for path in sorted(self.directory.glob("*.md")):
            body = path.read_text(encoding="utf-8")
            title = next((line.lstrip("# ") for line in body.splitlines()
                          if line.startswith("#")), path.stem)
            docs.append({"id": path.stem, "title": title, "excerpt": body[:1200]})
        return docs

    def search(self, query: str, collected: list[dict], ranker: Any
               ) -> tuple[list[dict], dict]:
        monitoring = [
            f"{item['data'].get('system_name', '')}: "
            f"{item['data'].get('status', '')}; "
            f"alerts={item['data'].get('alerts', [])}"
            for item in collected if item.get("source") == "monitoring"
            and isinstance(item.get("data"), dict)
        ]
        search_query = query + "\n" + " | ".join(monitoring)
        if self.embedder is not None:
            # 한 문장에 서로 다른 장애 증상을 섞으면 한 증상이 다른 증상을
            # 압도할 수 있다. 질문과 각 Monitoring 알림을 별도 검색 질의로 둔다.
            alerts = [str(alert) for item in collected
                      if item.get("source") == "monitoring"
                      and isinstance(item.get("data"), dict)
                      for alert in item["data"].get("alerts", [])
                      if str(alert).strip()]
            return self._embedding_search([query] + alerts[:10])
        catalog = self.catalog()
        raw, usage = ranker.rank_runbooks(search_query, catalog)
        allowed = {document["id"] for document in catalog}
        accepted: dict[str, dict] = {}
        for candidate in raw.get("matches", []) if isinstance(raw, dict) else []:
            if not isinstance(candidate, dict) or candidate.get("id") not in allowed:
                continue
            try:
                score = float(candidate.get("score", 0))
            except (TypeError, ValueError):
                continue
            if not self.min_score <= score <= 1.0:
                continue
            doc_id = candidate["id"]
            if doc_id not in accepted or score > accepted[doc_id]["score"]:
                accepted[doc_id] = {"id": doc_id, "score": score,
                                    "reason": str(candidate.get("reason", ""))}
        matches = sorted(accepted.values(), key=lambda item: (-item["score"], item["id"]))
        return matches[:self.top_k], usage

    @staticmethod
    def _cosine(left: list[float], right: list[float]) -> float:
        if len(left) != len(right) or not left:
            raise ValueError("Embedding 벡터 차원이 일치하지 않습니다.")
        if not all(math.isfinite(value) for value in left + right):
            raise ValueError("Embedding 벡터에 유한하지 않은 값이 있습니다.")
        norm = math.sqrt(sum(value * value for value in left)) * math.sqrt(
            sum(value * value for value in right))
        if not norm:
            raise ValueError("길이가 0인 Embedding 벡터입니다.")
        return sum(a * b for a, b in zip(left, right)) / norm

    def _embedding_search(self, search_queries: list[str]) -> tuple[list[dict], dict]:
        # 절차 본문에는 여러 Runbook에 공통인 표현이 많다. 의미 검색은 각 문서의
        # 간결한 제목으로 하고, 선택 후 본문 원문은 계속 MCP가 제공한다.
        # 본문 해시로 변경을 감지해 필요한 문서의 제목 벡터만 다시 만든다.
        documents = [(path.stem, path.read_text(encoding="utf-8"))
                     for path in sorted(self.directory.glob("*.md"))]
        if not documents:
            return [], {"tokens": 0, "usd": 0.0, "requests": 0}
        changed = []
        for doc_id, body in documents:
            digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
            if doc_id not in self._vectors or self._vectors[doc_id][0] != digest:
                changed.append((doc_id, body, digest))
        usage = {"tokens": 0, "usd": 0.0, "requests": 0}
        if changed:
            titles = [next((line for line in body.splitlines() if line.startswith("#")),
                           body[:300]) for _, body, _ in changed]
            vectors, spent = self.embedder.embed(titles)
            if len(vectors) != len(changed):
                raise ValueError("문서 Embedding 결과 수가 문서 수와 다릅니다.")
            if any(not isinstance(vector, list) or not vector for vector in vectors):
                raise ValueError("문서 Embedding 벡터가 유효하지 않습니다.")
            for (doc_id, _, digest), vector in zip(changed, vectors):
                self._vectors[doc_id] = (digest, vector)
            for key in usage:
                usage[key] += spent.get(key, 0)
        self._vectors = {doc_id: self._vectors[doc_id] for doc_id, _ in documents}
        query_vectors, spent = self.embedder.embed(search_queries)
        if len(query_vectors) != len(search_queries):
            raise ValueError("질문 Embedding 결과 수가 요청과 다릅니다.")
        for key in usage:
            usage[key] += spent.get(key, 0)
        accepted: dict[str, float] = {}
        for query_vector in query_vectors:
            scored = sorted(
                ((doc_id, self._cosine(query_vector, self._vectors[doc_id][1]))
                 for doc_id, _ in documents),
                key=lambda item: (-item[1], item[0]),
            )
            best = scored[0][1]
            for doc_id, score in scored:
                if score >= self.embedding_min_score and score >= best - self.embedding_margin:
                    accepted[doc_id] = max(score, accepted.get(doc_id, -1.0))
        # 코사인 점수는 LLM의 0.55 관련도와 의미가 다르므로 별도 임계값을 쓴다.
        return [
            {"id": doc_id, "score": round(score, 4),
             "reason": "질문 또는 Monitoring 알림과 Runbook 제목 간 임베딩 유사도"}
            for doc_id, score in sorted(accepted.items(),
                                        key=lambda item: (-item[1], item[0]))
        ][:self.top_k], usage
