"""对话引擎主入口"""
import asyncio
import logging
from datetime import datetime
from memory import manager, retriever, extractor
from core import postprocess
from core.background_tasks import BackgroundTaskSupervisor
from core.character_context import CharacterContext, character_scope
from core.interaction_analysis import analyze_conversation
from core.naturalness import find_reply_violations
from core.reply_policy import plan_reply
from memory.interaction_service import (
    apply_conversation_signal,
    get_interaction_state,
    get_legacy_inner_state,
)
from persona import prompt_builder
from api import llm_client


logger = logging.getLogger(__name__)


# 全局单例
_engine_instance = None

def get_engine():
    return _engine_instance


class ConversationEngine:
    def __init__(self):
        global _engine_instance
        _engine_instance = self
        self._background_tasks = BackgroundTaskSupervisor()

    async def initialize(self):
        """初始化所有已启用角色的独立工作空间。"""
        from core.character_context import character_scope
        from persona.dossier import ensure_dossier_schema
        from scripts.character_registry import list_characters
        from scripts.init_db import init

        initialized = 0
        for record in list_characters(include_disabled=False):
            context = CharacterContext.from_record(record)
            with character_scope(context):
                init(data_dir=context.workspace_dir)
                ensure_dossier_schema()
            initialized += 1
        if not initialized:
            raise RuntimeError("没有可初始化的角色工作空间")
        logger.info("已初始化 %d 个隔离角色工作空间", initialized)

    def spawn_background(
        self,
        context: CharacterContext,
        operation: str,
        work,
        *,
        drain_on_shutdown: bool,
    ):
        """将运行期后台工作统一交给角色感知的生命周期边界。"""
        return self._background_tasks.spawn(
            context,
            operation,
            work,
            drain_on_shutdown=drain_on_shutdown,
        )

    async def shutdown(self) -> None:
        """停止接收新的后台工作，并按任务语义完成关闭。"""
        await self._background_tasks.shutdown()

    async def wait_for_background_tasks(self) -> None:
        """测试和受控维护流程可显式等待后台状态提交。"""
        await self._background_tasks.wait_for_idle()

    async def process(
        self,
        context: CharacterContext,
        wxid: str,
        message: str,
    ) -> dict | None:
        """在角色作用域内处理一条消息。"""
        with character_scope(context):
            return await self._process(context, wxid, message)

    async def _process(
        self,
        context: CharacterContext,
        wxid: str,
        message: str,
    ) -> dict | None:
        """
        主处理流程。
        返回 {"chunks": [...]} 或 None（决定不回复）。
        """
        # 1. 运行时人格必须来自当前角色的活跃快照；没有快照即失败关闭。
        from persona.dossier import get_active_snapshot

        persona_snapshot = get_active_snapshot()
        if persona_snapshot is None:
            logger.warning("缺少活跃人格快照，消息不会生成回复 character=%s", context.character_id)
            return None
        persona = persona_snapshot.get("stable_profile", {})
        if not isinstance(persona, dict):
            logger.error("人格快照格式无效，消息不会生成回复 character=%s", context.character_id)
            return None

        # 2. 当前互动状态是唯一运行时权威；旧状态表仅在首次读取时迁移。
        interaction_state = await get_interaction_state(context)

        # 3. 已完成入口校验的消息默认回复；状态只决定语气与发送节奏。
        reply_plan = plan_reply(
            interaction_state,
            message,
            cold_reply_enabled=context.cold_reply_enabled,
            shallow_relationship_ignore_probability=(
                context.shallow_relationship_ignore_probability
            ),
        )
        if not reply_plan.should_reply:
            logger.info("本轮决定不回复 character=%s reason=%s", context.character_id, reply_plan.reason)
            return None

        # 4. 记录用户消息，并保留不可伪造的证据主键供事实链验证。
        user_message_id = await manager.save_message("user", message)

        # 5. 并行检索关系上下文；读取侧临时故障不能中断已获准的回复。
        try:
            memory_ctx = await retriever.retrieve_context(message)
        except Exception as exc:
            logger.warning(
                "记忆检索失败，使用空上下文继续 character=%s type=%s",
                context.character_id,
                type(exc).__name__,
            )
            memory_ctx = {"relevant_memories": []}

        # 6. 日程是独立角色数据，只在本轮投影视图中覆盖当前活动。
        agenda = await manager.get_today_agenda()
        state = await get_legacy_inner_state(context)
        if agenda:
            state.current_activity = _activity_for_now(agenda)

        # 7. 组装隐藏决策 Prompt。历史中若已包含刚保存的当前消息，构建器会去重。
        history = await manager.get_recent_messages(limit=20)
        decision_messages = await prompt_builder.build_decision_prompt(
            message=message,
            state=state,
            memory_context=memory_ctx,
            persona_snapshot=persona_snapshot,
            history=history,
            style_override=reply_plan.style_override,
        )

        # 8. 低随机度隐藏决策失败时使用保守中性决定，避免把内部错误或 JSON 发给联系人。
        try:
            decision = await llm_client.structured_chat(decision_messages)
        except Exception as exc:
            logger.warning(
                "隐藏决策器失败，使用中性回退 character=%s type=%s",
                context.character_id,
                type(exc).__name__,
            )
            decision = {
                "should_reply": True,
                "intent": "自然回应当前消息，不编造信息",
                "stance": "neutral",
                "tone": "贴合当前关系和状态",
                "media_action": {"type": "none", "asset_query": ""},
                "memory_candidates": [],
                "reply_constraints": {"max_chunks": 3, "avoid": []},
            }
        if decision.get("should_reply") is False:
            logger.info("隐藏决策器决定不回复 character=%s", context.character_id)
            return None

        # 9. 自然语言渲染器只生成可发送微信文本，不承担结构化记忆输出。
        render_messages = await prompt_builder.build_render_prompt(
            message=message,
            decision=decision,
            state=state,
            memory_context=memory_ctx,
            persona_snapshot=persona_snapshot,
            history=history,
            style_override=reply_plan.style_override,
        )
        try:
            reply_text = (await llm_client.text_chat(render_messages)).strip()
        except Exception as exc:
            logger.error(
                "自然语言渲染失败 character=%s type=%s",
                context.character_id,
                type(exc).__name__,
            )
            return None
        raw_candidates = decision.get("memory_candidates", [])
        remember = raw_candidates if isinstance(raw_candidates, list) else []

        if not reply_text:
            logger.warning("自然语言渲染返回空文本 character=%s", context.character_id)
            return None

        violations = find_reply_violations(
            user_message=message,
            reply_text=reply_text,
            decision=decision,
        )
        if violations:
            try:
                retry_messages = prompt_builder.build_naturalness_retry_prompt(
                    render_messages,
                    violations,
                )
                reply_text = (await llm_client.text_chat(retry_messages)).strip()
            except Exception as exc:
                logger.error(
                    "真人感定向重写失败 character=%s type=%s",
                    context.character_id,
                    type(exc).__name__,
                )
                return None
            remaining = find_reply_violations(
                user_message=message,
                reply_text=reply_text,
                decision=decision,
            )
            if remaining:
                logger.warning(
                    "真人感硬规则复核失败 character=%s rules=%s",
                    context.character_id,
                    ",".join(remaining),
                )
                return None

        # 10. 后处理。清洗后无可发送内容时，不能制造空 assistant 历史，
        # 也不能推进记忆、互动状态或启动发送任务。
        chunks = postprocess.humanize_reply(reply_text, state)
        if not chunks:
            logger.warning("回复经后处理后为空，跳过保存和发送 character=%s", context.character_id)
            return None

        # 11. 保存回复
        await manager.save_message("assistant", reply_text)

        # 12. 后台保存记忆，并独立提交可审计的互动状态变化。
        self.spawn_background(
            context,
            "interaction_post_process",
            self._async_post_process(
                context,
                user_message_id,
                message,
                reply_text,
                remember,
                persona,
            ),
            drain_on_shutdown=True,
        )

        # 13. 打字节奏序列（typing_seqs[i] = 发第 i 条前的 TypingStep 列表）
        from core.typing_rhythm import calculate_typing_sequence
        typing_seqs = calculate_typing_sequence(chunks, state)

        # 14. 只返回发送节奏。生产微信链路统一由 outbox + Plus 代理调度，
        # 旧 PWA 调用会显式把 reply_delay_max_seconds 设为 0。
        chunk_delays = [postprocess.calculate_chunk_delay(c, i == 0) for i, c in enumerate(chunks)]
        from core.reply_delay import calculate_reply_delay
        send_delay = calculate_reply_delay(
            state,
            message,
            context=context,
        )
        return {
            "chunks": chunks,
            "delays": chunk_delays,
            "reply_delay_seconds": send_delay,
            "typing_seqs": typing_seqs,
            "decision": reply_plan.to_payload(),
            "media_action": decision.get("media_action", {"type": "none", "asset_query": ""}),
        }

    async def _async_post_process(
        self,
        context: CharacterContext,
        source_message_id: int,
        user_msg: str,
        reply: str,
        remember: list,
        persona: dict,
    ) -> None:
        await self._run_async_post_process(
            context,
            source_message_id,
            user_msg,
            reply,
            remember,
            persona,
        )

    async def _run_async_post_process(
        self,
        context: CharacterContext,
        source_message_id: int,
        user_msg: str,
        reply: str,
        remember: list,
        persona: dict,
    ) -> None:
        """不阻塞发送地保存记忆，并提交本轮互动状态。"""
        try:
            if remember:
                await extractor.extract_and_save(
                    remember,
                    source_message_id=source_message_id,
                )
        except Exception as exc:
            logger.error("记忆后处理失败 character=%s type=%s", context.character_id, type(exc).__name__)

        try:
            current_state = await get_interaction_state(context)
            signal = await analyze_conversation(
                user_message=user_msg,
                reply_text=reply,
                profile=persona,
                state=current_state,
            )
            await apply_conversation_signal(context, signal)
        except Exception as exc:
            logger.error("互动状态后处理失败 character=%s type=%s", context.character_id, type(exc).__name__)

def _activity_for_now(agenda) -> str:
    """根据当前时间返回议程中对应时段的活动"""
    hour = datetime.now().hour
    if 7 <= hour < 12:
        return agenda.morning or "没什么特别的"
    elif 12 <= hour < 18:
        return agenda.afternoon or "没什么特别的"
    else:
        return agenda.evening or "没什么特别的"
