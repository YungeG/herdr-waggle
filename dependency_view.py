"""Explicit local dependency forms. Separate final confirmation; never dispatch."""
from collections.abc import Callable
from dataclasses import asdict

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Input, Select, SelectionList, Static, TextArea

from dependencies import (
    DependencyCheck,
    OwnershipReceipt,
    OwnershipRelease,
    TaskDependency,
)
from evidence_view import EvidenceDialog, EvidenceReview
from store import StorageError
from task_context import canonical

DependencyRecord = TaskDependency | DependencyCheck | OwnershipRelease | OwnershipReceipt


class DependencyPanel(EvidenceDialog):
    def __init__(self, title: str, body: str, *, writable: bool) -> None:
        super().__init__()
        self.title_text, self.body_text, self.writable = title, body, writable

    def compose(self) -> ComposeResult:
        with Vertical(id='dialog', classes='contract-dialog'):
            yield Static(self.title_text, classes='dialog-title', markup=False)
            with VerticalScroll(classes='form-body'):
                yield Static(self.body_text, markup=False, id='dependency-body')
            with Horizontal(classes='dialog-buttons'):
                yield Button('返回', id='cancel')
                if self.writable:
                    yield Button('新增依赖', id='dependency-add')
                    yield Button('核对/交接', id='dependency-audit')

    def on_mount(self) -> None:
        self.query_one('#cancel', Button).focus()

    @on(Button.Pressed, '#dependency-add, #dependency-audit')
    def choose(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss('add' if event.button.id == 'dependency-add' else 'audit')


class DependencyForm(EvidenceDialog):
    def __init__(self, task_label: str, mode: str, *, choices: dict[str, str], hints: dict[str, str],
                 receivers: dict[str, str], items: dict[str, str],
                 prepare: Callable[[str, dict[str, str], list[str]], DependencyRecord],
                 save: Callable[[DependencyRecord], None], durable: bool) -> None:
        super().__init__()
        self.task_label, self.mode, self.choices, self.hints = task_label, mode, choices, hints
        self.receivers, self.items, self.prepare, self.save, self.durable = receivers, items, prepare, save, durable

    def compose(self) -> ComposeResult:
        labels = {'add': '声明局部依赖', 'check': '核对明确条件', 'release': '原写者释放（不是接收）', 'receipt': '接续者独立接收'}
        with Vertical(id='dialog', classes='contract-dialog'):
            yield Static(self.task_label + ' · ' + labels[self.mode], classes='dialog-title', markup=False)
            with VerticalScroll(classes='form-body'):
                yield Static('只保存你明确说明的人工事实；身份未认证。不会探测文件、自动指派/调度、启动或取消执行。', classes='muted')
                yield Static('本地持久化；释放和接收分开保存，派发前仍需原 Pi 空闲归属。' if self.durable else
                             '仅本会话，尚未持久化；不能显示已交接或接管写入权。', classes='muted')
                yield Static('明确选择来源 / 已保存记录 · 默认未选', classes='field-label')
                yield Select(((label, key) for key, label in self.choices.items()), id='dependency-selected')
                yield Static('尚未选择来源。', id='dependency-source-hint', markup=False)
                if self.mode == 'add':
                    yield Static('依赖类型 · 默认未选', classes='field-label')
                    yield Select((('契约条件', 'contract'), ('证据条件', 'evidence'), ('共享文件写冲突', 'write-conflict')), id='dependency-kind')
                    yield Static('证明等级 · 默认未选，不把验收当成原件核验', classes='field-label')
                    yield Select([], id='dependency-proof')
                    yield Static('只阻塞明确操作 · 默认均未选；写冲突仅允许实施', classes='field-label')
                    yield SelectionList(('成果盘点', 'inventory', False), ('原任务补证', 'evidence', False),
                                        ('核查/实施（保守视为可能写入）', 'implementation', False), id='dependency-operations')
                    yield Static('需满足的具体条件 · 必填最多 500 字', classes='field-label')
                    yield TextArea(id='dependency-condition', classes='contract-input')
                if self.mode == 'check':
                    yield Static('人工核对结果 · 默认未选', classes='field-label')
                    yield Select((('明确条件已满足（人工陈述）', 'satisfied'), ('仍阻塞 / 未核实', 'blocked')), id='dependency-result')
                    yield Static('证据类型须选匹配来源的已保存库存项；契约条件不选', classes='field-label')
                    yield Select(((label, key) for key, label in self.items.items()), id='dependency-evidence-item')
                if self.mode in {'release', 'receipt'}:
                    yield Static('精确接续 Pi 身份 · 默认未选；不会自动分配', classes='field-label')
                    yield Select(((label, key) for key, label in self.receivers.items()), id='dependency-receiver')
                if self.mode != 'check':
                    yield Static('具体文件定位 · 每行一个相对路径，最多 16 个；契约/证据依赖留空。\n释放/接收须独立填写与来源一致的精确列表，不猜测文本范围或符号链接。', classes='field-label')
                    yield TextArea(id='dependency-files', classes='contract-input')
                if self.mode != 'add':
                    for key, label, limit in (('verifier', '实际核对/释放/接收的人（不是平台身份认证）', 120),
                                              ('method', '实际核对方法 / human-origin 释放或接收来源', 500)):
                        yield Static(f'{label} · 必填最多 {limit} 字', classes='field-label')
                        yield TextArea(id='dependency-' + key, classes='contract-input')
                    if self.mode != 'receipt':
                        for key, label, limit in (('limits', '覆盖边界', 300), ('gaps', '缺口 / 未知', 300), ('next_owner', '下一责任人 / 未知', 200)):
                            yield Static(f'{label} · 必填最多 {limit} 字', classes='field-label')
                            yield TextArea(id='dependency-' + key, classes='contract-input')
                    yield Static('实际核对时间 · 必填 UTC 毫秒，例如 2026-01-01T00:00:00.000Z', classes='field-label')
                    yield Input(id='dependency-checked-at', max_length=24)
                    if self.mode == 'release':
                        yield Static('释放到期时间 · 必填 UTC 毫秒；过期不允许接管', classes='field-label')
                        yield Input(id='dependency-expires-at', max_length=24)
                yield Static('', id='form-error', classes='error', markup=False)
            with Horizontal(classes='dialog-buttons'):
                yield Button('返回', id='cancel')
                yield Button('核对此记录', id='submit', variant='primary')

    def on_mount(self) -> None:
        self.query_one('#dependency-selected', Select).focus()

    @on(Select.Changed, '#dependency-selected')
    def selected(self, event: Select.Changed) -> None:
        self.query_one('#dependency-source-hint', Static).update(self.hints.get(str(event.value), '尚未选择来源。'))

    @on(Select.Changed, '#dependency-kind')
    def kind_changed(self, event: Select.Changed) -> None:
        proofs = {'contract': [('人工核对具体条件', 'human_condition')],
                  'evidence': [('仅已验收报告（原件未核实）', 'accepted_report'), ('明确人工原件核验', 'human_original_verified')],
                  'write-conflict': [('原写者释放 + 独立接收', 'release_receipt')]}
        select = self.query_one('#dependency-proof', Select)
        select.set_options(proofs.get(str(event.value), []))
        select.value = Select.NULL

    def action_submit(self) -> None:
        try:
            selected = str(self.query_one('#dependency-selected', Select).value)
            if selected not in self.choices:
                raise ValueError('必须明确选择来源 / 已保存记录。')
            values: dict[str, str] = {}
            for widget in self.query(TextArea):
                if widget.id:
                    values[widget.id.removeprefix('dependency-').replace('-', '_')] = widget.text
            for widget in self.query(Input):
                if widget.id:
                    values[widget.id.removeprefix('dependency-').replace('-', '_')] = widget.value
            for widget in self.query(Select):
                if widget.id != 'dependency-selected' and widget.id:
                    key = widget.id.removeprefix('dependency-').replace('-', '_')
                    values[key] = '' if widget.value is Select.NULL else str(widget.value)
            operations = list(self.query_one('#dependency-operations', SelectionList).selected) if self.mode == 'add' else []
            record = self.prepare(selected, values, operations)
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
        self.app.push_screen(EvidenceReview('核对局部依赖记录 · 默认返回', canonical(asdict(record)) +
                             '\n仅保存此人工记录，不分配、不派发。释放并不证明接续者接收；接收也不替代原 Pi 的新鲜空闲归属核对。' +
                             ('\n未持久化，尚未交接。' if not self.durable else '')), confirmed)
