"""Soul Mate 启动入口。"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

load_dotenv()

from core.character_context import CharacterContext
from core.engine import ConversationEngine
from core.external_information import get_external_information_service
from bridge.v1_store import BridgeStore
from bridge.inbound_worker import InboundAggregationWorker
from core.contact_runtime import create_character_conversation_service
from core.runtime_catalog import CharacterRuntime, list_character_runtimes
from scheduler.agenda import generate_daily_agenda
from scheduler.contact_proactive import ContactProactiveScheduler
from scheduler.nightly import sleep_consolidation
from scheduler.run_ledger import complete_job_run, start_job_run
from scheduler.ticker import tick
from web.app import create_app
from core.logging_config import configure_logging
from core.device_context import get_device_context_service
from api.llm_client import embed, embedding_health


logger = logging.getLogger(__name__)


async def _run_character(
    context: CharacterContext,
    job_name: str,
    job: Callable[[CharacterContext], Awaitable[None]],
) -> None:
    run = start_job_run(job_name, character_id=context.character_id)
    try:
        await job(context)
    except Exception as exc:
        try:
            complete_job_run(run.id, status="failed", error=exc)
        except Exception as ledger_exc:
            logger.error(
                "调度审计失败 character=%s type=%s",
                context.character_id,
                type(ledger_exc).__name__,
            )
        raise
    try:
        complete_job_run(run.id, status="succeeded")
    except Exception as ledger_exc:
        logger.error(
            "调度审计失败 character=%s type=%s",
            context.character_id,
            type(ledger_exc).__name__,
        )


async def _run_for_all_characters(
    job_name: str,
    job: Callable[[CharacterContext], Awaitable[None]],
) -> None:
    for runtime in list_character_runtimes(active_only=True):
        await _run_character(runtime.context, job_name, job)


async def run_tick() -> None:
    await _run_for_all_characters("tick", tick)


async def run_daily_agenda() -> None:
    await _run_for_all_characters("daily_agenda", generate_daily_agenda)


async def run_nightly_consolidation() -> None:
    await _run_for_all_characters("nightly_consolidation", sleep_consolidation)


async def run_external_information_refresh() -> None:
    """刷新全局只读资讯缓存；资讯不会写入人物快照或联系人记忆。"""
    try:
        await get_external_information_service().refresh(force=False)
    except Exception as exc:
        logger.warning("外界信息定时刷新失败 type=%s", type(exc).__name__)


async def prewarm_local_embedding() -> None:
    """后台预热本地 BGE-M3，避免第一条真实聊天承担冷启动。"""
    started = time.monotonic()
    await embed("本地中文记忆检索预热")
    health = embedding_health()
    logger.info(
        "本地向量预热完成 provider=%s model=%s seconds=%.1f",
        health.get("provider", "unknown"),
        health.get("model", "unknown"),
        time.monotonic() - started,
    )


def _register_character_runtime_jobs(
    scheduler: AsyncIOScheduler,
    runtime: CharacterRuntime,
) -> None:
    inbound_worker = InboundAggregationWorker(
        store=runtime.bridge_store,
        service=create_character_conversation_service(
            character_context=runtime.context,
            bridge_store=runtime.bridge_store,
        ),
    )
    proactive_scheduler = ContactProactiveScheduler(
        context=runtime.context,
        bridge_store=runtime.bridge_store,
    )
    scheduler.add_job(
        inbound_worker.run_once,
        "interval",
        id=f"inbound:{runtime.context.character_id}",
        seconds=1,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=5,
    )
    scheduler.add_job(
        proactive_scheduler.run_once,
        "interval",
        id=f"proactive:{runtime.context.character_id}",
        minutes=30,
        max_instances=1,
        coalesce=True,
    )


async def main() -> None:
    configure_logging()
    get_device_context_service().purge_expired()
    engine = ConversationEngine()
    await engine.initialize()
    active_runtimes = list_character_runtimes(active_only=True)
    if not active_runtimes:
        raise RuntimeError("没有已激活的角色，核心不会启动")
    embedding_task = asyncio.create_task(prewarm_local_embedding(), name="embedding-prewarm")

    scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
    for runtime in active_runtimes:
        _register_character_runtime_jobs(scheduler, runtime)
    scheduler.add_job(run_tick, "interval", minutes=15)
    scheduler.add_job(run_daily_agenda, "cron", hour=7)
    scheduler.add_job(run_nightly_consolidation, "cron", hour=4)
    scheduler.add_job(
        run_external_information_refresh,
        "interval",
        minutes=15,
        max_instances=1,
        coalesce=True,
    )
    await run_external_information_refresh()
    scheduler.start()

    app = create_app(engine)
    config = uvicorn.Config(app, host="0.0.0.0", port=8080, log_level="info", log_config=None)
    server = uvicorn.Server(config)
    try:
        await server.serve()
    finally:
        scheduler.shutdown(wait=False)
        await engine.shutdown()
        if not embedding_task.done():
            embedding_task.cancel()
        await asyncio.gather(embedding_task, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
