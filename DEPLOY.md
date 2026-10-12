# Waggle 源码包部署

这是可拷贝的源码快照，不是离线运行镜像。包含最新工作区代码（包括未提交的新模块），不含 Python、Node、Pi、依赖缓存、真实任务库、邮箱或本机 Pi 配置。
首次安装依赖通常需要联网；只解压不会安装软件、连接 Pi 或派发任务。

## 解包与启动

推荐 Linux；macOS 按 POSIX 设计但未完整实测。原生 Windows 桥接尚不支持，建议 WSL/Linux；数据库和邮箱放在支持私有权限的 Linux 文件系统上。
使用支持 UTF-8 的终端，建议至少 80×24，中文字体由终端提供。

```sh
# 压缩包旁的校验文件用于传输后核对
sha256sum -c SHA256SUMS
# macOS 可用 shasum -a 256 -c SHA256SUMS
tar -xzf waggle-portable-<快照编号>.tar.gz
cd waggle
sha256sum -c SHA256SUMS   # 核对包内文件
```

保留整个 waggle 目录；不要只复制 app.py。移动代码后重开工作台即可，Pi 的项目加载入口需另外核对。

### 有 uv

需要 Python >=3.11、uv。`run.sh` 优先用 uv 安装 app.py 声明的依赖并运行：

```sh
./run.sh --demo  # 虚构内存样例，不读取数据库或发现 Pi
./run.sh         # 正式工作台，仍须明确派发任务
```

### 没有 uv

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
./run.sh --demo
./run.sh
# 也可直接：.venv/bin/python app.py
```

没有 uv 时，启动器依次使用包内 .venv/bin/python、PATH 中的 python3，不会偷偷安装依赖。
requirements.txt 在打包时从 app.py 的 PEP 723 声明生成；不要复制其他机器的 .venv。这里只固定源码声明的直接依赖，不是所有传递依赖的离线锁文件。
Python 自带 sqlite3，不需要数据库服务。完整说明见 README.md。

## Pi 与可选组件

界面、任务管理不要求安装 Pi。需要真实执行时，另装并配置 Pi 的模型与凭据。
当前验证过 Pi 0.85.1，该版本要求 Node.js >=22.19.0；pi-ai、typebox 等由 Pi 安装，不需要给 Waggle 额外配置 npm 开发依赖。

```sh
npm install -g @earendil-works/pi-coding-agent@0.85.1
./pi.sh '/项目绝对路径'
# 在该 Pi 中 /waggle connect 并确认
```

`pi.sh` 同样支持 uv / 包内 .venv / python3。它启动普通交互式 Pi，不自动接入或执行任务。
工作台与 Pi 按同一机器、同一用户部署，并使用相同邮箱配置；当前不是远程任务服务。
Herdr 仅用于工作台内“一键新建 Pi”；手动连接不需要 Herdr。pi-context 的压缩续接和 pi-subagents 的后台状态观察是可选能力，不会自动安装。

如果使用项目加载入口，先等旧执行及相关子进程结束，再执行：

```sh
python3 runtime.py install '/项目绝对路径'
# 回 Pi 手动 /reload，然后 /waggle connect 并确认
```

入口记录程序的绝对路径。旧路径的同名入口会拒绝覆盖，须先核对归属后手动处理；不同时加载两份桥接，不为补齐活动信息重派旧任务。

新版工作台要求当前会话的新鲜 **v2 报告通道证明** 后才能派发新请求：不仅检查注册 schema，还实际探测原生或 Fabric 的调用入口。Fabric full-code 使用 `fabric_exec` 内的 `extensions.waggle_report`；缺失、禁用、白名单排除或探测拒绝均阻止新请求，不新增替代 provider、不放宽权限。空会话的原生归属保护回执仅证明路由边界，不代表报告已执行或保存；正常报告仍须经过原有权限与归属检查。旧 v1 报告能力文件不再足以授权新派发，agent.json 及历史报告格式不变。旧连接仍可观察，但在线/空闲或源码已更新不等于实际工具已兼容；首页“检查连接”仅提供步骤，不执行更新。先保存输入并确认旧工作及相关子任务已结束，再核对加载路径、手动更新/重连；不要通过反复补证或重发原任务修复 schema。不迁移数据库，也不自动改变任何既有批准。

## 恢复与诊断的上线边界

恢复草稿是现有有界 requirements 文本，不扩展 Agent v1、Run/mailbox v1 或 report-capabilities v2。失败前真实报告只作自报历史，不能重新打开验收或授权。保存后启动仍检查原会话新鲜、空闲且精确归属；离线、未知或忙碌不能直接重派，也不能自动改选同目录连接。

“检查连接”只读已有能力证明，不触发探测/模型回合，不安装、不索取凭据、不改 trust。工作台重开、扩展 reload 和完整 Pi 进程刷新是不同操作；源码版本不是内存 SDK 版本。实际重启、重连、真实库迁移和任务控制仍需独立授权，隔离测试通过不构成部署授权。

## 数据与校验边界

- 默认数据位于 `~/.local/state/waggle/`（遵循绝对 XDG_STATE_HOME），不在源码包里。新环境最简单的用法是建新库、添加新目录并重新连接 Pi。
- `./run.sh --db '/绝对路径/tasks.sqlite3'` 可指定独立库；WAGGLE_BRIDGE_DIR 只改变邮箱，且两端必须一致。
- 本工具不迁移旧库、绝对路径、会话和运行授权。若要保存历史，先结束旧执行、退出工作台并另外备份，不把旧邮箱当成新环境的活跃连接。
- SOURCE-MANIFEST.json 记录源 HEAD（若有）与实际内容哈希。它是工作区快照，不表示所有内容已提交。
- 打包器只做文件/边界、Python 语法与依赖声明检查，不执行项目或完整测试；清单中的 tests 为 not_run。检查代码质量应另跑 `just test`，需要开发测试工具及对应的可选集成组件；跳过测试不算通过。
- test_*.py / fixture-*.ts 是隔离测试材料，不在正常启动时加载。check_native_launch.py 会创建临时 Herdr pane，只在明确需要原生验证时手动执行。
