"""Explicit, human-maintained business steps. No execution or report inference."""
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime
import re
from uuid import UUID

MAX_STEPS = 20
STEP_STATES = {"pending": "待处理", "doing": "进行中", "blocked": "阻塞", "done": "已记录完成"}
STEP_LIMITS = {"title": 80, "goal": 2000, "output": 4000, "evidence": 4000, "blocker": 2000, "next_action": 2000}


def step_time() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class Step:
    id: str
    title: str
    updated_at: str
    goal: str = ""
    status: str = "pending"
    output: str = ""
    evidence: str = ""
    blocker: str = ""
    next_action: str = ""
    run_ids: list[str] = field(default_factory=list)
    source: str = "human"

    @classmethod
    def from_record(cls, row: object) -> "Step":
        if not isinstance(row, dict) or set(row) != {item.name for item in fields(cls)}:
            raise ValueError("步骤字段缺失或不兼容。")
        for name, value in row.items():
            if name == "run_ids":
                if type(value) is not list or any(type(item) is not str for item in value):
                    raise ValueError("关联轮次必须是 ID 列表。")
            elif type(value) is not str:
                raise ValueError("步骤字段类型错误。")
        step = cls(**{**row, "run_ids": list(row["run_ids"])})
        if any(str(UUID(value)) != value for value in [step.id, *step.run_ids]):
            raise ValueError("步骤或关联轮次身份无效。")
        if len(set(step.run_ids)) != len(step.run_ids):
            raise ValueError("关联轮次不能重复。")
        for name, limit in STEP_LIMITS.items():
            value = getattr(step, name)
            if len(value) > limit or value != value.strip():
                raise ValueError(f"步骤 {name} 超过 {limit} 字符或含首尾空白。")
            value.encode("utf-8")
            if any(not char.isprintable() and char not in "\n\t" for char in value):
                raise ValueError("步骤文字不能包含终端控制字符。")
        if not step.title or not step.title.isprintable():
            raise ValueError("步骤标题需要 1–80 个单行可见字符。")
        if step.status not in STEP_STATES or step.source != "human":
            raise ValueError("步骤只能来自人工记录，状态必须明确。")
        if step.status == "done" and (not step.output or not step.evidence):
            raise ValueError("记录完成需要交付说明与依据；不代表整任务验收。")
        if step.status == "blocked" and (not step.blocker or not step.next_action):
            raise ValueError("记录阻塞需要原因和下一步。")
        if (not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", step.updated_at)
                or datetime.fromisoformat(step.updated_at).isoformat(timespec="milliseconds").replace("+00:00", "Z") != step.updated_at):
            raise ValueError("步骤更新时间必须是已保存的 UTC 时间。")
        return step


def validate_plan(steps: list[Step]) -> None:
    if len(steps) > MAX_STEPS or len({step.id for step in steps}) != len(steps):
        raise ValueError("每个任务最多 20 个步骤，步骤 ID 不得重复。")
    if sum(step.status in {"doing", "blocked"} for step in steps) > 1:
        raise ValueError("同时只能有一个进行中或阻塞步骤；请先明确更新当前步骤。")
