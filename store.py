"""Atomic task/decision snapshots with revision checks and backed-up v1–v6 migrations."""
import json
import os
import sqlite3
from dataclasses import asdict, fields
from pathlib import Path

from budget_ledger import (
    CLASSES as BUDGET_CLASSES,
)
from budget_ledger import (
    REGISTRIES as BUDGET_REGISTRIES,
)
from budget_ledger import (
    check_budget_after,
    check_budget_structure,
    stage_ledger_invalidations,
)
from decisions import (
    DecisionRecord,
    check_records,
    decision_record_wire,
    validate_preview,
)
from dependencies import (
    DependencyCheck,
    OwnershipReceipt,
    OwnershipRelease,
    TaskDependency,
    check_dependencies_after,
    check_dependencies_structure,
    stage_binding_invalidations,
)
from evidence import (
    EvidenceItem,
    EvidenceVerification,
    check_evidence,
    check_evidence_after,
)
from journal import _unique_object, check_history, parse_event, validate_events
from model import (
    AGENTS,
    PROJECTS,
    RETENTIONS,
    RUNS,
    STATUSES,
    Board,
    Run,
    Supplement,
    Task,
    valid_uuid,
)
from steps import Step, validate_plan

APPLICATION_ID = 0x54524D31
SCHEMA_VERSION = 12


class StorageError(RuntimeError):
    """A rejected or failed disk operation; callers must not publish the candidate."""


def default_path() -> Path:
    state = os.environ.get("XDG_STATE_HOME", "")
    root = Path(state) if state and Path(state).is_absolute() else Path.home() / ".local" / "state"
    return root / "waggle" / "tasks.sqlite3"


def _tasks(board: Board, rows: object, *, legacy: bool = False, version: int = SCHEMA_VERSION) -> None:
    if not isinstance(rows, list):
        raise StorageError("任务数据应为列表。")
    names = {item.name for item in fields(Task)} - ({"execution", "run_id"} if legacy else set())
    if legacy or version == 2:
        names -= {"retention"}
    if legacy or version <= 5:
        names -= {"steps"}
    for row in rows:
        if not isinstance(row, dict) or set(row) != names:
            raise StorageError("任务字段缺失或不兼容，拒绝用默认值覆盖。")
        step_rows = row.get("steps", [])
        if type(step_rows) is not list:
            raise StorageError("步骤计划必须是列表。")
        steps = [Step.from_record(item) for item in step_rows]
        validate_plan(steps)
        for name, value in row.items():
            if name == "steps":
                continue
            expected = int if name == "id" else bool if name == "full_delivery" else list if name in ("checks", "events") else str
            if type(value) is not expected or (isinstance(value, list) and any(type(item) is not str for item in value)):
                raise StorageError(f"任务字段 {name} 的类型不正确。")
        task = Task(**{**row, "steps": steps})
        if task.id <= 0 or task.id in board.tasks or not task.title.strip():
            raise StorageError("任务 ID 重复/无效或标题为空，拒绝覆盖原数据。")
        if task.retention not in RETENTIONS:
            raise StorageError("任务归档/回收站状态不兼容。")
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
    return json.dumps({"live": board.live, "projects": board.projects, "next_task_id": board.next_task_id,
                       "tasks": [asdict(task) for task in board.tasks.values()],
                       "runs": [asdict(run) for run in board.runs.values()],
                       "supplements": [asdict(note) for note in board.supplements.values()],
                       "decisions": [decision_record_wire(record) for record in board.decisions.values()],
                       "task_contexts": [asdict(ctx) for ctx in board.task_contexts.values()],
                       "evidence_items": [asdict(item) for item in board.evidence_items.values()],
                       "evidence_verifications": [asdict(record) for record in board.evidence_verifications.values()],
                       "task_dependencies": [asdict(record) for record in board.task_dependencies.values()],
                       "dependency_checks": [asdict(record) for record in board.dependency_checks.values()],
                       "ownership_releases": [asdict(record) for record in board.ownership_releases.values()],
                       "ownership_receipts": [asdict(record) for record in board.ownership_receipts.values()],
                       **{name: [asdict(record) for record in getattr(board, name).values()]
                          for name in BUDGET_REGISTRIES}}, ensure_ascii=False)


