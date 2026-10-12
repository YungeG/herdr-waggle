#!/usr/bin/env python3
"""Create a local working-tree source bundle. Never tag, push, install, or launch Pi."""
import argparse
import ast
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory
import tomllib
from uuid import uuid4

ROOT = Path(__file__).resolve().parent
PRIVATE_DIRS = {"node_modules", "__pycache__", "dist", "venv", "env", "bridges", "launches", "sessions", "previews"}
REQUIRED = {"app.py", "runtime.py", "pi-bridge.ts", "style.tcss", "run.sh", "pi.sh", "DEPLOY.md", "release.py", "release-files.txt"}
GENERATED = {"requirements.txt", "SOURCE-MANIFEST.json", "SHA256SUMS"}
MAX_FILE = 2 * 1024 * 1024
MAX_TOTAL = 16 * 1024 * 1024


class ReleaseError(ValueError):
    """A missing, unsafe, inconsistent, or oversized source snapshot."""


def git(source: Path, *args: str) -> bytes:
    try:
        return subprocess.run(["git", "-C", str(source), *args], check=True,
                              capture_output=True, timeout=15).stdout
    except (OSError, subprocess.SubprocessError) as error:
        raise ReleaseError("需要可读的 Git 工作区和 git 命令；不会自动初始化仓库。") from error


