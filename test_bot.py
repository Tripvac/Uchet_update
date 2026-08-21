import os
import json
import pytest

def test_screens_json_valid():
    """Проверяем, что файл карты экранов существует и является валидным JSON"""
    screens_path = os.getenv("SCREENS_PATH", "map/screens.json")
    assert os.path.exists(screens_path), f"Файл {screens_path} не найден!"
    
    with open(screens_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert isinstance(data, dict), "screens.json должен содержать словарь экранов"
    assert len(data) > 0, "Карта экранов пуста!"


def test_locales_match():
    """Проверяем, что файлы локалей ru и kz существуют и содержат одинаковые ключи"""
    locales_dir = os.getenv("LOCALES_DIR", "locales")
    ru_path = os.path.join(locales_dir, "ru.json")
    kz_path = os.path.join(locales_dir, "kz.json")

    assert os.path.exists(ru_path), "Файл локали ru.json не найден!"
    assert os.path.exists(kz_path), "Файл локали kz.json не найден!"

    with open(ru_path, "r", encoding="utf-8") as f:
        ru_data = json.load(f)
    with open(kz_path, "r", encoding="utf-8") as f:
        kz_data = json.load(f)

    # Проверяем, что ключи переводов полностью совпадают на обоих языках
    ru_keys = set(ru_data.keys())
    kz_keys = set(kz_data.keys())

    missing_in_kz = ru_keys - kz_keys
    missing_in_ru = kz_keys - ru_keys

    assert not missing_in_kz, f"В казахской локали (kz.json) отсутствуют ключи: {missing_in_kz}"
    assert not missing_in_ru, f"В русской локали (ru.json) отсутствуют ключи: {missing_in_ru}"