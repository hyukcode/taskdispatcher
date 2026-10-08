from __future__ import annotations

from collections.abc import (
    Callable,
    Iterable,
    Mapping,
)

from dataclasses import dataclass

from typing import (
    Any,
    TypeVar,
)

from .checkpoint import (
    AttemptCheckpoint,
    CheckpointStore,
)

T = TypeVar("T")

# 策略恢复

@dataclass(frozen=True)
class RecoveryIssue:
    task_id: str
    reason: str
    attempt_ids: tuple[str, ...] = ()

class RecoveryBlocked(RuntimeError):
    def __init__(
        self,
        issues: list[RecoveryIssue],
    ) -> None:
        self.issues = issues

        summary = ", ".join(
            f"{issue.task_id}: {issue.reason}"
            for issue in issues
        )

        super().__init__(
            f"无法安全自动恢复：{summary}"
        )

class AttemptOutcomeUnknown(
    RuntimeError
):
    def __init__(
        self,
        *,
        task_id: str,
        attempt_id: str,
        cause: Exception,
    ) -> None:
        self.task_id = task_id
        self.attempt_id = attempt_id
        self.cause = cause
        super().__init__(
            "attempt outcome unknown: "
            f"{task_id}/{attempt_id}: "
            f"{cause}"
        )

