#!/usr/bin/env python3
"""Document inventory and caller-reviewed organization; semantic classification is supplied by AI.

All project mutations are sealed, resumable batches. Each individual file is written
atomically, but the batch is deliberately not described as an atomic transaction.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
from urllib.parse import quote, unquote, urlsplit

from ai_memory import is_reparse, project_root, valid_task_id
from memory_common import atomic_write, json_bytes, project_lock, safe_path


CATALOG = ".ai-memory/documents.json"
LAYOUT = ".ai-memory/layout.json"
START = "<!-- ai-memory:documents:start -->"
END = "<!-- ai-memory:documents:end -->"
MD = {".md", ".markdown"}
ATTACHMENTS = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".csv", ".ppt", ".pptx", ".odt", ".ods",
               ".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".txt", ".rst", ".adoc"}
REFERENCE_TEXT = {".py", ".js", ".jsx", ".ts", ".tsx", ".json", ".yaml", ".yml", ".toml", ".html",
                  ".htm", ".css", ".xml", ".ini", ".cfg", ".sh", ".ps1", ".bat", ".cmd", ".go",
                  ".rs", ".java", ".kt", ".c", ".cpp", ".h", ".cs", ".vue", ".svelte"}
EXCLUDED = {".git", ".ai-memory", "node_modules", "vendor", ".venv", "venv", "__pycache__", ".cache",
            ".pytest_cache", ".mypy_cache", "dist", "build", "target", "coverage", ".next", ".nuxt",
            "secrets", ".secrets", ".ssh", ".gnupg", "credentials"}
PROTECTED_NAMES = {"agents.md", "claude.md", "readme.md", "skill.md", "copilot-instructions.md",
                   "task_plan.md", "findings.md", "progress.md", "plan.md", "state.md"}
FRAMEWORKS = {".planning", "openspec", ".beads", ".specify", ".superpowers", ".cursor", ".claude", ".github"}
MAX_TEXT_BYTES = 4 * 1024 * 1024
NOTICE = "确认是调用者声明；脚本不独立验证真人授权。批次逐文件应用，可恢复或回滚，不保证多文件同时原子可见。"


def _stamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _relative(value):
    from memory_admin import relative_path
    return relative_path(value)


def _path(root, relative, *, directory=False):
    path = root / _relative(relative)
    safe_path(root, path, directory=directory)
    return path


def _json(root, relative):
    value = json.loads(_path(root, relative).read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"预期 JSON 对象：{relative}")
    return value


def _identity(root):
    from memory_admin import identity
    return identity(root)


def load_layout(root):
    """Return existing explicit roles, or conservative defaults without writing files."""
    root = project_root(str(root))
    config = _path(root, LAYOUT)
    if config.exists():
        value = _json(root, LAYOUT)
        if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
            raise ValueError("layout schema_version 必须为 1。")
        document_root = _relative(value.get("document_root", "文档"))
        index = _relative(value.get("index", document_root + "/文档索引.md"))
        roles = value.get("roles", {})
        if not isinstance(roles, dict) or any(key not in {"rules", "readme", "status", "architecture"} for key in roles):
            raise ValueError("layout roles 仅接受 rules/readme/status/architecture 路径映射。")
        roles = {key: _relative(path) for key, path in roles.items()}
    else:
        roots = [name for name in ("文档", "docs", "doc", "documentation") if (root / name).is_dir()]
        document_root = roots[0] if roots else "文档"
        index = next((name for name in (document_root + "/文档索引.md", document_root + "/INDEX.md",
                                      document_root + "/index.md") if (root / name).is_file()),
                     document_root + "/文档索引.md")
        roles = {"rules": "AGENTS.md", "readme": "README.md", "status": "DEV.md", "architecture": "ARCHITECTURE.md"}
    _path(root, document_root, directory=True)
    if PurePosixPath(document_root).parts[0].casefold() in EXCLUDED:
        raise ValueError("文档根目录不能指向依赖、缓存或内部状态目录。")
    for path in [index, *roles.values()]:
        _path(root, path)
        if PurePosixPath(path).parts[0].casefold() in EXCLUDED:
            raise ValueError("文档入口不能位于依赖、缓存或内部状态目录。")
    if _reference_key(index) in {_reference_key(path) for path in roles.values()}:
        raise ValueError("文档索引不能同时作为规则、说明、状态或架构入口。")
    return {"schema_version": 1, "document_root": document_root, "index": index, "roles": roles,
            "explicit": config.exists()}


def _sensitive(name):
    lower = name.casefold()
    return (lower == ".env" or lower.startswith(".env.") or lower.endswith((".key", ".pem", ".p12", ".pfx"))
            or lower in {"credentials.json", "secrets.json", "secrets.yaml", "secrets.yml", "id_rsa", "id_ed25519"})


def _walk(root):
    files, skipped, errors = [], [], []

    def visit(folder):
        try:
            children = sorted(folder.iterdir(), key=lambda p: p.name.casefold())
        except OSError as exc:
            errors.append({"path": folder.relative_to(root).as_posix(), "reason": str(exc)})
            return
        for path in children:
            relative = path.relative_to(root).as_posix()
            if is_reparse(path):
                skipped.append({"path": relative, "reason": "符号链接或 junction；未跟随"})
                continue
            try:
                if path.is_dir():
                    if path.name.casefold() in EXCLUDED:
                        skipped.append({"path": relative, "reason": "依赖、缓存、敏感目录或内部状态"})
                    elif (path / ".git").exists() or (path / ".ai-memory/project.json").exists():
                        skipped.append({"path": relative, "reason": "独立子项目边界"})
                    elif relative.casefold().startswith("docs/memory/archive"):
                        skipped.append({"path": relative, "reason": "历史备份只保留，不参加整理"})
                    else:
                        visit(path)
                elif _sensitive(path.name):
                    skipped.append({"path": relative, "reason": "可能包含凭据，未读取"})
                elif path.is_file():
                    files.append(path)
                else:
                    skipped.append({"path": relative, "reason": "非普通文件"})
            except OSError as exc:
                errors.append({"path": relative, "reason": str(exc)})

    visit(root)
    return files, skipped, errors


def _catalog(root):
    path = _path(root, CATALOG)
    if not path.exists():
        return {"schema_version": 1, "documents": []}
    value = _json(root, CATALOG)
    if (type(value.get("schema_version")) is not int or value["schema_version"] != 1
            or not isinstance(value.get("documents"), list)):
        raise ValueError("文档清单格式无效；不会覆盖现有清单。")
    seen = set()
    for entry in value["documents"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("sha256"), str):
            raise ValueError("文档清单条目无效。")
        source = _relative(entry.get("source"))
        if source.casefold() in seen:
            raise ValueError("文档清单包含重复路径。")
        seen.add(source.casefold())
    return value


def _protected(relative, layout, plans=()):
    lower = relative.casefold()
    parts = PurePosixPath(lower).parts
    if lower.startswith("docs/memory/journal/"):
        return "受管理日志保持原路径和字节"
    if PurePosixPath(lower).name in PROTECTED_NAMES:
        return "工具入口、目录规则或活动计划保留原位"
    if parts[0] in FRAMEWORKS or lower.startswith("docs/superpowers/"):
        return "框架拥有的文档保留原位"
    if _reference_key(relative) in {_reference_key(path) for path in
                                   {layout["index"], *layout["roles"].values(), *plans}}:
        return "已登记入口或唯一活动计划保留原位"
    return None


def _plans(root):
    directory = _path(root, ".ai-memory/scopes", directory=True)
    result, errors = set(), []
    if directory.exists():
        for path in sorted(directory.glob("*.json")):
            try:
                value = _json(root, path.relative_to(root).as_posix())
                if value.get("plan"):
                    result.add(_relative(value["plan"]))
            except (OSError, ValueError) as exc:
                errors.append({"path": path.relative_to(root).as_posix(), "reason": str(exc)})
    return result, errors


def _scope_hashes(root):
    directory = _path(root, ".ai-memory/scopes", directory=True)
    if not directory.exists():
        return {}
    return {path.relative_to(root).as_posix(): _digest(_path(root, path.relative_to(root).as_posix()).read_bytes())
            for path in sorted(directory.glob("*.json"))}


def inspect_documents(root):
    root = project_root(str(root))
    if not root.is_dir():
        raise ValueError("盘点的项目目录不存在。")
    layout = load_layout(root)
    prior = {entry["source"]: entry for entry in _catalog(root)["documents"]}
    files, skipped, errors = _walk(root)
    plans, plan_errors = _plans(root)
    errors.extend(plan_errors)
    documents = []
    for path in files:
        if path.suffix.casefold() not in MD | ATTACHMENTS:
            continue
        relative = path.relative_to(root).as_posix()
        item = {"path": relative, "format": "markdown" if path.suffix.casefold() in MD else "attachment",
                "protected": _protected(relative, layout, plans)}
        try:
            digest = hashlib.sha256()
            count = 0
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(65536), b""):
                    digest.update(block)
                    count += len(block)
            item.update(sha256=digest.hexdigest(), bytes=count)
            previous = prior.get(relative)
            item["change"] = "new" if not previous else "unchanged" if previous["sha256"] == item["sha256"] else "changed"
            item["classification"] = previous if previous and item["change"] == "unchanged" else None
            if item["format"] == "markdown":
                if count > MAX_TEXT_BYTES:
                    item["readable"] = False
                    item["reason"] = "超过 4 MiB 文本处理限制；登记哈希，保留原位，请分段人工阅读"
                else:
                    try:
                        content = path.read_bytes().decode("utf-8-sig")
                        item["readable"] = True
                        item["lines"] = len(content.splitlines())
                    except UnicodeError:
                        item["readable"] = False
                        item["reason"] = "不是可读取的 UTF-8 Markdown；保留原位"
            else:
                item["readable"] = None
                item["reason"] = "只登记来源、用途和关联；不移动或提取二进制正文"
        except (OSError, ValueError) as exc:
            errors.append({"path": relative, "reason": str(exc)})
            item.update(readable=False, reason=str(exc))
        documents.append(item)
    found = {item["path"] for item in documents}
    pending = [item["path"] for item in documents if not item.get("classification") or
               not item["classification"].get("classified", False)]
    return {"command": "inspect", "read_only": True, "root": str(root), "layout": layout,
            "documents": documents, "missing_catalog_paths": sorted(set(prior) - found),
            "pending": pending, "unreviewed_count": len(pending),
            "skipped": skipped, "errors": errors, "scan_complete": not errors,
            "scope": "项目内普通文档；不跟随链接，不读取内部状态、凭据、依赖、缓存、历史备份或独立子项目",
            "note": "用途由 AI 阅读内容后提交；文件名、格式、哈希和目录不等于语义理解。"}


def _masked(text):
    """Keep offsets stable while excluding code, HTML, and wiki syntax from rewriting."""
    spans = []
    fence = None
    offset = 0
    for line in text.splitlines(keepends=True):
        # Container prefixes do not turn a quoted/listed code fence into prose.
        fence_line = re.sub(r"^(?: {0,3}>[ \t]?)+", "", line)
        fence_line = re.sub(r"^ {0,3}(?:[-+*]|\d+[.)])[ \t]+", "", fence_line)
        match = re.match(r" {0,3}(`{3,}|~{3,})", fence_line)
        if fence:
            spans.append((offset, offset + len(line), "code"))
            closing = re.fullmatch(r" {0,3}" + re.escape(fence[0]) + "{" + str(len(fence))
                                   + r",}[ \t]*(?:\r?\n)?", fence_line)
            if closing:
                fence = None
        elif match:
            spans.append((offset, offset + len(line), "code"))
            fence = match.group(1)
        elif line.startswith(("    ", "\t")):
            spans.append((offset, offset + len(line), "code"))
        offset += len(line)
    for pattern, kind in ((r"(`+)(?!`)(.*?)\1(?!`)", "code"),
                          (r"<!--.*?-->|</?[A-Za-z][A-Za-z0-9-]*(?:\s(?:[^'\"<>]|\"[^\"]*\"|'[^']*')*)?/?>", "html"),
                          (r"!?\[\[.*?\]\]", "wiki")):
        for match in re.finditer(pattern, text, flags=re.S):
            spans.append((match.start(), match.end(), kind))
    visible = list(text)
    for start, end, _ in spans:
        for index in range(start, end):
            if visible[index] not in "\r\n":
                visible[index] = " "
    return "".join(visible), [(text[a:b], kind) for a, b, kind in spans]


def _destinations(visible):
    """Yield destination spans; support angle URLs, spaces, titles and balanced parentheses."""
    spans = []
    # Track label brackets so image links nested inside an outer link expose both
    # destinations. A pattern ending at the first ']' silently loses the outer URL.
    labels = []
    cursor = 0
    while cursor < len(visible):
        if visible[cursor] == "\\":
            cursor += 2
            continue
        if visible[cursor] == "[":
            labels.append(cursor)
        if visible[cursor] != "]" or not labels:
            cursor += 1
            continue
        labels.pop()
        if visible[cursor + 1:cursor + 2] != "(":
            cursor += 1
            continue
        cursor += 2
        while cursor < len(visible) and visible[cursor].isspace():
            cursor += 1
        start = cursor
        index = start
        if index >= len(visible):
            continue
        if visible[index] == "<":
            end = visible.find(">", index + 1)
            if end != -1 and "\n" not in visible[index:end]:
                spans.append((index + 1, end))
                cursor = end + 1
            continue
        depth = 0
        while index < len(visible) and visible[index] not in "\r\n":
            char = visible[index]
            if char == "\\":
                index += 2
                continue
            if char == "(":
                depth += 1
            elif char == ")":
                if not depth:
                    raw = visible[start:index]
                    title = re.search(r"\s+[\"'(]", raw)
                    end = start + (title.start() if title else len(raw.rstrip()))
                    if end > start:
                        spans.append((start, end))
                    cursor = index + 1
                    break
                depth -= 1
            index += 1
        if cursor == start:
            cursor = max(start + 1, index)
    for match in re.finditer(r"(?m)^ {0,3}\[[^\]\n]+\]:[ \t]*(<[^>\n]+>|[^\r\n]+)", visible):
        start, end = match.span(1)
        raw = match.group(1)
        if raw.startswith("<"):
            spans.append((start + 1, end - 1))
        else:
            title = re.search(r"\s+[\"'(]", raw)
            spans.append((start, start + (title.start() if title else len(raw.rstrip()))))
    return sorted(set(spans))


def _local_target(source, url, *, allow_outside=False):
    if url.startswith(("#", "//", "/")) or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", url):
        return None
    parsed = urlsplit(url)
    if not parsed.path:
        return None
    decoded = re.sub(r"\\([() ])", r"\1", unquote(parsed.path)).replace("\\", "/")
    target = posixpath.normpath(posixpath.join(posixpath.dirname(source), decoded))
    if not allow_outside and (target.startswith("../") or target == ".."):
        return None
    return target, url[len(parsed.path):]


def _reference_key(value):
    # Windows resolves ordinary paths without case distinctions; POSIX does not.
    return os.path.normcase(value).replace("\\", "/")


def _rewrite(text, source, destination, moves):
    visible, _ = _masked(text)
    replacements = []
    move_targets = {_reference_key(path): target for path, target in moves.items()}
    for start, end in _destinations(visible):
        local = _local_target(source, text[start:end])
        if local is None:
            continue
        target, suffix = local
        final_target = move_targets.get(_reference_key(target), target)
        if source == destination and final_target == target:
            continue
        relative = posixpath.relpath(final_target, posixpath.dirname(destination) or ".")
        replacements.append((start, end, quote(relative, safe="/-._~") + suffix))
    for start, end, replacement in reversed(replacements):
        text = text[:start] + replacement + text[end:]
    return text


def _mentions(text, source):
    decoded = _reference_key(unquote(text))
    source = _reference_key(source)
    name = PurePosixPath(source).name
    stem = PurePosixPath(source).stem
    return source in decoded or name in decoded or bool(re.search(r"\[\[(?:[^\]|]*/)?" + re.escape(stem) + r"(?:[|\]#])", decoded))


def _unsafe_outgoing(segment, kind):
    """A moved document must not change the base of an unrewritable local URL."""
    if kind == "wiki":
        return True
    if kind == "html":
        # HTML is retained verbatim. Recognize quoted and unquoted attributes,
        # responsive image lists and inline CSS solely to stop unsafe relocation.
        attributes = re.findall(r"\b([A-Za-z_:][A-Za-z0-9_:.-]*)\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s\"'=<>`]+))",
                                segment, flags=re.I)
        uri_attributes = {"href", "src", "poster", "data", "action", "formaction", "background",
                          "cite", "longdesc", "xlink:href", "manifest", "profile", "codebase", "archive"}
        urls = []
        for name, double, single, bare in attributes:
            name, value = name.casefold(), double or single or bare
            if name in uri_attributes:
                urls.append(value.strip())
            elif name in {"srcset", "imagesrcset"}:
                urls.extend(item.strip().split()[0] for item in value.split(",") if item.strip())
            elif name == "style":
                urls.extend(match.strip().strip("\"'") for match in re.findall(r"url\(\s*([^)]*)\)", value, re.I))
        return any(url and not url.startswith(("#", "/"))
                   and not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", url) for url in urls)
    if kind == "code":
        without_urls = re.sub(r"[A-Za-z][A-Za-z0-9+.-]*://\S+", "", segment)
        return bool(re.search(r"(?:\.{1,2}/|[\w\u4e00-\u9fff-]+[/\\][\w\u4e00-\u9fff-]+|[\w-]+\.(?:md|json|yaml|yml|png|jpg|pdf))", without_urls))
    return False


def _reference_inputs(root, files):
    texts, hashes, errors = {}, {}, []
    for path in files:
        if path.suffix.casefold() not in MD | REFERENCE_TEXT | {".txt", ".rst", ".adoc", ".svg"}:
            continue
        relative = path.relative_to(root).as_posix()
        try:
            data = path.read_bytes()
            hashes[relative] = _digest(data)
            if len(data) > MAX_TEXT_BYTES:
                errors.append({"path": relative, "reason": "引用检查文本超过 4 MiB；保留可能受影响的文档"})
            else:
                texts[relative] = data.decode("utf-8-sig")
        except (OSError, UnicodeError) as exc:
            errors.append({"path": relative, "reason": "无法检查引用：" + str(exc)})
    return texts, hashes, errors


def _managed_index(original, entries, index):
    if original.count(START) != original.count(END) or original.count(START) > 1:
        raise ValueError("索引受管理区域标记不完整或重复；不会修改人工内容。")
    rows = [START, "", "## 文档用途与位置", "", "仅登记用途、来源和位置；当前进度以对应文档为准。", ""]
    for entry in sorted(entries, key=lambda e: (e.get("role", "unknown"), e["source"])):
        source = entry["source"]
        if source == index or entry.get("missing"):
            continue
        relative = quote(posixpath.relpath(source, posixpath.dirname(index) or "."), safe="/-._~")
        name = PurePosixPath(source).name.replace("[", "\\[").replace("]", "\\]")
        purpose = " ".join(str(entry.get("purpose", "用途待阅读确认")).splitlines())
        uncertainty = entry.get("uncertainty")
        suffix = "；待核验：" + " ".join(str(uncertainty).splitlines()) if uncertainty else ""
        rows.append(f"- [{name}]({relative})：{purpose}{suffix}")
    rows.extend(["", END])
    block = "\n".join(rows)
    if START in original:
        start, end = original.index(START), original.index(END) + len(END)
        if start > end:
            raise ValueError("索引受管理区域标记顺序无效。")
        return original[:start] + block + original[end:]
    return original + ("\n\n" if original and not original.endswith("\n\n") else "") + block + "\n"


def _manifest(root, filename, inventory, layout):
    path = Path(filename).expanduser().absolute()
    if is_reparse(path) or not path.is_file():
        raise ValueError("分类清单必须为普通 JSON 文件。")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if (not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"] != 1
            or not isinstance(value.get("documents"), list)):
        raise ValueError("manifest 必须含 schema_version: 1 和 documents 数组。")
    entries = []
    seen = set()
    for item in value["documents"]:
        if not isinstance(item, dict):
            raise ValueError("manifest 条目必须为对象。")
        source = _relative(item.get("source"))
        if source.casefold() in seen or source not in inventory:
            raise ValueError(f"重复或不在本项目可盘点范围内的来源：{source}")
        seen.add(source.casefold())
        for key in ("purpose", "role", "state", "reason"):
            if not isinstance(item.get(key), str) or not item[key].strip():
                raise ValueError(f"{source} 缺少 AI 阅读后提供的 {key}。")
        if item.get("uncertainty") is not None and not isinstance(item["uncertainty"], str):
            raise ValueError("uncertainty 必须为文字或 null。")
        if item.get("sha256") and item["sha256"] != inventory[source].get("sha256"):
            raise ValueError(f"分类依据的文件哈希已变化：{source}")
        target = item.get("target")
        if target is not None:
            target = _relative(target)
        elif item["role"] == "unknown" and inventory[source]["format"] == "markdown":
            target = layout["document_root"] + "/待分类/" + PurePosixPath(source).name
        else:
            target = source
        if inventory[source]["format"] != "markdown" and target != source:
            raise ValueError("非 Markdown 附件仅登记，不支持移动：" + source)
        if PurePosixPath(target).suffix.casefold() != PurePosixPath(source).suffix.casefold():
            raise ValueError("整理不能改变文件格式扩展名。")
        _path(root, target)
        if any(part.casefold() in EXCLUDED for part in PurePosixPath(target).parts):
            raise ValueError("目标不能位于内部状态、依赖、缓存或敏感目录。")
        cursor = (root / target).parent
        while cursor != root:
            if (cursor / ".git").exists() or (cursor / ".ai-memory/project.json").exists():
                raise ValueError("目标跨越独立子项目边界。")
            cursor = cursor.parent
        if target.casefold().startswith(("docs/memory/journal/", "docs/memory/archive/")) and target != source:
            raise ValueError("不能把普通文档移入受管理日志或历史备份。")
        entries.append({key: item.get(key) for key in ("purpose", "role", "state", "reason", "uncertainty")} |
                       {"source": source, "target": target})
    return entries


def _artifact(root, review_id, path):
    return _path(root, f".ai-memory/reviews/{review_id}/{path}")


def propose_organize(root, review_id, manifest_file, reason):
    root = project_root(str(root))
    valid_task_id(review_id)
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("提案原因不能为空。")
    project_id = _identity(root)
    initial_scope_hashes = _scope_hashes(root)
    initial_layout_hash = _current(root, LAYOUT)
    inspection = inspect_documents(root)
    if inspection["errors"]:
        raise ValueError("盘点存在错误；先解决错误再创建整理提案：" + json.dumps(inspection["errors"], ensure_ascii=False))
    layout = inspection["layout"]
    inventory = {item["path"]: item for item in inspection["documents"]}
    entries = _manifest(root, manifest_file, inventory, layout)
    files, _, errors = _walk(root)
    # Match project_root's canonical paths (macOS /var aliases, Windows short
    # names/junctions, and parent segments). _manifest already rejects a linked
    # final file before this normalization.
    manifest_path = Path(manifest_file).expanduser().resolve()
    ignored_inputs = [path.relative_to(root).as_posix() for path in files if path == manifest_path]
    files = [path for path in files if path != manifest_path]
    texts, input_hashes, ref_errors = _reference_inputs(root, files)
    warnings = ref_errors + errors
    moves = {}
    blocked = []
    for item in entries:
        source, target = item["source"], item["target"]
        if source == target:
            continue
        reason_blocked = inventory[source].get("protected")
        if not inventory[source].get("readable"):
            reason_blocked = inventory[source].get("reason", "无法可靠读取 Markdown")
        if ref_errors or errors:
            reason_blocked = "引用扫描不完整，保留原位"
        if not reason_blocked:
            for other, text in texts.items():
                if PurePosixPath(other).suffix.casefold() not in MD:
                    unsafe = [(text, "代码或配置")]
                else:
                    visible, unsafe = _masked(text)
                    if other.casefold().startswith("docs/memory/journal/"):
                        unsafe.append((text, "不可变日志"))
                    for start, end in _destinations(visible):
                        url = text[start:end]
                        local = _local_target(other, url, allow_outside=True)
                        if other == source and local and (local[0].startswith("../") or local[0] == ".."):
                            reason_blocked = f"{other} 含项目外相对路径引用，移动会改变其基准"
                            break
                        if url.startswith("/") and not url.startswith("//"):
                            unsafe.append((url, "站点根路径"))
                    if reason_blocked:
                        break
                for segment, kind in unsafe:
                    if _mentions(segment, source) or (other == source and _unsafe_outgoing(segment, kind)):
                        reason_blocked = f"{other} 含无法可靠改写的 {kind} 引用"
                        break
                if reason_blocked:
                    break
        if reason_blocked:
            blocked.append({"source": source, "requested_target": target, "reason": reason_blocked})
            item["target"] = source
            item["uncertainty"] = "; ".join(x for x in (item.get("uncertainty"), reason_blocked) if x)
        else:
            moves[source] = target
    lowered = {}
    all_paths = {path.relative_to(root).as_posix().casefold(): path for path in files}
    for source, target in moves.items():
        if target.casefold() in lowered or target.casefold() in all_paths or (root / target).exists():
            raise ValueError("目标冲突（包括仅大小写改名）；不会覆盖：" + target)
        if _reference_key(target) in {_reference_key(path) for path in
                                     {layout["index"], *layout["roles"].values()}}:
            raise ValueError("不能覆盖文档入口：" + target)
        lowered[target.casefold()] = source
    # Full path states permit recovery after every individual atomic file operation.
    before, after = {}, {}

    def update(relative, data):
        if relative not in before:
            path = _path(root, relative)
            before[relative] = path.read_bytes() if path.exists() else None
        after[relative] = data

    for relative, text in texts.items():
        if PurePosixPath(relative).suffix.casefold() not in MD or relative.casefold().startswith("docs/memory/journal/"):
            continue
        destination = moves.get(relative, relative)
        rewritten = _rewrite(text, relative, destination, moves)
        raw = _path(root, relative).read_bytes()
        data = (b"\xef\xbb\xbf" if raw.startswith(b"\xef\xbb\xbf") else b"") + rewritten.encode("utf-8")
        if destination != relative:
            update(destination, data)
            update(relative, None)
        elif data != raw:
            update(relative, data)
    prior = {item["source"]: dict(item) for item in _catalog(root)["documents"]}
    for source, info in inventory.items():
        if source not in prior:
            prior[source] = {"source": source, "sha256": info["sha256"], "purpose": "用途待阅读确认", "role": "unknown",
                             "state": "unknown", "reason": "只盘点，尚未由 AI 阅读分类", "uncertainty": "待阅读", "classified": False}
        elif prior[source]["sha256"] != info["sha256"]:
            prior[source]["uncertainty"] = "内容已变化，需要重新阅读分类"
            prior[source]["classified"] = False
    for item in entries:
        source, target = item["source"], item["target"]
        previous = prior.pop(source)
        origins = list(dict.fromkeys(previous.get("original_paths", []) + ([source] if target != source else [])))
        data = after.get(target)
        prior[target] = {key: item[key] for key in ("purpose", "role", "state", "reason", "uncertainty")} | {
            "source": target, "sha256": _digest(data) if data is not None else inventory[source]["sha256"],
            "original_paths": origins, "classified": True}
    for source, entry in prior.items():
        entry["missing"] = source not in inventory and source not in moves.values()
        if source in after and after[source] is not None:
            entry["sha256"] = _digest(after[source])
    index = layout["index"]
    path = _path(root, index)
    index_before = after.get(index, path.read_bytes() if path.exists() else "# 文档索引\n".encode("utf-8"))
    index_text = _managed_index(index_before.decode("utf-8-sig"), list(prior.values()), index)
    index_data = (b"\xef\xbb\xbf" if index_before.startswith(b"\xef\xbb\xbf") else b"") + index_text.encode("utf-8")
    update(index, index_data)
    if index not in prior:
        prior[index] = {"source": index, "purpose": "文档用途与位置的受管理索引", "role": "index", "state": "current",
                        "reason": "本次批量整理生成导航，人工正文在管理区域之外保留", "uncertainty": None,
                        "classified": True, "original_paths": [], "missing": False}
    prior[index]["sha256"] = _digest(index_data)
    catalog = {"schema_version": 1, "project_id": project_id, "review_id": review_id,
               "updated_at": _stamp(), "documents": sorted(prior.values(), key=lambda e: e["source"])}
    update(CATALOG, json_bytes(catalog))
    operations = []
    blobs = {}
    # Publish all destinations/edits before deleting old source files.
    for relative in sorted(after, key=lambda name: (after[name] is None, name)):
        if before[relative] == after[relative]:
            continue
        number = len(operations)
        operation = {"path": relative, "before_sha256": _digest(before[relative]) if before[relative] is not None else None,
                     "after_sha256": _digest(after[relative]) if after[relative] is not None else None}
        for phase, content in (("before", before[relative]), ("after", after[relative])):
            filename = f"{phase}/{number:05d}.bin" if content is not None else None
            operation[phase + "_file"] = filename
            if filename:
                blobs[filename] = content
        operations.append(operation)
    layout_hash = initial_layout_hash
    proposal = {"schema": 1, "kind": "organize", "id": review_id, "project_id": project_id, "created_at": _stamp(),
                "reason": reason.strip(), "layout": layout, "layout_sha256": layout_hash,
                "scope_hashes": initial_scope_hashes,
                "moves": [{"source": source, "target": target} for source, target in sorted(moves.items())],
                "blocked": blocked, "warnings": warnings, "reference_errors": ref_errors, "skipped": inspection["skipped"],
                "input_hashes": input_hashes, "ignored_inputs": ignored_inputs,
                "document_hashes": {name: item["sha256"] for name, item in inventory.items()},
                "operations": operations, "confirmation_notice": NOTICE}
    encoded = json_bytes(proposal)
    proposal_file = _artifact(root, review_id, "proposal.json")
    seal = _path(root, f".ai-memory/review-seals/{review_id}.json")
    with project_lock(root):
        if _identity(root) != project_id:
            raise ValueError("项目标识已变化。")
        if _scope_hashes(root) != initial_scope_hashes or _current(root, LAYOUT) != initial_layout_hash:
            raise ValueError("读取期间布局或任务计划映射已变化。")
        if proposal_file.parent.exists() or seal.exists():
            raise ValueError("提案 ID 已存在；不会覆盖备份或暂存文件。")
        for relative, expected in {**input_hashes, **proposal["document_hashes"]}.items():
            if _digest(_path(root, relative).read_bytes()) != expected:
                raise ValueError("读取期间文档或引用已变化：" + relative)
        for operation in operations:
            _check_state(root, operation["path"], {operation["before_sha256"]})
        for name, content in blobs.items():
            atomic_write(root, _artifact(root, review_id, name), content)
        atomic_write(root, proposal_file, encoded)
        atomic_write(root, seal, json_bytes({"id": review_id, "project_id": project_id, "proposal_sha256": _digest(encoded)}))
    pending = sorted(entry["source"] for entry in prior.values() if not entry.get("classified") and not entry.get("missing"))
    return {"command": "propose-organize", "id": review_id, "proposal": str(proposal_file), "source_modified": False,
            "moves": proposal["moves"], "blocked": blocked, "warnings": warnings, "operation_count": len(operations),
            "pending": pending, "unreviewed_count": len(pending), "scan_complete": inspection["scan_complete"] and not ref_errors,
            "reference_scan_complete": not ref_errors,
            "confirmation_notice": NOTICE}


def _current(root, relative):
    path = _path(root, relative)
    return _digest(path.read_bytes()) if path.exists() else None


def _check_state(root, relative, allowed):
    if _current(root, relative) not in allowed:
        raise ValueError("文件哈希与提案/回执不符；检测到人工改动或目标冲突：" + relative)


def _load_review(root, review_id):
    valid_task_id(review_id)
    encoded = _artifact(root, review_id, "proposal.json").read_bytes()
    proposal = json.loads(encoded.decode("utf-8"))
    seal = _json(root, f".ai-memory/review-seals/{review_id}.json")
    project_id = _identity(root)
    if (type(proposal.get("schema")) is not int or proposal["schema"] != 1 or
            proposal.get("kind") != "organize" or proposal.get("id") != review_id or
            proposal.get("project_id") != project_id or
            seal != {"id": review_id, "project_id": project_id, "proposal_sha256": _digest(encoded)}):
        raise ValueError("项目身份或提案封存哈希已变化。")
    seen = set()
    for index, operation in enumerate(proposal["operations"]):
        relative = _relative(operation["path"])
        if relative.casefold() in seen or (relative.startswith(".ai-memory/") and relative != CATALOG):
            raise ValueError("提案操作路径无效。")
        seen.add(relative.casefold())
        _path(root, relative)
        for phase in ("before", "after"):
            filename, expected = operation[phase + "_file"], operation[phase + "_sha256"]
            if filename is None:
                if expected is not None:
                    raise ValueError("提案文件状态不一致。")
            elif filename != f"{phase}/{index:05d}.bin" or _digest(_artifact(root, review_id, filename).read_bytes()) != expected:
                raise ValueError("提案备份或暂存文件已变化。")
    return proposal, _digest(encoded)


def _receipt(root, review_id, digest):
    path = _artifact(root, review_id, "batch-receipt.json")
    if not path.exists():
        return None
    value = _json(root, path.relative_to(root).as_posix())
    if (value.get("id") != review_id or value.get("proposal_sha256") != digest or
            value.get("project_id") != _identity(root) or value.get("phase") not in {"applying", "applied", "rolling_back", "rolled_back"}
            or not isinstance(value.get("confirmation"), str) or not value["confirmation"].strip()):
        raise ValueError("批次回执与当前提案不一致。")
    return value


def _save_receipt(root, review_id, receipt):
    atomic_write(root, _artifact(root, review_id, "batch-receipt.json"), json_bytes(receipt), replace=True)


def _mutate(root, review_id, operation, phase):
    path = _path(root, operation["path"])
    filename = operation[phase + "_file"]
    if filename is None:
        if path.exists():
            # Only a preflighted, individually hashed file is removed; never a tree.
            path.unlink()
    else:
        atomic_write(root, path, _artifact(root, review_id, filename).read_bytes(), replace=path.exists())


def _preflight_inputs(root, proposal, recovering):
    layout_hash = _current(root, LAYOUT)
    if layout_hash != proposal["layout_sha256"]:
        raise ValueError("文档布局已变化；请重新审阅整理方案。")
    if _scope_hashes(root) != proposal.get("scope_hashes", {}):
        raise ValueError("任务范围或活动计划映射已变化；请重新审阅整理方案。")
    operations = {item["path"]: item for item in proposal["operations"]}
    files, _, scan_errors = _walk(root)
    files = [path for path in files if path.relative_to(root).as_posix() not in proposal.get("ignored_inputs", [])]
    _, reference_hashes, errors = _reference_inputs(root, files)
    if scan_errors:
        raise ValueError("引用扫描发生错误，无法验证提案仍适用。")
    if errors != proposal.get("reference_errors", []):
        raise ValueError("引用扫描错误已变化，无法验证提案仍适用；先解决编码、大小或读取错误。")
    expected_paths = set(proposal["input_hashes"])
    allowed_added = {path for path, item in operations.items() if item["before_sha256"] is None}
    allowed_removed = {path for path, item in operations.items() if item["after_sha256"] is None}
    actual_paths = set(reference_hashes)
    if (actual_paths - expected_paths) - (allowed_added if recovering else set()) or (
            expected_paths - actual_paths) - (allowed_removed if recovering else set()):
        raise ValueError("引用文件集合已变化，可能出现新引用；请重新创建提案。")
    for relative, expected in {**proposal["input_hashes"], **proposal["document_hashes"]}.items():
        allowed = {expected}
        if recovering and relative in operations:
            allowed.add(operations[relative]["after_sha256"])
        _check_state(root, relative, allowed)


def apply_organize(root, review_id, confirmed, confirmation):
    if not confirmed or not isinstance(confirmation, str) or not confirmation.strip():
        raise ValueError("应用需要 --confirmed 和非空 --confirmation，记录已有授权声明。")
    root = project_root(str(root))
    with project_lock(root):
        proposal, digest = _load_review(root, review_id)
        receipt = _receipt(root, review_id, digest)
        if receipt and receipt["phase"] in {"rolling_back", "rolled_back"}:
            raise ValueError("提案已进入回滚；请完成回滚后创建新提案。")
        if receipt and receipt["phase"] == "applied":
            for operation in proposal["operations"]:
                _check_state(root, operation["path"], {operation["after_sha256"]})
            return {"command": "apply-review", "id": review_id, "kind": "organize", "already_applied": True,
                    "audit": str(_artifact(root, review_id, "batch-receipt.json"))}
        _preflight_inputs(root, proposal, bool(receipt))
        for operation in proposal["operations"]:
            allowed = {operation["before_sha256"]}
            if receipt:
                allowed.add(operation["after_sha256"])
            _check_state(root, operation["path"], allowed)
        if receipt is None:
            receipt = {"schema": 1, "id": review_id, "project_id": proposal["project_id"], "proposal_sha256": digest,
                       "phase": "applying", "completed": [], "confirmation": confirmation.strip(),
                       "confirmed_at": _stamp(), "confirmation_type": "caller_declared", "notice": NOTICE}
            _save_receipt(root, review_id, receipt)
        for index, operation in enumerate(proposal["operations"]):
            current = _current(root, operation["path"])
            if current != operation["after_sha256"]:
                _check_state(root, operation["path"], {operation["before_sha256"]})
                _mutate(root, review_id, operation, "after")
            _check_state(root, operation["path"], {operation["after_sha256"]})
            if index not in receipt["completed"]:
                receipt["completed"].append(index)
                _save_receipt(root, review_id, receipt)
        receipt["phase"] = "applied"
        receipt["finished_at"] = _stamp()
        _save_receipt(root, review_id, receipt)
    return {"command": "apply-review", "id": review_id, "kind": "organize", "already_applied": False,
            "moves": proposal["moves"], "blocked": proposal["blocked"],
            "audit": str(_artifact(root, review_id, "batch-receipt.json")), "confirmation_notice": NOTICE}


def rollback_review(root, review_id, confirmed, confirmation):
    if not confirmed or not isinstance(confirmation, str) or not confirmation.strip():
        raise ValueError("回滚需要 --confirmed 和非空 --confirmation。")
    root = project_root(str(root))
    with project_lock(root):
        proposal, digest = _load_review(root, review_id)
        receipt = _receipt(root, review_id, digest)
        if receipt is None:
            raise ValueError("提案尚未应用，无需回滚。")
        if receipt["phase"] == "rolled_back":
            for operation in proposal["operations"]:
                _check_state(root, operation["path"], {operation["before_sha256"]})
            return {"command": "rollback-review", "id": review_id, "already_rolled_back": True}
        _preflight_inputs(root, proposal, recovering=True)
        for operation in proposal["operations"]:
            allowed = {operation["after_sha256"]}
            if receipt["phase"] in {"applying", "rolling_back"}:
                allowed.add(operation["before_sha256"])
            _check_state(root, operation["path"], allowed)
        if receipt["phase"] != "rolling_back":
            receipt.update(phase="rolling_back", rollback_confirmation=confirmation.strip(), rollback_completed=[],
                           rollback_started_at=_stamp())
            _save_receipt(root, review_id, receipt)
        for index in reversed(range(len(proposal["operations"]))):
            operation = proposal["operations"][index]
            current = _current(root, operation["path"])
            if current != operation["before_sha256"]:
                _check_state(root, operation["path"], {operation["after_sha256"]})
                _mutate(root, review_id, operation, "before")
            _check_state(root, operation["path"], {operation["before_sha256"]})
            if index not in receipt["rollback_completed"]:
                receipt["rollback_completed"].append(index)
                _save_receipt(root, review_id, receipt)
        receipt.update(phase="rolled_back", rollback_finished_at=_stamp())
        _save_receipt(root, review_id, receipt)
    return {"command": "rollback-review", "id": review_id, "already_rolled_back": False,
            "audit": str(_artifact(root, review_id, "batch-receipt.json")), "confirmation_notice": NOTICE}


def check_documents(root):
    """Read-only layout/catalog/link checks; semantic claims remain the caller's work."""
    root = project_root(str(root))
    errors, warnings, checked = [], [], []
    try:
        inspection = inspect_documents(root)
        layout = inspection["layout"]
        errors.extend(inspection["errors"])
        catalog = _catalog(root)
        if catalog.get("project_id") and catalog["project_id"] != _identity(root):
            errors.append({"path": CATALOG, "reason": "文档清单属于其他项目。"})
        for relative in inspection["missing_catalog_paths"]:
            warnings.append({"path": relative, "reason": "已登记文档缺失；需核验移动、删除或归档情况。"})
        required = {layout["index"]: "index", **{path: role for role, path in layout["roles"].items()}}
        for relative, role in required.items():
            path = _path(root, relative)
            if not path.exists():
                item = {"path": relative, "reason": "缺少文档职责入口：" + role}
                (errors if layout["explicit"] or role == "index" else warnings).append(item)
        # Budgets follow the configured responsibility, not an English filename.
        # AGENTS.md remains a host entrypoint even when another rules path is mapped.
        budget_targets = {}
        for role in ("rules", "status"):
            if layout["roles"].get(role):
                budget_targets.setdefault(layout["roles"][role], set()).add(role)
        if os.path.lexists(root / "AGENTS.md"):
            budget_targets.setdefault("AGENTS.md", set()).add("rules")
        for relative, roles in budget_targets.items():
            try:
                path = _path(root, relative)
                if not path.is_file():
                    continue
                data = path.read_bytes()
                lines = len(data.decode("utf-8-sig").splitlines())
                if ("rules" in roles and len(data) > 8192) or lines > 100:
                    limit = "8 KiB / 100 行" if "rules" in roles else "100 行"
                    warnings.append({"path": relative, "code": "entry_budget", "roles": sorted(roles),
                                     "bytes": len(data), "lines": lines,
                                     "reason": f"超过 {limit} 入口预算，需经确认后拆分或精简；不能静默截断原文。"})
            except (OSError, ValueError) as exc:
                errors.append({"path": relative, "reason": str(exc)})
        for item in inspection["documents"]:
            relative = item["path"]
            if item["format"] != "markdown" or not item.get("readable"):
                continue
            if item["change"] == "changed":
                warnings.append({"path": relative, "reason": "内容与分类依据哈希不同；需要重新阅读用途。"})
            text = _path(root, relative).read_text(encoding="utf-8-sig")
            visible, _ = _masked(text)
            checked.append(relative)
            for start, end in _destinations(visible):
                url = text[start:end]
                target = _local_target(relative, url)
                if target is None:
                    continue
                if target[0] == ".":
                    continue
                try:
                    path = root / target[0]
                    _path(root, target[0], directory=path.is_dir())
                    if not path.exists():
                        errors.append({"path": relative, "link": url, "reason": "本地链接目标不存在。"})
                except (ValueError, OSError) as exc:
                    errors.append({"path": relative, "link": url, "reason": str(exc)})
        index_path = _path(root, layout["index"])
        if index_path.exists():
            text = index_path.read_text(encoding="utf-8-sig")
            if text.count(START) != text.count(END) or text.count(START) > 1 or (
                    START in text and text.index(START) > text.index(END)):
                errors.append({"path": layout["index"], "reason": "索引受管理区域标记不完整、重复或顺序无效。"})
            elif _path(root, CATALOG).exists() and START not in text:
                warnings.append({"path": layout["index"], "reason": "文档清单存在但索引缺少受管理区域；请提出整理方案。"})
        return {"command": "check", "root": str(root), "read_only": True, "layout": layout,
                "document_checks_passed": not errors, "checked_files": checked, "errors": errors, "warnings": warnings,
                "pending": inspection["pending"], "unreviewed_count": inspection["unreviewed_count"],
                "scan_complete": inspection["scan_complete"], "business_validation": "not_performed",
                "scope": "实际文档布局、登记状态和本地链接；不验证内容理解、锚点、远程链接或业务事实。"}
    except (OSError, ValueError) as exc:
        return {"command": "check", "root": str(root), "read_only": True, "document_checks_passed": False,
                "checked_files": checked, "errors": errors + [{"path": LAYOUT, "reason": str(exc)}],
                "warnings": warnings, "business_validation": "not_performed"}
