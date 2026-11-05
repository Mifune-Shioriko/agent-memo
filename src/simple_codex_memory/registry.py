"""A local JSON directory of project locations, without note contents."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile


def default_registry():
    base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "simple-codex-memory" / "projects.json"


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
    roots = set()
    for entry in data["projects"]:
        if (not isinstance(entry, dict)
                or not isinstance(entry.get("root"), str)
                or not Path(entry["root"]).is_absolute()
                or entry.get("memory_dir") != str(Path(entry["root"]) / ".memory")
                or entry["root"] in roots):
            raise ValueError(f"Invalid registry entry: {path}; existing file preserved")
        roots.add(entry["root"])
    return data["projects"]


def write_registry(path, projects):
    data = {"version": 1, "projects": sorted(projects, key=lambda entry: entry["root"])}
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


def register(path, root):
    with locked(path):
        projects = read_registry(path)
        entry = {"root": str(root), "memory_dir": str(root / ".memory")}
        if entry not in projects:
            projects.append(entry)
            write_registry(path, projects)


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
    return [dict(entry, status=status(entry)) for entry in sorted(projects, key=lambda entry: entry["root"])]
