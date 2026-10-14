"""夜间事实索引维护。"""
import logging

from memory import fact_store, manager
from core.character_context import CharacterContext, character_scope
from persona.dossier import get_active_snapshot
from bridge.v1_store import BridgeStore
from core.contact_runtime_store import ContactRuntimeStore


logger = logging.getLogger(__name__)


async def run(context: CharacterContext) -> None:
    """在明确角色工作空间内执行不产生新事实的维护操作。"""
    with character_scope(context):
        snapshot = get_active_snapshot()
        if not snapshot or not snapshot.get("is_active"):
            logger.warning("夜间维护跳过：缺少活跃人格快照 character=%s", context.character_id)
            return

        recovered = fact_store.recover_stale_indexing(context)
        bridge_store = BridgeStore(context.workspace_dir / "soul_mate.db")
        bridge_store.initialize_schema()
        runtime_store = ContactRuntimeStore(bridge_store.database_path)
        contact_results = {
            contact.id: runtime_store.consolidate_expired_memories(contact.id)
            for contact in bridge_store.list_contacts(include_disabled=True)
        }
        checkpoint_results: dict[str, str | None] = {}
        for contact in bridge_store.list_contacts(include_disabled=True):
            try:
                checkpoint_results[contact.id] = await runtime_store.create_conversation_checkpoint(
                    contact.id,
                )
            except Exception as exc:
                logger.warning(
                    "对话连续性摘要生成失败 contact=%s type=%s",
                    contact.id[:8],
                    type(exc).__name__,
                )
        await manager.archive_old_conversations(days=30)
        logger.info(
            "夜间维护完成 character=%s recovered=%s contact_memory=%s checkpoints=%s",
            context.character_id,
            len(recovered),
            contact_results,
            checkpoint_results,
        )
