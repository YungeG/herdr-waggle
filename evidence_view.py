"""Explicit inventory selection and independent human verification, never dispatch."""
from collections.abc import Callable
from dataclasses import asdict

from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Select, SelectionList, Static, TextArea

from evidence import EvidenceItem, EvidenceSource, EvidenceVerification
from store import StorageError
from task_context import canonical


class EvidenceDialog(ModalScreen):
    BINDINGS = [Binding('escape', 'cancel', '返回'), Binding('ctrl+enter', 'submit', '核对', priority=True)]

    def on_resize(self, event: events.Resize) -> None:
        self.set_class(event.size.height < 28 or event.size.width < 60, 'compact-dialog')

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed)
    def buttons(self, event: Button.Pressed) -> None:
        if event.button.id not in {'cancel', 'submit'}:
            return
        event.stop()
        self.action_cancel() if event.button.id == 'cancel' else self.action_submit()

    def action_submit(self) -> None:
        pass

    def error(self, error: Exception) -> None:
        self.query_one('#form-error', Static).update(f'未保存：{error}')
        self.query_one('#form-error', Static).scroll_visible(animate=False)


class EvidenceReview(EvidenceDialog):
    def __init__(self, title: str, body: str) -> None:
        super().__init__()
        self.title_text, self.body_text = title, body

    def compose(self) -> ComposeResult:
        with Vertical(id='dialog', classes='contract-dialog'):
            yield Static(self.title_text, classes='dialog-title', markup=False)
            with VerticalScroll(classes='form-body'):
                yield Static(self.body_text, markup=False)
                yield Static('最后确认仅保存此明确记录；不发送、不运行、不变更旧批准或任务验收。', classes='muted')
            with Horizontal(classes='dialog-buttons'):
                yield Button('返回修改', id='cancel')
                yield Button('确认仅保存', id='submit', variant='primary')

    def on_mount(self) -> None:
        self.query_one('#cancel', Button).focus()

    def action_submit(self) -> None:
        self.dismiss(True)


