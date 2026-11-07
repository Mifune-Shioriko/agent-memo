"""A local JSON directory of project locations, without note contents."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import tempfile


def default_registry():
    base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    new_path = base / "agent-memo" / "projects.json"
    old_path = base / "simple-codex-memory" / "projects.json"
    if not new_path.exists() and old_path.exists():
        try:
            new_path.parent.mkdir(parents=True, exist_ok=True)
            new_path.write_text(old_path.read_text(encoding="utf-8"), encoding="utf-8")
        except Exception:
            return old_path
    return new_path



@contextmanager
def locked(path):
    """Serialize read-modify-write operations; OS releases the lock after a crash."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_name(path.name + ".lock")
    if path.is_symlink() or lock.is_symlink():
        raise ValueError("Registry and lock must not be symlinks")
    with lock.open("a+b") as stream:
        if os.name == "nt":
            import msvcrt
            stream.seek(0, 2)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def read_registry(path):
    if path.is_symlink():
        raise ValueError("Registry must not be a symlink")
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("projects"), list):
        raise ValueError(f"Invalid registry format: {path}; existing file preserved")
    memory_dirs = set()
    for entry in data["projects"]:
        if (not isinstance(entry, dict)
                or not isinstance(entry.get("root"), str)
                or not Path(entry["root"]).is_absolute()
                or not isinstance(entry.get("memory_dir"), str)
                or not Path(entry["memory_dir"]).is_absolute()):
            raise ValueError(f"Invalid registry entry: {path}; existing file preserved")
        profile = entry.get("profile")
        if profile is not None:
            if not isinstance(profile, str) or not re.match(r"^[a-zA-Z0-9_\-]+$", profile):
                raise ValueError(f"Invalid registry entry: {path}; existing file preserved")
            expected_dir = str(Path(entry["root"]) / f".memory.{profile}")
        else:
            expected_dir = str(Path(entry["root"]) / ".memory")
        if entry["memory_dir"] != expected_dir or entry["memory_dir"] in memory_dirs:
            raise ValueError(f"Invalid registry entry: {path}; existing file preserved")
        memory_dirs.add(entry["memory_dir"])
    return data["projects"]


def write_registry(path, projects):
    data = {"version": 1, "projects": sorted(projects, key=lambda entry: (entry["root"], entry.get("profile") or ""))}
    # Replace only after a complete write, on the same filesystem.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def register(path, root, profile=None):
    with locked(path):
        projects = read_registry(path)
        mem_dir = str(root / (f".memory.{profile}" if profile else ".memory"))
        entry = {"root": str(root), "memory_dir": mem_dir}
        if profile:
            entry["profile"] = profile
        if entry not in projects:
            projects = [p for p in projects if p.get("memory_dir") != mem_dir]
            projects.append(entry)
            write_registry(path, projects)


def unregister(path, memory_dir_path):
    with locked(path):
        projects = read_registry(path)
        target = str(memory_dir_path)
        kept = [p for p in projects if p.get("memory_dir") != target]
        if len(kept) != len(projects):
            write_registry(path, kept)


def list_projects(path, prune=False):
    def status(entry):
        directory = Path(entry["memory_dir"])
        return "available" if directory.is_dir() and not directory.is_symlink() else "missing"

    if prune:
        with locked(path):
            projects = read_registry(path)
            kept = [entry for entry in projects if status(entry) == "available"]
            if kept != projects:
                write_registry(path, kept)
            projects = kept
    else:
        projects = read_registry(path)
    return [dict(entry, status=status(entry)) for entry in sorted(projects, key=lambda entry: (entry["root"], entry.get("profile") or ""))]

