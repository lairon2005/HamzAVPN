"""Общие функции checkout Platega для бота и сайта."""

async def create_payment(amount: float, description: str, return_url: str,
                         user_id: int, metadata: dict | None = None):
    from loader import platega_client
    result = await platega_client.create_payment(
        amount, description, return_url, user_id, metadata=metadata,
    )
    return result.url, result.id
