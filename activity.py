"""Strict optional current activity DTO. Never persisted in task state or history."""
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
import re

from model import Run

MAX_ACTIVITY_BYTES = 16 * 1024
STATES = {"queued": "排队", "running": "运行", "complete": "完成", "failed": "失败", "partial": "部分完成",
          "paused": "暂停", "stopped": "停止", "rejected": "拒绝", "unknown": "未知"}


def require(condition: bool) -> None:
    if not condition:
        raise ValueError("活动快照版本、字段或边界无效。")


def shape(row: dict, keys: set[str]) -> None:
    require(type(row) is dict and set(row) == keys)


def timestamp(value: int, observed: int) -> None:
    require(type(value) is int and 0 < value <= min(observed + 2000, 253_402_214_399_000))


def token(value: str) -> bool:
    return type(value) is str and 0 < len(value) <= 160 and all(33 <= ord(char) <= 126 for char in value)


def stamp(value: int) -> str:
    return datetime.fromtimestamp(value / 1000).astimezone().strftime("%m-%d %H:%M:%S")


@dataclass(frozen=True)
class Tool:
    id: str
    name: str
    started_at: int

    @classmethod
    def from_record(cls, row: dict, observed: int) -> "Tool":
        shape(row, {"id", "name", "started_at"})
        require(token(row["id"]) and type(row["name"]) is str and re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", row["name"]) is not None)
        timestamp(row["started_at"], observed)
        return cls(**row)


@dataclass(frozen=True)
class ToolEnd(Tool):
    ended_at: int
    is_error: bool

    @classmethod
    def from_record(cls, row: dict, observed: int) -> "ToolEnd":
        shape(row, {"id", "name", "started_at", "ended_at", "is_error"})
        Tool.from_record({key: row[key] for key in ("id", "name", "started_at")}, observed)
        timestamp(row["ended_at"], observed)
        require(row["ended_at"] >= row["started_at"] and type(row["is_error"]) is bool)
        return cls(**row)


@dataclass(frozen=True)
class Child:
    id: str
    state: str


@dataclass(frozen=True)
class Activity:
    run_id: str
    agent_id: str
    session_id: str
    cwd: str
    seq: int
    observed_at: int
    bound: bool
    tools: tuple[Tool, ...]
    last_tool: ToolEnd | None
    children: tuple[Child, ...]
    children_observed_at: int | None
    overflow: bool

    def matches(self, run: Run) -> bool:
        return (self.run_id, self.agent_id, self.session_id, self.cwd) == (run.id, run.agent_id, run.session_id, run.cwd)

    @classmethod
    def from_record(cls, row: dict, run: Run) -> "Activity":
        shape(row, set(cls.__annotations__) | {"version"})
        require(type(row["version"]) is int and row["version"] == 1)
        require(type(row["seq"]) is int and 0 < row["seq"] <= 2**53 - 1)
        require(type(row["bound"]) is bool and type(row["overflow"]) is bool)
        observed = row["observed_at"]
        require(type(observed) is int)
        timestamp(observed, observed)
        if row["children_observed_at"] is not None:
            timestamp(row["children_observed_at"], observed)
        for key in ("tools", "children"):
            require(type(row[key]) is list and len(row[key]) <= 8)
        tools = tuple(Tool.from_record(tool, observed) for tool in row["tools"])
        children = []
        for child in row["children"]:
            shape(child, {"id", "state"})
            require(token(child["id"]) and type(child["state"]) is str and child["state"] in STATES)
            children.append(Child(**child))
        require(len({tool.id for tool in tools}) == len(tools) and len({child.id for child in children}) == len(children))
        last = ToolEnd.from_record(row["last_tool"], observed) if row["last_tool"] is not None else None
        require(last is None or last.id not in {tool.id for tool in tools})
        data = {key: value for key, value in row.items() if key != "version"}
        data.update(tools=tools, children=tuple(children), last_tool=last)
        result = cls(**data)
        require(result.matches(run))
        return result

    def check_after(self, previous: "Activity | None") -> None:
        if previous is not None and (self.seq < previous.seq or (self.seq == previous.seq and self != previous)):
            raise ValueError("活动修订倒退或同修订内容改变；保留上次观测。")

    def describe(self, now_ms: int, *, issue: str = "") -> str:
        if not self.bound:
            return f"活动归属未确认 · 上次观测 {stamp(self.observed_at)}"
        if issue or not -2000 <= now_ms - self.observed_at <= 10_000:
            return f"当前未知 · 上次观测 {stamp(self.observed_at)}"
        if self.tools:
            tool = self.tools[0]
            text = f"工具 {tool.name} · 开始 {stamp(tool.started_at)}"
            if len(self.tools) > 1:
                text += f" · 另 {len(self.tools) - 1} 个工具"
        elif self.last_tool:
            tool = self.last_tool
            text = f"工具 {tool.name} {'返回失败' if tool.is_error else '已返回'} · {stamp(tool.ended_at)}"
        else:
            text = "当前步骤未上报"
        if self.children:
            fresh = self.children_observed_at is not None and -2000 <= now_ms - self.children_observed_at <= 10_000
            counts = Counter(child.state if fresh else "unknown" for child in self.children)
            text += "\n后台执行 · " + " / ".join(f"{STATES[state]} {count}" for state, count in sorted(counts.items()))
            text += f" · 核对 {stamp(self.children_observed_at)}" if self.children_observed_at else " · 尚未核实"
        if self.overflow:
            text += "\n仅部分活动；另有超出记录上限的项目"
        return text
