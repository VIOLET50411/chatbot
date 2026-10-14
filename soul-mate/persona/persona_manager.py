"""兼容层：运行时人格只能读取已激活档案快照。"""
from persona.dossier import get_active_snapshot
from memory import manager


async def get_persona() -> dict:
    """保留旧读取名称，但不再读取 legacy persona KV。"""
    snapshot = get_active_snapshot()
    profile = snapshot.get("stable_profile") if snapshot else None
    if not snapshot or not snapshot.get("is_active") or not isinstance(profile, dict):
        raise RuntimeError("没有活跃人格快照，禁止读取兼容人格入口")
    return dict(profile)


async def update_persona(updates: dict) -> None:
    """旧 KV 写入已停用，避免绕过素材审核和快照发布。"""
    raise RuntimeError("旧人格 KV 写入已停用，请通过角色档案审核工作台发布")


async def get_user_profile() -> dict:
    return await manager.get_user_profile()


async def update_profile(updates: dict, confirmed: bool = False) -> None:
    for key, value in updates.items():
        await manager.set_profile_key(key, str(value), confirmed=confirmed)
