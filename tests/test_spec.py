from app.services.spec import SpecService


def test_fallback_extracts_quantity_and_article():
    lines = SpecService._fallback_extract("Автомат Legrand арт. 027228 | 3 шт\nИтого: 3")
    assert len(lines) == 1
    assert lines[0]["quantity"] == 3
    assert lines[0]["article"] == "027228"


def test_exact_article_has_high_confidence():
    score = SpecService._confidence("Автомат Legrand", "027228", "Автомат Legrand DRX250", "027228", 515291)
    assert score == 0.99


def test_unrelated_product_has_zero_confidence():
    score = SpecService._confidence("Кабель ВВГнг 3x2.5", "", "Клемма соединительная WAGO", "2273-205", 48785)
    assert score == 0


def test_fulfillment_prefers_single_warehouse():
    class Store:
        def __init__(self, name, quantity):
            self.name = name
            self.quantity = quantity

    plan = SpecService._fulfillment([Store("Алматы", 8), Store("Астана", 3)], 5)
    assert plan["fully_coverable"] is True
    assert plan["single_warehouse_options"][0]["name"] == "Алматы"


def test_fulfillment_builds_split_plan():
    class Store:
        def __init__(self, name, quantity):
            self.name = name
            self.quantity = quantity

    plan = SpecService._fulfillment([Store("Алматы", 3), Store("Астана", 2)], 5)
    assert plan["fully_coverable"] is True
    assert len(plan["split_plan"]) == 2
