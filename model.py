"""Pure tasks, project catalog and per-attempt execution state. No I/O."""
from dataclasses import dataclass, field, fields
from uuid import UUID

PROJECTS = {
    "收集箱": "未选择目录 · 启动前请安排项目",
    "数据实验室": "/demo/data-lab/research/中文样本/feature-pipeline",
    "终端工作台": "/demo/terminal-workbench/packages/task-ui",
    "个人事务": "/demo/personal/notes",
}
AGENTS = ("Pi · 数据助手（虚构）", "Pi · 界面助手（虚构）", "Pi · 通用助手（虚构）")
STATUSES = {
    "draft": "待安排", "ready": "待执行", "running": "执行中",
    "blocked": "阻塞", "review": "待验收", "done": "已验收",
}
RUNS = {"none": "未启动", "queued": "已排队，未确认", "working": "已确认，运行中", "blocked": "等待补充", "finished": "本轮结束，验收独立确认"}

RUN_STATES = {"queued", "working", "waiting", "finished", "failed", "stopped", "interrupted", "abandoned"}
DELIVERIES = {"", "progress", "blocked", "partial", "full"}


def valid_uuid(value: str) -> bool:
    try:
        return str(UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


@dataclass
class Run:
    id: str
    task_id: int
    agent_id: str
    session_id: str
    cwd: str
    title: str
    description: str
    requirements: str
    state: str = "queued"
    seq: int = 0
    delivery: str = ""
    summary: str = ""
    checks: list[str] = field(default_factory=list)
    remaining: str = ""
    events: list[str] = field(default_factory=list)

    @property
    def terminal(self) -> bool:
        return self.state in {"finished", "failed", "stopped", "interrupted", "abandoned"}

    @property
    def identity(self) -> tuple:
        return (self.id, self.task_id, self.agent_id, self.session_id, self.cwd,
                self.title, self.description, self.requirements)

    @classmethod
    def from_record(cls, row: object) -> "Run":
        if not isinstance(row, dict) or set(row) != {item.name for item in fields(cls)}:
            raise ValueError("执行记录字段不兼容。")
        for name, value in row.items():
            expected = int if name in ("task_id", "seq") else list if name in ("checks", "events") else str
            if type(value) is not expected or (isinstance(value, list) and any(type(item) is not str for item in value)):
                raise ValueError(f"执行字段 {name} 类型错误。")
        run = cls(**row)
        if not all(valid_uuid(value) for value in (run.id, run.agent_id, run.session_id)):
            raise ValueError("执行/连接/会话身份无效。")
        if run.task_id <= 0 or run.seq < 0 or not run.cwd.startswith("/") or not run.title.strip():
            raise ValueError("执行任务、目录或版本无效。")
        if run.state not in RUN_STATES or run.delivery not in DELIVERIES:
            raise ValueError("执行状态不兼容。")
        if run.delivery == "full" and (not run.summary.strip() or not run.checks or run.remaining.strip()):
            raise ValueError("完整交付必须有摘要、证据且无剩余工作。")
        return run


@dataclass
class Task:
    id: int
    title: str
    project: str
    description: str = ""
    status: str = "draft"
    agent: str = ""
    run: str = "none"
    blocker: str = ""
    summary: str = ""
    checks: list[str] = field(default_factory=list)
    remaining: str = ""
    full_delivery: bool = False
    events: list[str] = field(default_factory=lambda: ["任务创建 · 未发起执行"])
    execution: str = "simulation"
    run_id: str = ""

    @property
    def can_accept(self) -> bool:
        return self.status == "review" and self.full_delivery and not self.remaining


class Board:
    def __init__(self, live: bool = False) -> None:
        self.live = live
        self.projects: dict[str, str] = {"收集箱": ""} if live else dict(PROJECTS)
        self.tasks: dict[int, Task] = {}
        self.runs: dict[str, Run] = {}

    def add_project(self, name: str, canonical_path: str) -> None:
        name = name.strip()
        if not name or name in self.projects or name in {"all", "__add_project__"}:
            raise ValueError("项目名称不能为空、重复或使用保留名称。")
        if not canonical_path.startswith("/") or canonical_path in self.projects.values():
            raise ValueError("需要唯一的绝对目录；不会回退到 Git 根目录。")
        self.projects[name] = canonical_path

    def create(self, title: str, project: str, description: str = "") -> Task:
        if not title.strip():
            raise ValueError("请先写一个任务标题。")
        if project not in self.projects:
            raise ValueError("请选择已登记的项目。")
        task = Task(max(self.tasks, default=0) + 1, title.strip(), project, description.strip(),
                    execution="pi" if self.live else "simulation")
        self.tasks[task.id] = task
        return task

    def edit(self, task: Task, title: str, project: str, description: str) -> None:
        if task.execution == "pi" and task.run != "none":
            raise ValueError("本轮输入已固定；请先结束执行并通过补充/退回安排新一轮。")
        if not title.strip() or project not in self.projects:
            raise ValueError("标题不能为空，项目必须已登记。")
        if task.run in ("queued", "working", "blocked") and project != task.project:
            raise ValueError("执行尚未结束，不能更换目录。")
        if project != task.project and task.execution == "pi":
            task.agent, task.status = "", "draft"
        task.title, task.project, task.description = title.strip(), project, description.strip()
        task.events.append("人工编辑 · 未自动启动")

    def assign(self, task: Task, agent: str) -> None:
        valid_agent = agent in AGENTS if task.execution == "simulation" else valid_uuid(agent)
        if not valid_agent or task.status not in ("draft", "ready") or task.run != "none":
            raise ValueError("仅未启动的待安排/待执行任务可分配。")
        task.agent, task.status = agent, "ready"
        task.events.append(f"人工分配给 {agent} · 尚未启动")

    def queue(self, task: Task) -> None:
        if task.execution != "simulation":
            raise ValueError("真实任务必须通过已连接 Pi 派发。")
        if not task.agent or task.project == "收集箱":
            raise ValueError("先选择具体项目并分配执行者，再模拟启动。")
        if task.status != "ready" or task.run != "none":
            raise ValueError("任务已启动或尚未准备好；不会重复启动。")
        task.run = "queued"
        task.events.append("模拟排队 · 尚未获得执行确认")

    def simulate(self, task: Task, action: str, reason: str = "") -> None:
        if task.execution != "simulation":
            raise ValueError("真实任务不能模拟进展。")
        if action == "ack" and task.run == "queued":
            task.run, task.status = "working", "running"
            task.events.append("模拟执行者确认 · 开始处理")
        elif action == "progress" and task.run == "working":
            task.events.append("模拟进展 · 已梳理样例，正在补充边界检查")
        elif action == "block" and task.run == "working":
            if not reason.strip():
                raise ValueError("请填写阻塞原因。")
            task.run, task.status, task.blocker = "blocked", "blocked", reason.strip()
            task.events.append(f"模拟阻塞 · {task.blocker}")
        elif action in ("full", "partial") and task.run == "working":
            task.run, task.status = "finished", "review"
            task.full_delivery = action == "full"
            task.summary = "样例方案和回归说明已整理。" if task.full_delivery else "仅完成第一阶段：样例与方案整理。"
            task.checks = ["模拟通过 · 基础流程（没有运行真实测试）", "模拟通过 · 中文及空输入边界（非真实证据）"]
            task.remaining = "" if task.full_delivery else "第二阶段实现与验证尚未完成，不能验收整个任务。"
            task.events.append("模拟整任务交付 · 等待人工验收" if task.full_delivery else "模拟阶段交付 · 整任务仍未完成")
        else:
            raise ValueError("当前状态不支持这个模拟动作。")

    def accept(self, task: Task) -> None:
        run = self.runs.get(task.run_id)
        if task.execution == "pi" and (not run or run.state != "finished" or run.delivery != "full"):
            raise ValueError("Pi 尚未完整交付且完全空闲，不能验收。")
        if not task.can_accept:
            raise ValueError("仅完整交付、无剩余工作的任务可人工验收。")
        task.status = "done"
        task.events.append("人工验收 · 已确认整任务成果" if task.execution == "pi" else "人工验收样例 · 这是本地交互演示，不代表真实成果通过")

    def return_work(self, task: Task, reason: str) -> None:
        if task.execution == "pi" and task.run_id and not self.runs[task.run_id].terminal:
            raise ValueError("执行尚未确认结束；请先停止，不能并发重派。")
        if task.status not in ("review", "blocked") or not reason.strip():
            raise ValueError("请给待验收或阻塞任务填写补充/退回原因。")
        task.events.append(f"人工补充/退回 · {reason.strip()}")
        task.status, task.run = "ready", "none"
        task.blocker, task.remaining = "", reason.strip()
        task.full_delivery = False
        task.summary = "上轮交付已退回；补充要求见剩余工作和时间线。"
        task.checks = []

    def queue_real(self, task: Task, run_id: str, session_id: str, cwd: str) -> Run:
        if task.execution != "pi" or task.status != "ready" or task.run != "none" or not valid_uuid(task.agent):
            raise ValueError("请先分配已连接的 Pi；不会重复派发。")
        if len(task.title) > 300 or len(task.description) > 20000 or len(task.remaining) > 6000:
            raise ValueError("派发上限：标题 300、说明 20000、补充要求 6000 字符。")
        if not cwd or cwd != self.projects[task.project]:
            raise ValueError("Pi 必须在所选项目的精确目录中。")
        if run_id in self.runs or any(not run.terminal and (run.agent_id == task.agent or run.task_id == task.id) for run in self.runs.values()):
            raise ValueError("任务或 Pi 仍有未确认结束的执行，不能重派。")
        run = Run(run_id, task.id, task.agent, session_id, cwd, task.title, task.description, task.remaining)
        if not valid_uuid(run.id) or not valid_uuid(run.session_id):
            raise ValueError("执行身份无效。")
        self.runs[run.id] = run
        task.run_id, task.run = run.id, "queued"
        task.full_delivery, task.summary, task.checks = False, "", []
        task.events.append(f"派发意图已保存 · {run.id} · 等待 Pi 确认（未自动重试）")
        return run

    def ingest(self, incoming: Run) -> bool:
        previous = self.runs.get(incoming.id)
        if previous is None or previous.identity != incoming.identity:
            raise ValueError("执行结果身份与派发快照不匹配。")
        if previous.terminal or incoming.seq <= previous.seq:
            return False
        self.runs[incoming.id] = incoming
        task = self.tasks[incoming.task_id]
        if task.run_id != incoming.id or task.status == "done":
            return True  # A historical attempt cannot overwrite the current task.
        task.summary, task.checks = incoming.summary, list(incoming.checks)
        task.remaining = incoming.remaining or incoming.requirements
        task.full_delivery = False
        if not incoming.terminal:
            task.run = "queued" if incoming.state == "queued" else "working"
            task.status = "blocked" if incoming.state == "waiting" else "ready" if incoming.state == "queued" else "running"
            task.blocker = "等待 Pi 窗口中的人工确认；工作台不会自动回答。" if incoming.state == "waiting" else ""
        elif incoming.state == "finished" and incoming.delivery in ("full", "partial"):
            task.run, task.status = "finished", "review"
            task.full_delivery = incoming.delivery == "full"
            task.remaining, task.blocker = incoming.remaining, ""
            task.events.append(f"Pi 已交付且完全空闲 · {incoming.delivery} · 等待人工验收")
        else:
            task.run, task.status = "finished", "blocked"
            task.blocker = incoming.summary or f"执行 {incoming.state}；没有可验收的完整交付。"
            task.events.append(f"本轮结束 · {incoming.state} · {task.blocker}")
        return True

    def abandon(self, task: Task, reason: str) -> None:
        run = self.runs.get(task.run_id)
        if not run or run.terminal or not reason.strip():
            raise ValueError("仅未确认结束的执行可人工解除，且必须填写已核实情况。")
        run.state = "abandoned"
        run.events.append(f"人工确认旧 Pi 已停止 · {reason.strip()}")
        task.run, task.status, task.full_delivery = "finished", "blocked", False
        task.blocker = "人工解除失联执行；可补充要求后另起一轮。"
        task.events.append(run.events[-1])


def sample_board() -> Board:
    board = Board()
    samples = [
        ("把任务详情改成可读、可滚动的交付视图", "终端工作台", "保留摘要、证据、剩余工作和时间线。\n长内容不能挤掉关键操作。"),
        ("核对中文样本的时间边界与缺失字段处理，不读取任何真实数据", "数据实验室", "这是虚构任务，不是数据采集指令。\n检查跨日样例、空值和重复记录；结果仅作交互演示。"),
        ("等待确认：样例字段应该采用哪个口径？", "数据实验室", "执行者缺少定义，需要人类补充后才能继续。"),
        ("让窄终端也能舒适地查看任务", "终端工作台", "80×24 使用列表/详情切换，不压缩成九条窄泳道。"),
        ("整理一份周末阅读清单", "个人事务", "先记下来，不需要立刻交给 Agent。"),
        ("写下下一轮想试的交互", "收集箱", "只写标题也能创建草稿。"),
        ("补齐路径显示与长中文标题的回归说明", "终端工作台", "确认目录可完整读取，不能静默回退到其他路径。"),
        ("为样例统计补充异常输入说明", "数据实验室", "没有真实文件，也不会运行命令。"),
        ("清理演示任务命名", "个人事务", "人类验收与执行者回合结束是两件事。"),
        ("设计收藏和稍后处理的键盘手感", "收集箱", "讨论项，尚未安排。"),
        ("给排队反馈增加明确的确认状态", "终端工作台", "提交不等于开始，必须等待模拟执行确认。"),
        ("完成任务工作台原型的第一阶段", "终端工作台", "此卡故意只交付阶段成果，用于演示不能直接整任务验收。"),
    ]
    for title, project, description in samples:
        board.create(title, project, description)
    for task_id in (1, 2, 3, 4, 7, 8, 9, 11, 12):
        task = board.tasks[task_id]
        board.assign(task, AGENTS[0 if task.project == "数据实验室" else 1])
        if task_id == 7:
            continue
        board.queue(task)
        if task_id == 11:
            continue
        board.simulate(task, "ack")
        if task_id == 3:
            board.simulate(task, "block", "需要你确认样例口径：按事件时间还是按入库时间？")
        elif task_id in (1, 9, 12):
            board.simulate(task, "partial" if task_id == 12 else "full")
            if task_id == 9:
                board.accept(task)
    return board
