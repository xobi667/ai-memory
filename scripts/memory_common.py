"""Shared local file boundaries and the single project writer lock."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import stat
import time
import uuid

MAX_SCOPE_BYTES = 32 * 1024


def safe_path(root: Path, path: Path, directory: bool = False) -> Path:
    root, path = Path(root), Path(path)
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"路径越出项目根目录：{path}") from exc
    canonical = root.resolve()
    cursor = root
    for part in relative.parts:
        cursor /= part
        try:
            info = cursor.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError(f"不接受符号链接或 junction：{cursor}")
        if not cursor.resolve().is_relative_to(canonical):
            raise ValueError(f"解析后的路径越出项目根目录：{cursor}")
        if cursor != path and not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"父路径不是目录：{cursor}")
    if not path.resolve().is_relative_to(canonical):
        raise ValueError(f"解析后的路径越出项目根目录：{path}")
    if path.exists() and (not path.is_dir() if directory else not path.is_file()):
        raise ValueError(f"路径类型不正确：{path}")
    return path


def json_bytes(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def read_json(root: Path, path: Path) -> dict:
    value = json.loads(safe_path(root, path).read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 根节点必须是对象：{path}")
    return value


def sync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write(root: Path, path: Path, data: bytes, replace: bool = False) -> None:
    safe_path(root, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    safe_path(root, path)
    temporary = path.parent / (".tmp-" + uuid.uuid4().hex)
    with temporary.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    safe_path(root, path)
    if replace:
        os.replace(temporary, path)
    else:
        os.link(temporary, path)
        temporary.unlink()
    sync_directory(path.parent)


@contextmanager
def project_lock(root: Path, timeout: float = 10.0):
    """Coordinate every current writer; never break a legacy administrative lock."""
    root = Path(root)
    if not root.is_dir():
        raise ValueError("项目根目录必须已存在。")
    directory = safe_path(root, root / ".ai-memory", directory=True)
    legacy = safe_path(root, directory / "admin.lock")
    if legacy.exists():
        raise ValueError("存在旧版整理锁；核实持有进程已结束后再人工修复，不自动破锁。")
    directory.mkdir(exist_ok=True)
    lock_path = safe_path(root, directory / "store.lock")
    stream = lock_path.open("a+b")
    acquired = False
    try:
        if stream.seek(0, os.SEEK_END) == 0:
            stream.write(b"0")
            stream.flush()
        deadline = time.monotonic() + timeout
        while not acquired:
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except (OSError, BlockingIOError):
                if time.monotonic() >= deadline:
                    raise ValueError("项目记忆正在被其他进程写入，请稍后重试。")
                time.sleep(0.05)
        if legacy.exists():
            raise ValueError("发现旧版整理锁，请先结束旧版写入进程。")
        yield
    finally:
        if acquired:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()