def decode(data: str, *, legacy: bool = False, version: int = SCHEMA_VERSION) -> Board:
    """Exact schema and links; never turn a legacy simulation into real work."""
    try:
        raw = json.loads(data, object_pairs_hook=_unique_object)
        board = Board(live=True)
        if legacy:
            board.projects = dict(PROJECTS)
            _tasks(board, raw, legacy=True)
            board.next_task_id = max(board.tasks, default=0) + 1
            return board
        names = {"live", "projects", "tasks", "runs"} | ({"next_task_id"} if version >= 4 else set()) | ({"supplements"} if version >= 5 else set())
        if version >= 7:
            names.add("decisions")
        if version >= 8:
            names.add("task_contexts")
        if version >= 9:
            names.update({"evidence_items", "evidence_verifications"})
        if version >= 10:
            names.update({"task_dependencies", "dependency_checks", "ownership_releases", "ownership_receipts"})
        if version >= 12:
            names.update(BUDGET_REGISTRIES)
        if not isinstance(raw, dict) or set(raw) != names or type(raw["live"]) is not bool:
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
        _tasks(board, raw["tasks"], version=version)
        board.next_task_id = raw["next_task_id"] if version >= 4 else max(board.tasks, default=0) + 1
        if type(board.next_task_id) is not int or board.next_task_id <= max(board.tasks, default=0):
            raise StorageError("任务 ID 分配序号无效，拒绝复用已有身份。")
        if not isinstance(raw["runs"], list):
            raise StorageError("执行记录应为列表。")
        for row in raw["runs"]:
            run = Run.from_record(row)
            if run.id in board.runs or run.task_id not in board.tasks or board.tasks[run.task_id].execution != "pi":
                raise StorageError("执行记录重复或关联任务无效。")
            board.runs[run.id] = run
        if version >= 5:
            if not isinstance(raw["supplements"], list):
                raise StorageError("补充说明应为列表。")
            counts: dict[str, int] = {}
            for row in raw["supplements"]:
                note = Supplement.from_record(row)
                run = board.runs.get(note.run_id)
                if note.id in board.supplements or not run or note.run_seq > run.seq:
                    raise StorageError("补充说明重复、轮次缺失或回执修订超前。")
                counts[note.run_id] = counts.get(note.run_id, 0) + 1
                if counts[note.run_id] > 16:
                    raise StorageError("单轮补充说明超过上限。")
                board.supplements[note.id] = note
        if version >= 7:
            if type(raw["decisions"]) is not list:
                raise StorageError("决策记录必须是列表。")
            for row in raw["decisions"]:
                record = DecisionRecord.from_record(row)
                if record.id in board.decisions:
                    raise StorageError("决策记录 ID 重复。")
                board.decisions[record.id] = record
            check_records(board)
        if version >= 8:
            if type(raw["task_contexts"]) is not list:
                raise StorageError("任务上下文必须是列表。")
            from task_context import TaskContext
            for row in raw["task_contexts"]:
                ctx = TaskContext.from_record(row)
                if ctx.id in board.task_contexts:
                    raise StorageError("任务上下文 ID 重复。")
                board.task_contexts[ctx.id] = ctx
        if version >= 9:
            if type(raw['evidence_items']) is not list or type(raw['evidence_verifications']) is not list:
                raise StorageError('成果库存及人工核验必须是列表。')
            for row in raw['evidence_items']:
                item = EvidenceItem.from_record(row)
                if item.id in board.evidence_items:
                    raise StorageError('成果库存 ID 重复。')
                board.evidence_items[item.id] = item
            for row in raw['evidence_verifications']:
                verification = EvidenceVerification.from_record(row)
                if verification.id in board.evidence_verifications:
                    raise StorageError('人工核验 ID 重复。')
                board.evidence_verifications[verification.id] = verification
        if version >= 10:
            for name, cls in (('task_dependencies', TaskDependency), ('dependency_checks', DependencyCheck),
                              ('ownership_releases', OwnershipRelease), ('ownership_receipts', OwnershipReceipt)):
                if type(raw[name]) is not list:
                    raise StorageError('局部依赖及交接审计必须是列表。')
                registry = getattr(board, name)
                for row in raw[name]:
                    record = cls.from_record(row)
                    if record.id in registry:
                        raise StorageError('局部依赖/交接审计 ID 重复。')
                    registry[record.id] = record
        if version >= 12:
            for name, cls in zip(BUDGET_REGISTRIES, BUDGET_CLASSES, strict=True):
                if type(raw[name]) is not list:
                    raise StorageError('累计账本审计必须是列表。')
                registry = getattr(board, name)
                for row in raw[name]:
                    record = cls.from_record(row)
                    if record.id in registry:
                        raise StorageError('累计账本审计身份重复。')
                    registry[record.id] = record
        check_budget_structure(board)
        from task_context import check_contexts
        check_contexts(board)
        check_evidence(board)
        check_dependencies_structure(board)
        active_agents: set[str] = set()
        active_tasks: set[int] = set()
        continued_parents: set[str] = set()
        for run in board.runs.values():
            if parent_id := run.continuation_parent:
                if parent_id in continued_parents:
                    raise StorageError("执行续接出现分叉。")
                continued_parents.add(parent_id)
                board.lineage(run)  # Includes orphan, cycle, frozen identity and scope checks.
            if not run.terminal:
                if run.agent_id in active_agents or run.task_id in active_tasks or board.tasks[run.task_id].run_id != run.id:
                    raise StorageError("执行身份冲突；不会并发启动。")
                active_agents.add(run.agent_id)
                active_tasks.add(run.task_id)
        for task in board.tasks.values():
            board.check_steps(task, task.steps)
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
            if task.full_delivery and (not run or run.state != "finished" or run.delivery != "full" or not board.supplements_satisfied(run)):
                raise StorageError("完整交付尚未结束执行。")
        for task in board.tasks.values():
            if task.retention != "active":
                target = board.retirement_target(task)
                if (version < 4 or task.retention == "archived") and target != task.retention:
                    raise StorageError("任务隐藏状态与执行历史不一致。")
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
                # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- compile-time application ID, not user input
                self.connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
                # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- compile-time schema version, not user input
                self.connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            elif app_id == APPLICATION_ID and version in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11):
                revision, original = self._read_state()
                migrated = encode(decode(original, legacy=version == 1, version=version))
                decode(migrated)
                backup = f"legacy_v{version}"
                # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- backup uses only allowlisted integer versions 1..11
                self.connection.execute(f"CREATE TABLE {backup} (revision INTEGER NOT NULL, data TEXT NOT NULL)")
                # Identifier comes only from the closed integer version allowlist above; values stay bound.
                # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- allowlisted identifier; snapshot values are bound below
                self.connection.execute(f"INSERT INTO {backup} VALUES (?, ?)", (revision, original))  # noqa: S608
                changed = self.connection.execute("UPDATE state SET data=? WHERE singleton=1", (migrated,))
                if changed.rowcount != 1:
                    raise StorageError("迁移未被数据库接受；原库保持不变。")
                # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- compile-time schema version, not user input
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
                previous = decode(current)  # Refuse to overwrite damaged records or recycle deleted IDs.
                for run_id, old_run in previous.runs.items():
                    if old_run.task_id in board.tasks:
                        new_run = board.runs.get(run_id)
                        if new_run is None or new_run.identity != old_run.identity:
                            raise StorageError('保留执行的身份、目录、标题、目标与请求不可删除或改写；必须创建新轮。')
                        if old_run.terminal and new_run != old_run:
                            raise StorageError('已结束执行及交付原文不可改写；新工作必须另起轮次。')
                        check_history(old_run.events, new_run.events)
                        old_raw = {event.id: raw for raw in old_run.events if (event := parse_event(raw))}
                        if any(raw != old_raw[event.id] for raw in new_run.events
                               if (event := parse_event(raw)) and event.id in old_raw):
                            raise StorageError('保留报告的原始字节不能同 ID 重写或重新编码。')
                for task_id, old_task in previous.tasks.items():
                    task = board.tasks.get(task_id)
                    if task and old_task.status == 'done':
                        if task.status != 'done':
                            raise StorageError('已验收任务不可降级以改写原始范围；新工作请创建后续草稿。')
                        if (task.title, task.description, task.execution) != (old_task.title, old_task.description, old_task.execution):
                            raise StorageError('已验收任务的原始标题、目标与执行类型不可改写；请创建后续草稿。')
                original_board = board
                board = stage_binding_invalidations(previous, board)
                board = stage_ledger_invalidations(previous, board)
                if board is not original_board:
                    data = encode(board)
                    decode(data)
                if board.next_task_id < previous.next_task_id:
                    raise StorageError("任务 ID 分配序号不能倒退，删除后也不会复用。")
                for task_id, old_task in previous.tasks.items():
                    current_task = board.tasks.get(task_id)
                    if current_task is None:
                        continue  # Existing purge guard below also removes nested steps.
                    old_ids = [step.id for step in old_task.steps]
                    if [step.id for step in current_task.steps[:len(old_ids)]] != old_ids:
                        raise StorageError("保留任务的步骤 ID 和顺序不能删除或替换。")
                    if current_task.steps != old_task.steps and (old_task.status == "done" or old_task.retention != "active"
                            or current_task.status == "done" or current_task.retention != "active"):
                        raise StorageError("已验收或隐藏任务的步骤只读。")
                check_evidence_after(previous, board)
                check_dependencies_after(previous, board)
                check_budget_after(previous, board)
                for record_id, record in previous.decisions.items():
                    if record.task_id in board.tasks and board.decisions.get(record_id) != record:
                        raise StorageError("保留任务的决策记录不可删除或改写。")
                retained = [key for key, record in previous.decisions.items() if record.task_id in board.tasks]
                if list(board.decisions)[:len(retained)] != retained:
                    raise StorageError("决策记录的保存顺序不可改写。")
                new_records = board.decisions.keys() - previous.decisions.keys()
                if len(new_records) > 1:
                    raise StorageError("一次确认只能保存一份决策意图。")
                for record_id in new_records:
                    record = board.decisions[record_id]
                    old_task = previous.tasks.get(record.task_id)
                    task = board.tasks[record.task_id]
                    if not old_task or any(t.retention != "active" or t.status == "done" for t in (old_task, task)):
                        raise StorageError("已验收或非当前任务不能追加决定。")
                    validate_preview(previous, record.preview)
                    if record.intent.kind == "new_run" and record.intent.run_id in previous.runs:
                        raise StorageError("正式决定必须与新的执行意图共同保存。")
                    if record.intent.kind == "live_supplement" and record.intent.supplement_id in previous.supplements:
                        raise StorageError("正式决定必须与新的补充意图共同保存。")
                for note_id, previous_note in previous.supplements.items():
                    current_note = board.supplements.get(note_id)
                    if current_note is not None:
                        current_note.check_after(previous_note)
                    elif previous.runs[previous_note.run_id].task_id in board.tasks:
                        raise StorageError("不能删除仍保留任务的补充意图来绕过验收保护。")
                for task_id in previous.tasks.keys() - board.tasks.keys():
                    previous.purge(previous.tasks[task_id])  # Persisted trash is required, not a one-step wipe.
                if data == current:
                    return  # Exact receipt retries are physical / revision no-ops, after all guards.
                changed = self.connection.execute("UPDATE state SET revision=?, data=? WHERE singleton=1", (revision + 1, data))
                if changed.rowcount != 1:
                    raise StorageError("任务库未接受本次更新；请保留原库检查，不会显示保存成功。")
            self.revision = revision + 1
            if board is not original_board:
                # Mirror non-authorizing invalidations only AFTER the commit; failed writes leave caller input intact.
                original_board.dependency_checks = board.dependency_checks
                original_board.ledger_verifications = board.ledger_verifications
                for task_id, task in original_board.tasks.items():
                    task.remaining = board.tasks[task_id].remaining
        except (sqlite3.Error, TypeError, ValueError) as error:
            raise StorageError(f"保存失败，原任务未改变（可能锁定、只读或磁盘错误）: {error}") from error

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()
