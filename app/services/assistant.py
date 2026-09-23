import json
import re
from typing import Any

from openai import AsyncOpenAI

from app.config import Settings
from app.models import CartItemRequest, ChatRequest
from app.services.cart import CartError, DemoCartService
from app.services.ekt_client import EktApiError, EktClient
from app.services.terms import TermsService


SYSTEM_PROMPT = """Ты — консультант интернет-магазина электротехнической продукции ekt.kz.
Отвечай на языке пользователя, кратко и предметно.
1. Никогда не выдумывай цену, остаток, характеристики, сертификат или условия покупки. Получай их только инструментами.
2. Для точной информации о товаре сначала вызови get_product. Для поиска используй search_products.
3. Если quantity равен 0, предложи аналоги через find_analogs и объясни совпадения и отличия.
4. Никогда не добавляй товар сразу. Сначала вызови prepare_cart и покажи итог. Фактическое добавление выполняет отдельный серверный endpoint только после кнопки явного подтверждения.
5. Не принимай и не запрашивай платёжные данные.
6. Не называй демонстрационные условия покупки официальными.
7. При неоднозначности задай один уточняющий вопрос.
"""


TOOLS = [
    {"type": "function", "name": "search_products", "description": "Search EKT catalog by name, article or id.",
     "strict": True, "parameters": {"type": "object", "properties": {
         "query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 8}},
         "required": ["query", "limit"], "additionalProperties": False}},
    {"type": "function", "name": "get_product", "description": "Get verified product details and stock.",
     "strict": True, "parameters": {"type": "object", "properties": {"product_id": {"type": "integer"}},
         "required": ["product_id"], "additionalProperties": False}},
    {"type": "function", "name": "find_analogs", "description": "Find in-stock alternatives.",
     "strict": True, "parameters": {"type": "object", "properties": {
         "product_id": {"type": "integer"}, "limit": {"type": "integer", "minimum": 1, "maximum": 5}},
         "required": ["product_id", "limit"], "additionalProperties": False}},
    {"type": "function", "name": "get_purchase_terms", "description": "Get purchase terms.",
     "strict": True, "parameters": {"type": "object", "properties": {
         "topic": {"type": "string", "enum": ["all", "payment", "delivery", "minimum_order"]}},
         "required": ["topic"], "additionalProperties": False}},
    {"type": "function", "name": "prepare_cart", "description": "Validate stock and prepare preview; never mutates cart.",
     "strict": True, "parameters": {"type": "object", "properties": {
         "items": {"type": "array", "items": {"type": "object", "properties": {
             "product_id": {"type": "integer"}, "quantity": {"type": "integer", "minimum": 1}},
             "required": ["product_id", "quantity"], "additionalProperties": False}}},
         "required": ["items"], "additionalProperties": False}},
]


class AssistantService:
    def __init__(self, settings: Settings, ekt: EktClient, carts: DemoCartService, terms: TermsService):
        self.settings = settings
        self.ekt = ekt
        self.carts = carts
        self.terms = terms
        self.client = AsyncOpenAI(api_key=settings.openai_api_key) if settings.openai_api_key else None

    async def chat(self, request: ChatRequest) -> dict[str, Any]:
        if not self.client:
            return await self._fallback(request)
        content: list[dict[str, Any]] = [{"type": "input_text", "text": request.message}]
        if request.image_data_url:
            content.append({"type": "input_image", "image_url": request.image_data_url})
        input_items: list[dict[str, Any]] = [{"role": item.role, "content": item.content}
                                                   for item in request.history[-10:]]
        input_items.append({"role": "user", "content": content})
        products: list[dict] = []
        pending: dict | None = None
        for _ in range(5):
            response = await self.client.responses.create(
                model=self.settings.openai_model, instructions=SYSTEM_PROMPT, input=input_items,
                tools=TOOLS, reasoning={"effort": self.settings.openai_reasoning_effort}, store=False)
            calls = [item for item in response.output if item.type == "function_call"]
            if not calls:
                return {"answer": response.output_text, "products": products, "pending_cart": pending,
                        "cart": None, "mode": "openai"}
            input_items.extend(item.model_dump(exclude_none=True) for item in response.output)
            for call in calls:
                result = await self._execute(call.name, json.loads(call.arguments), request.session_id)
                if call.name in {"search_products", "get_product", "find_analogs"}:
                    raw_products = result if isinstance(result, list) else [result]
                    products = [item for item in raw_products if isinstance(item, dict) and item.get("id")]
                if call.name == "prepare_cart" and isinstance(result, dict) and result.get("id"):
                    pending = result
                input_items.append({"type": "function_call_output", "call_id": call.call_id,
                                    "output": json.dumps(result, ensure_ascii=False, default=str)})
        return {"answer": "Не удалось завершить запрос за допустимое число шагов.", "products": products,
                "pending_cart": pending, "cart": None, "mode": "openai"}

    async def _execute(self, name: str, args: dict, session_id: str) -> Any:
        try:
            if name == "search_products":
                return [item.model_dump() for item in await self.ekt.search_products(**args)]
            if name == "get_product":
                return (await self.ekt.get_product(args["product_id"])).model_dump()
            if name == "find_analogs":
                return await self.ekt.find_analogs(**args)
            if name == "get_purchase_terms":
                return self.terms.get(args["topic"])
            if name == "prepare_cart":
                items = [CartItemRequest.model_validate(item) for item in args["items"]]
                return (await self.carts.prepare(session_id, items)).model_dump()
            return {"error": f"Unknown tool: {name}"}
        except (EktApiError, CartError, ValueError) as exc:
            return {"error": str(exc)}

    async def _fallback(self, request: ChatRequest) -> dict[str, Any]:
        text = request.message.strip()
        lowered = text.casefold()
        if any(term in lowered for term in ("достав", "оплат", "минималь", "delivery", "payment")):
            return {"answer": _format_terms(self.terms.get("all")), "products": [], "pending_cart": None,
                    "cart": None, "mode": "fallback"}
        try:
            products = await self.ekt.search_products(text, limit=5)
        except EktApiError as exc:
            return {"answer": f"Каталог временно недоступен: {exc}", "products": [], "pending_cart": None,
                    "cart": None, "mode": "fallback"}
        if not products:
            return {"answer": "Товар не найден. Уточните артикул или название. Для свободного диалога добавьте OPENAI_API_KEY в .env.",
                    "products": [], "pending_cart": None, "cart": None, "mode": "fallback"}
        detailed = await self.ekt.get_product(products[0].id)
        stock = f"В наличии: {int(detailed.quantity)} шт." if detailed.quantity > 0 else "Сейчас нет в наличии."
        answer = f"{detailed.name}\nАртикул: {detailed.article}\nЦена: {detailed.price:,.0f} ₸\n{stock}"
        return {"answer": answer, "products": [detailed.model_dump()], "pending_cart": None,
                "cart": None, "mode": "fallback"}


def _format_terms(terms: dict) -> str:
    return "\n".join(f"{key}: {value}" for key, value in terms.items())


def is_explicit_confirmation(text: str) -> bool:
    normalized = re.sub(r"\s+", " ", re.sub(r"[^\w\s]+", " ", text.casefold())).strip()
    allowed = {"да", "да добавить", "да добавь", "подтверждаю", "подтвердить добавление",
               "иә", "иә қосыңыз", "себетке қосыңыз", "yes add", "confirm"}
    return normalized in allowed
