from __future__ import annotations

from tg_spam_agent.repositories import AccessRepository


class AccessService:
    def __init__(self, repo: AccessRepository) -> None:
        self.repo = repo

    async def can_manage(self, user_id: int) -> bool:
        return await self.repo.is_allowed_manager_user(user_id)

    async def can_admin_platform(self, user_id: int) -> bool:
        return await self.repo.is_platform_admin(user_id)
