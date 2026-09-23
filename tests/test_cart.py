import pytest

from app.config import Settings
from app.models import CartItemRequest, ProductDetail
from app.services.cart import CartError, DemoCartService


class FakeEkt:
    async def get_product(self, product_id: int) -> ProductDetail:
        return ProductDetail(id=product_id, name="Тестовый автомат", article="TEST-1", price=1000, quantity=3)


@pytest.mark.asyncio
async def test_cart_requires_confirmation_and_is_idempotent():
    service = DemoCartService(Settings(ekt_api_password="test"), FakeEkt())
    pending = await service.prepare("session", [CartItemRequest(product_id=1, quantity=2)])
    assert service.get_cart("session")["items"] == []
    with pytest.raises(CartError):
        await service.commit("session", pending.id, confirmed=False)
    first = await service.commit("session", pending.id, confirmed=True)
    second = await service.commit("session", pending.id, confirmed=True)
    assert first == second
    assert first["items"][0]["quantity"] == 2


@pytest.mark.asyncio
async def test_cart_rejects_quantity_above_stock():
    service = DemoCartService(Settings(ekt_api_password="test"), FakeEkt())
    with pytest.raises(CartError):
        await service.prepare("session", [CartItemRequest(product_id=1, quantity=4)])