class CheckpointCoordinator:
    def __init__(
        self,
        store: CheckpointStore,
        *,
        session_id: str,
        plan_signature: str,
    ) -> None:
        if not session_id:
            raise ValueError(
                "session_id is required"
            )
        if not plan_signature:
            raise ValueError(
                "plan_signature is required"
            )
        self.store = store
        self.session_id = session_id
        self.plan_signature = plan_signature

    def recovery_issues(
        self,
        task_ids: Iterable[str],
        resume_runs: Mapping[
            str,
            Mapping[str, Any],
        ] | None = None,
        *,
        is_resuming: bool = False,
    ) -> list[RecoveryIssue]:
        snapshots = resume_runs or {}
        issues: list[RecoveryIssue] = []
        for task_id in task_ids:
            checkpoints = self.store.list_for_task(
                self.session_id,
                task_id,
            )
            saved_raw = snapshots.get(task_id)
            saved = saved_raw if isinstance(saved_raw, Mapping) else None
            current = [
                checkpoint for checkpoint in checkpoints if checkpoint.plan_signature == self.plan_signature
            ]
            foreign = [
                checkpoint for checkpoint in checkpoints if checkpoint.plan_signature != self.plan_signature
            ]
            started = [
                checkpoint for checkpoint in checkpoints if checkpoint.status == "started"
            ]
            # 任何未确认完成的历史执行必须由人工核实 
            if started:
                issues.append(
                    RecoveryIssue(
                        task_id=task_id,
                        reason="RECOVERY_REQUIRED",
                        attempt_ids=tuple(
                            checkpoint.attempt_id
                            for checkpoint
                            in started
                        ),
                    )
                )
                continue
            foreign_failures = [
                checkpoint for checkpoint in foreign if checkpoint.status == "failed"
            ]
            # 旧计划失败已经产生副作用
            if foreign_failures:
                issues.append(
                    RecoveryIssue(
                        task_id=task_id,
                        reason=(
                            "OLD_PLAN_"
                            "REVIEW_REQUIRED"
                        ),
                        attempt_ids=tuple(
                            checkpoint.attempt_id
                            for checkpoint
                            in foreign_failures
                        ),
                    )
                )
                continue
            current_successes = [
                checkpoint
                for checkpoint in current
                if checkpoint.status == "success"
            ]
            current_failures = [
                checkpoint
                for checkpoint in current
                if checkpoint.status == "failed"
            ]
            # Session 有记录
            if saved is not None:
                saved_status = str(
                    saved.get(
                        "status",
                        "",
                    )
                )
                if saved_status == "success":
                    if not current:
                        continue
                    if not current_successes:
                        issues.append(
                           RecoveryIssue(
                                task_id=task_id,
                                reason="SNAPSHOT_MISMATCH",
                                attempt_ids=tuple(
                                    checkpoint.attempt_id
                                    for checkpoint
                                    in current
                                    if checkpoint.status
                                    not in {
                                        "abandoned",
                                        "superseded",
                                    }
                                ),
                            ) 
                        )
                        continue
                    saved_attempt_id = str(
                        saved.get(
                            "attempt_id",
                            "",
                        )
                        or ""
                    )
                    if saved_attempt_id:
                        checkpoint_ids = {
                            checkpoint.attempt_id
                            for checkpoint
                            in current_successes
                        }
                        if saved_attempt_id not in checkpoint_ids:
                            issues.append(
                                RecoveryIssue(
                                    task_id=task_id,
                                    reason="SNAPSHOT_MISMATCH",
                                    attempt_ids=tuple(
                                        checkpoint.attempt_id
                                        for checkpoint
                                        in current_successes
                                    ),
                                )
                            )
                    continue
                if saved_status in {"pending", "skipped"} and not current:
                    continue
                if (
                    saved_status == "failed"
                    and not current
                    and is_resuming
                ):
                    issues.append(
                        RecoveryIssue(
                            task_id=task_id,
                            reason=(
                                "LEGACY_"
                                "REVIEW_REQUIRED"
                            ),
                        )
                    )
                    continue

            if current_successes:
                issues.append(
                    RecoveryIssue(
                    task_id=task_id,
                    reason=(
                        "SUCCESS_WITHOUT_"
                        "SNAPSHOT"
                    ),
                    attempt_ids=tuple(
                        checkpoint.attempt_id
                        for checkpoint
                        in current_successes
                    ),
                )
            )
                continue
            if current_failures:
                issues.append(
                    RecoveryIssue(
                        task_id=task_id,
                        reason=(
                            "REVIEW_BEFORE_RETRY"
                        ),
                        attempt_ids=tuple(
                            checkpoint.attempt_id
                            for checkpoint
                            in current_failures
                        ),
                    )
                )

        return issues

    def preflight(
        self,
        task_ids: Iterable[str],
        resume_runs: Mapping[
            str, 
            Mapping[str, Any]
        ] | None = None,
        *,
        is_resuming: bool = False,
    ) -> None:
        issues = self.recovery_issues(
            task_ids,
            resume_runs,
            is_resuming=is_resuming,
        )

        if issues:
            raise RecoveryBlocked(issues)


    def run_attempt(
        self,
        *,
        task_id: str,
        execution_id: str,
        attempt_id: str,
        executor: str,
        invoke: Callable[[], T]
    ) -> T:
        self.store.start(
            session_id=self.session_id,
            task_id=task_id,
            execution_id=execution_id,
            attempt_id=attempt_id,
            executor=executor,
            plan_signature=self.plan_signature,
        )
        try:
            result = invoke()
        except Exception as exc:
            raise AttemptOutcomeUnknown(
                task_id=task_id,
                attempt_id=attempt_id,
                cause=exc,
            ) from exc

        status = getattr(
            result,
            "status",
            None,
        )

        if status not in {
            "success",
            "failed",
        }:
            raise AttemptOutcomeUnknown(
                task_id=task_id,
                attempt_id=attempt_id,
                cause=RuntimeError(
                    "runner returned "
                    f"uncertain status: "
                    f"{status!r}"
                ),
            )

        self.store.finish(
            self.session_id,
            task_id,
            attempt_id,
            success=(
                status == "success"
            ),
            exit_code=getattr(
                result,
                "exit_code",
                None,
            ),
            error=str(
                getattr(
                    result,
                    "error",
                    "",
                )
                or ""
            ),
        )
        return result

    def allow_retry(
        self,
        *,
        task_id: str,
        attempt_id: str,
        reason: str = "",
    ) -> AttemptCheckpoint:
        checkpoint = self.store.load(
            self.session_id,
            task_id,
            attempt_id,
        )
        if checkpoint is None:
            raise FileNotFoundError(attempt_id)
        if checkpoint.plan_signature != self.plan_signature:
            raise RecoveryBlocked(
                [
                    RecoveryIssue(
                        task_id=task_id,
                        reason="PLAN_CHANGED",
                        attempt_ids=(
                            attempt_id,
                        ),
                    )
                ]
            )

        if checkpoint.status in {
            "abandoned",
            "superseded",
        }:
            return checkpoint
        
        if checkpoint.status in {
            "started",
            "failed",
        }:
            return self.store.abandon_for_retry(
                self.session_id,
                task_id,
                attempt_id,
                reason=reason,
            )

        if checkpoint.status == "success":
            return self.store.supersede(
                self.session_id,
                task_id,
                attempt_id,
                reason=reason or "operator chose to rerun successful checkpoint"
            )

        raise RuntimeError(
            "unsupported checkpoint status: "
            f"{checkpoint.status}"
        )

    def supersede_tasks(
        self,
        task_ids: Iterable[str],
        *,
        reason: str,
    ) -> list[AttemptCheckpoint]:
        task_ids = tuple(
            dict.fromkeys(task_ids)
        )
        blockers: list[RecoveryIssue] = []
        targets: list[AttemptCheckpoint] = []
        for task_id in task_ids:
            checkpoints = [
                checkpoint
                for checkpoint in self.store.list_for_task(
                    self.session_id,
                    task_id,
                )
                if checkpoint.plan_signature == self.plan_signature
            ]
            started = [
                checkpoint
                for checkpoint in checkpoints
                if checkpoint.status == "started"
            ]
            if started:
                blockers.append(
                    RecoveryIssue(
                        task_id=task_id,
                        reason="RECOVERY_REQUIRED",
                        attempt_ids=tuple(
                            checkpoint.attempt_id
                            for checkpoint
                            in started
                        ),
                    )
                )
                continue
            targets.extend(
                checkpoint
                for checkpoint in checkpoints
                if checkpoint.status
                not in {
                    "started",
                    "superseded",
                }
            )
        if blockers:
            raise RecoveryBlocked(blockers)
        
        updated: list[AttemptCheckpoint] = []

        for checkpoint in targets:
            updated.append(
                self.store.supersede(
                    self.session_id,
                    checkpoint.task_id,
                    checkpoint.attempt_id,
                    reason=reason,
                )
            )
        return updated

    def prepare_retry(
        self,
        *,
        task_id: str,
        attempt_id: str,
        invalidated_task_ids: Iterable[str],
        reason: str = "",
    ) -> AttemptCheckpoint:
        invalidated = tuple(
            dict.fromkeys(invalidated_task_ids)
        )
        target = self.store.load(
            self.session_id,
            task_id,
            attempt_id,
        )
        if target is None:
            raise FileNotFoundError(attempt_id)
        if target.plan_signature != self.plan_signature:
            raise RecoveryBlocked(
                [
                    RecoveryIssue(
                        task_id=task_id,
                        reason="PLAN_CHANGED",
                        attempt_ids=(
                            attempt_id,
                        ),
                    )
                ]
            )
        blockers: list[RecoveryIssue] = []
        for current_task_id in invalidated:
            checkpoints = [
                checkpoint
                for checkpoint
                in self.store.list_for_task(
                    self.session_id,
                    current_task_id,
                )
                if checkpoint.plan_signature == self.plan_signature
            ]

            unknown = [
                checkpoint for checkpoint in checkpoints
                if checkpoint.status == "started" and checkpoint.attempt_id != attempt_id
            ]

            if unknown:
                blockers.append(
                    RecoveryIssue(
                        task_id=current_task_id,
                        reason="RECOVERY_REQUIRED",
                        attempt_ids=tuple(
                            checkpoint.attempt_id
                            for checkpoint
                            in unknown
                        ),
                    )
                )
        if blockers:
            raise RecoveryBlocked(blockers)
        
        resolved = self.allow_retry(
            task_id=task_id,
            attempt_id=attempt_id,
            reason=reason,
        )

        for current_task_id in invalidated:
            checkpoints = [
                checkpoint
                for checkpoint
                in self.store.list_for_task(
                    self.session_id,
                    current_task_id,
                )
                if checkpoint.plan_signature == self.plan_signature
            ]
            for checkpoint in checkpoints:
                if checkpoint.attempt_id == attempt_id:
                    continue
                if checkpoint.status in {
                    "started",
                    "superseded",
                }:
                    continue
                self.store.supersede(
                    self.session_id,
                    current_task_id,
                    checkpoint.attempt_id,
                    reason=(
                        "invalidated by recovery retry "
                        f"of {task_id}"
                    ),
                )
        return resolved
        