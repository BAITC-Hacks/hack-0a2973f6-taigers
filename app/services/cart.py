from datetime import UTC, datetime, timedelta
from threading import RLock
from uuid import uuid4

from app.config import Settings
from app.models import CartItemRequest, CartLine, PendingCart
from app.services.ekt_client import EktClient


class CartError(ValueError):
    pass


class DemoCartService:
    """Safe two-phase cart adapter. Replace commit() when a real cart API is provided."""

    def __init__(self, settings: Settings, ekt: EktClient):
        self.settings = settings
        self.ekt = ekt
        self._pending: dict[str, PendingCart] = {}
        self._carts: dict[str, dict[int, CartLine]] = {}
        self._committed_pending_ids: set[str] = set()
        self._lock = RLock()

    async def prepare(self, session_id: str, items: list[CartItemRequest]) -> PendingCart:
        if not session_id.strip():
            raise CartError("Missing session_id")
        if not items:
            raise CartError("At least one item is required")
        lines: list[CartLine] = []
        for request in items:
            product = await self.ekt.get_product(request.product_id)
            available = max(0, int(product.quantity))
            if available == 0:
                raise CartError(f"{product.name} is out of stock")
            if request.quantity > available:
                raise CartError(f"Requested {request.quantity}, but only {available} are available")
            lines.append(CartLine(product_id=product.id, article=product.article, name=product.name,
                                  quantity=request.quantity, unit_price=product.price,
                                  total=round(product.price * request.quantity, 2), available=available))
        expires = datetime.now(UTC) + timedelta(seconds=self.settings.pending_cart_ttl_seconds)
        pending = PendingCart(id=str(uuid4()), session_id=session_id, items=lines,
                              total=round(sum(line.total for line in lines), 2),
                              expires_at=expires.isoformat(), status="awaiting_confirmation")
        with self._lock:
            self._pending[pending.id] = pending
        return pending

    async def commit(self, session_id: str, pending_id: str, confirmed: bool) -> dict:
        if not confirmed:
            raise CartError("Explicit confirmation is required")
        with self._lock:
            pending = self._pending.get(pending_id)
            if not pending or pending.session_id != session_id:
                raise CartError("Pending cart was not found")
            if pending_id in self._committed_pending_ids:
                return self.get_cart(session_id)
            if datetime.fromisoformat(pending.expires_at) <= datetime.now(UTC):
                pending.status = "expired"
                raise CartError("Confirmation expired; please prepare the cart again")

        refreshed: list[CartLine] = []
        for line in pending.items:
            product = await self.ekt.get_product(line.product_id)
            available = max(0, int(product.quantity))
            if line.quantity > available:
                raise CartError(f"Stock changed for {line.name}: only {available} remain")
            refreshed.append(line.model_copy(update={"available": available, "unit_price": product.price,
                                                     "total": round(product.price * line.quantity, 2)}))

        with self._lock:
            cart = self._carts.setdefault(session_id, {})
            for line in refreshed:
                existing = cart.get(line.product_id)
                new_quantity = line.quantity + (existing.quantity if existing else 0)
                if new_quantity > line.available:
                    raise CartError(f"Cart quantity for {line.name} would exceed stock ({line.available})")
                cart[line.product_id] = line.model_copy(
                    update={"quantity": new_quantity, "total": round(line.unit_price * new_quantity, 2)})
            pending.status = "committed"
            self._committed_pending_ids.add(pending_id)
            return self.get_cart(session_id)

    def get_cart(self, session_id: str) -> dict:
        lines = list(self._carts.get(session_id, {}).values())
        return {"session_id": session_id, "items": [line.model_dump() for line in lines],
                "total": round(sum(line.total for line in lines), 2),
                "cart_url": f"/cart.html?session={session_id}"}
