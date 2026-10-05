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
                checkpoint
                for checkpoint
                in checkpoints
                if checkpoint.plan_signature == self.plan_signature
            ]

            foreign = [
                checkpoint
                for checkpoint
                in checkpoints
                if checkpoint.plan_signature != self.plan_signature
            ]

            attempt_ids = tuple(
                checkpoint.attempt_id
                for checkpoint
                in checkpoints
            )

            # 任何未确认完成的历史执行必须由人工核实
            if any(
                checkpoint.status
                == "started"
                for checkpoint
                in checkpoints
            ):
                issues.append(
                    RecoveryIssue(
                        task_id=task_id,
                        reason="RECOVERY_REQUIRED",
                        attempt_ids=attempt_ids,
                    )
                )
                continue

            # 旧计划失败已经产生副作用
            if any(checkpoint.status == "failed"
                for checkpoint in foreign
            ):
                issues.append(
                    RecoveryIssue(
                        task_id=task_id,
                        reason="OLD_PLAN_REVIEW_REQUIRED",
                        attempt_ids=attempt_ids,
                    )
                )
                continue
            
            current_successes = [
                checkpoint for checkpoint in current
                if checkpoint.status == "success"
            ]

            current_failures = [
                checkpoint for checkpoint in current
                if checkpoint.status == "failed"
            ]
            
            # Session 有记录
            if saved is not None:
                saved_status = str(
                    saved.get(
                        "status", ""
                    )
                )
                if saved_status == "success":
                    if not current:
                        continue
                    if not current_successes:
                        issues.append(
                            RecoveryIssue(
                                task_id=task_id,
                                reason=(
                                    "SNAPSHOT_"
                                    "MISMATCH"
                                ),
                                attempt_ids=attempt_ids,
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
                        if (
                            saved_attempt_id
                            not in checkpoint_ids
                        ):
                            issues.append(
                                RecoveryIssue(
                                    task_id=task_id,
                                    reason=(
                                        "SNAPSHOT_"
                                        "MISMATCH"
                                    ),
                                    attempt_ids=attempt_ids,
                                )
                            )
                    continue
                
                if saved_status in {"pending", "skipped"} and not current:
                    continue
                
                if saved_status == "failed" and not current and is_resuming:
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
                        attempt_ids=attempt_ids,
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
                        attempt_ids=attempt_ids,
                    )
                )

                continue

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

