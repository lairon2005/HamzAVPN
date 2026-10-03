from sqlalchemy import select, delete

from db import Channel


class ChannelRepository:
    def __init__(self, session_maker):
        self._session_maker = session_maker

    async def add(self, channel_id: int, title: str, invite_link: str) -> Channel:
        async with self._session_maker() as session:
            channel = Channel(channel_id=channel_id, title=title, invite_link=invite_link)
            session.add(channel)
            await session.commit()
            await session.refresh(channel)
            return channel

    async def get_all(self) -> list[Channel]:
        async with self._session_maker() as session:
            stmt = select(Channel)
            result = await session.execute(stmt)
            return result.scalars().all()

    async def delete(self, channel_id: int) -> bool:
        async with self._session_maker() as session:
            stmt = delete(Channel).where(Channel.channel_id == channel_id)
            result = await session.execute(stmt)
            await session.commit()
            return result.rowcount > 0
