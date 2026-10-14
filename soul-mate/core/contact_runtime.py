"""微信桥接使用的单角色空间、多联系人对话核心。"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, is_dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from api import llm_client
from bridge.v1_store import (
    BridgeStore,
    OutboundActionNotFound,
    StoredAction,
    UnknownContact,
)
from core import postprocess
from core.character_context import CharacterContext, character_scope
from core.contact_runtime_store import ContactRuntimeStore
from core.interaction_analysis import analyze_conversation
from core.interaction_reducer import apply_conversation
from core.interaction_state import from_legacy_inner_state
from core.naturalness import find_reply_violations
from core.reply_delay import calculate_reply_delay
from core.reply_policy import plan_reply
from core.typing_rhythm import calculate_typing_sequence
from memory import manager
from memory.interaction_service import get_legacy_inner_state
from persona import prompt_builder
from persona.dossier import get_active_snapshot
from persona.review_cases import PersonaReviewStore


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DirectConversationReply:
    """无需进入微信 outbox 的直接对话结果。"""

    chunks: list[str]
    reply_text: str


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _activity_for_now(agenda) -> str:
    hour = datetime.now().hour
    if 7 <= hour < 12:
        return agenda.morning or "没什么特别的"
    if 12 <= hour < 18:
        return agenda.afternoon or "没什么特别的"
    return agenda.evening or "没什么特别的"


def _json_safe(value: Any) -> Any:
    if is_dataclass(value):
        return _json_safe(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


class ContactConversationService:
    """本角色内的联系人共享人格生活，联系人运行数据由独立数据库隔离。"""

    def __init__(
        self,
        *,
        character_context: CharacterContext,
        bridge_store: BridgeStore,
        runtime_store: ContactRuntimeStore | None = None,
    ) -> None:
        self.character_context = character_context
        self.bridge_store = bridge_store
        self.runtime_store = runtime_store or ContactRuntimeStore(bridge_store.database_path)
        # 验收消息不写正式会话表，但连续测试仍必须听得懂上一轮。这里只在当前
        # 核心进程内保存最多 20 条、30 分钟的临时上下文；重启即消失。
        self._validation_history: dict[str, list[dict[str, str]]] = {}
        self._validation_history_updated: dict[str, datetime] = {}

    def _get_validation_history(self, contact_id: str) -> list[dict[str, str]]:
        updated = self._validation_history_updated.get(contact_id)
        if updated is None or _utc_now() - updated > timedelta(minutes=30):
            self._validation_history.pop(contact_id, None)
            self._validation_history_updated.pop(contact_id, None)
            return []
        return [dict(item) for item in self._validation_history.get(contact_id, [])]

    def _append_validation_messages(self, contact_id: str, *messages: dict[str, str]) -> None:
        current = self._get_validation_history(contact_id)
        for item in messages:
            role = str(item.get("role") or "").strip()
            content = str(item.get("content") or "").strip()
            if role in {"user", "assistant"} and content:
                current.append({"role": role, "content": content})
        self._validation_history[contact_id] = current[-20:]
        self._validation_history_updated[contact_id] = _utc_now()

    async def process(
        self,
        *,
        contact_id: str,
        message: str,
        source_event_ids: list[str],
        batch_id: str,
        context_mode: str = "live",
    ) -> StoredAction | None:
        result = await self._process(
            contact_id=contact_id,
            message=message,
            source_event_ids=source_event_ids,
            batch_id=batch_id,
            direct=False,
            context_mode=context_mode,
        )
        return result if isinstance(result, StoredAction) else None

    async def process_direct(
        self,
        *,
        contact_id: str,
        message: str,
        source_event_id: str,
        conversation_id: str,
    ) -> DirectConversationReply | None:
        """PWA 直接对话：共享同一联系人进度，但不把回复投递到微信。"""
        normalized_conversation = str(conversation_id or "").strip()
        existing = self.runtime_store.get_message(
            contact_id,
            f"assistant-direct:{normalized_conversation}",
        )
        if existing is not None:
            reply_text = existing["content"]
            return DirectConversationReply(chunks=[reply_text], reply_text=reply_text)
        result = await self._process(
            contact_id=contact_id,
            message=message,
            source_event_ids=[source_event_id],
            batch_id=normalized_conversation,
            direct=True,
            context_mode="live",
        )
        return result if isinstance(result, DirectConversationReply) else None

    async def _process(
        self,
        *,
        contact_id: str,
        message: str,
        source_event_ids: list[str],
        batch_id: str,
        direct: bool,
        context_mode: str,
    ) -> StoredAction | DirectConversationReply | None:
        normalized_message = str(message or "").strip()
        normalized_batch = str(batch_id or "").strip()
        normalized_events = list(dict.fromkeys(str(item).strip() for item in source_event_ids if str(item).strip()))
        if not normalized_message:
            raise ValueError("聚合消息不能为空")
        if not normalized_batch or not normalized_events:
            raise ValueError("联系人运行时必须携带 batch_id 和 source_event_ids")
        normalized_context_mode = str(context_mode or "live").strip().lower()
        if normalized_context_mode not in {"live", "validation"}:
            raise ValueError("联系人运行时上下文模式无效")
        validation_mode = normalized_context_mode == "validation"

        # 先验证白名单/关系记录；未知联系人不能进入人格、记忆或模型调用。
        relationship = self.runtime_store.get_relationship(contact_id)
        dedupe_key = f"reply:{normalized_batch}"
        try:
            return self.bridge_store.get_action_by_dedupe_key(dedupe_key)
        except OutboundActionNotFound:
            pass

        with character_scope(self.character_context):
            snapshot = get_active_snapshot()
            if snapshot is None:
                raise RuntimeError("单一角色没有激活的人格快照")
            profile = snapshot.get("stable_profile", {})
            if not isinstance(profile, dict):
                raise RuntimeError("激活人格快照格式无效")

            global_state = await get_legacy_inner_state(self.character_context)
            state = replace(
                global_state,
                stage=relationship.stage,
                trust=relationship.trust,
                intimacy=relationship.intimacy,
                affection=relationship.affection,
                resentment=relationship.resentment,
                intimacy_context=relationship.intimacy_context,
                last_chat_time=relationship.last_chat_at,
            )
            agenda = await manager.get_today_agenda()
            if agenda:
                state.current_activity = _activity_for_now(agenda)

            interaction_state = from_legacy_inner_state(state)
            reply_plan = plan_reply(
                interaction_state,
                normalized_message,
                cold_reply_enabled=self.character_context.cold_reply_enabled,
                shallow_relationship_ignore_probability=(
                    self.character_context.shallow_relationship_ignore_probability
                ),
            )
            user_message_id = None
            if not validation_mode:
                user_message_id = self.runtime_store.save_message(
                    contact_id=contact_id,
                    role="user",
                    content=normalized_message,
                    source_event_id=normalized_events[0],
                    message_id=f"inbound-batch:{normalized_batch}",
                )
                self.runtime_store.note_topic_state(
                    contact_id=contact_id,
                    message=normalized_message,
                    source_message_id=user_message_id,
                    relationship_stage=relationship.stage,
                )
            if not reply_plan.should_reply:
                if validation_mode:
                    self._append_validation_messages(
                        contact_id,
                        {"role": "user", "content": normalized_message},
                    )
                return None

            memory_context = await self.runtime_store.retrieve_context(
                contact_id,
                normalized_message,
            )
            checkpoint = self.runtime_store.get_conversation_checkpoint(contact_id)
            if checkpoint is not None:
                memory_context["conversation_checkpoint"] = checkpoint
            memory_context["style_examples"] = PersonaReviewStore(
                self.bridge_store.database_path
            ).approved_style_examples(
                character_id=self.character_context.character_id,
                message=normalized_message,
                limit=3,
            )
            memory_context["available_media_assets"] = self.bridge_store.list_approved_media_for_contact(
                contact_id=contact_id,
                limit=12,
            )
            from core.external_information import get_external_information_service

            memory_context["external_information"] = await get_external_information_service().context_for(
                normalized_message
            )
            history = self.runtime_store.get_recent_messages(contact_id, limit=20)
            if validation_mode:
                history = (history + self._get_validation_history(contact_id))[-20:]
            decision_messages = await prompt_builder.build_decision_prompt(
                message=normalized_message,
                state=state,
                memory_context=memory_context,
                persona_snapshot=snapshot,
                history=history,
                style_override=reply_plan.style_override,
                context_mode=normalized_context_mode,
            )
            try:
                decision = await llm_client.structured_chat(decision_messages)
            except Exception as exc:
                logger.warning(
                    "联系人隐藏决策器失败 contact=%s type=%s",
                    contact_id[:8],
                    type(exc).__name__,
                )
                # 不发送由降级模板拼出的假回复；批次留在有界重试/失败队列中。
                raise RuntimeError("隐藏决策模型暂时不可用") from exc
            if decision.get("should_reply") is False:
                return None

            render_messages = await prompt_builder.build_render_prompt(
                message=normalized_message,
                decision=decision,
                state=state,
                memory_context=memory_context,
                persona_snapshot=snapshot,
                history=history,
                style_override=reply_plan.style_override,
                context_mode=normalized_context_mode,
            )
            reply_text = (await llm_client.text_chat(render_messages)).strip()
            if not reply_text:
                return None
            violations = find_reply_violations(
                user_message=normalized_message,
                reply_text=reply_text,
                decision=decision,
                relationship_stage=relationship.stage,
                intimacy=relationship.intimacy,
            )
            if violations:
                retry_messages = prompt_builder.build_naturalness_retry_prompt(
                    render_messages,
                    violations,
                )
                reply_text = (await llm_client.text_chat(retry_messages)).strip()
                remaining = find_reply_violations(
                    user_message=normalized_message,
                    reply_text=reply_text,
                    decision=decision,
                    relationship_stage=relationship.stage,
                    intimacy=relationship.intimacy,
                )
                if remaining:
                    logger.warning(
                        "真人感硬规则复核失败 contact=%s rules=%s",
                        contact_id[:8],
                        ",".join(remaining),
                    )
                    raise RuntimeError("自然语言回复未通过真人感硬规则")
            if prompt_builder.needs_context_audit(normalized_message, history):
                audit = await llm_client.structured_chat(
                    prompt_builder.build_context_audit_prompt(
                        message=normalized_message,
                        reply_text=reply_text,
                        history=history,
                        relationship_stage=relationship.stage,
                    ),
                    temperature=0.1,
                    max_tokens=400,
                )
                if audit.get("pass") is not True:
                    reply_text = (
                        await llm_client.text_chat(
                            prompt_builder.build_context_rewrite_prompt(render_messages, audit)
                        )
                    ).strip()
                    if not reply_text:
                        raise RuntimeError("语境复核重写返回空内容")
                    final_audit = await llm_client.structured_chat(
                        prompt_builder.build_context_audit_prompt(
                            message=normalized_message,
                            reply_text=reply_text,
                            history=history,
                            relationship_stage=relationship.stage,
                        ),
                        temperature=0.1,
                        max_tokens=400,
                    )
                    if final_audit.get("pass") is not True:
                        logger.warning("语境复核重写仍未通过 contact=%s", contact_id[:8])
                        raise RuntimeError("自然语言回复未通过语境复核")
                remaining = find_reply_violations(
                    user_message=normalized_message,
                    reply_text=reply_text,
                    decision=decision,
                    relationship_stage=relationship.stage,
                    intimacy=relationship.intimacy,
                )
                if remaining:
                    logger.warning(
                        "语境复核后真人感硬规则失败 contact=%s rules=%s",
                        contact_id[:8],
                        ",".join(remaining),
                    )
                    raise RuntimeError("语境复核后的回复未通过真人感硬规则")
            chunks = postprocess.humanize_reply(reply_text, state)
            if not chunks:
                return None

            if not validation_mode:
                selected_memory_ids = [
                    str(item.get("id") or "")
                    for item in memory_context.get("relevant_memories", [])
                    if isinstance(item, dict) and str(item.get("id") or "").strip()
                ]
                self.runtime_store.mark_memory_used(
                    contact_id=contact_id,
                    memory_ids=selected_memory_ids,
                    source_message_id=f"assistant-batch:{normalized_batch}",
                )

            raw_candidates = decision.get("memory_candidates", [])
            candidates = raw_candidates if isinstance(raw_candidates, list) else []
            if not validation_mode:
                self.runtime_store.save_memory_candidates(
                    contact_id=contact_id,
                    user_message=normalized_message,
                    candidates=candidates,
                    source_event_ids=normalized_events,
                )

                signal = await analyze_conversation(
                    user_message=normalized_message,
                    reply_text=reply_text,
                    profile=profile,
                    state=interaction_state,
                )
                changed = apply_conversation(
                    interaction_state,
                    signal,
                    occurred_at=_utc_now(),
                )
                self.runtime_store.commit_relationship(
                    contact_id=contact_id,
                    expected_version=relationship.version,
                    state=changed.state,
                    comfort=relationship.comfort,
                )

            typing_sequences = calculate_typing_sequence(chunks, state)
            chunk_delays = [
                postprocess.calculate_chunk_delay(chunk, index == 0)
                for index, chunk in enumerate(chunks)
            ]
            send_delay = calculate_reply_delay(
                state,
                normalized_message,
                context=self.character_context,
            )
            if direct:
                if not validation_mode:
                    self.runtime_store.save_message(
                        contact_id=contact_id,
                        role="assistant",
                        content=reply_text,
                        message_id=f"assistant-direct:{normalized_batch}",
                    )
                else:
                    self._append_validation_messages(
                        contact_id,
                        {"role": "user", "content": normalized_message},
                        {"role": "assistant", "content": reply_text},
                    )
                return DirectConversationReply(chunks=chunks, reply_text=reply_text)

            action = self.bridge_store.enqueue_action(
                contact_id=contact_id,
                action_type="text",
                payload={
                    "chunks": chunks,
                    "chunk_delays_seconds": chunk_delays,
                    "typing_sequences": _json_safe(typing_sequences),
                    "media_action": _json_safe(
                        decision.get("media_action", {"type": "none", "asset_query": ""})
                    ),
                    "context_mode": normalized_context_mode,
                },
                scheduled_at=_utc_now() + timedelta(seconds=max(0, int(send_delay))),
                dedupe_key=dedupe_key,
            )
            if not validation_mode:
                self.runtime_store.save_message(
                    contact_id=contact_id,
                    role="assistant",
                    content=reply_text,
                    outbound_action_id=action.action_id,
                    message_id=f"assistant-batch:{normalized_batch}",
                )
            else:
                self._append_validation_messages(
                    contact_id,
                    {"role": "user", "content": normalized_message},
                    {"role": "assistant", "content": reply_text},
                )
            try:
                self._enqueue_reviewed_media(
                    contact_id=contact_id,
                    batch_id=normalized_batch,
                    media_action=decision.get("media_action"),
                    scheduled_at=action.scheduled_at + timedelta(seconds=2),
                )
            except Exception as exc:
                logger.warning(
                    "已审核媒体动作入队失败 contact=%s type=%s",
                    contact_id[:8],
                    type(exc).__name__,
                )
            del user_message_id
            return action

    def _enqueue_reviewed_media(
        self,
        *,
        contact_id: str,
        batch_id: str,
        media_action: object,
        scheduled_at: datetime,
    ) -> StoredAction | None:
        if not isinstance(media_action, dict):
            return None
        action_type = str(media_action.get("type") or "none").strip().lower()
        if action_type not in {"image", "sticker"}:
            return None
        asset = self.bridge_store.find_approved_media_asset(
            contact_id=contact_id,
            kind=action_type,
            query=str(media_action.get("asset_query") or ""),
        )
        if asset is None:
            logger.info(
                "媒体动作无已审核匹配资产 contact=%s kind=%s",
                contact_id[:8],
                action_type,
            )
            return None
        payload = (
            {"attachment_refs": [asset["attachment_ref"]], "origin": "reviewed_media"}
            if action_type == "image"
            else {
                "favorite_fingerprint": asset["favorite_fingerprint"],
                "origin": "reviewed_media",
            }
        )
        return self.bridge_store.enqueue_action(
            contact_id=contact_id,
            action_type=action_type,
            payload=payload,
            scheduled_at=scheduled_at,
            dedupe_key=f"reply:{batch_id}:media",
        )


def single_character_context() -> CharacterContext:
    """旧调用兼容：返回管理台默认角色。"""
    from scripts.character_registry import get_character_context

    context = get_character_context()
    if context is None:
        raise RuntimeError("唯一角色工作空间无效")
    return context


def create_contact_conversation_service() -> ContactConversationService:
    bridge_store = BridgeStore.from_environment()
    bridge_store.initialize_schema()
    return ContactConversationService(
        character_context=single_character_context(),
        bridge_store=bridge_store,
    )


def create_character_conversation_service(
    *,
    character_context: CharacterContext,
    bridge_store: BridgeStore,
) -> ContactConversationService:
    return ContactConversationService(
        character_context=character_context,
        bridge_store=bridge_store,
    )
