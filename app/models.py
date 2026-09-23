from typing import Any, Literal

from pydantic import BaseModel, Field


class ProductSummary(BaseModel):
    id: int
    name: str
    article: str = ""
    price: float = 0
    image: str | None = None
    url: str | None = None


class StoreStock(BaseModel):
    id: int | None = None
    name: str
    quantity: float = 0


class ProductDetail(ProductSummary):
    description: str = ""
    quantity: float = 0
    stores: list[StoreStock] = Field(default_factory=list)
    properties: dict[str, Any] = Field(default_factory=dict)
    offers: list[Any] = Field(default_factory=list)


class CartItemRequest(BaseModel):
    product_id: int
    quantity: int = Field(ge=1, le=10_000)


class CartLine(BaseModel):
    product_id: int
    article: str
    name: str
    quantity: int
    unit_price: float
    total: float
    available: int


class PendingCart(BaseModel):
    id: str
    session_id: str
    items: list[CartLine]
    total: float
    expires_at: str
    status: Literal["awaiting_confirmation", "committed", "expired"]


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    session_id: str
    message: str
    history: list[ChatMessage] = Field(default_factory=list)
    image_data_url: str | None = None


class ChatResponse(BaseModel):
    answer: str
    products: list[dict[str, Any]] = Field(default_factory=list)
    pending_cart: dict[str, Any] | None = None
    cart: dict[str, Any] | None = None
    mode: Literal["openai", "fallback"]


class SpecPrepareRequest(BaseModel):
    session_id: str
    analysis_id: str
    line_ids: list[str]
