"""角色感知的后台任务生命周期边界。"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from core.character_context import CharacterContext, character_scope


logger = logging.getLogger(__name__)


# 完成审计环形历史的上限；只保留最近若干条，避免无界增长。
_RECENT_HISTORY_LIMIT = 200

_Outcome = Literal["succeeded", "failed", "cancelled"]
_OUTCOMES: tuple[_Outcome, ...] = ("succeeded", "failed", "cancelled")


@dataclass(frozen=True, slots=True)
class _TrackedTask:
    task: asyncio.Task[Any]
    context: CharacterContext
    operation: str
    drain_on_shutdown: bool
    started_at: datetime
    started_monotonic: float


class BackgroundTaskSupervisor:
    """集中管理后台任务的上下文、引用、异常和关闭语义。"""

    def __init__(self) -> None:
        self._tasks: dict[asyncio.Task[Any], _TrackedTask] = {}
        self._closing = False
        # 只读可观测性状态：不持有 task/work 对象，只保存原始数据。
        self._recent_history: deque[dict[str, Any]] = deque(maxlen=_RECENT_HISTORY_LIMIT)
        self._lifetime_counts: dict[str, int] = {outcome: 0 for outcome in _OUTCOMES}

    @property
    def pending_count(self) -> int:
        return len(self._tasks)

    def spawn(
        self,
        context: CharacterContext,
        operation: str,
        work: Awaitable[Any],
        *,
        drain_on_shutdown: bool,
    ) -> asyncio.Task[Any] | None:
        """以明确角色作用域启动并追踪一项后台工作。"""
        if self._closing:
            _close_if_possible(work)
            logger.warning(
                "后台任务在停止过程中被拒绝 character=%s operation=%s",
                context.character_id,
                operation,
            )
            return None

        task = asyncio.create_task(
            self._run_in_context(context, work),
            name=f"soul-mate:{context.character_id}:{operation}",
        )
        self._tasks[task] = _TrackedTask(
            task=task,
            context=context,
            operation=operation,
            drain_on_shutdown=drain_on_shutdown,
            started_at=datetime.now(),
            started_monotonic=time.monotonic(),
        )
        task.add_done_callback(self._consume_completion)
        return task

    def snapshot(self) -> dict[str, Any]:
        """只读运行态快照：不暴露 task/work 对象，只暴露原始可序列化数据。

        结构：
        - running: 当前正在运行的任务列表，每项含 character_id、operation、
          drain_on_shutdown、started_at、elapsed_ms。
        - recent: 最近完成（成功/失败/取消）的有限环形历史，每项含
          character_id、operation、outcome、error_summary、started_at、
          finished_at、elapsed_ms。
        - counts: running 的当前数量与各终态结果的累计计数。

        本方法本身不应向业务抛出异常；任何内部异常都会被吞掉并返回安全的空快照。
        """
        try:
            now_monotonic = time.monotonic()
            running = [
                {
                    "character_id": tracked.context.character_id,
                    "operation": tracked.operation,
                    "drain_on_shutdown": tracked.drain_on_shutdown,
                    "started_at": tracked.started_at.isoformat(),
                    "elapsed_ms": _elapsed_ms(tracked.started_monotonic, now_monotonic),
                }
                for tracked in self._tasks.values()
            ]
            recent = [dict(entry) for entry in self._recent_history]
            counts: dict[str, int] = {"running": len(self._tasks)}
            counts.update(self._lifetime_counts)
            return {"running": running, "recent": recent, "counts": counts}
        except Exception:
            return {
                "running": [],
                "recent": [],
                "counts": {"running": 0, **{outcome: 0 for outcome in _OUTCOMES}},
            }

    async def wait_for_idle(self) -> None:
        """等待当前任务及其已派生任务完成，并消费所有异常。"""
        while self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)

    async def shutdown(self, *, drain_timeout: float = 5.0) -> None:
        """停止新任务；排空持久化工作，取消尚未发送的任务。"""
        if self._closing:
            await self.wait_for_idle()
            return

        self._closing = True
        tracked = tuple(self._tasks.values())
        cancellable = [item.task for item in tracked if not item.drain_on_shutdown]
        durable = [item.task for item in tracked if item.drain_on_shutdown]

        await _cancel_and_wait(cancellable)
        if not durable:
            return

        _, pending = await asyncio.wait(durable, timeout=drain_timeout)
        if pending:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            logger.warning(
                "后台任务关闭超时后已取消 count=%d timeout=%.1fs",
                len(pending),
                drain_timeout,
            )

    async def _run_in_context(
        self,
        context: CharacterContext,
        work: Awaitable[Any],
    ) -> Any:
        with character_scope(context):
            return await work

    def _consume_completion(self, task: asyncio.Task[Any]) -> None:
        tracked = self._tasks.pop(task, None)
        if tracked is None:
            return
        outcome: _Outcome = "succeeded"
        error_summary = ""
        try:
            task.result()
        except asyncio.CancelledError:
            outcome = "cancelled"
        except BaseException as exc:
            outcome = "failed"
            error_summary = str(exc)
            logger.error(
                "后台任务失败 character=%s operation=%s type=%s",
                tracked.context.character_id,
                tracked.operation,
                type(exc).__name__,
            )
        finally:
            self._record_outcome(tracked, outcome, error_summary)

    def _record_outcome(
        self,
        tracked: _TrackedTask,
        outcome: _Outcome,
        error_summary: str,
    ) -> None:
        """记录审计环形历史；任何内部异常都被吞掉，不再向业务传播。"""
        try:
            finished_monotonic = time.monotonic()
            self._lifetime_counts[outcome] = self._lifetime_counts.get(outcome, 0) + 1
            self._recent_history.append({
                "character_id": tracked.context.character_id,
                "operation": tracked.operation,
                "outcome": outcome,
                "error_summary": error_summary[:500],
                "started_at": tracked.started_at.isoformat(),
                "finished_at": datetime.now().isoformat(),
                "elapsed_ms": _elapsed_ms(tracked.started_monotonic, finished_monotonic),
            })
        except Exception:
            pass


async def _cancel_and_wait(tasks: list[asyncio.Task[Any]]) -> None:
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _close_if_possible(work: Awaitable[Any]) -> None:
    close = getattr(work, "close", None)
    if callable(close):
        close()


def _elapsed_ms(started_monotonic: float, now_monotonic: float) -> int:
    return max(0, int((now_monotonic - started_monotonic) * 1000))
