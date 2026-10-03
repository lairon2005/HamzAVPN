from database.repositories.user import UserRepository


class SupportService:
    def __init__(self, user_repo: UserRepository):
        self._user_repo = user_repo

    async def get_topic_id(self, user_id: int) -> int | None:
        """Возвращает ID топика поддержки, если он есть."""
        user = await self._user_repo.get(user_id)
        return user.support_topic_id if user else None

    async def save_topic(self, user_id: int, topic_id: int):
        await self._user_repo.set_support_topic(user_id, topic_id)

    async def close_topic(self, user_id: int):
        await self._user_repo.clear_support_topic(user_id)

    async def get_user_by_topic(self, topic_id: int):
        return await self._user_repo.get_by_support_topic(topic_id)
