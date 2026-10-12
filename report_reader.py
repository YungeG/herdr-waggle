"""Full-pane, read-only report snapshots; opening a report never follows links."""

import re

from markdown_it import MarkdownIt
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Markdown, Static, TextArea
from textual.widgets._markdown import MarkdownFence, MarkdownTable


class ReportTable(MarkdownTable):
    """Keep Textual's parsing, but use a scrollable table instead of shrinking cells."""

    def compose(self) -> ComposeResult:
        # These block internals belong to our pinned Textual 8.2.8; UI tests cover the adapter.
        headers, rows = self._get_headers_and_rows()
        table = DataTable(show_cursor=False)
        table.add_columns(*(Text(header.plain) for header in headers))
        for row in rows:
            table.add_row(*(Text(cell.plain) for cell in row), height=None)
        yield table


class ReportCode(MarkdownFence, can_focus=True):
    BINDINGS = [Binding("left", "scroll_left", "左移", show=False),
                Binding("right", "scroll_right", "右移", show=False)]


class ReportSource(TextArea):
    def on_mouse_up(self, event: events.MouseUp) -> None:
        # TextArea captures drags, including release outside its visible bounds.
        event.stop()
        if event.button == 1 and self.read_only and self.selected_text:
            self.app.copy_to_clipboard(self.selected_text)


class ReportMarkdown(Markdown):
    BLOCKS = {**Markdown.BLOCKS, "table_open": ReportTable,
              "fence": ReportCode, "code_block": ReportCode}


_REPORT_HEADINGS = {
    "需要你补充 / 核实", "剩余工作与限制", "本轮交付", "待派发的补充要求",
    "本轮结论（自报）", "本轮做了什么", "本轮交付正文",
    "产物与核实依据 · 自报，不是独立验证",
    "任务说明 / 验收范围 · 本轮派发时固定", "任务说明 / 验收范围 · 当前保存的说明",
    "本轮补充要求 · 派发时固定", "连接提示 · 打开时状态",
}
_FIELD_LABEL = re.compile(r"^([\u3400-\u9fff][\u3400-\u9fffA-Za-z0-9（）()/／、 -]{0,31})([：:]| · )(.*)$")


def _report_parser() -> MarkdownIt:
    return MarkdownIt("gfm-like", {"html": False})


def _reading_markdown(source: str) -> str:
    """Style plain report fields only; the original snapshot remains the copy/source contract."""
    normalized = source.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    rendered = list(lines)
    paragraphs: set[int] = set()
    for token in _report_parser().parse(normalized):
        if token.type == "paragraph_open" and token.level == 0 and token.map:
            start, end = token.map
            paragraphs.update(range(start, end))
    # Token ranges exclude Markdown headings, nested lists/quotes, tables and code.
    for index in sorted(paragraphs):
        raw = lines[index]
        if raw.startswith(("    ", "\t")):
            continue
        line = raw.strip()
        if line in _REPORT_HEADINGS:
            rendered[index] = "\n### " + line + "\n"
        elif raw.startswith("• "):
            rendered[index] = "- " + raw[2:]
            if index == 0 or not lines[index - 1].startswith("• "):
                rendered[index] = "\n" + rendered[index]
            if index + 1 == len(lines) or not lines[index + 1].startswith("• "):
                rendered[index] += "\n"
        elif match := _FIELD_LABEL.fullmatch(line):
            label, separator, value = match.groups()
            rendered[index] = f"\n**{label}{separator.rstrip()}** {value.lstrip()}\n"
    return "\n".join(rendered)


def report_markdown(identifier: str) -> ReportMarkdown:
    return ReportMarkdown(id=identifier, open_links=False, parser_factory=_report_parser)


class ReportReader(ModalScreen[None]):
    BINDINGS = [Binding("escape", "close", "返回", priority=True)]
    DEFAULT_CSS = """
    ReportReader { layout: vertical; background: #182332; }
    #report-title { height: 1; padding: 0 1; text-style: bold; color: #d2ecfa; }
    #report-notice { height: auto; padding: 0 1; color: #9bb0c8; }
    #report-rendered, #report-source { height: 1fr; width: 1fr; }
    #report-source { display: none; border: none; }
    ReportReader DataTable { height: auto; max-height: 16; }
    ReportCode { overflow-x: auto; scrollbar-size-horizontal: 1; }
    ReportCode > Label { width: auto; text-wrap: nowrap; }
    ReportCode:focus { background: #243247; }
    #report-actions { height: 3; padding: 0 1; }
    #report-actions Button { width: 1fr; min-width: 12; margin-right: 1; }
    """

    def __init__(self, title: str, body: str) -> None:
        super().__init__()
        self.report_title = title
        self.body = body
        self.source_view = False

    def compose(self) -> ComposeResult:
        yield Static(Text(f"报告阅读 · {self.report_title}", no_wrap=True, overflow="ellipsis"), id="report-title")
        yield Static("打开时快照 · 只读，不验收；重新打开查看最新内容\n拖选即复制 · Tab/←→ 滚动 · 表格局部文字请看原文", id="report-notice", markup=False)
        with VerticalScroll(id="report-rendered", can_focus=True):
            yield report_markdown("report-markdown")
        yield ReportSource(self.body, id="report-source", read_only=True, soft_wrap=False)
        with Horizontal(id="report-actions"):
            yield Button("查看原文", id="report-toggle")
            yield Button("复制全文", id="report-copy")
            yield Button("原文折行：关", id="report-wrap", disabled=True)
            yield Button("返回  Esc", id="report-close")

    async def on_mount(self) -> None:
        await self.query_one(ReportMarkdown).update(_reading_markdown(self.body))
        self.query_one("#report-rendered", VerticalScroll).focus()

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        if isinstance(event.widget, DataTable):
            event.widget.scroll_visible(top=True, animate=False)
        # Code uses native focus scrolling; forcing top here moves text beneath a mouse drag.

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "report-close":
            self.action_close()
        elif event.button.id == "report-copy":
            self.app.copy_to_clipboard(self.body)
            self.notify("已发送全文复制请求；能否粘贴取决于终端剪贴板支持。", timeout=3)
        elif event.button.id == "report-toggle":
            self.source_view = not self.source_view
            rendered = self.query_one("#report-rendered", VerticalScroll)
            source = self.query_one("#report-source", TextArea)
            rendered.display, source.display = not self.source_view, self.source_view
            event.button.label = "排版阅读" if self.source_view else "查看原文"
            self.query_one("#report-wrap", Button).disabled = not self.source_view
            (source if self.source_view else rendered).focus()
        elif event.button.id == "report-wrap":
            source = self.query_one("#report-source", TextArea)
            source.soft_wrap = not source.soft_wrap
            event.button.label = "原文折行：开" if source.soft_wrap else "原文折行：关"
            source.focus()

    def on_markdown_link_clicked(self, event: Markdown.LinkClicked) -> None:
        event.stop()  # Reading a report must not open a URL or load a local file.

    def action_close(self) -> None:
        self.dismiss(None)
