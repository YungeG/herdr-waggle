# Waggle 待处理事项与决策单：内容组织修改方案

> 状态：本目标已按批准范围最终接受，原任务板 **10/10 done**（见第 23 节）。新界面已按独立加载/输入处置批准加载到原 Waggle pane，D010 停在待补材料页、0 项答复；最新库 revision 2213、166 Runs、34 决定及全部数据/请求指纹不变。自动 Supervisor 保持停止，未操作原 Pi 或派发/验收 D010，未提交/推送/发布。
> 当前授权：仅按第 14 节冻结的 9 个源码/样式/隔离测试文件及符号范围实现本仓展示重构，保留全部既有未提交修改。授权来自真实用户命令，不来自目标记录、READY、任务板编译或监督建议。
> 不包含：真实任务或数据库、D010 答复/派发/验收、真实工作台加载、金融目录/库/研究/预算、原 Pi 操作、宿主改装、提交或推送。后续人工可读性与真实加载门仍保留。

## 1. 结论与目标

**优先做基于现有结构化字段的展示重构，不改决策协议，不引入模型自动改写。**

用户指出的主要问题是内容没有条理，而不是字号、颜色或单纯的文本长度。当前决策单把结论、执行方责任、缺口、出处、回复方式和授权说明混在一起；负责人待补材料又被统一放在“要决定”之下。

修改后的界面应让用户能依次回答：

1. 当前发生了什么，哪些只是执行方自报或尚未核验？
2. 下一步由谁做什么？
3. 缺哪些材料，或者真正需要我回答什么？
4. 我这次回复会改变什么，不会授权什么？
5. 到哪里核对原文、依据和历史？

这是内容组织和交互呈现的方案，不改变金融任务的事实、执行范围、预算或研究资格。

## 2. 现有实现与问题位置

| 现有能力/问题 | 代码位置 | 处理方向 |
| --- | --- | --- |
| 已有受众分类 `user_fact / user_decision / owner_evidence` | `decisions.py: DecisionRouting`、`DecisionSpec.from_record()` | 使用既有分类，不从文字猜受众 |
| 缺口逐项绑定负责人和来源；其顺序须与 `missing` 一致 | `EvidenceNeed`、`DecisionSpec.from_record()` | 保留每条关联，改为清单呈现 |
| 负责人及来源被连接成一段长文字 | `decision_view.py: DecisionSheet.compose()` | 缺口成为正文，出处按条展开 |
| 所有事项共用回复类型和方案选择控件 | `DecisionSheet.reply_controls()`、`intent_options()`、`answer_options()` | 按受众组织首要输入，保留其他回应能力 |
| 单项列表过早截断问题，并反复重复责任/状态 | `DecisionSheet.item_prompt()`、`update_live_status()` | 导航与正文承担不同职责 |
| 确认页无论有无业务问题都列“业务/方案/意图” | `DecisionReview.summary()` | 仅显示与本次实际答复有关的内容 |
| 首页把所有缺口再拼成一个长段落 | `app.py: Waggle.refresh_decision_items()` | 首页仅显示事项、状态、缺口数量和入口 |
| 现有发送守卫、历史来源及草稿恢复 | `decisions.py`、`DecisionSheet.__init__()`、`app.py` | 保留，不为简化界面放宽 |

现有 `formal_available()` 不只是检查单项，还考虑全单材料、追问轮次和受众；展示重构不能绕过它。预算正式批准入口保持未启用。

## 3. 事项分类与内容顺序

### 3.1 分类使用现有结构化字段

| `routing.audience` | 展示名称 | 首要内容 | 用户操作 |
| --- | --- | --- | --- |
| `owner_evidence` | 待补材料 | 缺什么、由谁提供、下一步 | 请求补证、追问、等待 |
| `user_fact` | 待答问题 | 明确的业务问题、回答用途 | 回答、提出条件、明确不知道 |
| `user_decision` | 待核对方案 | 备选方案、范围、风险、成本 | 核对后明确选择 |

规则：

- 展示分类不是新的授权来源。正式选择是否可用，继续服从现有领域守卫和 `formal_available()`。
- 已有校验保证 `owner_evidence` 不能同时伪装成 `human_input` 用户问题；本方案不放松该校验。
- 旧报告缺少 `routing` 时保留兼容处理，不靠正则、关键字或任务编号猜一个新受众。已有明确业务输入及历史适配仍按原来源绑定解析。
- 全单存在缺证、处于仅追问轮次或预算入口未启用时，不能因一项看起来完整就显示可正式执行。
- 混合单使用“待处理事项”作为总标题，逐项显示自身类别，不把全单强行归为一种受众。

### 3.2 单项正文统一五层

在条目区之前，单独显示一次“本轮情况 / 全局交接”，数据来自与来源事件绑定的报告快照；字段与旧报告回退见第 5.5 节。切换条目不复制或改写它。

单项保留五层：

1. **本项问题与原因**：显示 `human_input.question` 或 `spec.question` 及 `spec.why`，不从长报告猜结论。
2. **本项责任与后续**：负责人来自本项字段；下一步使用该受众的固定保守提示，不虚构联系人或把“报告列出”当作已核实。
3. **需要处理的内容**：缺口清单、业务问题或方案对照。
4. **你的回应**：只突出当前事项需要的操作和输入。
5. **依据与历史**：各缺口对应出处、完整原报告和旧答复，按需展开。

报告级交接与单项问题不同。即使仅有一项，也不把全局 `handoff.next_action` 改标为已证明的单项职责；单项未提供后续信息时明确显示保守提示。报告快照接线与完整原报告的取回规则见第 5.5 节。

### 3.3 待补材料的呈现规则

- 每条 `gap` 独立显示，不再把 `owner + source` 串成段落。
- 同一负责人可归为一组；按原字符串分组，不推断同名就是同一个实际联系人。
- 分组只影响展示，不改原 `DecisionSpec`；每条保留原序号以及其 `gap / owner / source` 对应关系。
- 来源放在对应缺口的“查看依据”内。原文字完整可取回，不能仅留下截断后的摘要。
- 未逐项给来源的旧报告显示“原报告未逐项提供来源”，不补造一个满足新校验的来源。
- 不自动把缺口分类为“结果/账本/环境”等业务类别。那些标题需要可靠结构化字段或另行确认，不能依赖 D010 专用关键词。

### 3.4 重要信息不得折叠隐藏

可以按需展开：文件路径、行号、专业解释、完整原文、连接 UUID 和历史细节。

必须在相关审阅页直接可见：实际操作范围、适用条件、风险、成本、重要未知项、排除项及确认后果。长内容允许滚动；不能为省空间隐藏会影响批准判断的条件或否定。

“保存草稿”“正式确认仅保存”“发送/派发”继续保持不同动作。临时选择、渲染成功、执行者自报和人工验收也不得混同。

## 4. D010 内容重排示例

以下依据讨论时的只读快照：SQLite revision `2213`，Run `54fb42cb-f521-4dac-8788-f8ddf7a10785`，报告 seq `3`，`finished / partial`。这是历史样例，不是当前执行状态承诺，也不授予读取候选文件的许可。

```text
待补材料 · D010

当前情况（执行方自报）
已提出10个候选文件的核查草案，尚未读取和核验。
原始结果及累计用量仍待确认。

下一步
Main负责在明确的新核查范围获确认后，
核对已有结果和历史用量，不先重做诊断。

待补材料 · 5项
1. E16原结果、注册与关闭记录未核
2. 原总上限与累计已用未核
3. 解释器及代码版本未核
4. 轮次、写权依据及持有人待明确
5. 已留测量能否覆盖缺口尚未核
   每项都可展开对应依据和完整原文。

[请求执行方补证]  [其他意见]
反馈（选填）：……

本次回应不直接授权读取候选文件、重做实验或使用旧预算。

[核对所选请求] [仅本地保存] [保存草稿] [返回]
```

示例中的简短缺口是人工整理示意，不是实现时的自动改写规则。实际第一版使用原始 `gap` 文本并重新组织。

用户不必先理解“答复类型/技术方案”等内部术语。点击补证快捷入口可复用现有显式选择路径；不自动发送，仍须进入最后核对。

## 5. 实现方案与最小写入范围

### 5.1 一个纯展示投影，不增加执行引擎

拟新增 `decision_presentation.py`，供首页、单项处理和最终确认使用。

拟接口：

```python
present_decision(spec: DecisionSpec, *, human_input: BusinessInput | None) -> DecisionPresentation
```

`DecisionPresentation` 为不可变的单项展示结果；冻结为下列值，不承担报告级交接或授权判断：

- `category: Literal["owner_evidence", "user_fact", "user_decision", "legacy"]`：显式 routing 的受众；无 routing 使用 `legacy`，保留原有合法回应能力。
- `question: str`、`why: str`、`guidance: str`：问题/原因原文与受众固定提示。业务问题仍由已有来源绑定解析；最终确认按实际 `Answer` 选择已回答的问题，不用该类别替代答复校验。
- `provider_groups: tuple[ProviderGroup, ...]`；`ProviderGroup(owner: str, needs: tuple[PresentedNeed, ...])`；`PresentedNeed(index: int, gap: str, owner: str, source: str | None)`。两个分组子值同样使用 frozen dataclass，内层只含字符串/整数/None 和 tuple。索引为本项原始缺口的 1 起始序号，保留每条原文和映射；仅旧报告未逐项提供出处时 `source=None`，渲染缺失提示而不造一个真实来源。

显示建议、范围、风险、成本与排除时读取原 `DecisionSpec` 的对应字段，不复制一个可能不同步的授权模型。报告级信息使用独立的 `ReportViewSnapshot`（第 5.5 节）。

职责边界：

- 只接收已解析的现有值，确定性投影，不读取文件、不访问数据库或会话。
- 不生成或修改 `Answer`，不生成 `DecisionPreview`、意图或 Run，不编译发送文本。
- 不修改原值、不重算协议身份、不改变批准、预算或派发守卫。
- 显式路由优先；无路由的旧记录保留为兼容分支。
- 业务问题仍由现有 `business_input()` 的来源绑定取得。
- “是否可以正式提交”继续由调用方现有守卫决定，不在投影中重建授权逻辑。

投影是展示的一致入口，而不是新权限层。避免为每个受众创建新的框架、插件或独立持久化实体。

### 5.2 精确写入范围