def read_source(source: Path, name: str) -> bytes:
    file = source / name
    if file.resolve() != file.absolute() or not stat.S_ISREG(file.lstat().st_mode):
        raise ReleaseError(f"拒绝链接或非普通文件：{name}")
    descriptor = os.open(file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ReleaseError(f"源码不是普通文件：{name}")
        data = stream.read(MAX_FILE + 1)
    if len(data) > MAX_FILE:
        raise ReleaseError(f"文件超过 2 MiB 上限：{name}")
    return data


def allowed(name: str) -> bool:
    path = PurePosixPath(name)
    return (not path.is_absolute() and ".." not in path.parts and "\\" not in name
            and all(char.isprintable() for char in name)
            and all(part not in PRIVATE_DIRS and (not part.startswith(".") or name == ".gitignore") for part in path.parts)
            and not re.search(r"\.(?:sqlite3?|db|log|pem|key)(?:[-.]|$)", path.name, re.IGNORECASE))


def source_files(source: Path) -> list[str]:
    """Select names through Git's ignore rules, then the public release allowlist."""
    source = source.resolve()
    top = Path(os.fsdecode(git(source, "rev-parse", "--show-toplevel")).strip()).resolve()
    if top != source:
        raise ReleaseError("请在 Waggle 独立仓库中打包，不从父仓库收集文件。")
    patterns = [line.strip() for line in read_source(source, "release-files.txt").decode().splitlines()
                if line.strip() and not line.lstrip().startswith("#")]
    for pattern in patterns:
        if PurePosixPath(pattern).is_absolute() or ".." in PurePosixPath(pattern).parts or "\\" in pattern:
            raise ReleaseError(f"打包规则必须是相对路径：{pattern}")
    candidates = {os.fsdecode(name) for name in git(source, "ls-files", "--cached", "--others", "--exclude-standard", "-z").split(b"\0") if name}
    ignored = {os.fsdecode(name) for name in git(source, "ls-files", "--cached", "--ignored", "--exclude-standard", "-z").split(b"\0") if name}
    candidates -= ignored
    selected = []
    for name in sorted(candidates):
        matches = any(PurePosixPath(name).match(pattern) if any(char in pattern for char in "*?[") else name == pattern for pattern in patterns)
        if allowed(name) and name not in GENERATED and matches:
            try:
                (source / name).lstat()
            except FileNotFoundError:
                continue  # A deleted tracked source is not copied from HEAD.
            selected.append(name)
    if len(selected) > 1000:
        raise ReleaseError("源码文件超过 1000 个，请缩小 release-files.txt 范围。")
    return selected


def dependencies(app: bytes) -> bytes:
    match = re.search(r"(?ms)^# /// script\n(.*?)^# ///\s*$", app.decode().replace("\r\n", "\n"))
    if not match:
        raise ReleaseError("app.py 缺少 PEP 723 依赖声明。")
    lines = match.group(1).splitlines()
    if any(not line.startswith("#") for line in lines):
        raise ReleaseError("app.py 依赖声明必须是注释。")
    metadata = tomllib.loads("\n".join(line[1:].removeprefix(" ") for line in lines))
    requirements = metadata.get("dependencies")
    if not isinstance(requirements, list) or not requirements or any(
        not isinstance(item, str) or not item.strip() or item.startswith("-") or "\n" in item or "\r" in item
        for item in requirements
    ):
        raise ReleaseError("app.py dependencies 必须是非空、单行依赖列表。")
    return ("\n".join(requirements) + "\n").encode()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_package(source: Path, output: Path) -> Path:
    source, output = source.resolve(), output.absolute()
    selected = source_files(source)
    missing = REQUIRED - set(selected)
    if missing:
        raise ReleaseError("缺少必要发布文件：" + ", ".join(sorted(missing)))
    # ponytail: buffer this small source bundle; stream if the explicit 16 MiB ceiling becomes insufficient.
    payload: dict[str, bytes] = {}
    total = 0
    for name in selected:
        data = read_source(source, name)
        total += len(data)
        if total > MAX_TOTAL:
            raise ReleaseError("源码总大小超过 16 MiB；不会生成截断包。")
        if name.endswith(".py"):
            try:
                ast.parse(data, filename=name)
            except (SyntaxError, ValueError) as error:
                raise ReleaseError(f"Python 语法检查失败：{name}: {error}") from error
        payload[name] = data
    payload["requirements.txt"] = dependencies(payload["app.py"])
    now = datetime.now(timezone.utc)
    label = f"waggle-portable-{now:%Y%m%dT%H%M%SZ}-{uuid4().hex[:12]}"
    try:
        head = git(source, "rev-parse", "--verify", "HEAD").decode().strip()
    except ReleaseError:
        head = None  # A new repository can have source files before its first commit.
    manifest = {"format": 1, "package": label, "created_at": now.isoformat(), "base_git_head": head,
                "working_tree_snapshot": True, "tests": "not_run", "source_files": selected,
                "files": {name: sha(data) for name, data in sorted(payload.items())}}
    payload["SOURCE-MANIFEST.json"] = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()
    payload["SHA256SUMS"] = "".join(f"{sha(data)}  {name}\n" for name, data in sorted(payload.items())).encode()
    if source_files(source) != selected or any(read_source(source, name) != payload[name] for name in selected):
        raise ReleaseError("打包期间源码发生变化；请待修改完成后重试。")
    output.mkdir(parents=True, exist_ok=True)
    destination = output / label
    if destination.exists():
        raise ReleaseError("输出已存在；不会覆盖。")
    with TemporaryDirectory(prefix=".waggle-build-", dir=output) as temporary:
        directory = Path(temporary)
        archive = directory / (label + ".tar.gz")
        with tarfile.open(archive, "w:gz", format=tarfile.PAX_FORMAT) as bundle:
            for name, data in sorted(payload.items()):
                info = tarfile.TarInfo("waggle/" + name)
                info.size, info.mtime = len(data), int(now.timestamp())
                info.mode = 0o755 if name in {"run.sh", "pi.sh"} else 0o644
                bundle.addfile(info, io.BytesIO(data))
        archive.chmod(0o600)
        (directory / "SHA256SUMS").write_text(f"{sha(archive.read_bytes())}  {archive.name}\n")
        directory.rename(destination)  # Publish the completed pair on the same filesystem.
    return destination / archive.name


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Waggle 本地源码打包；不提交、不打 tag、不推送、不安装或启动 Pi。")
    parser.add_argument("--output", type=Path, default=ROOT / "dist", help="输出目录，默认源码目录下 dist/")
    parser.add_argument("--list", action="store_true", help="只列出将收集的源文件，不生成包")
    args = parser.parse_args(argv)
    try:
        if args.list:
            print("\n".join(source_files(ROOT)))
        else:
            archive = build_package(ROOT, args.output)
            print(f"已打包：{archive}\n校验文件：{archive.parent / 'SHA256SUMS'}\n未自动运行测试；包内不含运行时或真实任务数据。")
    except (OSError, ValueError) as error:
        print(f"打包失败：{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
