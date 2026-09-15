"""Revision-checked local snapshots with an atomic, backed-up v1 migration."""
from dataclasses import asdict, fields
import json
import os
from pathlib import Path
import sqlite3

from journal import validate_events
from model import AGENTS, PROJECTS, RUNS, STATUSES, Board, Run, Task, valid_uuid

APPLICATION_ID = 0x54524D31
SCHEMA_VERSION = 2


class StorageError(RuntimeError):
    """A rejected or failed disk operation; callers must not publish the candidate."""


def default_path() -> Path:
    state = os.environ.get("XDG_STATE_HOME", "")
    root = Path(state) if state and Path(state).is_absolute() else Path.home() / ".local" / "state"
    return root / "taskroom" / "tasks.sqlite3"


def _tasks(board: Board, rows: object, *, legacy: bool = False) -> None:
    if not isinstance(rows, list):
        raise StorageError("任务数据应为列表。")
    names = {item.name for item in fields(Task)} - ({"execution", "run_id"} if legacy else set())
    for row in rows:
        if not isinstance(row, dict) or set(row) != names:
            raise StorageError("任务字段缺失或不兼容，拒绝用默认值覆盖。")
        for name, value in row.items():
            expected = int if name == "id" else bool if name == "full_delivery" else list if name in ("checks", "events") else str
            if type(value) is not expected or (isinstance(value, list) and any(type(item) is not str for item in value)):
                raise StorageError(f"任务字段 {name} 的类型不正确。")
        task = Task(**row)
        if task.id <= 0 or task.id in board.tasks or not task.title.strip():
            raise StorageError("任务 ID 重复/无效或标题为空，拒绝覆盖原数据。")
        if task.project not in board.projects or task.status not in STATUSES or task.run not in RUNS:
            raise StorageError("任务项目或状态与当前版本不兼容。")
        if task.execution not in ("simulation", "pi"):
            raise StorageError("未知执行方式。")
        if task.execution == "simulation" and (task.agent not in ("", *AGENTS) or task.run_id):
            raise StorageError("模拟任务的执行者/执行 ID 无效。")
        if task.execution == "pi" and task.agent and not valid_uuid(task.agent):
            raise StorageError("Pi 执行者身份无效。")
        for event in validate_events(task.events):
            if event.source not in {"human", "system", "simulation"} or (not event.run_id and event.seq):
                raise StorageError("任务关键记录来源或修订锚点无效。")
        board.tasks[task.id] = task


def encode(board: Board) -> str:
    return json.dumps({"live": board.live, "projects": board.projects,
                       "tasks": [asdict(task) for task in board.tasks.values()],
                       "runs": [asdict(run) for run in board.runs.values()]}, ensure_ascii=False)


