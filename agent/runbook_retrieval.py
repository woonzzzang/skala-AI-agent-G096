"""Small-corpus Runbook retrieval; authoritative contents still come from MCP."""

from pathlib import Path
from typing import Any

RUNBOOK_DIR = Path(__file__).resolve().parent.parent / "data" / "runbooks"


class RunbookRetriever:
    def __init__(self, directory: Path = RUNBOOK_DIR, min_score: float = 0.55,
                 top_k: int = 3):
        self.directory = directory
        self.min_score = min_score
        self.top_k = top_k

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
        catalog = self.catalog()
        monitoring = [
            f"{item['data'].get('system_name', '')}: "
            f"{item['data'].get('status', '')}; "
            f"alerts={item['data'].get('alerts', [])}"
            for item in collected if item.get("source") == "monitoring"
            and isinstance(item.get("data"), dict)
        ]
        search_query = query + "\n" + " | ".join(monitoring)
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
