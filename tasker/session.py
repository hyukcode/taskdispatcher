

from __future__ import annotations

import json
import secrets
import time
from pathlib import Path

from .config import SessionConfig
from .models import CompiledGraph, Session, graph_from_dict, graph_to_dict


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
        "history": s.history,
        "created_at": s.created_at,
        "updated_at": s.updated_at,
    }


def _session_from_dict(d: dict) -> Session:
    return Session(
        session_id=d.get("session_id", ""),
        goal=d.get("goal", ""),
        status=d.get("status", "running"),
        iteration=int(d.get("iteration", 0)),
        state=d.get("state") or {},
        refs=d.get("refs") or {},
        history=d.get("history") or [],
        created_at=d.get("created_at", ""),
        updated_at=d.get("updated_at", ""),
    )


class SessionStore:

    def __init__(self, cfg: SessionConfig | None = None, base_dir: str | Path | None = None):
        self.cfg = cfg or SessionConfig()
        self.base = Path(base_dir) if base_dir else self.cfg.path
        self.base.mkdir(parents=True, exist_ok=True)

    def dir_for(self, session_id: str) -> Path:
        return self.base / session_id

    def workspace(self, session_id: str | None = None) -> Path:
        d = self.cfg.workspace_path
        if session_id:
            d = d / session_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def events_file(self, session_id: str, task_id: str) -> Path:
        d = self.dir_for(session_id) / "events"
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{task_id}.events.jsonl"

    def save(self, session: Session, plan: CompiledGraph | None = None) -> Path:
        d = self.dir_for(session.session_id)
        d.mkdir(parents=True, exist_ok=True)
        session.updated_at = _now()
        if not session.created_at:
            session.created_at = _now()
        (d / "session.json").write_text(
            json.dumps(_session_to_dict(session), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if plan is not None:
            (d / "plan.json").write_text(
                json.dumps(graph_to_dict(plan), ensure_ascii=False, indent=2), encoding="utf-8"
            )
        return d

    def append_event(self, session_id: str, task_id: str, event: dict) -> None:
        try:
            f = self.events_file(session_id, task_id)
            with open(f, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
        except Exception:
            pass

    def load(self, session_id: str) -> Session | None:
        f = self.dir_for(session_id) / "session.json"
        if not f.exists():
            return None
        try:
            return _session_from_dict(json.loads(f.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            return None

    def load_plan(self, session_id: str) -> CompiledGraph | None:
        f = self.dir_for(session_id) / "plan.json"
        if not f.exists():
            return None
        try:
            return graph_from_dict(json.loads(f.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            return None

    def list(self) -> list[dict]:
        out: list[dict] = []
        for d in sorted(self.base.iterdir()):
            if not d.is_dir():
                continue
            f = d / "session.json"
            if not f.exists():
                continue
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
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
