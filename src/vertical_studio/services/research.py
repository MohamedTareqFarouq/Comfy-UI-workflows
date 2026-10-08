from __future__ import annotations

from typing import Any

import httpx


class ResearchUnavailable(RuntimeError):
    pass


class BraveResearchProvider:
    def __init__(self, api_key: str | None):
        self.api_key = api_key

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def search(self, query: str, count: int = 5) -> list[dict[str, Any]]:
        if not self.api_key:
            raise ResearchUnavailable(
                "BRAVE_API_KEY is not configured; provide source URLs before planning"
            )
        response = httpx.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": count, "safesearch": "moderate"},
            headers={"X-Subscription-Token": self.api_key, "Accept": "application/json"},
            timeout=20,
        )
        response.raise_for_status()
        results = response.json().get("web", {}).get("results", [])
        return [
            {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "description": item.get("description", ""),
            }
            for item in results
        ]
