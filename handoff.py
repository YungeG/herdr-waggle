"""Short platform handoff, never a decision, grant or substitute for the full report."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Handoff:
    result: str
    blocker: str
    next_actor: str
    next_action: str
    user_need: str

    @classmethod
    def from_record(cls, value: object) -> "Handoff":
        if type(value) is not dict or set(value) != {"result", "blocker", "next_actor", "next_action", "user_need"}:
            raise ValueError("短交接字段缺失或不兼容。")
        for key in ("result", "blocker", "next_action"):
            text = value[key]
            if (type(text) is not str or len(text) > 160 or
                    (not text.replace("\ufeff", "").strip() and (key != "blocker" or text)) or
                    any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in text)):
                raise ValueError("短交接须为160字以内的一行非空白话；阻碍可为空。")
            text.encode("utf-8")
        if (type(value["next_actor"]) is not str or type(value["user_need"]) is not str or
                value["next_actor"] not in {"executor", "user", "none"} or value["user_need"] not in {"none", "input", "decision"}):
            raise ValueError("短交接责任或用户操作类型无效。")
        if (value["next_actor"] == "user") != (value["user_need"] != "none"):
            raise ValueError("短交接责任与是否需要用户操作不一致。")
        return cls(**value)
