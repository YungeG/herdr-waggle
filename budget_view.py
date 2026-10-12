"""Separate blank-default cumulative ledger forms. No approval rollout or execution."""
from collections.abc import Callable
from dataclasses import asdict

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Input, Select, Static, TextArea

from budget_ledger import BudgetRecord
from evidence_view import EvidenceDialog, EvidenceReview
from store import StorageError
from task_context import canonical


class BudgetPanel(EvidenceDialog):
    def __init__(self, body: str) -> None:
        super().__init__()
        self.body_text = body

    def compose(self) -> ComposeResult:
        with Vertical(id='dialog', classes='contract-dialog'):
            yield Static('独立累计预算核对 · 保存不执行', classes='dialog-title')
            with VerticalScroll(classes='form-body'):
                yield Static(self.body_text, markup=False, id='budget-body')
            with Horizontal(classes='dialog-buttons'):
                yield Button('返回', id='cancel')
                yield Button('记录账本', id='budget-record')
                yield Button('正式批准未启用', id='budget-approve', disabled=True)

    def on_mount(self) -> None:
        self.query_one('#cancel', Button).focus()

    @on(Button.Pressed, '#budget-record')
    def choose(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss('record')


FIELDS = {
    'scope': (('resource_key', '稳定资源标识（相同资源不能新建范围清零）', False),
              ('meter_id', '稳定计量来源标识', False), ('definition', '计量定义：跨所有 Task / Run 的累计范围及不覆盖项', True),
              ('author', '明确记录人（非身份认证）', False), ('started_at', '全部累计用量起点 · UTC 毫秒', False)),
    'observe': (('receipt_id', '原始稳定回执标识（同一回执只计一次）', False),
                ('meter_id', '明确填写同一计量来源（不预填）', False), ('revision', '原始来源修订 · 正整数', False),
                ('attempts', '累计已用次数（不是本轮增量）', False), ('minutes', '累计已用分钟', False),
                ('storage_mib', '累计计量 MiB（非 GB，无自动转换）', False), ('observed_at', '回执覆盖到何时 · UTC 毫秒', False)),
    'verify': (('verifier', '实际核验人（非身份认证）', False), ('method', '核对全部历史回执、计量覆盖及未计入期间的方法', True),
               ('limits', '覆盖边界与执行器不能强制的限制', True), ('gaps', '缺口；只有完整核验才明确留空', True),
               ('next_owner', '下一核验责任 / 未知，不猜联系人', False), ('checked_at', '完整核验覆盖到的实际 UTC 毫秒时间', False)),
    'revoke': (('author', '明确撤销人（非身份认证）', False), ('reason', '撤销原因；不派发停止命令、不新增总帽', True)),
}


class BudgetForm(EvidenceDialog):
    def __init__(self, mode: str, *, scopes: dict[str, str], observations: dict[str, tuple[str, str]],
                 items: dict[str, str], prepare: Callable[[str, dict[str, str]], BudgetRecord],
                 save: Callable[[BudgetRecord], None], durable: bool) -> None:
        super().__init__()
        self.mode, self.scopes, self.observations, self.items = mode, scopes, observations, items
        self.prepare, self.save, self.durable = prepare, save, durable

    def compose(self) -> ComposeResult:
        labels = {'scope': '声明稳定累计范围', 'observe': '记录累计回执（尚未核验）',
                  'verify': '独立人工完整核验（不是授权）', 'revoke': '明确撤销已存累计授权'}
        with Vertical(id='dialog', classes='contract-dialog'):
            yield Static(labels[self.mode], classes='dialog-title', markup=False)
            with VerticalScroll(classes='form-body'):
                yield Static('仅保存明确审计；不扫描原件、不读取填写路径、不启动 / 停止 Pi。人工陈述非可信自动计量；不能承诺硬额度。', classes='muted')
                yield Static('本地持久化；正式批准入口仍关闭。' if self.durable else
                             '尚未持久化，退出丢失；不能声称已保存授权。', classes='muted')
                if self.mode != 'scope':
                    yield Static('稳定累计范围 · 默认未选，新 Task / Run 不能清零', classes='field-label')
                    yield Select(((label, key) for key, label in self.scopes.items()), id='budget-scope')
                if self.mode == 'observe':
                    yield Static('已保存 N4 来源 · 默认未选；验收报告本身不证明用量', classes='field-label')
                    yield Select(((label, key) for key, label in self.items.items()), id='budget-evidence-item')
                    yield Static('回执来源等级 · 默认未选，两者均须独立核验', classes='field-label')
                    yield Select((('人工记录回执', 'human_receipt'), ('执行者自报', 'executor_self_report')), id='budget-source-kind')
                if self.mode == 'verify':
                    yield Static('本范围已保存累计回执 · 默认未选，原始数字不会自动采纳', classes='field-label')
                    yield Select([], id='budget-observation')
                    yield Static('核验结果 · 默认未选', classes='field-label')
                    yield Select((('完整人工核验', 'verified'), ('仍未知', 'unknown'), ('存在冲突', 'conflict')), id='budget-result')
                    yield Static('是否完整覆盖从起点到核验时刻的所有累计用量 · 必须明确选择', classes='field-label')
                    yield Select((('明确完整覆盖（人工陈述）', 'yes'), ('未完整覆盖', 'no')), id='budget-complete')
                for key, label, multiline in FIELDS[self.mode]:
                    yield Static(label, classes='field-label')
                    if multiline:
                        yield TextArea(id='budget-' + key.replace('_', '-'), classes='contract-input')
                    else:
                        yield Input(id='budget-' + key.replace('_', '-'))
                yield Static('', id='form-error', classes='error', markup=False)
            with Horizontal(classes='dialog-buttons'):
                yield Button('返回', id='cancel')
                yield Button('核对此记录', id='submit', variant='primary')

    def on_mount(self) -> None:
        self.query_one('#cancel', Button).focus()

    @on(Select.Changed, '#budget-scope')
    def scope_changed(self, event: Select.Changed) -> None:
        if self.mode == 'verify':
            select = self.query_one('#budget-observation', Select)
            select.set_options((label, key) for key, (scope_id, label) in self.observations.items() if scope_id == str(event.value))
            select.value = Select.NULL

    def action_submit(self) -> None:
        try:
            values: dict[str, str] = {}
            for widget in self.query(Input):
                assert widget.id is not None
                values[widget.id.removeprefix('budget-').replace('-', '_')] = widget.value
            for widget in self.query(TextArea):
                assert widget.id is not None
                values[widget.id.removeprefix('budget-').replace('-', '_')] = widget.text
            for widget in self.query(Select):
                assert widget.id is not None
                values[widget.id.removeprefix('budget-').replace('-', '_')] = '' if widget.value is Select.NULL else str(widget.value)
            selected = values.get('scope', '')
            if self.mode != 'scope' and selected not in self.scopes:
                raise ValueError('必须明确选择已保存稳定累计范围。')
            record = self.prepare(selected, values)
        except (ValueError, StorageError) as error:
            self.error(error)
            return
        def confirmed(yes: bool | None) -> None:
            if not yes:
                return
            try:
                self.save(record)
            except (ValueError, StorageError) as error:
                self.error(error)
                return
            self.dismiss('saved')
        self.app.push_screen(EvidenceReview('独立累计账本 · 默认返回', canonical(asdict(record)) +
                             '\n仅保存此记录，不发送、不运行、不重置旧用量，不产生派发许可或执行器硬额度。'
                             '\n正式预算批准入口未启用；本核验不是批准。'), confirmed)
