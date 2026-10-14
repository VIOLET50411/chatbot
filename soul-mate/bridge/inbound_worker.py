"""把联系人级入站聚合批次交给唯一对话核心。"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, Protocol

from api.vision import describe_image_file
from bridge.v1_store import BridgeStore, InboundBatch, InboundMedia


logger = logging.getLogger(__name__)
VisionDescriber = Callable[[Path, str], Awaitable[str]]


class ContactProcessor(Protocol):
    async def process(
        self,
        *,
        contact_id: str,
        message: str,
        source_event_ids: list[str],
        batch_id: str,
        context_mode: str = "live",
    ): ...


class InboundAggregationWorker:
    def __init__(
        self,
        *,
        store: BridgeStore,
        service: ContactProcessor,
        debounce_seconds: int | None = None,
        batch_limit: int = 20,
        vision_describer: VisionDescriber | None = None,
    ) -> None:
        self.store = store
        self.service = service
        self.debounce_seconds = debounce_seconds
        self.batch_limit = batch_limit
        self.vision_describer = vision_describer or describe_image_file

    async def _describe_media(self, media: InboundMedia) -> str:
        if media.vision_summary:
            return media.vision_summary
        try:
            path = self.store.resolve_attachment_path(media)
            description = await self.vision_describer(path, media.mime_type)
            return self.store.cache_attachment_vision_summary(
                media.attachment_id,
                description,
            ) if description else ""
        except Exception as exc:
            logger.warning(
                "入站媒体理解失败 attachment=%s type=%s",
                media.attachment_id[:8],
                type(exc).__name__,
            )
            return ""

    async def _render_message(self, batch: InboundBatch) -> str:
        parts = self.store.get_inbound_batch_parts(batch.batch_id)
        rendered: list[str] = []
        for part in parts:
            if part.text_content:
                rendered.append(part.text_content)
            if part.event_type in {"image", "sticker"}:
                descriptions: list[str] = []
                for media in part.media:
                    description = await self._describe_media(media)
                    if description:
                        descriptions.append(description)
                label = "图片" if part.event_type == "image" else "表情包"
                if descriptions:
                    rendered.extend(f"[{label}内容：{item}]" for item in descriptions)
                else:
                    rendered.append(f"[{label}]")
                continue
            if not part.text_content:
                rendered.append("[消息]")
        return "\n".join(rendered) or batch.combined_text

    async def run_once(self, *, now: datetime | None = None) -> dict[str, int]:
        batches = self.store.claim_ready_inbound_batches(
            now=now,
            debounce_seconds=self.debounce_seconds,
            limit=self.batch_limit,
        )
        completed = 0
        failed = 0
        for batch in batches:
            try:
                current_batch = batch
                message = await self._render_message(current_batch)
                # 图片理解或宿主语音转写期间可能又到达同一轮消息。生成前再吸收一次，
                # 避免“先回图片、再回语音”或相反顺序造成割裂。
                for _ in range(3):
                    extended = self.store.extend_processing_inbound_batch(current_batch.batch_id)
                    if extended.event_ids == current_batch.event_ids:
                        break
                    current_batch = extended
                    message = await self._render_message(current_batch)
                await self.service.process(
                    contact_id=current_batch.contact_id,
                    message=message,
                    source_event_ids=list(current_batch.event_ids),
                    batch_id=current_batch.batch_id,
                    context_mode=current_batch.context_mode,
                )
            except Exception as exc:
                self.store.fail_inbound_batch(batch.batch_id, exc)
                failed += 1
                logger.exception(
                    "入站批次处理失败 batch=%s type=%s",
                    batch.batch_id[:8],
                    type(exc).__name__,
                )
                continue
            self.store.complete_inbound_batch(batch.batch_id)
            completed += 1
        return {"completed": completed, "failed": failed}
