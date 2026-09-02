

from __future__ import annotations

import json
import hashlib
import logging
import secrets
import threading
import time
from pathlib import Path

from .config import SessionConfig
from .models import CompiledGraph, Session, graph_from_dict, graph_to_dict, is_valid_id, validate_graph


logger = logging.getLogger(__name__)


def new_session_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _session_to_dict(s: Session) -> dict:
    return {
        "session_id": s.session_id,
        "goal": s.goal,
        "status": s.status,
        "iteration": s.iteration,
        "state": s.state,
        "refs": s.refs,
        "plan_signature": s.plan_signature,
        "task_runs": s.task_runs,
        "deleted_at": s.deleted_at,
        "deleted_from_status": s.deleted_from_status,
        "history": s.history,
        "created_at": s.created_at,
        "updated_at": s.updated_at,
    }


def _session_from_dict(d: dict) -> Session:
    if not isinstance(d, dict):
        raise ValueError("session.json 根节点必须是 JSON 对象")
    state = d.get("state") or {}
    refs = d.get("refs") or {}
    task_runs = d.get("task_runs") or {}
    history = d.get("history") or []
    if not all(isinstance(value, dict) for value in (state, refs, task_runs)):
        raise ValueError("session.json 的 state、refs、task_runs 必须是对象")
    if not isinstance(history, list):
        raise ValueError("session.json 的 history 必须是数组")
    session_id = str(d.get("session_id", "") or "")
    status = str(d.get("status", "running") or "running")
    if status not in {"running", "goal_achieved", "failed", "paused", "stopped", "deleted"}:
        raise ValueError(f"session.json 的 status 非法: {status}")
    return Session(
        session_id=session_id,
        goal=d.get("goal", ""),
        status=status,
        iteration=int(d.get("iteration", 0)),
        state=state,
        refs=refs,
        plan_signature=str(d.get("plan_signature", "") or ""),
        task_runs=task_runs,
        deleted_at=str(d.get("deleted_at", "") or ""),
        deleted_from_status=str(d.get("deleted_from_status", "") or ""),
        history=history,
        created_at=d.get("created_at", ""),
        updated_at=d.get("updated_at", ""),
    )


class SessionStore:

    def __init__(self, cfg: SessionConfig | None = None, base_dir: str | Path | None = None):
        self.cfg = cfg or SessionConfig()
        self.base = Path(base_dir) if base_dir else self.cfg.path
        self.base.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.RLock()

    def dir_for(self, session_id: str) -> Path:
        if not is_valid_id(session_id):
            raise ValueError(f"非法 session_id: {session_id}")
        return self.base / session_id

    def workspace(self, session_id: str | None = None) -> Path:
        d = self.cfg.workspace_path
        if session_id:
            if not is_valid_id(session_id):
                raise ValueError(f"非法 session_id: {session_id}")
            d = d / session_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def events_file(self, session_id: str, task_id: str) -> Path:
        if not is_valid_id(task_id):
            raise ValueError(f"非法 task_id: {task_id}")
        d = self.dir_for(session_id) / "events"
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{task_id}.events.jsonl"

    def save(self, session: Session, plan: CompiledGraph | None = None) -> Path:
        if not session.session_id:
            raise ValueError("session_id 不能为空")
        d = self.dir_for(session.session_id)
        d.mkdir(parents=True, exist_ok=True)
        session.updated_at = _now()
        if not session.created_at:
            session.created_at = _now()
        with self._write_lock:
            if plan is not None:
                validate_graph(plan)
                session.plan_signature = graph_signature(plan)
                _atomic_write(d / "plan.json", json.dumps(graph_to_dict(plan), ensure_ascii=False, indent=2))
            _atomic_write(d / "session.json", json.dumps(_session_to_dict(session), ensure_ascii=False, indent=2))
        return d

    def append_event(self, session_id: str, task_id: str, event: dict) -> None:
        try:
            f = self.events_file(session_id, task_id)
            with open(f, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
        except (OSError, TypeError, ValueError) as exc:
            # 事件写入是 best-effort，但不能静默掩盖磁盘或 ID 错误。
            logger.warning("写入会话事件失败（%s/%s）: %s", session_id, task_id, exc)

    def load(self, session_id: str) -> Session | None:
        try:
            f = self.dir_for(session_id) / "session.json"
        except ValueError:
            return None
        if not f.exists():
            return None
        try:
            session = _session_from_dict(json.loads(f.read_text(encoding="utf-8")))
            if session.session_id not in ("", session_id):
                raise ValueError("session.json 的 session_id 与路径不一致")
            # 文件缺少旧版本 session_id 时，以受过路径校验的查询参数补齐。
            session.session_id = session_id
            return session
        except (json.JSONDecodeError, OSError, TypeError, ValueError) as exc:
            logger.warning("读取会话 %s 失败: %s", session_id, exc)
            return None

    def load_plan(self, session_id: str) -> CompiledGraph | None:
        try:
            f = self.dir_for(session_id) / "plan.json"
        except ValueError:
            return None
        if not f.exists():
            return None
        try:
            graph = graph_from_dict(json.loads(f.read_text(encoding="utf-8")))
            validate_graph(graph)
            return graph
        except (json.JSONDecodeError, OSError, TypeError, ValueError) as exc:
            logger.warning("读取会话 %s 的任务图失败: %s", session_id, exc)
            return None

    def list(self, *, include_deleted: bool = False) -> list[dict]:
        out: list[dict] = []
        for d in sorted(self.base.iterdir()):
            if not d.is_dir():
                continue
            f = d / "session.json"
            if not f.exists():
                continue
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                logger.debug("跳过损坏的会话文件 %s: %s", f, exc, exc_info=True)
                continue
            if not include_deleted and (data.get("status") == "deleted" or data.get("deleted_at")):
                continue
            out.append(
                {
                    "session_id": data.get("session_id", d.name),
                    "goal": (data.get("goal") or "")[:80],
                    "status": data.get("status", ""),
                    "iteration": data.get("iteration", 0),
                    "updated_at": data.get("updated_at", ""),
                }
            )
        return out

    def soft_delete(self, session_id: str) -> Session | None:
        """逻辑删除会话，保留 plan、events 和 workspace 以便审计或恢复。"""
        session = self.load(session_id)
        if session is None:
            return None
        if session.status == "deleted":
            return session
        session.deleted_from_status = session.status
        session.status = "deleted"
        session.deleted_at = _now()
        self.save(session)
        return session

    def restore(self, session_id: str) -> Session | None:
        """恢复逻辑删除的会话；恢复后由 /resume 决定继续执行还是新规划。"""
        session = self.load(session_id)
        if session is None or session.status != "deleted":
            return session
        session.status = session.deleted_from_status or "stopped"
        session.deleted_at = ""
        session.deleted_from_status = ""
        self.save(session)
        return session


def graph_signature(graph: CompiledGraph) -> str:
    payload = json.dumps(graph_to_dict(graph), ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _atomic_write(path: Path, text: str) -> None:
    """先写临时文件再替换，避免进程中断留下半个 JSON。"""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
