from __future__ import annotations

import hashlib
import re
import time

from dataclasses import replace

from .domain.memory import (
    MemoryKind,
    MemoryRecord,
    MemoryScope,
    new_memory_record,
)

from .memory_store import MemoryStore

from .artifact_store import ArtifactStore
from .domain.artifact import ArtifactKind

from .models import SubTask, TaskRun

class MemoryManager:
    def __init__(
        self,
        store: MemoryScope,
        *,
        session_id: str,
        artifact_store: ArtifactStore | None = None,
        session_limit: int = 8,
        long_term_limit: int = 4,
        task_result_max_chars: int = 4000,
        artifact_threshold_chars: int = 3000,
        artifact_preview_chars: int = 800,
        context_max_chars: int = 6000,
        long_term_enabled: bool = False,
    ) -> None:
        self.store = store
        self.artifact_store = artifact_store
        self.session_id = session_id
        self.artifact_threshold_chars = max(500, artifact_threshold_chars)
        self.artifact_preview_chars = max(100, artifact_preview_chars)
        self.session_limit = max(0, session_limit)
        self.long_term_limit = max(0, long_term_limit)
        self.task_result_max_chars = max(200, task_result_max_chars)
        self.context_max_chars = max(500, context_max_chars)
        self.long_term_enabled = long_term_enabled
    
    @staticmethod
    def _stable_id(
        *,
        scope: MemoryScope,
        session_id: str,
        kind: MemoryKind,
        key: str,
    ) -> str:
        raw = (
            f"{scope.value}|"
            f"{session_id}|"
            f"{kind.value}|"
            f"{key}"
        )

        digest = hashlib.sha256(
            raw.encode("utf-8")
        ).hexdigest()[:24]

        return f"m-{digest}"

   
    def remember_session(
        self,
        *,
        kind: MemoryKind,
        content: str,
        key: str,
        source_task_id: str = "",
        tags: tuple[str, ...] = (),
        artifact_ids: tuple[
            str, ...
        ] = (),
        priority: int = 50,
    ) -> MemoryKind:
        content = content.strip()
        if not content:
            raise ValueError("memory content is empty")
        memory_id = self._stable_id(
            scope=MemoryScope.SESSION,
            session_id=self.session_id,
            kind=kind,
            key=key,
        )
        existing = self.store.load(
            scope=MemoryScope.SESSION,
            session_id=self.session_id,
            memory_id=memory_id,
        )
        if existing is None:
            record = new_memory_record(
                memory_id=memory_id,
                scope=MemoryScope.SESSION,
                kind=kind,
                content=content,
                session_id=self.session_id,
                source_task_id=(
                    source_task_id
                ),
                key=key,
                tags=tags,
                artifact_ids=artifact_ids,
                priority=priority,
            )
        else:
            record = replace(
                existing,
                content=content,
                source_task_id=(
                    source_task_id
                    or existing.source_task_id
                ),
                tags=tags or existing.tags,
                artifact_ids=(
                    artifact_ids
                    or existing.artifact_ids
                ),
                priority=max(
                    0,
                    min(100, priority),
                ),
                updated_at=time.time(),
            )
        return self.store.save(record)
    
    def record_task_run(
        self,
        run: TaskRun,
    ) -> MemoryRecord | None:
    # 只记录成功的信息，失败信息有checkpoint taskrun eventlog recoveryhistory
        if run.status != "success":
            return None
        output = (run.output or "").strip()
        if not output:
            return None
        artifact_ids: tuple[str, ...] = ()
        if (
            self.artifact_store is None
            or len(output) <= self.artifact_threshold_chars
        ):
            memory_content = (
                f"任务 {run.task.id} "
                f"({run.task.title})"
                f"执行成功。\n"
                f"{output[:self.task_result_max_chars]}"
            )
        else:
            artifact = (
                self.artifact_store.save_text(
                    task_id=run.task.id,
                    name=(
                        f"{run.task.id}-output.txt"
                    ),
                    content=output,
                    kind=ArtifactKind.TASK_OUTPUT,
                )
            )
            artifact_ids = (
                artifact.id,
            )

            preview = output[
                :self.artifact_preview_chars
            ]

            memory_content = (
                f"任务 {run.task.id} "
                f"({run.task.title}) "
                "执行成功。\n\n"
                "完整输出已保存为 Artifact：\n"
                f"- id: {artifact.id}\n"
                f"- path: "
                f"{artifact.relative_path}\n"
                f"- size: "
                f"{artifact.size_bytes} bytes\n"
                f"- sha256: "
                f"{artifact.sha256}\n\n"
                "输出预览：\n"
                f"{preview}"
            )
        return self.remember_session(
            kind=MemoryKind.TASK_RESULT,
            key=(
                f"task-result:"
                f"{run.task.id}"
            ),
            content=memory_content,
            source_task_id=(
                run.task.id
            ),
            tags=(
                "task-result",
                run.task.id,
                run.task.executor,
            ),
            artifact_ids=artifact_ids,
            priority=70,
        )

    def promote(
        self,
        record: MemoryRecord,
        *,
        key: str,
        priority: int | None = None,
    ) -> MemoryRecord:
        if (
            record.scope
            != MemoryScope.SESSION
        ):
            raise ValueError(
                "only session memory can be promoted"
            )

        if record.artifact_ids:
            raise ValueError("memory with session artifacts cannot be promoted directly")

        memory_id = self._stable_id(
            scope=MemoryScope.LONG_TERM,
            session_id="",
            kind=record.kind,
            key=key,
        )

        promoted = new_memory_record(
            memory_id=memory_id,
            scope=(
                MemoryScope.LONG_TERM
            ),
            kind=record.kind,
            content=record.content,
            source_task_id=record.source_task_id,
            key=key,
            tags=record.tags,
            priority=(
                record.priority
                if priority is None
                else priority
            ),
        )

        return self.store.save(promoted)
    
    @staticmethod
    def _terms(text: str) -> set[str]:
        return {
            token.casefold()
            for token in re.findall(
                r"[A-Za-z0-9_./-]+|"
                r"[\u4e00-\u9fff]{2,}",
                text,
            )
            if token.strip()
        }

    def _score(
        self,
        record: MemoryRecord,
        query: str,
    ) -> tuple[int, int, float]:

        terms = self._terms(
            query
        )

        haystack = (
            record.content
            + " "
            + " ".join(record.tags)
            + " "
            + record.key
        ).casefold()

        matches = sum(
            1
            for term in terms
            if term in haystack
        )

        return (
            matches,
            record.priority,
            record.updated_at,
        )
    
    def _relevant(
        self,
        records: list[MemoryRecord],
        *,
        query: str,
        limit: int,
    ) -> list[MemoryRecord]:

        if limit <= 0:
            return []

        ranked = sorted(
            records,
            key=lambda record: (
                self._score(
                    record,
                    query,
                )
            ),
            reverse=True,
        )

        return ranked[:limit]

    def _task_result_memory(
        self,
        task_id: str,
    ) -> MemoryRecord | None:

        memory_id = self._stable_id(
            scope=MemoryScope.SESSION,
            session_id=self.session_id,
            kind=MemoryKind.TASK_RESULT,
            key=(
                f"task-result:"
                f"{task_id}"
            ),
        )

        return self.store.load(
            scope=MemoryScope.SESSION,
            session_id=self.session_id,
            memory_id=memory_id,
        )

    def build_working_memory(
        self,
        *,
        node: SubTask,
        dependency_runs: list[TaskRun],
        session_state: dict,
    ) -> str:
        sections: list[str] = []
        dependency_task_ids = {
            run.task.id
            for run in dependency_runs
        }
        for run in dependency_runs:
            if run.status != "success" or not run.output:
                continue
            remembered = (
                self._task_result_memory(
                    run.task.id
                )
            )

            # 优先使用 Memory 版本，大输出时它已经变成Artifact preview。
            if remembered is not None:
                sections.append(
                    "[直接依赖]\n"
                    + remembered.content
                )
                continue

            # 兼容旧 Session。
            output = (
                run.output
                or ""
            ).strip()

            if not output:
                continue

            preview = output[
                :self.artifact_preview_chars
            ]

            if len(output) > len(preview):

                preview += (
                    "\n...[旧任务输出已截断]..."
                )

            sections.append(
                "[直接依赖]\n"
                f"{run.task.id} "
                f"{run.task.title}\n"
                f"{preview}"
            )

        feedback = str(session_state.get("feedback", "") or "").strip()
        if feedback:
            sections.append(
                "[上一轮反馈]\n"
                + feedback
            )
        # session memory
        session_records = self.store.list_session(self.session_id)
        session_records = [
            record
            for record
            in session_records
            if not (
                record.kind == MemoryKind.TASK_RESULT
                and record.source_task_id in dependency_task_ids
            )
        ]
        query = " ".join(
            [
                node.title,
                node.description,
                node.acceptance,
                node.tool,
            ]
        )

        relevant_session = self._relevant(
            session_records,
            query=query,
            limit=self.session_limit,
        )

        for record in relevant_session:
            sections.append(
                "[Session Memory "
                f"{record.kind.value}]\n"
                f"{record.content}"
            )
        
        if self.long_term_enabled:
            long_term_records = self.store.list_long_term()
            relevant_long_term = self._relevant(
                long_term_records,
                query=query,
                limit=self.long_term_limit,
            )
            for record in relevant_long_term:
                sections.append(
                    "[Long-term Memory "
                    f"{record.kind.value}]\n"
                    f"{record.content}"
                )
        return self._render_budgeted(sections)

    def _render_budgeted(
        self,
        sections: list[str],
    ) -> str:
        if not sections:
            return ""
        remaining = self.context_max_chars
        result: list[str] = []
        for section in sections:
            if remaining <= 0:
                break
            if len(section) <= remaining:
                result.append(section)
                remaining -= len(section) + 2
                continue
            if remaining >= 80:
                result.append(section[:remaining-30]+"\n...[Memory 截断]...")
            break
        return "\n\n".join(result)

