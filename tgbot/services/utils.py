# tgbot/services/utils.py

from urllib.parse import urlparse

from utils.formatting import format_traffic, get_user_attribute


def decline_word(number: int, titles: list[str]) -> str:
    """
    Правильно склоняет слово после числа.
    Пример: decline_word(5, ['день', 'дня', 'дней']) -> 'дней'
    :param number: Число.
    :param titles: Список из трех вариантов слова (для 1, 2, 5).
    """
    if (number % 10 == 1) and (number % 100 != 11):
        return titles[0]
    elif (number % 10 in [2, 3, 4]) and (number % 100 not in [12, 13, 14]):
        return titles[1]
    else:
        return titles[2]


def _parse_link(link: str):
    try:
        parsed = urlparse(link)
        host = parsed.hostname or parsed.netloc.split("@")[-1].split(":")[0]
        port = str(parsed.port or parsed.netloc.split(":")[-1])
        return host, port
    except Exception:
        return "unknown", "unknown"