| 文件/符号 | 拟修改内容 |
| --- | --- |
| `decision_presentation.py`（新增） | `present_decision()` 与小型不可变单项值；`ReportViewSnapshot`、`capture_report_view()`、`report_view_event()`；无 I/O 与授权判断 |
| `DecisionSheet.__init__()`、`compose()`、`item_prompt()` | 捕获原报告快照，按类别组织全局信息与单项正文；单项时避免重复完整问题 |
| `DecisionSheet.reply_controls()`、`update_live_status()` | 其他回应按需展开；折叠外保留当前答复/发送参与提示；维持原控件身份、临时值和恢复能力 |
| `DecisionSheet.reveal_reply()`（新增）、`on_mount()`、`on_button_pressed()`、`on_collapsible_collapsed()`（新增） | 落实第 5.6 节的初始化、快捷入口、收起后焦点和布局后检查；不改 `answers()` 的参与判定 |
| `DecisionReview.__init__()`、`compose()`、`summary()` | 接收强制绑定的报告展示快照；分开显示实际答复、原报告与提交全文；落实第 5.7 节关键字段可见性 |
| `Waggle.prepare_decision_sheet()`、`submit_decision_preview()` | 将表单快照独立传给确认页，并在既有 `validate_preview()` 守卫之外核对展示快照一致性；不改变意图生成/派发算法 |
| `Waggle.refresh_decision_items()` | 首页仅显示短事项、真实状态、缺口数和入口，不再串联所有缺口 |
| `style.tcss` | 仅为分组、换行、折叠与窄屏做必要调整，不另做主题改造 |
| `test_decision_presentation.py`（新增） | 纯投影、分组、旧记录和不变性测试 |
| `test_decisions_ui.py`、`test_decision_review_layout.py`、`test_n3_intents.py`、`test_workbench.py` | 内容顺序、实际可见性、草稿/焦点与确认流程回归；布局夹具同步传入 report_view |

上述方法是审阅/实施锚点。第一版不修改 `DecisionSpec` wire、`decisions.ts`、SQLite schema、`store.py`、桥接、纳管协议或原始报告。

### 5.3 兼容的交互变化

- 优先复用 `decision-owner-request-*` 的显式选择行为：点击才选择 `feedback + request_info`，不会保存或发送。
- “其他回应”保留现有回复类型和方案/倾向选择能力。相关控件保持挂载和原 ID；折叠不是清空或重建答案。有效回应的恢复、展开、收起与焦点规则按第 5.6 节执行。
- 不预选任何新答案。已有草稿恢复原值并展开对应回应区，旧确认继续只作原规则允许的背景。
- 用户点击最后核对入口后才构造既有预览；确认页仍默认聚焦“返回修改”。
- 不自动丢弃其他已选条目；实际涉及哪些条目，在确认页明确列出。未选项不发送。
- 正式选择与事实/反馈混合时，仍要求拆分提交，不能借内容重排放宽。
- 没有新证据时继续显示等待负责人，不默认重复派发。
- 当历史/旧报告同时支持业务回答和技术选择时，不因新的展示类别藏掉原有合法能力。

### 5.4 最终确认页

按“本次答复 → 接收对象 → 实际后果 → 原文来源”组织。

- 无业务输入且未回答业务事实：不再显示“本项无独立业务问题”。
- 只回答业务事实：突出真正回答的业务问题，不把另一个技术问题当作已回答。
- 只表达倾向或请求补证：明确不授权实施，不生成“正式选择”的文案。
- 正式选择：保留所选方案、准确范围、条件、风险、成本及排除项；预算正式批准入口保持未启用。
- 保存草稿/仅本地保存/新轮发送/本轮补充四种后果继续明确，不能用统一“确认”掩盖。
- 长目录与完整确认按钮必须换行/可见，不靠省略号隐藏关键目标或后果。
- 第 5.7 节列出的条件、未知项、签收状态和排除项直接可见；会实际发送时默认展示 `preview.compiled_text` 的权威全文，不用摘要替代它。

### 5.5 报告展示快照：值、接线、来源与回退（补齐 P1）

**数据值与接口**

```python
@dataclass(frozen=True)
class ReportViewSnapshot:
    source: Source       # 既有 frozen Source(run_id, event_id, seq)
    source_bytes: str    # Run.events 中原有的完整保留 envelope；不重新编码

capture_report_view(source: Source, source_bytes: str) -> ReportViewSnapshot
report_view_event(snapshot: ReportViewSnapshot) -> Event
```

- `capture_report_view()` 用既有 `journal.parse_event()` 解析，要求事件的 `(run_id, id, seq)` 与 `source` 完全相同、`source="executor"`，且报告及决策项存在；不符则 `ValueError`，不生成默认快照。这里的 `source="executor"` 指事件来源字段，不是 `Source` 数据类。
- `report_view_event()` 每次从保留字符串返回新解析的 `Event / Report` 并复核同一绑定。不得返回缓存的可变 `checks`/`brief.actions` 列表给多个页面共享；UI 不修改解析结果。核心快照只有 frozen `Source` 和不可变字符串，避免浅 frozen 的 `Report` 内仍共享列表。
- 原 envelope 已包含 `summary / checks / remaining / brief / decisions / handoff` 的实际出现字段及原始文字；没有的可选字段仍保持没有。不用 `encoded_event()` 补新 ID、时间或默认字段，不解析/打开报告内的文件链接。
- 快照不是新的许可、请求、数据库记录或 fingerprint 输入；不新增 JSON/wire 字段，也不改 `DecisionPreview`。

**三个消费入口与确认接线**

1. `DecisionSheet.__init__()` 仍先调用 `decision_source(board, task_id)`。从当前 `Run.events` 取唯一一个 `parse_event(raw) == event` 的原字符串（与 `preview_decisions()` 的 `source_bytes` 取法相同），构造并保存 `self.report_view`；若有 `self.basis`，必须与 `self.basis.source` 相同。找不到或绑定不符就拒绝创建新交互，不序列化一个替代原件。
2. 表单顶部使用该快照显示一次全局现状和交接；单项使用 `present_decision()`，不得复用全局动作填单项职责。完整原报告使用该快照取回，不跟随后续轮次或链接。
3. `Waggle.prepare_decision_sheet()` 在既有来源/异步前置检查后，将 `form.report_view` 作为独立、强制的 keyword 参数传给 `DecisionReview(..., report_view=form.report_view)`，而不是从新状态另取报告覆盖用户已看的材料。传递前要求快照 `Source == preview.source`、解析出的 `report.decisions == preview.specs`，并对照当前来源原 envelope 与 `source_bytes` 一致；任何差异均保留输入、提示重新核对。
4. `DecisionReview.__init__()` 增加必需的 keyword-only `report_view: ReportViewSnapshot`，再次核对 source/specs 绑定，保存只读引用。仓内现有两个直接构造位置是 `app.py` 与 `test_decision_review_layout.py: make_review()`，两处同步显式传入；不提供缺快照仍可发送的默认降级路径。
5. `DecisionReview.compose()` 将“完整原报告（来源快照）”与“本次实际提交文本”分开。前者从保留 envelope 逐字段展示完整报告，并保留只读原 envelope 取回入口；后者始终使用原 `preview.compiled_text`。二者均不得被新报告静默替换。
6. `Waggle.submit_decision_preview()` 保留原 `validate_preview(board, preview)`、当前窗口/目标/意图守卫；在存储或发送之前再次对照当前来源原 envelope 与 `review.report_view` 以及 preview 的 source/specs。新检查仅拒绝“展示与确认不一致”，不改变编译文本、fingerprint、记录格式、预算或发布流程。若来源变化，不能靠刷新快照继续提交。
7. 首页 `refresh_decision_items()` 从当前已验证报告得到条目投影，只显示原问题、状态、缺口数；首页不缓存全局交接作为将来批准依据，也不额外读取邮箱。

**字段与旧报告回退**

| 报告字段 | 展示规则 |
| --- | --- |
| `handoff.result / blocker` | 顶部“本轮情况 / 阻碍（执行方自报）”，原句显示；空 blocker 显示“报告未列全局阻碍”，不声称已经没有阻碍 |
| `handoff.next_actor / next_action / user_need` | 单独标为“全局交接”，使用现有枚举的固定标签；仅显示一次，不推断每条 gap 的执行许可 |
| 无 `handoff` 但有 `brief` | 只用 `brief.outcome / verification` 展示结果/核对情况（自报），动作列表供展开；显示“全局下一责任未提供”，不从 brief.actions 猜 next_actor 或 user_need |
| 无 `handoff`、无 `brief` | 顶部固定提示“收到本轮报告；未提供短交接，结论见完整原报告”；不截取 summary 首句冒充结论 |
| `summary / checks / remaining` | 完整原报告入口逐字段保留正文、原检查顺序和剩余事项，不等同独立验证或允许继续执行 |
| `decisions` 与其他实际出现字段 | 与同一来源 snapshot 一起完整可取回；不以正文短标签覆盖原字段 |

旧报告缺少 handoff/brief 是显示回退；缺少、损坏或不匹配的来源快照不是回退条件。打不开有效决策材料时继续原历史/只读路径，不补造字段或允许新提交。

**等价性验收**：在同一 Board、答案和 Target 下接线前后调用原 `preview_decisions()`，要求返回值、fingerprint、compiled_text 完全相同；保护原 Run、事件字符串、决策记录和预算字段。另用两项不同负责人和一个全局 next_action 验证全局动作只出现一次，原报告展开可恢复全部字段。

### 5.6 折叠回应、草稿恢复与焦点（补齐 P2）

每项“其他回应”容器固定为 `#decision-other-reply-{index}`；其外保留 `#decision-reply-status-{index}`。原 `#intent-* / #answer-* / #comment-* / #business-*` 控件保持挂载和 ID。折叠状态只保存在本窗口的 `Collapsible.collapsed`，不写入 `Answer`、fingerprint 或 SQLite。

| 场景 | 确定行为 |
| --- | --- |
| 新表单、原意图为空 | 默认收起其他回应；不填新答案。折叠外提示“未选择，本项不发送” |
| 同源同范围草稿/现规则允许恢复的本地反馈 | 用现有 `initial`/`answer_intent()` 恢复原值；恢复后 intent 非空的条目展开对应回应区。旧确认/旧业务事实仍只读背景，不由该规则自动填入 |
| 已选意图但回答未完整 | 保留意图与输入，展开区域；折叠外显示“本项已开始填写，提交参与以当前答案和既有校验为准”，不报已确认或已发送 |
| 用户收起当前有效回应 | 原值、意图和 `answers()` 结果不变；折叠外显示本次意图和当前答复。状态由既有答案校验决定，不能仅凭 intent 非空声称会发送，规则见下文 |
| 被收起内容仍持有焦点 | 布局刷新后，若焦点仍在该隐藏回应区内，统一转到可见 `#decision-overview`；不抢走已在别处或折叠标题上的焦点 |
| 用户展开 | 不改值或自动选择意图；普通展开不擅自聚焦输入；保持展开入口的键盘焦点 |
| 点击补证/其他意见/回答业务问题等快捷入口 | 沿用原显式意图/值选择路径；先展开对应容器，再刷新适用字段，最后滚动并聚焦原目标输入。只改变临时表单，仍不保存或发送 |
| 切项、从确认页返回、缩放 | 保留每项容器的当前展开状态和全部输入；切项仍由 overview 管理焦点。重新打开窗口只按已存同源草稿恢复，不为折叠状态新增持久化 |
| 有明确条目/字段的业务条件或备注校验失败 | 展开该回应区以显示既有就地错误；不改答案。不能定位到字段的错误保持既有 `#decision-error` 焦点，不解析错误文字猜条目 |
| 用户等待/只读、窗口已关闭或已有忙碌操作 | 保留原限制；不聚焦 disabled 字段、不恢复旧批准、不触发新请求 |

