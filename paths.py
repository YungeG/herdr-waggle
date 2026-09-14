"""Bounded, single-directory completion. Called off the UI thread, never during render."""
from dataclasses import dataclass
import os
from pathlib import Path


@dataclass(frozen=True)
class DirectorySuggestions:
    paths: tuple[str, ...] = ()
    message: str = "输入绝对目录或 ~/，只提示目录，不读取文件内容。"


def directory_suggestions(value: str, *, limit: int = 12, scan_limit: int = 1000) -> DirectorySuggestions:
    if limit <= 0 or scan_limit <= 0:
        raise ValueError("提示与扫描数量必须为正数。")
    if not value:
        return DirectorySuggestions()
    if len(value) > 4096 or any(ord(char) < 32 or ord(char) == 127 for char in value):
        return DirectorySuggestions(message="路径过长或含控制字符，无法提示；原输入未改变。")
    expanded = os.path.expanduser(value)
    if not os.path.isabs(expanded):
        return DirectorySuggestions(message="请从 / 或 ~/ 开始输入；不会回退到 HOME。")
    try:
        # Exact paths remain reachable even if their parent exceeds the scan ceiling.
        if not value.endswith(os.sep) and os.path.basename(expanded) != "." and Path(expanded).is_dir():
            return DirectorySuggestions((expanded.rstrip(os.sep) + os.sep,), "↓ 选择 · Enter 填入后可继续选择子目录；Ctrl+Enter 保存项目。")
        parent, prefix = os.path.split(expanded)
        matches: list[str] = []
        scanned = 0
        with os.scandir(parent) as entries:
            for entry in entries:
                scanned += 1
                if entry.name.startswith(prefix) and (prefix.startswith(".") or not entry.name.startswith(".")):
                    try:
                        if entry.is_dir():
                            matches.append(os.path.join(parent, entry.name) + os.sep)
                    except OSError:
                        pass  # A disappearing/unreadable entry is not a candidate.
                if scanned >= scan_limit:
                    break
        message = "↓ 选择 · Enter 填入 · Tab 正常切换控件；不会自动保存。" if matches else "没有匹配目录；可继续输入。已有目录没有子目录时仍可直接保存。"
        if scanned >= scan_limit or len(matches) > limit:
            message += f" 最多显示 {limit} 项、检查 {scan_limit} 项；可输入更完整路径。"
        return DirectorySuggestions(tuple(sorted(matches)[:limit]), message)
    except OSError as error:
        return DirectorySuggestions(message=f"无法读取目录：{error.strerror or error}；原输入未改变。")
