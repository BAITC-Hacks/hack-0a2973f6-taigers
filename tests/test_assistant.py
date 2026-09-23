from app.services.assistant import is_explicit_confirmation


def test_confirmation_is_explicit():
    assert is_explicit_confirmation("Да, добавить!")
    assert is_explicit_confirmation("Подтверждаю")
    assert is_explicit_confirmation("Иә, қосыңыз")


def test_ambiguous_message_is_not_confirmation():
    assert not is_explicit_confirmation("возможно")
    assert not is_explicit_confirmation("покажи корзину")
    assert not is_explicit_confirmation("да, но сначала измени количество")