拟新增 `DecisionSheet.reveal_reply(index: int, target_id: str) -> None`：展开该容器，调用现有适用字段更新，并在 `call_after_refresh()` 中核对表单仍运行、仍为当前页面、选中条目未变化且未忙碌，再滚动/聚焦目标。条件变化则只返回，不改值、不提交。原快捷入口统一调用，避免各调用点漏展开。

`on_collapsible_collapsed()` 只处理上述回应容器：更新外部状态提示并应用“隐藏焦点回 overview”规则，不修改 `self.intents / technical_values / owner_requests`。输入因意图不适用而隐藏与祖先折叠是两回事；`answers()` 继续按原意图判定，不能以 collapsed/visible 作为发送条件。初次自动展开在 `on_mount()` 的原值恢复后进行；后续 `update_live_status()` 只更新外部状态，不反复强行展开用户已手动收起的区域。

折叠外状态从既有 `answers()` 结果取得：原意图为空或本项等于 `Answer()`，显示“无新增答复，本项不发送”；有非空合法答复，显示“本项已选择，将列入最后核对，尚未发送”；当前校验失败则显示“答复待校验，不会提交”，并保留原错误提示。它只解释当前内容，不替代全单混合、准备度、目标或正式提交守卫，也不声称本地记录中的未选项会被删除。

验收必须覆盖真实点击/键盘展开与收起、恢复后目标输入的实际可见性、焦点回退及迟到布局回调。每次展开/收起前后比较 `answers()`、原控件值和 snapshot；它们不能因折叠本身变化。

### 5.7 关键字段来源与实际可见性（补齐 P2）

单项页与最终确认页使用同一份原 spec/routing，而非从拼接文案或长 summary 提取含义。

| 关键内容 | 字段来源与直接可见规则 |
| --- | --- |
| 本项范围、风险、成本 | `spec.scope / risk / cost`；在相关审阅段直接显示，不要求展开原文 |
| 所选方案范围、风险、成本 | 当前合法 choice 对应 `DecisionOption.scope / risk / cost`；与整体字段不同则两套分别显示，不覆盖整体限制或只显示较轻的一套 |
| 适用条件、建议值与单位 | `routing.advice.status == "supported"` 时直接显示 `value / unit / conditions`，标题注明“建议，非批准”；`insufficient` 时只说明暂无可推荐值，不造条件或设置数值 |
| 重要未知项 | 有 routing 时显示原 `advice.uncertainty`，并保留 `spec.missing` 清单；旧报告未提供结构化 uncertainty 时明确“未提供结构化不确定性”，不解析/改写长报告填空 |
| 排除项 | 原 `spec.exclusions` 按条直接显示；为空只沿用原“仍受任务范围限制”提示，不增加或缩小限制 |
| 相关签收状态 | 有 routing 时显示 `advice.owner_signed` 的“是/否”，标记“报告自报；不是独立核验或本次批准”；没有该字段时不能展示为已签收 |
| 实际目标和确认后果 | 原 `preview.target`、现有目标 label 和提交语义；精确目录、接收对象以及四类动作后果保持可见、完整换行 |
| 实际发送全文 | 当 `target.kind` 为 `new_run / live_supplement`，将只读 `preview.compiled_text` 从原来源折叠区移出，作为默认展开的“本次实际提交文本（权威全文）”段。不能用摘要替代，也不截断任何继承的条件；不跟链接或执行文本。草稿/仅本地保存仍可按需展开其完整编译文本 |

确认摘要只组织本次实际答复；未选条目仍显示未选数量和“不发送”，不能把其建议当所选方案。适用的条件、未知项、排除和签收说明保留在相关审阅区；缺字段不是已验证无风险。无需展开完整原报告即可核对这些结构化关键字段；遇到非结构化的继承要求，以默认展示的实际提交全文为准，不宣称投影已识别其中所有条件。

新增 routed 夹具必须含长条件、否定语句、UNKNOWN/尚未核实、未签收，以及整体与所选方案不同的范围/风险/成本；同时有缺 routing 的旧记录与 `insufficient` 反例。保持完整原报告折叠，用真实键盘滚动采集 compositor 画面中目标组件与滚动区的交集，按内容行位置重建可见文本；不得只检查 `Static.content`、`render_lines()` 或字串存在。40/80 列及宽屏都应完整取回关键文字、否定、目标目录和确认按钮，展开原文不成为测试成功的前提。

## 6. 实施顺序与验收

实施须另获授权；本次写文档和审阅不启动以下步骤。

### 阶段 A：锁定展示投影与隔离样例

先用纯 fixture 覆盖三种受众、旧报告、多个负责人、业务/技术双问题、预算项和无新证据等待。

先做一个小样 smoke，证明投影保留原字段和原索引、无 I/O、不改变 `DecisionSpec`；再扩大覆盖。另冻结第 5.5 节的来源快照和旧报告回退接口，先用一个双负责人/单全局交接的隔离 fixture 捕捉快照错绑或全局动作被复制，再扩大覆盖。不使用真实 D010 邮箱作为测试目标。

### 阶段 B：统一三个展示入口

同步处理单项页、确认页和首页摘要，避免只修 D010 单页。

先跑一个 80 列待补材料样例，再验证 40/80 列及宽屏；保留键盘聚焦、滚动、展开、切项与返回行为。缩放不能改变答复或来源。

### 阶段 C：回归与人工可读性核对

| 验收点 | 证据/测试位置 |
| --- | --- |
| 三种受众组织不同；旧记录不猜受众 | `test_decision_presentation.py` |
| 每条缺口的原索引、负责人和来源不丢失；分组不改原值 | 纯投影测试与原记录指纹对照 |
| 来源事件与原 envelope 的不可变快照绑定；完整报告所有字段可取回 | 纯快照测试拒绝错 Run/事件/seq 或非法 envelope；准备/提交接线测试拒绝同 ID 但原文已变化。修改新解析的 checks/actions 列表不改变快照；纯 helper 不假称能独立核验未知原文真实性 |
| 全局交接仅显示一次，不当作两项不同负责人的单项下一步 | `test_decisions_ui.py`：两项/双负责人/单全局动作；无 handoff 有 brief、两者均无的回退 |
| 展示接线不改变预览、fingerprint、compiled_text 或历史记录 | 原 `preview_decisions()` 同 Board/答案/Target 的精确等价对照；保护原 Run/事件/记录字节或指纹 |
| 首页不复制长段落，单项正文有明确顺序 | `test_workbench.py`、`test_decisions_ui.py` |
| 范围、条件、风险、成本、重要未知项、排除项、相关签收状态与确认后果直接可核对 | `test_decision_review_layout.py`：第 5.7 节 routed 长文/否定/未签收夹具；原报告保持折叠，用实际 compositor 与键盘滚动复原全部关键内容 |
| 会实际发送时默认展示完整权威提交文本；目录和完整确认按钮未被截断 | `test_decision_review_layout.py`：`new_run / live_supplement` 和长继承条件；不把只读全文当执行或许可 |
| 草稿重启恢复、隐藏字段不伪装成当前答案、无预选 | `test_decisions_ui.py`、`test_n3_intents.py`：恢复后对应区域展开且实际可见；已确认历史仍不填入 |
| 展开/收起不改变 answers 或控件值，有效回应收起后仍明确提示 | 40/80 列真实键盘/点击；未选意图保持 `Answer()`，收起有效回应不偷偷增减发送项 |
| 快捷入口先展开再聚焦；隐藏焦点回 overview；迟到回调不影响别的条目/页面 | `test_decisions_ui.py`、`test_n3_intents.py`：检查实际可见性和焦点，含关闭、切项、忙碌和缩放 |
| 返回、切项、缩放、校验失败和保存失败不丢输入 | UI/布局/对话回归；明确字段错误展开对应区域，全局错误保留可见 error 焦点 |
| 取消、来源改变、迟到响应不创建意图或发布请求 | 既有决策与对话流程测试 |
| 未选项不发送，混合答复仍拒绝，旧批准不默认沿用 | 既有领域/决策测试 |
| 纳管后的新轮仍能进入确认页，不复发 D010 校验问题 | `test_adoption_ui.py` |
| Python 静态检查与格式检查 | Pyright、Ruff；不能运行时明确记录缺口，不报通过 |

回归可沿用 `just test` 和现有隔离 SQLite、Textual Pilot、私有假邮箱，不新增测试框架。样例通过再执行相关完整回归，不拿大规模测试发现普通导入或夹具错误。

最终需用户实际阅读一版并检查：能否不依赖内部术语说明“谁做什么、缺什么、我的回复意味着什么”。自动化结构/布局测试不能替代这项判断，也不构成终端无障碍认证。

### 阶段 D：真实界面加载

仅在实施和验证完成、且加载真实界面已获授权后执行。

先核实工作台未提交输入并保存/保留，再重开 Waggle。不得为此自动刷新原 Pi、派发 D010、重复纳管、重放旧 Run 或续旧预算。

## 7. 不做的事与取舍

- **不做 D010 特判**：否则下一个报告仍会混乱。
- **不让模型先把所有内容重写一遍**：语义漂移可能误导批准，也增加成本和不可复现性。
- **不为本轮增加新 schema 或独立执行实体**：已有 `routing` 足够支撑第一版；未来结构化短标题等能力应独立设计兼容性。
- **不靠折叠藏掉难读但重要的条件**：折叠只管理次要细节。
- **不删除领域守卫来简化表单**：展示路径不能成为批准路径。
- **不把样例短句当已实现的自动摘要**：第一版重新组织原字段，不宣称已解决全部专业措辞问题。

## 8. 设计依据与适用边界

### 仓库依据

