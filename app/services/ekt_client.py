import asyncio
import re
import time
from typing import Any

import httpx

from app.config import Settings
from app.models import ProductDetail, ProductSummary


class EktApiError(RuntimeError):
    pass


class EktClient:
    """Read-only adapter for the partner catalog API."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._page_cache: dict[int, tuple[float, dict[str, Any]]] = {}
        self._detail_cache: dict[int, tuple[float, ProductDetail]] = {}
        self.cache_ttl = 300

    def _auth(self) -> httpx.BasicAuth:
        return httpx.BasicAuth(self.settings.ekt_api_username, self.settings.ekt_api_password)

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.settings.ekt_api_password:
            raise EktApiError("EKT_API_PASSWORD is not configured")
        try:
            async with httpx.AsyncClient(
                base_url=self.settings.ekt_api_base_url.rstrip("/"),
                auth=self._auth(),
                timeout=httpx.Timeout(15.0),
                follow_redirects=True,
            ) as client:
                response = await client.get(path, params=params)
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise EktApiError("Unexpected catalog response")
                return payload
        except (httpx.HTTPError, ValueError) as exc:
            raise EktApiError(f"Catalog API is unavailable: {exc}") from exc

    async def list_products(self, page: int = 1) -> dict[str, Any]:
        cached = self._page_cache.get(page)
        if cached and time.monotonic() - cached[0] < self.cache_ttl:
            return cached[1]
        payload = await self._get("/products", {"page": page})
        self._page_cache[page] = (time.monotonic(), payload)
        return payload

    async def get_product(self, product_id: int) -> ProductDetail:
        cached = self._detail_cache.get(product_id)
        if cached and time.monotonic() - cached[0] < self.cache_ttl:
            return cached[1]
        payload = await self._get("/products/detail", {"id": product_id})
        product = ProductDetail.model_validate(payload)
        self._detail_cache[product_id] = (time.monotonic(), product)
        return product

    async def search_products(self, query: str, limit: int = 5) -> list[ProductSummary]:
        normalized = self._normalize(query)
        numeric_id = self._extract_id(query)
        if numeric_id:
            try:
                detail = await self.get_product(numeric_id)
                return [ProductSummary.model_validate(detail.model_dump())]
            except Exception:
                pass

        pages = range(1, self.settings.catalog_search_max_pages + 1)
        payloads = await asyncio.gather(*(self.list_products(page) for page in pages), return_exceptions=True)
        scored: list[tuple[float, ProductSummary]] = []
        terms = set(normalized.split())
        for payload in payloads:
            if isinstance(payload, Exception):
                continue
            for raw in payload.get("items", []):
                product = ProductSummary.model_validate(raw)
                haystack = self._normalize(f"{product.name} {product.article} {product.id}")
                if normalized and normalized in haystack:
                    score = 100 + len(normalized)
                else:
                    words = set(haystack.split())
                    score = 10 * len(terms & words)
                    score += sum(2 for term in terms if len(term) > 3 and term in haystack)
                if score > 0:
                    scored.append((score, product))
        scored.sort(key=lambda pair: (-pair[0], pair[1].price))
        unique: dict[int, ProductSummary] = {}
        for _, product in scored:
            unique.setdefault(product.id, product)
        return list(unique.values())[:limit]

    async def find_analogs(self, product_id: int, limit: int = 3) -> list[dict[str, Any]]:
        source = await self.get_product(product_id)
        recommendations = source.properties.get("RECOMMEND", [])
        candidates: list[ProductDetail] = []
        for raw_id in recommendations[:8] if isinstance(recommendations, list) else []:
            try:
                candidate = await self.get_product(int(raw_id))
                if candidate.quantity > 0:
                    candidates.append(candidate)
            except Exception:
                continue

        if len(candidates) < limit:
            search_seed = " ".join(source.name.split()[:5])
            for summary in await self.search_products(search_seed, limit=12):
                if summary.id == product_id or any(item.id == summary.id for item in candidates):
                    continue
                try:
                    candidate = await self.get_product(summary.id)
                    if candidate.quantity > 0:
                        candidates.append(candidate)
                except Exception:
                    continue
                if len(candidates) >= limit:
                    break

        result = []
        source_props = {k: str(v) for k, v in source.properties.items() if self._is_characteristic(k, v)}
        for candidate in candidates[:limit]:
            matching = []
            differences = []
            for key, source_value in source_props.items():
                candidate_value = candidate.properties.get(key)
                if candidate_value is None:
                    continue
                if str(candidate_value).casefold() == source_value.casefold():
                    matching.append({"property": key, "value": source_value})
                elif len(differences) < 3:
                    differences.append({"property": key, "requested": source_value,
                                        "alternative": str(candidate_value)})
            result.append({**candidate.model_dump(), "matching_characteristics": matching[:5],
                           "differences": differences})
        return result

    @staticmethod
    def _normalize(value: str) -> str:
        return re.sub(r"[^a-zа-яәіңғүұқөһ0-9]+", " ", value.casefold()).strip()

    @staticmethod
    def _extract_id(value: str) -> int | None:
        match = re.search(r"(?:id\s*[=:]?\s*)?(\d{5,})", value.casefold())
        return int(match.group(1)) if match else None

    @staticmethod
    def _is_characteristic(key: str, value: Any) -> bool:
        ignored = {"RECOMMEND", "CML2_TRAITS", "IMYAKARTINKI", "BRAND_PRIORITY"}
        return key not in ignored and isinstance(value, (str, int, float))
