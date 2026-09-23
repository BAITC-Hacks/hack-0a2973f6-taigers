import json
import re
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fastapi import UploadFile
from openai import AsyncOpenAI

from app.config import Settings
from app.models import CartItemRequest
from app.services.cart import DemoCartService
from app.services.ekt_client import EktClient
from app.services.files import extract_upload


EXTRACTION_TOOL = {
    "type": "function",
    "name": "extract_specification",
    "description": "Extract purchasable electrical products and requested quantities from a specification.",
    "strict": True,
    "parameters": {
        "type": "object",
        "properties": {
            "document_title": {"type": ["string", "null"]},
            "lines": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "source_text": {"type": "string"},
                        "requested_name": {"type": "string"},
                        "article": {"type": ["string", "null"]},
                        "quantity": {"type": "integer", "minimum": 1},
                        "unit": {"type": "string"},
                        "required_characteristics": {"type": "array", "items": {"type": "string"}},
                        "notes": {"type": ["string", "null"]},
                    },
                    "required": ["source_text", "requested_name", "article", "quantity", "unit",
                                 "required_characteristics", "notes"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["document_title", "lines"],
        "additionalProperties": False,
    },
}


class SpecError(ValueError):
    pass


class SpecService:
    """Turns an uploaded bill of materials into an auditable, confidence-gated cart draft."""

    def __init__(self, settings: Settings, ekt: EktClient, carts: DemoCartService):
        self.settings = settings
        self.ekt = ekt
        self.carts = carts
        self.client = AsyncOpenAI(api_key=settings.openai_api_key) if settings.openai_api_key else None
        self._analyses: dict[str, dict[str, Any]] = {}

    async def analyze_upload(self, file: UploadFile, instructions: str = "") -> dict[str, Any]:
        extracted = await extract_upload(file, self.settings)
        lines, title, extraction_mode = await self._extract_lines(
            text=extracted.get("text", ""),
            image_data_url=extracted.get("image_data_url"),
            filename=file.filename or "specification",
            instructions=instructions,
        )
        if not lines:
            raise SpecError("Не удалось распознать товарные позиции. Проверьте файл или добавьте пояснение.")
        resolved = []
        for line in lines[:60]:
            resolved.append(await self._resolve_line(line))
        analysis_id = str(uuid4())
        summary = self._summary(resolved)
        analysis = {
            "id": analysis_id,
            "filename": file.filename,
            "document_title": title,
            "created_at": datetime.now(UTC).isoformat(),
            "extraction_mode": extraction_mode,
            "summary": summary,
            "lines": resolved,
            "audit": {
                "catalog_source": self.settings.ekt_api_base_url,
                "catalog_checked_at": datetime.now(UTC).isoformat(),
                "policy": "Только позиции с высокой уверенностью и достаточным остатком выбраны автоматически.",
            },
        }
        self._analyses[analysis_id] = analysis
        return analysis

    async def prepare_cart(self, session_id: str, analysis_id: str, line_ids: list[str]) -> dict[str, Any]:
        analysis = self._analyses.get(analysis_id)
        if not analysis:
            raise SpecError("Анализ не найден или сервер был перезапущен. Загрузите спецификацию снова.")
        chosen = [line for line in analysis["lines"] if line["id"] in set(line_ids)]
        if not chosen:
            raise SpecError("Выберите хотя бы одну подтверждённую позицию.")
        unsafe = [line for line in chosen if not line.get("product") or line["status"] in {"not_found", "review"}]
        if unsafe:
            raise SpecError("Позиции с высоким риском требуют ручного выбора товара.")
        items = [CartItemRequest(product_id=line["product"]["id"], quantity=line["quantity"]) for line in chosen]
        pending = await self.carts.prepare(session_id, items)
        return pending.model_dump()

    async def _extract_lines(
        self, text: str, image_data_url: str | None, filename: str, instructions: str
    ) -> tuple[list[dict[str, Any]], str | None, str]:
        if self.client:
            content: list[dict[str, Any]] = [{
                "type": "input_text",
                "text": (
                    "Извлеки все товарные позиции из спецификации. Не придумывай артикулы. "
                    "Не считай заголовки, итоги, адреса и реквизиты товарами. "
                    f"Имя файла: {filename}. Пояснение пользователя: {instructions or 'нет'}.\n\n{text[:30000]}"
                ),
            }]
            if image_data_url:
                content.append({"type": "input_image", "image_url": image_data_url})
            response = await self.client.responses.create(
                model=self.settings.openai_model,
                input=[{"role": "user", "content": content}],
                tools=[EXTRACTION_TOOL],
                tool_choice={"type": "function", "name": "extract_specification"},
                reasoning={"effort": self.settings.openai_reasoning_effort},
                store=False,
            )
            call = next((item for item in response.output if item.type == "function_call"), None)
            if call:
                payload = json.loads(call.arguments)
                return payload["lines"], payload.get("document_title"), "openai"
        return self._fallback_extract(text), filename, "rules"

    async def _resolve_line(self, raw: dict[str, Any]) -> dict[str, Any]:
        requested_name = str(raw.get("requested_name", "")).strip()
        article = str(raw.get("article") or "").strip()
        quantity = max(1, int(raw.get("quantity") or 1))
        query = article or requested_name
        candidates = await self.ekt.search_products(query, limit=5)
        details = []
        for candidate in candidates[:3]:
            try:
                details.append(await self.ekt.get_product(candidate.id))
            except Exception:
                continue
        scored = sorted(
            ((self._confidence(requested_name, article, item.name, item.article, item.id), item) for item in details),
            key=lambda pair: pair[0], reverse=True,
        )
        line_id = str(uuid4())
        if not scored:
            return {"id": line_id, **raw, "quantity": quantity, "status": "not_found", "risk": "high",
                    "confidence": 0, "product": None, "alternatives": [], "fulfillment": None,
                    "selected_by_default": False, "reason": "Совпадений в проверенной части каталога нет."}

        confidence, product = scored[0]
        if confidence < 0.35:
            return {"id": line_id, **raw, "quantity": quantity, "status": "not_found", "risk": "high",
                    "confidence": round(confidence, 2), "product": None, "alternatives": [],
                    "fulfillment": None, "selected_by_default": False,
                    "reason": "Ни один кандидат не прошёл минимальный порог уверенности 35%."}
        status = "matched" if confidence >= 0.76 else "review"
        risk = "low" if confidence >= 0.88 else ("medium" if confidence >= 0.76 else "high")
        reason = "Совпали артикул или ключевые слова наименования."
        if product.quantity <= 0:
            status, risk, reason = "out_of_stock", "medium", "Товар найден, но его нет в наличии."
        elif product.quantity < quantity:
            status, risk, reason = "insufficient_stock", "medium", "Остатка недостаточно для всего количества."
        alternatives = []
        if status in {"out_of_stock", "insufficient_stock"}:
            alternatives = await self.ekt.find_analogs(product.id, limit=2)
        fulfillment = self._fulfillment(product.stores, quantity)
        return {
            "id": line_id, **raw, "quantity": quantity, "status": status, "risk": risk,
            "confidence": round(confidence, 2), "product": product.model_dump(),
            "alternatives": alternatives, "fulfillment": fulfillment,
            "selected_by_default": status == "matched" and confidence >= 0.76 and product.quantity >= quantity,
            "reason": reason,
        }

    @staticmethod
    def _confidence(requested: str, article: str, candidate: str, candidate_article: str, product_id: int) -> float:
        normalize = lambda value: re.sub(r"[^a-zа-яәіңғүұқөһ0-9]+", " ", value.casefold()).strip()
        a, ca = normalize(article), normalize(candidate_article)
        if a and (a == ca or a == str(product_id)):
            return 0.99
        requested_terms = {term for term in normalize(requested).split() if len(term) > 1}
        candidate_terms = {term for term in normalize(candidate).split() if len(term) > 1}
        if not requested_terms:
            return 0
        overlap = len(requested_terms & candidate_terms) / len(requested_terms)
        if normalize(requested) in normalize(candidate):
            overlap = max(overlap, 0.9)
        return min(0.96, overlap)

    @staticmethod
    def _fulfillment(stores: list[Any], quantity: int) -> dict[str, Any]:
        available = [{"name": store.name, "quantity": int(store.quantity)} for store in stores if store.quantity > 0]
        single = [store for store in available if store["quantity"] >= quantity]
        remaining = quantity
        split = []
        for store in sorted(available, key=lambda item: item["quantity"], reverse=True):
            take = min(remaining, store["quantity"])
            if take > 0:
                split.append({"store": store["name"], "quantity": take})
                remaining -= take
            if remaining == 0:
                break
        return {"single_warehouse_options": single[:5], "split_plan": split,
                "fully_coverable": remaining == 0, "uncovered_quantity": remaining}

    @staticmethod
    def _fallback_extract(text: str) -> list[dict[str, Any]]:
        result = []
        for source in text.splitlines():
            line = source.strip(" \t|;,-")
            if len(line) < 3 or re.search(r"^(итого|всего|наименование|название|№)", line, re.I):
                continue
            quantity_match = re.search(r"(?:^|[\s|;,-])(\d{1,5})\s*(?:шт\.?|pcs?|ед\.?)?\s*$", line, re.I)
            quantity = int(quantity_match.group(1)) if quantity_match else 1
            name = line[:quantity_match.start()].strip(" \t|;,-") if quantity_match else line
            article_match = re.search(r"(?:арт(?:икул)?\.?\s*[:№-]?\s*)([\w-]{3,})", name, re.I)
            if name:
                result.append({"source_text": source, "requested_name": name,
                               "article": article_match.group(1) if article_match else None,
                               "quantity": quantity, "unit": "шт", "required_characteristics": [],
                               "notes": None})
        return result[:60]

    @staticmethod
    def _summary(lines: list[dict[str, Any]]) -> dict[str, Any]:
        orderable = [line for line in lines if line.get("selected_by_default")]
        return {
            "total_lines": len(lines),
            "auto_matched": len(orderable),
            "needs_review": sum(line["status"] == "review" for line in lines),
            "not_found": sum(line["status"] == "not_found" for line in lines),
            "stock_issues": sum(line["status"] in {"out_of_stock", "insufficient_stock"} for line in lines),
            "coverage_percent": round(100 * len(orderable) / len(lines)) if lines else 0,
            "order_total": round(sum(line["product"]["price"] * line["quantity"] for line in orderable), 2),
            "risk_level": "high" if any(line["risk"] == "high" for line in lines)
                          else ("medium" if any(line["risk"] == "medium" for line in lines) else "low"),
        }