def decode(data: str, *, legacy: bool = False) -> Board:
    """Exact schema and links; never turn a legacy simulation into real work."""
    try:
        raw = json.loads(data)
        board = Board(live=True)
        if legacy:
            board.projects = dict(PROJECTS)
            _tasks(board, raw, legacy=True)
            return board
        if not isinstance(raw, dict) or set(raw) != {"live", "projects", "tasks", "runs"} or type(raw["live"]) is not bool:
            raise StorageError("任务库信封不兼容。")
        projects = raw["projects"]
        if not isinstance(projects, dict) or "收集箱" not in projects:
            raise StorageError("项目目录缺失。")
        if any(type(name) is not str or not name.strip() or name == "all" or type(path) is not str
               or (name != "收集箱" and not path.startswith("/")) for name, path in projects.items()):
            raise StorageError("项目名或绝对目录无效。")
        paths = [path for name, path in projects.items() if name != "收集箱"]
        if len(paths) != len(set(paths)):
            raise StorageError("项目目录重复。")
        board.live, board.projects = raw["live"], projects
        _tasks(board, raw["tasks"])
        if not isinstance(raw["runs"], list):
            raise StorageError("执行记录应为列表。")
        for row in raw["runs"]:
            run = Run.from_record(row)
            if run.id in board.runs or run.task_id not in board.tasks or board.tasks[run.task_id].execution != "pi":
                raise StorageError("执行记录重复或关联任务无效。")
            board.runs[run.id] = run
        active_agents: set[str] = set()
        active_tasks: set[int] = set()
        for run in board.runs.values():
            if not run.terminal:
                if run.agent_id in active_agents or run.task_id in active_tasks or board.tasks[run.task_id].run_id != run.id:
                    raise StorageError("执行身份冲突；不会并发启动。")
                active_agents.add(run.agent_id)
                active_tasks.add(run.task_id)
        for task in board.tasks.values():
            for event in validate_events(task.events):
                if event.run_id:
                    owner = board.runs.get(event.run_id)
                    if not owner or owner.task_id != task.id or event.seq > owner.seq:
                        raise StorageError("任务关键记录指向不存在/其他任务的执行或未来修订。")
            if task.execution != "pi":
                continue
            run = board.runs.get(task.run_id)
            if task.run_id and (not run or run.task_id != task.id):
                raise StorageError("当前执行关联无效。")
            if task.run != "none" and not run:
                raise StorageError("真实任务缺少执行记录。")
            if task.full_delivery and (not run or run.state != "finished" or run.delivery != "full"):
                raise StorageError("完整交付尚未结束执行。")
        return board
    except (ValueError, TypeError) as error:
        raise StorageError(f"任务数据损坏或不兼容；请保留原库: {error}") from error


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().absolute()
        self.revision: int | None = None
        connection = None
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                pass  # Existing files are never chmodded, truncated or replaced.
            else:
                os.close(descriptor)
            connection = sqlite3.connect(self.path, timeout=0.2)
            self.connection = connection
            self._initialize()
        except (OSError, sqlite3.Error, StorageError) as error:
            if connection is not None:
                connection.close()
            raise StorageError(f"无法打开任务库 {self.path}: {error}") from error

    def _check_format(self) -> None:
        app_id = self.connection.execute("PRAGMA application_id").fetchone()[0]
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if app_id != APPLICATION_ID or version != SCHEMA_VERSION:
            raise StorageError("不是本应用的任务库，或数据库版本不受支持；不会迁移或覆盖。")

    def _initialize(self) -> None:
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            app_id = self.connection.execute("PRAGMA application_id").fetchone()[0]
            version = self.connection.execute("PRAGMA user_version").fetchone()[0]
            objects = self.connection.execute("SELECT name FROM sqlite_master").fetchall()
            if app_id == 0 and version == 0 and not objects:
                self.connection.execute("CREATE TABLE state (singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision INTEGER NOT NULL, data TEXT NOT NULL)")
                self.connection.execute("INSERT INTO state VALUES (1, 0, ?)", (encode(Board(live=True)),))
                self.connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
                self.connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            elif app_id == APPLICATION_ID and version == 1:
                revision, original = self._read_state()
                migrated = encode(decode(original, legacy=True))
                decode(migrated)
                self.connection.execute("CREATE TABLE legacy_v1 (revision INTEGER NOT NULL, data TEXT NOT NULL)")
                self.connection.execute("INSERT INTO legacy_v1 VALUES (?, ?)", (revision, original))
                changed = self.connection.execute("UPDATE state SET data=? WHERE singleton=1", (migrated,))
                if changed.rowcount != 1:
                    raise StorageError("迁移未被数据库接受；原库保持不变。")
                self.connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            self._check_format()

    def _read_state(self) -> tuple[int, str]:
        rows = self.connection.execute("SELECT singleton, revision, data FROM state").fetchall()
        if len(rows) != 1 or rows[0][0] != 1 or type(rows[0][1]) is not int or rows[0][1] < 0 or type(rows[0][2]) is not str:
            raise StorageError("任务库的状态记录缺失或损坏；请保留原库，不会自动重置。")
        return rows[0][1], rows[0][2]

    def load(self) -> Board:
        try:
            self._check_format()
            revision, data = self._read_state()
            board = decode(data)
        except sqlite3.Error as error:
            raise StorageError(f"读取任务库失败；原库未重置: {error}") from error
        self.revision = revision
        return board

    def save(self, board: Board) -> None:
        if self.revision is None:
            raise StorageError("保存前必须先加载任务库。")
        if any(type(key) is not int or key != task.id for key, task in board.tasks.items()):
            raise StorageError("任务字典与任务 ID 不一致。")
        if any(key != run.id for key, run in board.runs.items()):
            raise StorageError("执行字典与执行 ID 不一致。")
        # ponytail: O(total snapshot) per changed revision; use per-task rows if measured volume requires it.
        try:
            data = encode(board)
            decode(data)  # Never write a snapshot that this version cannot read back.
            with self.connection:
                self.connection.execute("BEGIN IMMEDIATE")
                self._check_format()
                revision, current = self._read_state()
                if revision != self.revision:
                    raise StorageError("另一窗口已更新任务；本次未保存。请复制未提交文本，再关闭重开。")
                decode(current)  # Refuse to overwrite a damaged record even after initial load.
                changed = self.connection.execute("UPDATE state SET revision=?, data=? WHERE singleton=1", (revision + 1, data))
                if changed.rowcount != 1:
                    raise StorageError("任务库未接受本次更新；请保留原库检查，不会显示保存成功。")
            self.revision = revision + 1
        except (sqlite3.Error, TypeError, ValueError) as error:
            raise StorageError(f"保存失败，原任务未改变（可能锁定、只读或磁盘错误）: {error}") from error

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()