- [人机协作与交接设计](WAGGLE_COLLABORATION_DESIGN.md)：表达的含义与权限不能悄悄变化；不应要求用户先学会回复分类；加工不能丢失条件和未知项。
- [决策反馈与建议闭环研究](DECISION_FEEDBACK_RESEARCH.md)：反馈/补证与正式授权分离；现有结构化受众及逐项缺口；历史记录不可改写。
- [既有实施计划](WAGGLE_IMPLEMENTATION_PLAN.md)：本稿不覆盖旧计划或已批准工作范围。

### 已核对的一手设计资料

- [GOV.UK Question pages](https://design-system.service.gov.uk/patterns/question-pages/)：只问真正需要用户提供的信息，说明为何询问；必要时允许“不知道”。
- [GOV.UK Details](https://design-system.service.gov.uk/components/details/)：次要详情可按需展开；多数用户必须知道的信息不应隐藏。
- [GOV.UK Check answers](https://design-system.service.gov.uk/patterns/check-answers/)：确认前分组展示相关答案，清楚写出提交动作，返回修改时保留输入。

借鉴的是信息组织和核对原则，不直接照搬网页控件。Textual 键盘、窄屏及可见性效果需本仓单独验证。

## 9. 本次审阅要求

本次由只读 reviewer 对照当前源码审阅该计划，不实施、不运行真实任务。审阅应检查：

1. 是否解决了内容组织问题，而非只做改名/折叠。
2. 纯展示投影的接口、职责和调用方是否足够明确且不过度设计。
3. 三类受众、旧报告、业务/技术双问题及混合单是否有遗漏。
4. 草稿恢复、隐藏字段、正式守卫、确认语义及完整范围是否保持兼容。
5. 验收是否能捕捉真实界面和流程回归，是否把尚未运行的检查写成已通过。

要求输出带计划章节及源码位置的具体问题、优先级、最小修改建议和实施准备度结论。审阅结论不等于实施授权；此文的审阅状态须待结果核对后更新。

## 10. 首次审阅启动记录（历史）：环境阻断，未执行

- 已尝试启动一个 fresh-context、只读 `reviewer`；预启动标识为 `1f13f607-2eef-4f47-836b-b8b67fb3d756`。
- 工具返回：后台子进程需要宿主 Pi npm 包及依赖，但当前安装不能提供 `@earendil-works/chord`、`@earendil-works/chord/context`。
- 只读核查：宿主 `@earendil-works/pi-coding-agent` 版本为 `1.2.0`；其 `package.json` 的 dependencies 未声明 chord；上述两个模块的解析均返回 `MODULE_NOT_FOUND`。这说明启动依赖条件不满足，尚不能断定正确修复是安装依赖、更新插件还是调整版本。
- 原预启动标识没有形成可查询的异步 Run，状态查询显示活动异步容量为 0。没有正在运行的该次审阅，也没有产出 `WAGGLE_DECISION_PRESENTATION_REVIEW.md`。
- 因此没有实施准备度结论、独立审阅通过或审阅发现；不能把启动失败当作方案被否决或已通过。
- 方案本地链接、结尾换行和空白检查通过；启动后对照指纹确认当时保护的 32 个文件均未改变，HEAD 未改变。随后仅在本方案中记录本阻断。
- 未自动修改宿主安装、运行中的 Pi 或真实任务，也未换用交互终端、前台子进程或其他 CLI 绕过失败。
- 当时的下一步：需先明确并处理 Pi 与 subagent 插件的依赖兼容性；修改宿主环境须另获用户授权。之后用户已授权启动环境修复与同协议重试，见第 11 节。

## 11. 启动环境兼容修复与检查

用户已允许修复 subagent 启动环境并重试，不操作 D010，不授权实施本方案。

- 根因：Pi 1.2.0 本身未声明或导入 chord，SDK 直接导入正常；pi-subagents 0.77.0 的 `resolveHostPeerAliases()` 只为已知早期 0.x 版本豁免 chord，对 1.2.0 仍强制要求两个 chord 入口。
- 为不重启或热改当前会话，使用本机已有真实 `@earendil-works/chord` 1.1.0 包补齐兼容解析路径，没有下载新包、伪造包身份或修改 Pi/插件源码。
- 新增的唯一兼容链接：`/home/ygguo/.nvm/versions/node/v25.9.0/lib/node_modules/@earendil-works/pi-coding-agent/node_modules/@earendil-works/chord`，指向 `/home/ygguo/agent-projs/agent-ext/pi-task-supervisor/node_modules/@earendil-works/chord`。真实路径保留该已有包的依赖解析范围。
- 修复记录与校验脚本：`/home/ygguo/.pi/agent/runtime-fixes/subagents-chord-20261013T073442Z/repair.json`、同目录 `verify.mjs`。
- 同一路径反馈环：修复前真实宿主别名检查因两个 chord 入口缺失返回 RED；链接后返回 PASS，实际 native runner preload 成功导入全部 12 个宿主别名。均没有模型调用或创建 AgentSession。
- 负向守卫：隔离夹具中的 chord/context 声明目标缺失、整个 chord 包缺失仍被原解析器拒绝。Pi 版本、宿主/插件源码和已缓存包的保护指纹未改变。
- 限制：这是本地兼容补偿，不是官方 Pi 1.2 全功能支持证明；插件或缓存包更新可能移除/改变链接。长期应采用正确识别新 Pi 版本的插件，在安全加载后撤除兼容链接。回滚仅删除已确认仍指向该目标的本次链接，不删除目标包。
- 后续：已由主助手使用原 `subagent` 工具完成只读审阅，实际结果见第 12 节；启动兼容检查通过不等于修改方案通过。

## 12. 独立审阅结果与主助手核对

完整报告：[WAGGLE_DECISION_PRESENTATION_REVIEW.md](WAGGLE_DECISION_PRESENTATION_REVIEW.md)。

- 审阅 Run：`360de08d-ba19-4153-a059-a46c2a135f66`，`reviewer`，fresh context。
- 模型：`cliproxyapi/gpt-6.1-sol`，thinking high；运行状态 `complete`，进程终态已观察，未等待其他 supervisor 请求。
- 审阅对象为当时的 280 行方案，SHA256 `3c6d82d7537ad962147482d312657a08341b6ba5e31b3319c7985f8c72b8078e`。本节记录该版本审阅结果及后续文档处置；本轮已补齐设计，但不是原 reviewer 已复核修订版的证据。
- 历史结论：该 280 行审阅版本为 `IMPLEMENTATION READINESS: NOT_READY`；方向和保留的安全约束得到认可。该结论不是实施授权，也不是针对金融任务的 Go/No-Go；本轮修订不覆盖原审阅报告，也不等于修订版已复审通过。

| 发现 | 主助手核对 | 本次文档补齐（非实现/复审结论） |
| --- | --- | --- |
| P1：报告级交接与完整原报告没有冻结数据接线 | 有效。`Report` 含摘要/检查/剩余/交接；`DecisionSpec` 及 `DecisionPreview` 不含完整报告。现确认页展示的是编译请求，不是完整原报告。 | 已写入第 5.5 节：frozen Source + 原 envelope 字符串，确定快照构造/读取接口、表单→确认页接线、源一致性核对和旧报告回退；不改 preview wire。 |
| P2：折叠回应的恢复/展开/焦点规则未冻结 | 有效。现快捷入口只设意图、滚动和聚焦，不会展开拟新增的祖先折叠容器；`answers()` 也不以是否折叠判断提交内容。 | 已写入第 5.6 节：控件/容器 ID、恢复展开、显式 reveal_reply、隐藏焦点回 overview、外部状态提示及迟到回调前置条件；展开状态不影响答案。 |
| P2：重要条件与未知项的可见性验收不完整 | 有效。结构化建议的条件/不确定性/签收状态存在于单项显示，但现确认摘要和布局夹具未完整覆盖。 | 已写入第 5.7、6 节：逐字段来源/回退/可见性矩阵，routed 长文与否定/未签收反例、实际 compositor 滚动验收，以及会发送时默认展开权威全文。 |

主助手定点读取了 `journal.py`、`decisions.py`、`DecisionReview.compose()`、快捷入口与 `prepare_decision_sheet()`/`submit_decision_preview()` 接线，核对上述问题成立。没有把 reviewer 的建议直接写入源码或当作已修复。

历史完成边界：“记录方案并用 subagent 审阅”已完成；当时设计缺口与实施许可仍待处理。后续用户已授权补齐方案，本轮只修改本文，未重新运行实施测试，未修改 D010、原 Pi、实际预算、既有报告或业务源码。

## 13. 本轮方案补齐与验证边界

- 当前授权仅为“补齐方案”。第 5.5–5.7 节、精确写入范围、内容顺序和第 6 节验收已按三项有效审阅发现修订，不是源码实现。
- 设计接线已对照实际 `Source`（仅 run_id/event_id/seq）、`Report`（含可变内层列表）、`preview_decisions()` 的原 envelope 指纹取法、`current_report()`、两处 `DecisionReview` 构造点以及现有快捷入口核对；没有假定 Preview 已带完整报告，也不新增 schema 字段。
- 独立 R1 审阅报告保留原文及原版本结论，不回写成 READY。此修订阶段结束时曾待复审；后续独立 R2 结论另见第 14 节。本文中的新实现测试、布局与实际可读性验收均尚未执行，不记录为通过。
- 文档验证限本地链接、标题/代码块结构、修订覆盖和文件保护指纹；这是文档检查，不是展示功能、发送流程或真人可读性通过。
- 后续由主助手完成针对三项发现的独立 R2 复审及准备冻结，见第 14 节；复审并不授予实施。源码修改与真实工作台加载仍须另获授权，不能自动保存或发送 D010 回应。

## 14. R2 复审与冻结的实施边界

完整 R2 报告：[WAGGLE_DECISION_PRESENTATION_REVIEW_R2.md](WAGGLE_DECISION_PRESENTATION_REVIEW_R2.md)。原 R1 报告保留，不以新结论覆盖其历史 NOT_READY。

### 14.1 准备结论与证据边界

- R2 Run `29992e80-c3dc-44d6-913f-452fc404d633` 已完成、进程终态已观察，无待答 supervisor 请求；不是根据“文件似乎存在”推断完成。
- 审阅对象为 414 行版本，SHA256 `1be07a5c0a407ef92b003be10f150024340c1b42302cc41cba68e57fe3f76ad2`；原始 R2 报告 SHA256 `cf68415df47b38540c51460b3926214dec5ce4e3f1046a06010320c23a5ad875`。
- 结论 `IMPLEMENTATION READINESS: READY`、`OK with notes`，原 1 项 P1 和 2 项 P2 均为 `RESOLVED`（方案层）；本次限定复审未发现新增具体 P1/P2。
- 主助手对照实际 Source/Report 字段、原 envelope 指纹取法、既有 validate_preview、构造点及保护指纹核对，不将 reviewer 的“尚未运行”检查升级为通过。
- 第 1–8 节已审阅核心内容保持不变，其 UTF-8 原文 SHA256 为 `6ee9d93683b2b210f75074e5c6a92e560f184c89f679f7f0f273d3c61f33aeb9`；本节只记录既有设计的写集/符号/拒绝条件，不增加新实施范围。

### 14.2 允许实施的精确文件集合（已由原 decision-12 批准）

| 范围 | 允许文件 | 限制 |
| --- | --- | --- |
| 新纯展示模块 | `decision_presentation.py` | 单项投影、不可变展示值、原事件快照，按 5.1/5.5 执行；无 I/O、意图生成或授权逻辑 |
| 表单和确认展示 | `decision_view.py` | 5.2/5.5–5.7 列明的展示、快照、折叠与焦点符号；既有答案/授权语义不变 |
| 主界面与展示接线 | `app.py` | `refresh_decision_items`、`prepare_decision_sheet`、`submit_decision_preview` 的展示/一致性接线；不修改底层发布、预算、纳管或真实任务数据 |
| 样式 | `style.tcss` | 仅分组、换行、焦点、滚动及窄屏可见性；不做主题重造 |
| 新纯测试 | `test_decision_presentation.py` | 快照绑定、不可变值、旧报告回退、投影和等价性 |
| 现有隔离 UI 测试 | `test_decisions_ui.py`、`test_decision_review_layout.py`、`test_n3_intents.py`、`test_workbench.py` | 仅为当前设计及已变的构造点补齐行为/实际可见性测试，不降低失败断言 |

共 9 个源码/样式/测试文件，其中 2 个拟新增。`WAGGLE_DECISION_PRESENTATION_PLAN.md` 只更新实施状态和证据，不改原审阅结论；审阅报告不回写。

保护范围：既有未提交修改必须保留；`decisions.py`、`decisions.ts`、`journal.py`、`journal.ts`、`model.py`、`store.py`、`runtime.py`、桥接/纳管模块、非本次预览以及金融项目不在实现写集中。不得修改 wire、schema、原 Run/报告/决定/预算或凭据，也不提交/推送。若确需改动集合外文件或授权语义，先停止该动作并收敛计划/取得新授权，不能由测试失败推断扩大范围。

### 14.3 符号与调用责任冻结

- 新模块：`DecisionPresentation / ProviderGroup / PresentedNeed / ReportViewSnapshot`；`present_decision()`、`capture_report_view()`、`report_view_event()`，字段与返回值沿用第 5.1/5.5 节。
- `DecisionSheet`：`__init__ / compose / item_prompt / reply_controls / update_live_status / on_mount / on_button_pressed` 与拟新增 `reveal_reply / on_collapsible_collapsed`。原 `answers()` 的按意图参与判定、formal_available、草稿同源匹配与混合拒绝只复用、不放宽。
- `DecisionReview`：`__init__ / compose / summary`；snapshot 为必需 keyword-only 参数，两处已查明构造点同步更新；确认默认返回修改，提交四种后果不合并。
- `Waggle`：首页只投影原问题/状态/缺口数；prepare 传递已捕获的快照；submit 在既有校验之后、实际存储/发布之前核对同源原文。新检查不能产生额外 intent、Run、发布、重试或预算。
- 同一 cwd 保持一名源码写者；主助手负责批准边界、最终 diff 与验收。只读 reviewer 不能成为第二写者。不是通过 profile、READY 或工具绑定给写者新增权限。

### 14.4 必须能失败的检查与拒绝路径

| 拒绝/失败条件 | 必须结果 | 最小证据 |
| --- | --- | --- |
| 未获得 t-2.1 的明确源码批准 | 不开始任何上述源码/测试修改 | 真实用户决策门和对应授权记录；不把目标/建议当批准 |
| snapshot 非法、缺失、错 Run/事件/seq/specs | 不建立可提交的新确认页面，不补造快照 | 快照纯测试及两处构造点负向测试 |
| 同 ID 的源 envelope 已变，或页面/目标/轮次在异步核对期间变化 | 原输入保留；不存意图、不发布；不得自动刷新快照继续提交 | 准备/提交原文对照与现有 validate_preview 的失败测试 |
| 只展开、收起、缩放、切项或迟到回调 | Answer、输入、source/fingerprint/compiled_text 不因此变化；无新副作用 | 折叠前后精确对照及实际焦点/可见性测试 |
| 除显示之外出现预览、编译文本、wire、历史内容或预算差异 | 验证失败，不接受“仅展示改动”的声明 | 同 Board/答案/Target 对照与保护指纹 |
| 关键条件/否定/UNKNOWN/排除/签收、目标或确认后果不完整可见 | 布局失败，不通过只检查字串或展开原报告规避 | routed 长文本 fixture、真实键盘滚动及 compositor 交集重建 |
| 实际发送全文被截断、隐藏或非预览原文 | 不验收，不从摘要推断全部条件 | `new_run / live_supplement` 的完整权威文本和长继承限制 fixture |
| 测试/静态检查失败或无法运行 | 显式记录失败/缺口，不标记对应验证完成 | 实际命令、退出码、日志/未验证项 |
| 人工可读性反馈尚缺 | t-5.1 仍待核对，不以模型或技术测试替代 | 用户实际核对隔离样例的反馈 |
| 缺少本次真实加载批准，或未提交输入不能无损保留 | 不重开真实工作台；不丢弃、默认保存或发送 | t-6.1 的独立批准及加载前检查 |

此表不取代领域校验；它限定实现和验收必须保持的拒绝结果。功能尚未实现，表内新测试没有运行通过证据。

### 14.5 任务板阶段与当前权限门

阶段 1 `t-1.1` 是准备验收，后续为源码授权 `t-2.1`、单写者实现 `t-3.1/3.2`、技术验证 `t-4.1/4.2`、人工可读性 `t-5.1`、真实加载批准/核对 `t-6.1/6.2`，最终接受由 `t-7.1` 负责。

t-1.1 已按新鲜宿主证据接受；t-2.1 的真实用户批准已收到并由原决策门登记，见第 15 节。READY、任务板编译或准备任务完成仍不授予任何额外权限，真实加载和 D010 等排除项不因恢复控制器而放开。

## 15. t-2.1 用户批准与当时批次检查点（历史）

- 原用户决策门：`decision-12`，归属任务 `t-2.1`；问题明确限定第 14 节的 9 个源码/样式/隔离测试文件及符号，排除真实加载、D010、金融事项、原 Pi、宿主改装与提交推送。
- 实际 `resolve_decision` 日志条目 ID：`68167bb2`；用户答复原文：`批准按第14节冻结范围实施源码和隔离验证，保留所有排除项`。这是用户对原问题的批准，不是模型从“继续/完成目标”推断的许可。
- 用户已显式恢复控制器。只读核查当时任务板 `workflowStatus=active`、Supervisor `status=active`，原等待门已解除；`t-2.1` 仍为当前已领取任务，无其执行引用，不需要新建或重复审阅。
- 批准适用的是第 14 节冻结范围；已审第 1–8 节核心 SHA256 `6ee9d93683b2b210f75074e5c6a92e560f184c89f679f7f0f273d3c61f33aeb9` 不变。本文新增的状态/授权记录不扩大方案和权限。
- 本批只核验并登记授权与保护指纹，不修改业务源码，不创建 writer、不领取 t-3.1、不运行实施测试或操作 D010。实际实现在后续已授权批次中进行。
- 检查点规则：只有授权记录、范围和保护检查通过，才接受 `t-2.1`；完成后报告结果与下一步，正常结束本批，再于后续批次领取 `t-3.1`。不得为了检查点放弃或把未完成任务标为 done。

## 16. t-3.1 纯展示模块批次

- 用户已手动停止自动监督并恢复原任务板，未清空原任务或批准；本批只领取 `t-3.1`，无其外部执行引用，没有重复 writer 或审阅。
- 新增 `decision_presentation.py`、`test_decision_presentation.py`；没有修改其余源码/协议/真实任务。来源接口仍是第 5.1/5.5 节的三个已批准纯接口。
- 实现：三类显式 routing 与 legacy 展示值、frozen 嵌套负责人分组、原序号/gap/owner/source、不猜联系人或新授权；Source + 原 envelope 的 frozen 快照、执行者/轮次/事件/修订绑定、完整原文不重编码，以及每次新解析避免共享 checks/actions 列表。
- TDD 记录：旧报告分类、负责人分组、业务问题、原报告快照、错误身份、模拟来源与无效类型/零修订等最小用例先呈现红，再实现并转绿；未将模块缺失或失败结果报为通过。
- 当前候选验证：新增纯模块 22 个测试通过；连同既有 `test_decisions` 共 62 个测试通过；新两文件 Pyright 0 errors/warnings；语法/空白检查通过。完整 UI、所有 Target 的接线后等价性和真人可读性未验证，也未加载真实工作台。
- 明确验证缺口：Ruff 离线解析器缺完整缓存；发现的临时 Ruff 文件不可执行且 ELF section 表范围超出实际文件长度，是截断下载，未把它当作可用工具。Ruff 检查未完成、不报通过；阶段 `t-4.1` 必须在最终技术验收前补足可用检查或明确阻断，不能用本批纯模块完成替代该门。
- 纯 capture 只验证所提供 envelope 的结构及 metadata 绑定，不独立证明它就是 Board 当前原件；第 5.5 节要求的准备/提交原文一致性检查由后续 `t-3.2` 实现，不能将此 snapshot 当许可或来源签名。
- `t-3.1` 已按实际纯模块/相邻测试、Pyright 和保护记录接受；这不是全部技术或界面验收，Ruff 和后续检查保持未完成。本批不领取 `t-3.2`，报告检查点并正常结束；下一批再接入首页、表单和确认页，原审批、预算及真实加载门均保持。

## 17. t-3.2 三个展示入口批次

- 本批只继续原板已领取的 `t-3.2`，没有外部执行引用、第二名源码写者或重复审阅；自动 Supervisor 仍为用户停止状态，未修复或重启它。源码批准仍是第 15 节的原 `decision-12`，没有扩大排除项。
- 修改 `decision_view.py`、`app.py`、`style.tcss` 和允许的四个隔离 UI/布局测试文件；已接受的两个 t-3.1 文件原字节不变。首页使用原受众/业务问题、真实状态和缺口数量，不复制缺口长段落；事项单显示一次全局自报交接，逐项保留问题、原因、责任、缺口分组、原序号和逐项来源，不把全局动作当成每项执行许可。
- 表单只捕获当前唯一原 envelope；确认页强制 keyword-only `report_view`。准备和提交均在既有 `validate_preview()` 之外比较 snapshot Source/specs/当前事件及准确原字符串。构造缺 snapshot、错 source/specs、假原件，以及同事件仅重新编码都能拒绝；保留输入，不自动换快照、存意图或发布。
- 原控件和 ID 保持挂载，初始未选区收起，同源草稿恢复原值并展开。正常展开/收起不改变 `answers()`、控件值、snapshot 或同条件 preview；外部状态区解释参与与未发送，隐藏输入焦点回 overview，折叠标题自己的焦点保留。已知业务条件/备注超限错误保持可展开核对，不截断输入，也不因 Ctrl+Enter 藏掉错误。
- 40 列 smoke 真实发现过“输入已经聚焦，但祖先折叠标题的稍后滚动把它遮住”。最终以 Textual 原生 `wait_for_refresh()` 排空祖先展开和输入可见性队列，再执行有当前页面/条目/忙碌/只读守卫的布局后聚焦；没有固定等待或循环重试。切项、忙碌、换页、关闭后的迟到回调均有隔离用例。
- 事项单和确认页直接显示原范围、风险/成本、所选方案差异、条件、重要未知、排除和签收自报说明。确认摘要只列本次实际答复与未选数量，不再为纯技术/业务答复填另一种无关占位。`new_run/live_supplement` 的 `preview.compiled_text` 默认展开；与折叠的完整原报告及只读原 envelope 分离，不执行或追随其内容。

### 17.1 实际验证记录

| 实际执行 | 结果与证据 |
| --- | --- |
| 80 列 owner-evidence 单样例先运行，然后 40 列与宽屏 | 最初缺接线测试 RED；实现后 3 个尺寸用例通过，最终纳入完整目标回归；`/tmp/waggle-t3-2-width-smoke.log` |
| `.venv/bin/python -B -m unittest -q test_decision_presentation test_decisions test_decisions_ui test_n3_intents test_decision_review_layout test_workbench`，`PYTHONDONTWRITEBYTECODE=1` | 175 tests，267.407 秒，退出码 0；`/tmp/waggle-t3-2-final-targeted-tests.log` |
| 纳管 successor 反馈核对与决定预检错误保留的两个相邻用例 | 2 tests，退出码 0；`/tmp/waggle-t3-2-adjacent-final.log`。只用原隔离夹具，不是 D010 真实操作 |
| 系统已有 `pyright` 检查本轮 8 个 Python 文件 | 0 errors/warnings/informations，退出码 0；`/tmp/waggle-t3-2-pyright.log`。`uvx --offline pyright` 的缓存解析失败未报通过，后来定位并使用可用既有命令 |
| routed 长条件/否定/UNKNOWN/未签收、整体与所选范围/风险/成本、长目录、完整发送文本和确认按钮 | 80 列代表样例先过，再覆盖 40 列与宽屏、两种发送 Target，以及 insufficient 反例。完整原报告保持折叠；真实键盘滚动采集 compositor 与滚动区/目标交集，重建每行，并核对默认展开发送全文，不只是字段字串或 `render_lines()` |
| 原值与写集核对、Python 语法、`git diff --check` | 保护范围 29 个写集外既有脏文件、两份审阅、已审 1–8 节和 t-3.1 文件不变；`app.py` 只改三处批准的方法，原 answers/formal/request/编译发布等算法未改；HEAD 仍为 `0c66709fddbae9a4f3f03880f01617cd326fe85d`。源码核对见 `/tmp/waggle-t3-2-source-review.log` |

- 早期回归的旧缺口段落/无关占位/单行导航坐标假定已按新组织规则更新，不删核心失败断言；导航鼠标仍用真实点击、现在按 native 行映射定位。工作台一处首帧断言曾抢在空闲归属观测就绪前执行：冻结基线单测通过，没有宣称稳定既有故障；测试现在等待实际观测对应的动作条件，不用固定时间或放宽守卫。上述修订后完整目标回归重新通过。
- Ruff 仍未完成：未找到可用 Ruff 可执行文件，离线缓存不完整；不使用第 16 节所述截断临时文件，不修宿主或隐藏缺口。阶段 `t-4.1` 仍必须补齐或保留明确技术阻断。
- 本批证据只支持源码接入与组件批次接受；不是整个阶段 4、独立最终源码验收、真人可读性或真实加载通过。未读取/修改真实数据库或邮箱、未操作原 Pi/金融事项、未加载真实 Waggle、未发送 D010、未提交/推送/发布。
- `t-3.2` 已以实际证据登记并接受，原板现在 4/10 done，`t-4.1/4.2` 为 ready、均未领取。本批到此检查点正常结束；下一批由主助手领取一个技术验证任务。保持人工可读性与独立真实加载批准门，当前无需用户操作。

## 18. t-4.1 领域、快照与发送等价性批次

- 本批只领取 `t-4.1`，执行引用为空，无第二写者或新审阅；自动 Supervisor 仍停止。原 `decision-12` 批准与排除项不变，未领取 `t-4.2`。
- 新增隔离等价性矩阵：三类受众、legacy、业务/技术双问题、未选项、两个正式答案、非授权倾向、预算补证、旧正式确认共 11 种情形，逐一覆盖 draft/saved/new_run/live_supplement，44 个同参数组合。展示前后完整 `DecisionPreview`、fingerprint、compiled_text、答案、Board 序列化和 Run 均精确一致。预算、负责人提案正式选择、混合正式/反馈、空白发送和原始备注不自动成为许可的拒绝路径仍成立。
- 四种目标均通过真实隔离 Waggle 准备路径与调用前原 `preview_decisions()` 精确对照；返回编辑保留选择及原快照，不保存意图或发布。另在 new_run/live_supplement 的准备及确认后异步预检等待点仅改变 envelope 排版，保持解析事件完全相等；四处都会因准确原件已变拒绝，原输入/显示快照和磁盘仍保留，没有意图或请求。
- 一项新增矩阵最初用未转换的原始备注直接调用发送预览，被原领域守卫正确拒绝；未改源码放宽它。正向样例改用显式 request_info，同时增加原始备注继续拒绝的反例。
- 本批生产文件只整理 `decision_view.py` 的 json 导入位置；所有非 import AST 节点与 t-3.2 一致，`app.py`、纯模块、样式及布局测试准确字节不变。四个批准测试文件补矩阵或修导入/回调捕获等静态问题；不改 codec、schema、预算、发布或纳管协议。

### 18.1 新鲜验证证据

| 执行 | 结果与位置 |
| --- | --- |
| 先单个混合受众 new_run 纯样例、实际准备/晚到来源样例，以及 codec/账本/纳管小样 | 代表用例先通过，再扩展同路径；`/tmp/waggle-t4-1-equivalence-smoke.log`、`/tmp/waggle-t4-1-ui-equivalence-smoke.log`、`/tmp/waggle-t4-1-adjacent-smoke.log` |
| `.venv/bin/python -B -m unittest -q test_decision_presentation test_decisions test_decisions_protocol test_decisions_ui test_n3_intents test_n1_integrity test_n5_ledger test_adoption_ui.AdoptionUI test_session_adoption.SessionAdoptionTests test_dialog_experience`，`PYTHONDONTWRITEBYTECODE=1` | 229 tests，204.893 秒，退出码 0；`/tmp/waggle-t4-1-domain-regression.log`。包含真实 Python/TS codec parity、失败/取消/迟到响应、账本不可重置、纳管 successor 四个守卫及隔离界面；不是操作 D010 或启动原 Pi |
| 工作台两个受影响入口与强制 snapshot 构造拒绝 | 3 tests，退出码 0；`/tmp/waggle-t4-1-adjacent-final.log` |
| 本轮八个 Python 文件 `pyright` | 0 errors/warnings/informations，退出码 0；`/tmp/waggle-t4-1-pyright-final.log` |
| Ruff `check --isolated --no-cache --select I001,E9,F,B023`，同八文件 | 通过，退出码 0；`/tmp/waggle-t4-1-ruff-strict.log`。不使用全项目自动 fix/format 扩大冻结方法写集 |
| 语法、空白、原算法与保护指纹 | 通过；29 个写集外既有脏文件、两份审阅、冻结 1–8 节和 HEAD 不变；`/tmp/waggle-t4-1-protection.log` |

### 18.2 Ruff 工具恢复与检查边界

- 通过既有本地历史证据定位 `/tmp/waggle-ruff-DLphd9/ruff-env/bin/ruff`，实际版本 `0.16.10`。未仅相信旧 PASS：重新执行 version、同配置正向样例、含 F821 的负向样例（必须退出 1）与本模块 smoke，均符合预期；工具 SHA 和命令记录在 `/tmp/waggle-t4-1-ruff-requalification.json`。没有下载、安装、改宿主/项目环境，未使用截断的 0.17.0 文件。
- 首次明确配置检查发现 json/import 排序及一个未用导入，已在批准文件内修正。扩展默认扫描的测试上下文/反射/未用变量/循环回调捕获问题也已修正并重新跑相关回归，不降低断言。
- **未限定规则的默认 Ruff 扫描仍退出 1**：剩 8 个 RUF012，全部是 t-3.2 前后 AST 完全相同的 Textual `BINDINGS` 声明。定点读取父类证明该字段继承 `ClassVar[list[BindingType]]`；不为消除样式提示修改冻结方法外的类属性。默认诊断和对照证据保留在 `/tmp/waggle-t4-1-ruff-raw-default.json`、`/tmp/waggle-t4-1-ruff-framework-baseline.json`。
- 除既定规则全部通过外，默认规则仅排除上述 RUF012 的明确配置 `check --isolated --no-cache --ignore RUF012` 也通过（退出码 0），见 `/tmp/waggle-t4-1-ruff-qualified.log`。这不是无例外默认扫描通过；未创建/改写项目 Ruff 配置。宽泛解析全类 type hints 的一次验证脚本因库中 TYPE_CHECKING 前向引用失败，保留 red 日志，后改为仅检查 BINDINGS 的不求值定点注解；未把失败脚本当 PASS。
- 原“没有可用 Ruff”缺口已补足为新鲜、可复现的限定检查，不掩盖旧声明的注解样式例外。最终源码/布局接受仍由 `t-4.2` 检查，真人可读性与真实加载仍是独立门。

- `t-4.1` 已以实际证据登记接受，原板 5/10 done；`t-4.2` ready、未领取，本批到此检查点正常结束。下一批由主助手继续最终布局/源码验收；没有原 Pi 操作、真实数据/邮箱/预算操作、D010 答复、真实界面加载、提交/推送/发布，当前无需用户操作。

## 19. t-4.2 实际布局、焦点与源码验收批次

- 只领取原板 `t-4.2`，无执行引用、重复写者或新独立 reviewer；自动 Supervisor 保持停止。主助手直接核对最终展示代码、全部构造消费点和当前批准写集，不将本轮核对描述成独立模型审阅。
- 新增单项页的实际绘制验收：supported、insufficient、legacy 三种材料分别覆盖 40/80 列与宽屏，原报告及其他回应保持收起。用原生键盘滚动，采集真实 compositor 中每个目标与滚动区/屏幕交集，并重建全部内容行；整体和所选方案的范围/风险/成本、条件、否定、UNKNOWN、排除、签收自报与折叠外参与提示均可完整取回。
- 最终确认页的两种发送目标继续默认展示完整权威提交文本，长继承否定条件、精确目录和完整确认按钮均由实际绘制验证，不靠展开原报告或 `Static.content`/`render_lines()` 冒充布局通过。新增组合行为覆盖真实键盘展开与选择、收起、返回确认页、切项和缩放；与既有草稿重开、隐藏焦点回 overview、普通标题焦点保留、忙碌/换页/关闭/切项后的迟到回调用例一起通过，答案、编辑值、快照和预览不因此变化。

### 19.1 主助手源码核对

- **Standards**：批准范围内未发现新的具体硬性违反。纯展示值、当前原 envelope 及原领域守卫复用，无新依赖/持久化实体、模型改写或自动执行；按本地单用户、最多六项/每项六个缺口与方案的已有限制判断，不宣称多用户或终端无障碍认证。已有 RUF012 继承 ClassVar 注解样式例外按第 18 节保留，未修改冻结方法外属性或项目配置。
- **Spec**：实际绘制发现一处 P2 文案歧义：已选方案发送页仍出现“未选 0 项，本次不发送”，可能被误读成整单不发送。已在允许的 `DecisionReview.summary()` 内明确改为“未选 0 项；未选项不发送”，给零未选和有未选情况加反例断言；不改答案、fingerprint、compiled_text、意图或发送逻辑。最终版本重新执行完整相关回归及全部绘制重建后通过，没有剩余具体 P1/P2 功能缺陷。
- 全特性相对保留的 pre-UI 脏工作树比较，`app.py` 仍只变三处批准方法，DecisionSheet/Review 仅为批准方法；原 answers/formal/mixing/request/发布算法字节或 AST 保留。本批生产改动只有上述单句澄清，另更新三个允许测试文件；源码保护日志见 `/tmp/waggle-t4-2-source-acceptance.log`。原审阅报告及设计 1–8 节不回写。

### 19.2 最终版本的新鲜证据

| 执行 | 实际结果与位置 |
| --- | --- |
| 80 列单项页小样先过，再扩 40/80/宽屏及三类资料；文案澄清后先复跑 80 列确认页和等价性矩阵 | 代表用例通过，日志 `/tmp/waggle-t4-2-sheet-80-smoke.log`、`/tmp/waggle-t4-2-sheet-matrix.log`、`/tmp/waggle-t4-2-header-fix-smoke.log` |
| Textual Pilot + 原测试 helper 的观察包装，执行 `test_decision_review_layout test_n3_intents test_workbench test_decisions_ui test_decision_presentation test_decisions` | 最终源码 189 tests，334.965 秒，退出码 0；`/tmp/waggle-t4-2-layout-focus-regression.log`。包装只保存原 helper 已验证的绘制数据，不删/换断言 |
| 每行实际绘制重建 | 18 份隔离 JSON：10 份单项页、8 份确认页，40x18/80x24/130x50；`/tmp/waggle-t4-2-compositor/index.json`。每次原报告仍收起、零提交；文案修正前的证据另留 `compositor-before-header-fix`，不冒充最终版本 |
| 八 Python 文件 Pyright、明确 Ruff 检查 | Pyright 0 errors/warnings/informations；I001/E9/F/B023 和仅排除既有 RUF012 的默认配置均退出 0；`/tmp/waggle-t4-2-pyright-final.log`、`/tmp/waggle-t4-2-ruff-final.log`、`/tmp/waggle-t4-2-ruff-qualified.log`。未宣称无例外默认 Ruff 通过 |
| Python 语法、`git diff --check`、方法差异及保护指纹 | 通过；29 个写集外既有脏文件、两份审阅、核心设计和 HEAD `0c66709fddbae9a4f3f03880f01617cd326fe85d` 保留；相同来源/答案/Target 下原编译与批准门不变 |

- 本证据是隔离实例的技术布局/源码接受，不是用户实际阅读、真实加载、发布或对金融报告事实的认可。没有触碰真实数据库/邮箱、原 Pi、D010、预算或宿主；未提交/推送/发布。
- `t-4.2` 已以实际证据登记接受，原板 6/10 done，`t-5.1` ready、未领取；本批到此检查点正常结束。下一批由主助手提供隔离阅读材料，再由用户实际判断内容组织；真人反馈不能由本轮测试或模型结论代替，真实加载仍须后续独立批准，本轮无需用户操作。

## 20. t-5.1 隔离样例与已取得的人工反馈

- 只领取 `t-5.1`，执行引用为空，无重复写者或审阅。提供样例时原板进入 `waiting_for_decision`、用户门 `decision-31`；未取得反馈时保持 implementing、不推进 done。现在用户已登记该门，原板恢复 active，自动 Supervisor 仍停止；本批只接受当前可读性任务，不领取加载批准任务。
- 阅读材料：`/tmp/waggle-t5-1-samples/index.html`，包含 A 待补材料及补证核对、B 明确业务问题与有条件回答、C 方案对照与正式选择核对。六张 PNG 由当前接受版本的 DecisionSheet/DecisionReview、生产 CSS 和 Textual 实际绘制的 SVG 导出；没有模型生成或改写画面。
- 样例全是内存合成 Board/报告，未创建 Store/Runtime/真实接收方；准备/提交回调明确拒绝。绘制前后 Board 精确一致，决定记录为 0、未发布任何请求。文件引用仅为虚构文字，没有打开链接或原件；图上的按钮不可操作，界面发送语义只供阅读，不触发真实动作。
- 同路径小样 owner 先成功，再扩大三个案例。已用现有本机 librsvg/Cairo 离线转换，并实际查看主要 PNG 确认中文及布局可读；六图 PNG signature、尺寸 994x1173 与 SHA 已验证，证据 `/tmp/waggle-decision-t5-1-materials.json`、`verification-all.json`。没有下载/安装或修改宿主/项目依赖。
- 环境限制不隐藏：ImageMagick 两次转换因缺 rsvg-convert 失败，日志保留；原生浏览器工具在启动前拒绝已安装 0.34.0，不支持其版本，未启动浏览器，也未执行升级、doctor 或 Pi reload。最终材料使用既有本地库生成，用户不需要为了阅读这些 PNG 修复该环境。
- 本批未改九个接受的实现/测试文件，其 hash 全部仍匹配 t-4.2；只在临时目录生成材料并在本文记录状态。未触碰真实数据库/邮箱、D010、金融目录/研究/预算或原 Pi，不加载真实 Waggle，不提交/推送/发布。
- 已取得真实人工反馈：用户消息 `252945a5`（2026-10-14T01:53:56.985Z）原文“这个设计可以”，是在上述隔离图片之后针对内容组织的接受，不是从测试、一般“继续”或模型意见推断。证据 `/tmp/waggle-t5-1-user-feedback.json`。无需因此再改源码或重复技术回归。
- 原生门的机械阻断现已解除：真实命令日志 `dfa2dfe1`（2026-10-14T02:45:27.355Z）为 `resolve_decision decision-31`，答复原文“这个设计可以，仅确认可读性，不批准真实加载或执行”。新鲜读取原板 active、decision 已清除，且自动 Supervisor 仍 stopped；没有助手代写用户决定、清空/替换任务板或重启监督。
- 已登记接受证据：`/tmp/waggle-decision-t5-1-acceptance.json`、`/tmp/waggle-t5-1-acceptance-validation.log`，验证真实聊天/原生答复、六图准确 SHA、零记录/派发以及源码 hash 与 t-4.2 一致。没有新修正要求或代码修改，不虚构一次新的技术回归；技术证据仍是第 18–19 节的实际执行。
- `t-5.1` 已 done，原板 7/10 完成，`t-6.1` ready、未领取。本批到检查点正常结束；真实加载必须在之后原独立任务明确取得批准，当前无需用户再操作。
- 已收到的接受仅用于可读性；真实加载仍须 `t-6.1` 的独立决定，不授权 D010、预算或执行。若后续指出组织问题，主助手仍仅在原批准源码范围内修正并重跑受影响验证。

## 21. t-6.1 仅加载真实工作台的独立批准门

- 只领取原 `t-6.1`，执行引用为空，无第二写者、审阅或真实工作台操作；原板 7/10 done，技术和真人可读性证据仍有效，九个接受文件 hash 与 t-4.2 一致。自动 Supervisor 仍 stopped。
- 原用户门为 `decision-36`，归属 `t-6.1`；建门后曾保持 waiting_for_decision、implementing，不借“继续工作”推断批准。现在原生解答日志 `242cb007`（2026-10-14T03:03:42.279Z）实际答复“批准仅按上述边界加载Waggle新界面；先核实输入可无损保留，需保存或丢弃时另问；不操作Pi、不派发D010、不授权研究或预算”。新鲜核对原板已 active、decision 清除，自动 Supervisor 仍 stopped；没有重启监督或改写用户决定。
- 请求范围：先核实真实 Waggle 的未提交输入可无损保留，条件满足后仅重开该工作台以加载本次展示改动并做零派发核对。凡需要保存/丢弃未提交内容或无法证明能保留，必须停下另问；本门不代替这些输入处理决定。
- 明确不含 Pi reload/重连/重启、新 Pi、D010 答复/派发/验收、金融目录/研究/预算、提交/推送/发布。旧加载批准、源码批准、可读性接受和任务目标均不能代替本次用户决定。
- 接受记录 `/tmp/waggle-decision-t6-1-acceptance.json` 与新鲜命令 `/tmp/waggle-t6-1-authorization-validation.log` 已核对实际原生批准、准确排除项及源码 hash 不变，`t-6.1` 已 done。本批仅登记当前授权，不检查/切换/重开真实 Waggle、不读取其真实数据库或邮箱、不操作原 Pi，也不领取 `t-6.2`，到此检查点正常结束。原板现在 8/10 done，`t-6.2` ready；真实加载及输入无损保留检查留到下一批，保存/丢弃门仍不得跳过，当前无需用户再操作。

## 22. t-6.2 输入处置批准与真实界面零派发加载

- 只领取 `t-6.2`，无执行引用或第二写者，`decision-36 / 242cb007` 的加载批准有效，源码 hash 与 t-4.2 一致。没有借批准跳过“保存/丢弃另问”门；任务仍 implementing、原板 8/10 done。
- MCP 未配置可用服务器；使用已安装 Herdr 的只读 CLI（未升级/修改宿主）。先列当前 workspace `wP`，重新确认助手在 `wP:pG`，Waggle 在 `wP:p1`，二者不混淆。目标 cwd 为本仓，前台链为 just run → run.sh → uv → app.py，Python PID `4133540`、进程组 `4133532`；没有给原 Pi pane/进程发任何操作。
- 实际可见窗口为 D010“准备恢复·仅保存”。继续检查点和未完成工作两个可见编辑框为空，新轮目的未选，但交付物及以下字段部分在滚动区外；没有声称所有字段均空或所有编辑内容已完整取回。该窗口存在进程内未提交编辑的可能，无法据部分空白证明无损保留，故在关闭/重开前停下。
- 输入决定之前**没有发送终端按键/文本**，未保存/关闭/重开工作台或丢弃表单；先以 mode=ro/query_only 仅查询 Waggle schema，user_version=12。没有从部分空白推断全部字段为空，保持真实输入门，未访问金融数据库或操作原 Pi。
- 前置证据：`/tmp/waggle-decision-t6-2-preflight.json`、`/tmp/waggle-t6-2-visible-before.txt`、`/tmp/waggle-t6-2-process-before.json`。输入处置门 `decision-41` 当时阻止继续；随后真实原生解答日志 `349ec257`（2026-10-14T03:49:08.397Z）答复“允许放弃当前恢复窗口全部未提交编辑，不保存；继续按decision-36边界加载”，才允许丢弃。它只覆盖当前恢复窗口，未扩大其他执行权限。
- 批准后重新核对同一 pane/cwd/PID 和同一恢复窗口，再以 SQLite 一致只读事务留存完整状态、D010 18 个 Run/事件字串、所有账本/批准/决定及既知请求目录指纹（只写临时 hash 证据，不复制原件）。数据体 2.2 MB，小范围 schema/目标行检查先过再读；未查询金融库或跟随报告路径。
- 仅对 `wP:p1` 发 Escape 放弃当前恢复窗口，不点保存。读取刚发键的即时画面仍是旧帧，随后只读等待重取确认已返回首页，未盲目重复按键；丢弃前后完整持久状态相同。记录搜索“量价关系-下一阶段”、全部项目/状态及 D010 选中位置，没有另一处任务编辑窗口。
- 先用实际运行解释器导入当前 app/纯展示模块及 Textual 8.2.8 做无 Store/Runtime 的 smoke，再以 Ctrl+Q 正常退出 Waggle；核实前台只剩原 shell 后用相同 `just run` 重开同 pane。Waggle Python PID 从 `4133540` 变为 `556237`，shell `14484` 和 pane/cwd 保持；没有给原 Pi 或助手 pane 发操作，也没有新建 Pi。
- 恢复原搜索与 D010 选中位置，首页出现“待补材料 / 缺口 5 项”；仅打开新事项单阅读，实际见一次全局交接、责任分组和原序号/来源入口，按钮为“核对本单0项”，未选择答复、未进入最终发送页、未提交任何任务。
- 新鲜零派发验证：schema 12、revision 2213、166 Runs、34 决定、18 个 D010 Run、既知请求文件 0；丢弃后/重开后/打开新页后与加载前的整个 state 字串 SHA、各桶/Task/Run/原事件/预算/请求 SHA 均**完全相同**。证据 `/tmp/waggle-t6-2-before-load.json`、`after-discard.json`、`after-load.json`、`final-state.json`（同前缀），`/tmp/waggle-t6-2-final-validation.log` 和 `/tmp/waggle-decision-t6-2-acceptance.json`。
- 本批未改源码，九文件 hash 与 t-4.2 一致；没有原 Pi 控制、新 Pi、D010 答复/派发/验收、金融路径访问/研究/预算或提交推送发布。`t-6.2` 已以实际证据接受，原板 9/10 done；`t-7.1` ready、未领取，本批到检查点正常结束。下一批由主助手汇总最终接受，不把真实加载当成金融事实或任务成果验收，当前无需用户操作。

## 23. t-7.1 最终接受与交付摘要

### 23.1 交付范围

- 当前代码已按原 9 文件写集完成确定性内容组织：`decision_presentation.py` 保留受众/原问题/原缺口索引及不可变来源快照；`decision_view.py`、`app.py`、`style.tcss` 接入首页、事项单与最终核对，分开全局交接、单项责任/缺口/回应、原报告与实际发送全文。`test_decision_presentation.py`、`test_decisions_ui.py`、`test_decision_review_layout.py`、`test_n3_intents.py`、`test_workbench.py` 提供隔离回归。
- 源码不猜分类或联系人，不用模型改写，不增加 schema/wire/执行实体；原答案参与、混合拒绝、预算禁用、原始请求编译和发布守卫保留。展开/收起、恢复、切项、返回、缩放和迟到回调只管理显示/焦点，不暗增答案或授权。
- 已加载实例：原 `wP:p1`、同本仓 cwd、Waggle PID `556237`，保留原搜索“量价关系-下一阶段”及 D010 选中，停在新的待补材料事项单；未选择回应或进入最终发送。助手 pane `wP:pG`、原 Pi 均未被重启/输入/控制。

### 23.2 门与证据逐项核齐

| 必需项 | 已核实事实 |
| --- | --- |
| 准备复审与原发现 | R1 原文保留；独立 R2 为 READY_PLAN_ONLY，原 3 项方案发现 RESOLVED。两份报告 SHA 与冻结记录一致，不把 READY 当批准 |
| 源码实施授权 | 原生 `decision-12 / 68167bb2` 批准第 14 节准确范围及排除项 |
| 纯模块/集成 | t-3.1 纯/相邻 62 tests；t-3.2 接线阶段 175 + 2 相邻 tests；原数据及算法保护有实际证据，早期 Ruff 缺口已在阶段 4 用可用工具补足 |
| 领域/等价性/失败 | t-4.1 的 11 场景×4 Target 共 44 个精确 Preview/fingerprint/compiled_text 组合，四实际准备路径与四晚到来源重编码拒绝；229 + 3 相关/相邻 tests。覆盖旧记录、双问题、预算/混合/未选、取消/失败/未知发布不重试及纳管 successor |
| 最终源码/真实绘制 | t-4.2 最终版本 189 tests，18 份 compositor 逐行重建（40/80/宽屏，原报告仍收起），含关键否定/未知/签收/不同方案风险成本/长目录/权威全文/完整确认；一处未选项文案歧义已修复并整组重跑 |
| 真人可读性 | 实际聊天 `252945a5` 接受；原生 `decision-31 / dfa2dfe1` 明确“仅确认可读性，不批准真实加载或执行” |
| 独立加载授权 | 原生 `decision-36 / 242cb007`；无损保留不能证明时确实停下，另获 `decision-41 / 349ec257` 只放弃当前恢复窗口未提交内容、不保存的授权 |
| 真实零派发加载 | 同 pane 正常退出/重开，仅恢复显示搜索和阅读。加载前/丢弃后/加载后/新页/最终只读核对：schema 12、revision 2213、166 Runs、34 决定、18 个 D010 Run、请求文件 0；整个 state/事件/账本/预算/请求指纹准确一致 |
| 工作树与状态保护 | 9 个最终文件 SHA 匹配已接受版本；29 个写集外既有脏文件、原审阅和设计 1–8 节保留；HEAD 仍 `0c66709fddbae9a4f3f03880f01617cd326fe85d`，暂存差异为空；无未结外部执行引用，未改/重启自动 Supervisor |

### 23.3 本批新鲜验证

- 最小最终回归实际运行：`test_decision_presentation test_decisions` 加强制 snapshot、实际新轮准备精确等价与纳管 successor 三个定点用例，共 **68 tests / 3.365 秒 / exit 0**，`/tmp/waggle-final-tests.log`；不是把历史 189/229 次数相加当新测试。
- 八 Python 文件 Pyright 0 errors/warnings/informations；Ruff 0.16.10 `--isolated --no-cache --select I001,E9,F,B023` exit 0；语法、`git diff --check`、原生 4 个决定、保护指纹和实机当前 0 回应均通过。`/tmp/waggle-final-pyright.log`、`/tmp/waggle-final-ruff.log`、`/tmp/waggle-final-evidence-validation.log`。
- 实例当前只读证据 `/tmp/waggle-final-live-state.json`、`/tmp/waggle-final-live-view.txt`、`/tmp/waggle-final-live-process.json`；从加载前以来全部指纹仍相同。没有新真实任务或实际研究运行。

### 23.4 残留限制与本地兼容状态

- 无例外的默认 Ruff 扫描仍有 8 条旧 Textual BINDINGS 的 RUF012 注解样式提示；只声明明确规则/已说明例外配置通过，不把它写成无条件默认全绿。没有为消除此旧提示扩大到批准方法外类属性。
- 先前授权的本地兼容链接仍存在且目标有效：`/home/ygguo/.nvm/versions/node/v25.9.0/lib/node_modules/@earendil-works/pi-coding-agent/node_modules/@earendil-works/chord` → `/home/ygguo/agent-projs/agent-ext/pi-task-supervisor/node_modules/@earendil-works/chord`，目标真实 package **1.1.0**。本批只读验证，未重装/修改/撤除；这是已有兼容补偿，不是新 Pi/子Agent全功能认证，未来依赖更新可能使其失效。状态证据 `/tmp/waggle-final-compatibility-link.json`。
- 用户对内容组织的接受不构成金融事实核验、终端无障碍认证或 D010 执行/验收许可。D010 仍待补材料/活动归属核对；未把原报告“结果/预算 UNKNOWN”改成通过，未操作原 Pi、新 Pi、金融目录/研究/预算。
- 未提交/推送、未打包/发布；交付是已验证并在原工作树加载的本地版本，不宣称发行归档完整。临时详细证据保存在 /tmp，可能被系统清理，关键结果/决定记录另保留于本文及原会话日志。
- 所有目标内要求及独立批准、人工反馈、零派发加载证据已取得；`t-7.1` 已登记最终证据接受，原板 **10/10 done**，无未结执行引用/用户门或活动任务。最终接受索引 `/tmp/waggle-decision-final-acceptance.json`；没有新修改要求或需要用户现在作出的决定，正常结束，不启动新目标。
