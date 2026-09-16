#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""项目机械扫描：一次给出陌生代码库的全局骨架，供备课阶段使用。

设计目标（面向 agent 消费）：
  - 非交互，全部输入走命令行参数
  - 结构化输出（单个 JSON 对象），便于后续用 jq 或 Python 解析
  - 输出体量可控（--top 限制各类列表长度），避免刷爆上下文
  - 幂等只读，不写入被扫描的项目

用法示例：
  python project_scan.py .                       # 扫描当前目录
  python project_scan.py D:/proj --top 30        # 各类列表最多 30 条
  python project_scan.py . --max-depth 3         # 目录树展开到 3 层
  python project_scan.py . --exclude lib,vendor  # 额外排除某些目录
  python project_scan.py . --no-git              # 跳过 git 信息（默认会尝试）

退出码：
  0 成功
  1 参数错误
  2 路径不存在或不是目录
"""

import argparse
import json
import os
import subprocess
import sys
from collections import defaultdict

# 输出可能含中文路径，Windows 默认 cp936 会直接抛异常，统一切到 UTF-8。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

# 各语言源码扩展名 → 语言名。用于语言分布统计与"最大文件"排序。
EXT_LANG = {
    ".py": "Python", ".pyi": "Python",
    ".js": "JavaScript", ".jsx": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript",
    ".java": "Java", ".kt": "Kotlin", ".kts": "Kotlin", ".scala": "Scala",
    ".c": "C", ".h": "C/C++ header", ".cpp": "C++", ".cc": "C++", ".cxx": "C++",
    ".hpp": "C++ header", ".hh": "C++ header", ".hxx": "C++ header", ".ino": "Arduino",
    ".cs": "C#", ".vb": "VB.NET",
    ".go": "Go", ".rs": "Rust", ".swift": "Swift", ".m": "Objective-C/MATLAB",
    ".rb": "Ruby", ".php": "PHP", ".pl": "Perl", ".lua": "Lua",
    ".sh": "Shell", ".bash": "Shell", ".zsh": "Shell", ".ps1": "PowerShell", ".bat": "Batch",
    ".sql": "SQL",
    ".html": "HTML", ".htm": "HTML", ".css": "CSS", ".scss": "CSS", ".sass": "CSS", ".less": "CSS",
    ".vue": "Vue", ".svelte": "Svelte",
    ".dart": "Dart", ".r": "R", ".jl": "Julia", ".hs": "Haskell", ".ex": "Elixir",
    ".asm": "Assembly", ".s": "Assembly", ".v": "Verilog", ".sv": "SystemVerilog", ".vhd": "VHDL",
    ".md": "Markdown", ".rst": "reStructuredText", ".txt": "Text",
    ".json": "JSON", ".yaml": "YAML", ".yml": "YAML", ".toml": "TOML", ".ini": "INI", ".cfg": "INI",
    ".xml": "XML", ".proto": "Protobuf", ".cmake": "CMake",
}

# 目录树中跳过的名字：构建产物、依赖缓存、版本控制内部数据。
# 这些占文件数的大头却不含项目信息，扫进去只会淹没真正的源码，
# 让"语言分布"和"最大文件"两个关键判断失真。
SKIP_DIRS = {
    ".git", ".svn", ".hg", ".bzr", ".idea", ".vscode", ".vs", ".settings",
    "node_modules", "__pycache__", ".venv", "venv", "env", ".env", "virtualenv",
    "dist", "build", "out", "output", "target", "debug", "release",
    ".pio", ".pioenvs", ".piolibdeps", ".platformio",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".cache", ".ipynb_checkpoints",
    "coverage", ".nyc_output", ".next", ".nuxt", ".svelte-kit", ".dart_tool", ".pub-cache",
    "vendor", "vendors", "third_party", "thirdparty", "3rdparty", "external", "extern",
    "obj", "bin",  # 见 .NET 特例处理
    ".terraform", ".serverless", ".parcel-cache", ".gradle", ".stack-work", ".bundle",
    "Pods", "Carthage", "site-packages", "deps", "packages", "cmake-build-debug",
    ".history", ".sass-cache", ".angular",
}

# 目录名里含这些片段的一律跳过：历史快照不是项目当前逻辑，
# 让它们进入统计会给出严重误导的"项目规模"印象。
SKIP_DIR_PATTERNS = ("backup", "备份", "存档", "快照", "_old", "-old")

# 构建 / 依赖 / 工程清单文件 → 说明文字。命中即报告，是"这个项目怎么跑"的最短答案。
MANIFESTS = {
    "package.json": "Node/JS 依赖与脚本入口",
    "pnpm-workspace.yaml": "pnpm 多包工作区",
    "yarn.lock": "yarn 锁文件",
    "pyproject.toml": "Python 项目元数据与构建配置",
    "setup.py": "Python 传统打包脚本",
    "setup.cfg": "Python 打包/工具配置",
    "requirements.txt": "Python 依赖清单",
    "Pipfile": "Python pipenv 依赖",
    "environment.yml": "Conda 环境",
    "Cargo.toml": "Rust 包清单（含 bin/lib 目标）",
    "Cargo.lock": "Rust 锁文件",
    "go.mod": "Go 模块与依赖",
    "go.sum": "Go 依赖校验",
    "pom.xml": "Maven Java 工程",
    "build.gradle": "Gradle 工程",
    "build.gradle.kts": "Gradle(Kotlin DSL) 工程",
    "settings.gradle": "Gradle 多模块设置",
    "CMakeLists.txt": "CMake 构建",
    "Makefile": "Make 构建",
    "makefile": "Make 构建",
    "meson.build": "Meson 构建",
    "configure.ac": "Autotools 构建",
    "platformio.ini": "PlatformIO 嵌入式构建（环境=板子配置）",
    "west.yml": "Zephyr west 清单",
    "Kconfig": "内核/固件配置系统",
    "composer.json": "PHP Composer 依赖",
    "Gemfile": "Ruby Bundler 依赖",
    "mix.exs": "Elixir 工程",
    "pubspec.yaml": "Dart/Flutter 工程",
    "Dockerfile": "容器镜像构建",
    "docker-compose.yml": "多容器编排",
    "docker-compose.yaml": "多容器编排",
    "conanfile.txt": "Conan C/C++ 依赖",
    "conanfile.py": "Conan C/C++ 依赖",
    "vcpkg.json": "vcpkg C/C++ 依赖",
}

# 入口候选：文件名或相对路径 → 重要性说明。
ENTRY_HINTS = [
    ("main.py", "Python 主入口"),
    ("__main__.py", "Python 包可执行入口"),
    ("app.py", "常见应用入口"),
    ("manage.py", "Django 管理入口"),
    ("wsgi.py", "WSGI 服务入口"),
    ("asgi.py", "ASGI 服务入口"),
    ("index.js", "Node 入口"),
    ("index.ts", "Node/TS 入口"),
    ("server.js", "Node 服务入口"),
    ("main.js", "JS 主入口"),
    ("main.ts", "TS 主入口"),
    ("main.c", "C 主入口"),
    ("main.cpp", "C++ 主入口"),
    ("main.cc", "C++ 主入口"),
    ("main.go", "Go 主入口"),
    ("main.rs", "Rust 主入口"),
    ("lib.rs", "Rust 库根"),
    ("Program.cs", "C# 主入口"),
    ("Application.java", "Java 引导类"),
    ("Application.kt", "Kotlin 引导类"),
    ("Main.java", "Java 主入口"),
    ("main.m", "Objective-C 主入口"),
    ("main.ino", "Arduino 主程序"),
    ("main.dart", "Flutter 主入口"),
    ("app/main.py", "FastAPI/Flask 常见结构"),
    ("src/index.js", "JS 源码入口"),
    ("src/index.ts", "TS 源码入口"),
    ("src/main.ts", "TS 源码入口"),
    ("src/App.jsx", "React 根组件"),
    ("src/App.tsx", "React 根组件"),
    ("src/app/page.tsx", "Next.js App Router 首页"),
    ("src/main.c", "嵌入式/系统主入口"),
    ("src/main.cpp", "嵌入式/系统主入口"),
    ("src/main.rs", "Rust 源码主入口"),
]

TEST_DIR_NAMES = {"test", "tests", "spec", "specs", "test_host", "unittest", "__tests__", "testing"}
DOC_DIR_NAMES = {"docs", "doc", "documentation", "wiki", "学习文档", "文档"}


def should_skip_dir(name, is_dotnet, extra_excludes):
    """判断目录是否应跳过。

    bin/obj 只在 .NET 工程里是构建产物；别的语言下这两个名字可能是真实源码目录，
    一刀切跳过会漏掉真正的项目代码。
    """
    low = name.lower()
    if low in extra_excludes or name in extra_excludes:
        return True
    if low in SKIP_DIRS:
        if low in ("bin", "obj") and not is_dotnet:
            return False
        return True
    return any(pat in low for pat in SKIP_DIR_PATTERNS)


def count_lines(path):
    try:
        with open(path, "rb") as fh:
            return fh.read().count(b"\n") + 1
    except OSError:
        return 0


def detect_dotnet(root):
    for dirpath, dirnames, filenames in os.walk(root):
        if any(f.endswith((".csproj", ".sln", ".fsproj", ".vbproj")) for f in filenames):
            return True
        dirnames[:] = [d for d in dirnames if d not in (".git", "node_modules", "packages")]
        if dirpath.count(os.sep) - root.count(os.sep) > 2:
            dirnames[:] = []
    return False


def git_info(root):
    """git 信息尽力而为：不在仓库里、没装 git、超时都静默跳过，绝不因此失败。"""
    def run(args):
        try:
            out = subprocess.run(
                ["git"] + args, cwd=root, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, timeout=15,
            )
            if out.returncode != 0:
                return None
            return out.stdout.decode("utf-8", errors="replace").strip()
        except (OSError, subprocess.SubprocessError):
            return None

    if run(["rev-parse", "--is-inside-work-tree"]) != "true":
        return {"is_repo": False}
    info = {"is_repo": True}
    branch = run(["rev-parse", "--abbrev-ref", "HEAD"])
    if branch:
        info["branch"] = branch
    log = run(["log", "--oneline", "-10", "--no-decorate"])
    if log:
        info["recent_commits"] = log.splitlines()
    head = run(["log", "-1", "--format=%ad", "--date=short"])
    if head:
        info["last_commit_date"] = head
    # 最近改动最频繁的目录往往就是开发热点，对决定"先读哪里"很有用。
    # core.quotePath=false：否则 git 会把非 ASCII 路径输出成 "\351\241..." 转义串，
    # 统计出来的"目录名"就是不可读的乱码。
    hot = run(["-c", "core.quotePath=false", "log", "--name-only", "--pretty=format:", "-200"])
    if hot:
        counter = defaultdict(int)
        for line in hot.splitlines():
            line = line.strip().strip('"')
            if not line:
                continue
            # 只统计带目录的路径：根目录下的单文件（AGENTS.md、platformio.ini 等）
            # 不是目录，混进来会让"热点目录"榜单失去意义。
            if "/" not in line:
                continue
            counter[line.replace("\\", "/").split("/")[0]] += 1
        info["recently_changed_top_level"] = sorted(
            counter.items(), key=lambda kv: -kv[1]
        )[:10]
    return info


def scan(root, top, max_depth, want_git, excludes):
    root = os.path.abspath(root)
    is_dotnet = detect_dotnet(root)

    lang_files = defaultdict(int)
    lang_lines = defaultdict(int)
    manifest_hits = []
    doc_files = []
    test_dirs = set()
    entry_hits = []
    biggest = []
    dir_tree = {}
    top_dir_files = defaultdict(int)
    skipped_top_dirs = []
    total_files = 0
    total_bytes = 0

    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        rel_parts = [] if rel_dir == "." else rel_dir.replace("\\", "/").split("/")
        rel_depth = len(rel_parts)
        rel_key = "." if rel_dir == "." else rel_dir.replace("\\", "/")

        raw_subdirs = list(dirnames)
        dirnames[:] = sorted(
            d for d in dirnames if not should_skip_dir(d, is_dotnet, excludes)
        )
        # 记下顶层被跳过的目录：省得 agent 以为"项目就这么点东西"。
        if rel_depth == 0:
            kept = set(dirnames)
            skipped_top_dirs = sorted(d for d in raw_subdirs if d not in kept)

        # 目录树只记到 max_depth；更深的层不记，避免输出膨胀。
        if rel_depth <= max_depth:
            node = {"subdirs": list(dirnames), "files": len(filenames)}
            if rel_depth == max_depth and dirnames:
                node["note"] = "子目录未展开（深度上限）"
            dir_tree[rel_key] = node

        if rel_dir != ".":
            leaf = rel_parts[-1]
            if leaf.lower() in TEST_DIR_NAMES:
                test_dirs.add(rel_key)
            if leaf.lower() in DOC_DIR_NAMES:
                doc_files.append(rel_key + "/ (目录)")

        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            rel = name if rel_dir == "." else rel_key + "/" + name
            total_files += 1
            top_dir_files[rel_parts[0] if rel_parts else "(项目根)"] += 1

            # 清单与文档检测放在扩展名过滤之前——它们本身就是判断依据。
            if name in MANIFESTS:
                manifest_hits.append({"path": rel, "why": MANIFESTS[name]})
            if name.endswith((".csproj", ".sln", ".fsproj", ".vbproj")):
                manifest_hits.append({"path": rel, "why": ".NET 工程文件"})
            if name.upper().startswith(("README", "AGENTS.MD", "CLAUDE.MD", "CONTRIBUTING",
                                        "ARCHITECTURE", "CHANGELOG", "NOTES", "TODO")):
                doc_files.append(rel)
            # SKILL.md / PROMPT.md 这类是"文档型项目"的事实源，对它们而言等同于入口文件。
            # 只在靠近根目录时才算：代码工程里常嵌套 agent 技能目录（.agents/skills/*/SKILL.md），
            # 那些是附带的工具配置，不是这个项目的入口。
            if name in ("SKILL.md", "PROMPT.md", "GEMINI.md", "MANIFEST.md"):
                if rel_depth <= 1:
                    doc_files.append(rel)
                    entry_hits.append(
                        {"path": rel, "why": "文档型项目的事实源（提示词/技能定义）"}
                    )

            for hint, why in ENTRY_HINTS:
                if rel == hint or name == hint:
                    entry_hits.append({"path": rel, "why": why})
                    break

            ext = os.path.splitext(name)[1].lower()
            lang = EXT_LANG.get(ext)
            if lang is None:
                continue

            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            total_bytes += size
            lang_files[lang] += 1
            lines = count_lines(full)
            lang_lines[lang] += lines
            biggest.append((lines, rel, lang))

    biggest.sort(key=lambda t: -t[0])
    langs = sorted(
        ({"language": l, "files": lang_files[l], "lines": lang_lines[l]} for l in lang_files),
        key=lambda d: -d["lines"],
    )

    # 去重后按路径排序，保证同样输入输出稳定（幂等，便于 diff）。
    seen = set()
    unique_entries = []
    for item in entry_hits:
        if item["path"] in seen:
            continue
        seen.add(item["path"])
        unique_entries.append(item)

    return {
        "root": root,
        "scan_limits": {"max_depth": max_depth, "top": top, "excludes": sorted(excludes)},
        "summary": {
            "total_files": total_files,
            "analyzable_files": sum(lang_files.values()),
            "total_bytes": total_bytes,
            "top_languages": langs[:top],
            "is_dotnet_project": is_dotnet,
        },
        # 文件数大头在哪：能一眼看出"核心源码 / 第三方库 / 备份"各占多少。
        "largest_top_level_dirs": [
            {"dir": d, "files": c}
            for d, c in sorted(top_dir_files.items(), key=lambda kv: -kv[1])[:top]
        ],
        # 被排除的顶层目录。这些目录里的文件没进上面的统计——缺席不等于不存在，
        # 备课时要靠这个字段判断"项目是不是把代码放在 lib/ 或 vendor/ 里了"。
        "skipped_top_level_dirs": skipped_top_dirs,
        "directory_tree": dir_tree,
        "build_and_dependency_manifests": manifest_hits[:top],
        "entry_point_candidates": unique_entries[:top],
        "docs_and_notes": doc_files[:top],
        "test_dirs": sorted(test_dirs)[:top],
        "largest_source_files": [
            {"lines": n, "path": p, "language": l} for n, p, l in biggest[:top]
        ],
        "git": git_info(root) if want_git else {"skipped": True},
    }


def main():
    parser = argparse.ArgumentParser(
        description="扫描一个代码库并输出结构化骨架 JSON，用于学习备课。",
        epilog="示例: python project_scan.py . --top 30 --max-depth 3",
    )
    parser.add_argument("path", nargs="?", default=".", help="项目根目录（默认当前目录）")
    parser.add_argument("--top", type=int, default=20,
                        help="每类列表最多输出多少条（默认 20，设小可省上下文）")
    parser.add_argument("--max-depth", type=int, default=2,
                        help="目录树展开深度（默认 2；浅层结构通常已足够）")
    parser.add_argument("--exclude", default="",
                        help="额外排除的目录名，逗号分隔，例如 lib,vendor,examples")
    parser.add_argument("--no-git", action="store_true", help="跳过 git 信息收集")
    args = parser.parse_args()

    if args.top < 1:
        print("Error: --top 必须大于 0。例如 --top 20", file=sys.stderr)
        return 1
    if args.max_depth < 0:
        print("Error: --max-depth 不能为负。例如 --max-depth 2", file=sys.stderr)
        return 1

    target = os.path.abspath(args.path)
    if not os.path.exists(target):
        print("Error: 路径不存在: %s" % target, file=sys.stderr)
        return 2
    if not os.path.isdir(target):
        print("Error: 不是目录: %s（本脚本只扫描目录）" % target, file=sys.stderr)
        return 2

    excludes = {p.strip() for p in args.exclude.split(",") if p.strip()}

    try:
        data = scan(target, args.top, args.max_depth, not args.no_git, excludes)
    except KeyboardInterrupt:
        print("Error: 扫描被中断", file=sys.stderr)
        return 1

    json.dump(data, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
