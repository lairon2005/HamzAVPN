def format_traffic(byte_count: int | None) -> str:
    """Красиво форматирует байты в Б, Кб, Мб или Гб."""
    if byte_count is None:
        return "Неизвестно"
    if byte_count == 0:
        return "0 Гб"

    power = 1024
    level = 0
    labels = {0: "Б", 1: "Кб", 2: "Мб", 3: "Гб"}
    while byte_count >= power and level < len(labels) - 1:
        byte_count /= power
        level += 1
    return f"{byte_count:.2f} {labels.get(level, 'Тб')}"


def get_user_attribute(user_obj, key, default=None):
    """Получает атрибут из словаря или объекта пользователя."""
    if isinstance(user_obj, dict):
        return user_obj.get(key, default)
    return getattr(user_obj, key, default)
