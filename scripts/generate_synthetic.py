"""Generate the separate, fictional team extension with a fixed calendar seed.

This is a build-time tool, never called by search. Re-running it reproduces the
committed CSV byte for byte. No original record is changed.
"""
from __future__ import annotations

import csv
from datetime import date, timedelta
import hashlib
from pathlib import Path

FIELDS = [
    "id", "anon_name", "categories", "city", "city_imputed", "synthetic",
    "price_from_kzt", "price_imputed", "event_formats", "languages", "max_hours",
    "busy_dates", "description",
]
SEED = "hackalem-demoVer-1-team-extension-v1"
START = date(2026, 9, 23)
END = date(2026, 12, 31)
PROFILES = [
    {
        "id": "TEAM-DEC-AST-01",
        "anon_name": "Демо-декоратор Астана 01",
        "categories": "Декоратор",
        "price_from_kzt": 1800000,
        "event_formats": "свадьба|той|юбилей",
        "languages": "русский|казахский",
        "max_hours": "",
        "description": "Вымышленный профиль команды для демонстрации. Текстильные композиции и оформление семейных церемоний: драпировки зоны регистрации, дорожки для столов и мягкий фон для семейных фотографий. Концепция строится вокруг фактуры ткани и спокойной цветовой палитры. Поддерживаемые форматы: свадьба, той и юбилей.",
    },
    {
        "id": "TEAM-DEC-AST-02",
        "anon_name": "Демо-декоратор Астана 02",
        "categories": "Декоратор",
        "price_from_kzt": 2000000,
        "event_formats": "корпоратив|конференция|свадьба",
        "languages": "русский|английский",
        "max_hours": "",
        "description": "Вымышленный профиль команды для демонстрации. Модульные бренд-зоны и оформление сцены: сборные панели, стойки регистрации и фон для выступлений. Элементы оформления можно переставить между деловой и вечерней частями мероприятия. Поддерживаемые форматы: корпоратив, конференция и свадьба.",
    },
    {
        "id": "TEAM-DEC-AST-03",
        "anon_name": "Демо-декоратор Астана 03",
        "categories": "Декоратор",
        "price_from_kzt": 2200000,
        "event_formats": "свадьба|день рождения|юбилей",
        "languages": "русский",
        "max_hours": "",
        "description": "Вымышленный профиль команды для демонстрации. Бумажные инсталляции и многоразовые декорации: объёмные бумажные цветы, складные ширмы и настольные композиции. Основной акцент — геометрические формы и повторное использование декоративных элементов. Поддерживаемые форматы: свадьба, день рождения и юбилей.",
    },
    {
        "id": "TEAM-INS-AST-01",
        "anon_name": "Демо-инструменталист Астана 01",
        "categories": "Инструменталист",
        "price_from_kzt": 400000,
        "event_formats": "свадьба|корпоратив|юбилей",
        "languages": "русский|казахский",
        "max_hours": 3,
        "description": "Вымышленный профиль команды для демонстрации. Скрипичная программа для welcome-зоны: инструментальные мелодии во время сбора гостей и короткие музыкальные переходы между частями вечера. Репертуар сочетает камерное звучание и обработки знакомых песен. Поддерживаемые форматы: свадьба, корпоратив и юбилей; максимальная продолжительность участия — 3 часа.",
    },
]


def busy_calendar(profile_id: str) -> str:
    dates: list[str] = []
    current = START
    while current <= END:
        # Weekday/weekend probabilities average approximately 40% in autumn
        # and 75% in December, without rewriting any date for a demo scenario.
        threshold = (0.68 if current.month == 12 else 0.32) + (0.25 if current.weekday() >= 5 else 0.0)
        digest = hashlib.sha256(f"{SEED}|{profile_id}|{current.isoformat()}".encode()).digest()
        unit = int.from_bytes(digest[:8], "big") / (1 << 64)
        if unit < threshold:
            dates.append(current.isoformat())
        current += timedelta(days=1)
    return "|".join(dates)


def write_synthetic(destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS, lineterminator="\n")
        writer.writeheader()
        for profile in PROFILES:
            writer.writerow({
                **profile,
                "city": "Астана",
                "city_imputed": False,
                "synthetic": True,
                "price_imputed": False,
                "busy_dates": busy_calendar(profile["id"]),
            })


if __name__ == "__main__":
    write_synthetic(Path(__file__).resolve().parents[1] / "data" / "team_synthetic.csv")