class EvidenceInventory(EvidenceDialog):
    def __init__(self, title: str, body: str, *, writable: bool, durable: bool) -> None:
        super().__init__()
        self.title_text, self.body_text, self.writable, self.durable = title, body, writable, durable

    def compose(self) -> ComposeResult:
        with Vertical(id='dialog', classes='contract-dialog'):
            yield Static(self.title_text, classes='dialog-title', markup=False)
            with VerticalScroll(classes='form-body'):
                yield Static('本地持久化记录；引用与核验均不派发。' if self.durable else '仅本会话记录，退出丢失；尚未持久化。', classes='muted')
                yield Static(self.body_text, markup=False, id='inventory-body')
            with Horizontal(classes='dialog-buttons'):
                yield Button('返回', id='cancel')
                if self.writable:
                    yield Button('选取引用', id='inventory-add')
                    yield Button('人工核验', id='inventory-verify')

    def on_mount(self) -> None:
        self.query_one('#cancel', Button).focus()

    @on(Button.Pressed, '#inventory-add, #inventory-verify')
    def choose(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss('add' if event.button.id == 'inventory-add' else 'verify')


class EvidenceForm(EvidenceDialog):
    def __init__(self, task_label: str, candidates: dict[str, EvidenceSource], *,
                 prepare: Callable[[str, list[str], dict[str, str]], EvidenceItem],
                 save: Callable[[EvidenceItem], None]) -> None:
        super().__init__()
        self.task_label, self.candidates, self.prepare, self.save = task_label, candidates, prepare, save

    def compose(self) -> ComposeResult:
        with Vertical(id='dialog', classes='contract-dialog'):
            yield Static(self.task_label + ' · 选取历史证据引用', classes='dialog-title', markup=False)
            with VerticalScroll(classes='form-body'):
                yield Static('保存是显式选择：以后核对派发时引用此数据；不会立即发送。旧成果、事实与原件核验不是新许可。', classes='muted')
                yield Static('来源 · 默认未选择；只有规范验收报告或人工业务答案', classes='field-label')
                yield Select(((source.label, key) for key, source in self.candidates.items()), id='evidence-source', allow_blank=True)
                yield Static('请明确选择来源。没有材料/联系人时填写未知，不猜测。', id='evidence-source-detail', markup=False)
                with Vertical(id='evidence-ref-fields'):
                    yield Static('明确选择报告字段 · 默认全不选', classes='field-label')
                    yield SelectionList(('摘要（执行者自报）', 'summary', False), ('核验依据（自报）', 'checks', False),
                                        ('原说明（旧范围）', 'description', False), id='evidence-refs')
                for key, label, limit in (('name', '成果名', 200), ('locator', '原件定位 / 未知 · 不是访问许可', 1000),
                                          ('limits', '历史适用边界', 500), ('gaps', '缺口 / 未核实事项', 500),
                                          ('next_owner', '下一责任 / 未知 · 不猜联系人', 300)):
                    yield Static(f'{label} · 必填，最多 {limit} 字', classes='field-label')
                    yield TextArea(id='evidence-' + key, classes='contract-input')
                yield Static('', id='form-error', classes='error', markup=False)
            with Horizontal(classes='dialog-buttons'):
                yield Button('返回', id='cancel')
                yield Button('核对引用', id='submit', variant='primary')

    def on_mount(self) -> None:
        self.query_one('#evidence-source', Select).focus()
        self.query_one('#evidence-ref-fields').display = False

    @on(Select.Changed, '#evidence-source')
    def source_changed(self, event: Select.Changed) -> None:
        source = self.candidates.get(str(event.value))
        self.query_one('#evidence-source-detail', Static).update(source.detail if source else '请明确选择来源；没有原件/联系人时填写未知。')
        self.query_one('#evidence-ref-fields').display = bool(source and source.kind == 'accepted_report')

    def action_submit(self) -> None:
        try:
            key = str(self.query_one('#evidence-source', Select).value)
            if key not in self.candidates:
                raise ValueError('必须显式选择一条真实来源。')
            refs = (list(self.query_one('#evidence-refs', SelectionList).selected)
                    if self.candidates[key].kind == 'accepted_report' else [])
            values = {key: self.query_one('#evidence-' + key, TextArea).text for key in
                      ('name', 'locator', 'limits', 'gaps', 'next_owner')}
            item = self.prepare(key, refs, values)
        except (ValueError, StorageError) as error:
            self.error(error)
            return
        def confirmed(yes: bool | None) -> None:
            if not yes:
                return
            try:
                self.save(item)
            except (ValueError, StorageError) as error:
                self.error(error)
                return
            self.dismiss('saved')
        self.app.push_screen(EvidenceReview('核对引用 · 默认返回', canonical(asdict(item)) +
                             '\n定位/原件状态：未核实，平台未访问；选定内容仅是历史引用数据。'), confirmed)


class EvidenceVerificationForm(EvidenceDialog):
    def __init__(self, task_label: str, items: dict[str, EvidenceItem], *,
                 prepare: Callable[[str, dict[str, str]], EvidenceVerification],
                 save: Callable[[EvidenceVerification], None]) -> None:
        super().__init__()
        self.task_label, self.items, self.prepare, self.save = task_label, items, prepare, save

    def compose(self) -> ComposeResult:
        with Vertical(id='dialog', classes='contract-dialog'):
            yield Static(self.task_label + ' · 独立人工核验', classes='dialog-title', markup=False)
            with VerticalScroll(classes='form-body'):
                yield Static('只记录你明确说明的人工核验；平台没有扫描原件、核实你的身份或执行新工作。记录追加，不覆写历史。', classes='muted')
                yield Static('已保存库存项 · 默认未选', classes='field-label')
                yield Select(((item.name + ' · ' + item.id, key) for key, item in self.items.items()), id='verification-item')
                yield Static('请明确选择已保存项。', id='verification-source', markup=False)
                yield Static('核验結果 · 默认未选；不是批准', classes='field-label')
                yield Select((('人工已核验（非平台探测）', 'verified'), ('仍未核实', 'unverified'),
                              ('冲突 / 需重新核对', 'conflict')), id='verification-result')
                for key, label, limit in (('verifier', '明确核验人', 120), ('method', '实际核验方法', 500),
                                          ('limits', '核验覆盖边界', 500), ('gaps', '缺口 / 未知', 500),
                                          ('next_owner', '下一责任 / 未知', 300)):
                    yield Static(f'{label} · 必填，最多 {limit} 字', classes='field-label')
                    yield TextArea(id='verification-' + key, classes='contract-input')
                yield Static('实际核验时间 · 必填 UTC，例如 2026-01-01T00:00:00.000Z', classes='field-label')
                yield Input(id='verification-checked-at', max_length=24)
                yield Static('', id='form-error', classes='error', markup=False)
            with Horizontal(classes='dialog-buttons'):
                yield Button('返回', id='cancel')
                yield Button('核对核验记录', id='submit', variant='primary')

    def on_mount(self) -> None:
        self.query_one('#verification-item', Select).focus()

    @on(Select.Changed, '#verification-item')
    def item_changed(self, event: Select.Changed) -> None:
        item = self.items.get(str(event.value))
        text = (f'{item.name}\n原件定位 {item.locator}\n来源摘要 {item.source_digest}'
                f'\n报告 {item.source_report_event_id} / 修订 {item.source_report_seq}\n{item.source_snapshot}'
                if item else '请明确选择已保存项。')
        self.query_one('#verification-source', Static).update(text)

    def action_submit(self) -> None:
        try:
            item_id = str(self.query_one('#verification-item', Select).value)
            if item_id not in self.items:
                raise ValueError('必须显式选择一项已保存库存。')
            values = {key: self.query_one('#verification-' + key, TextArea).text for key in
                      ('verifier', 'method', 'limits', 'gaps', 'next_owner')}
            values['result'] = str(self.query_one('#verification-result', Select).value)
            values['checked_at'] = self.query_one('#verification-checked-at', Input).value
            record = self.prepare(item_id, values)
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
        self.app.push_screen(EvidenceReview('核对人工核验 · 默认返回', canonical(asdict(record)) +
                             '\n这是明确人工陈述；不代表平台已访问原件、报告成为新成果或获得任何执行许可。'), confirmed)
