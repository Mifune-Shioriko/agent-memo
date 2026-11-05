"""Markdown is authoritative; SQLite is an on-demand, disposable FTS5 cache."""

import argparse
import hashlib
import json
from importlib.resources import files
import re
from pathlib import Path
import sqlite3
import sys

from .registry import default_registry, list_projects, register

TEMPLATES = {
    "summary.md": "# Project memory\n\n## Goal\n\n## Current state\n\n## Blockers\n\n## Next steps\n",
    "decisions.md": "# Decisions\n\n<!-- Dated decisions, reasons, and evidence. -->\n",
    "lessons.md": "# Lessons\n\n<!-- Verified problems, fixes, and evidence. -->\n",
}


def memory_dir(root):
    directory = root / ".memory"
    if directory.is_symlink():
        raise ValueError(".memory must be a real directory, not a symlink")
    return directory


def notes(directory):
    """Do not follow links to files outside the project's memory."""
    for path in sorted(directory.rglob("*.md")):
        relative = path.relative_to(directory)
        if any((directory / Path(*relative.parts[:i])).is_symlink()
               for i in range(1, len(relative.parts) + 1)):
            continue
        if path.is_file():
            yield path, relative.as_posix(), path.read_text(encoding="utf-8")


def sections(content):
    lines = content.splitlines()
    start, title, fenced = 0, "", None
    for i, line in enumerate(lines):
        fence = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if fence:
            marker = fence[1]
            if fenced is None:
                fenced = marker
            elif marker[0] == fenced[0] and len(marker) >= len(fenced):
                fenced = None
        if fenced is None and re.match(r"^#{1,6}\s+", line):
            if i > start:
                yield start + 1, title, "\n".join(lines[start:i])
            start, title = i, line.lstrip("# ")
    if lines:
        yield start + 1, title, "\n".join(lines[start:])


def connect(directory, rebuild=False):
    if not directory.is_dir():
        raise ValueError("No project memory. Run memory init first.")
    db_path = directory / "index.sqlite"
    for suffix in ("", "-wal", "-shm", "-journal"):
        if Path(str(db_path) + suffix).is_symlink():
            raise ValueError("Index paths must not be symlinks")
    db = sqlite3.connect(db_path, timeout=10)
    try:
        with db:
            if rebuild:
                db.execute("DROP TABLE IF EXISTS chunks")
                db.execute("DROP TABLE IF EXISTS sources")
            db.execute("CREATE TABLE IF NOT EXISTS sources(path TEXT PRIMARY KEY, digest TEXT)")
            db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(path UNINDEXED, line UNINDEXED, title, body)")
            current = set()
            for _, path, content in notes(directory):
                current.add(path)
                digest = hashlib.sha256(content.encode()).hexdigest()
                if db.execute("SELECT digest FROM sources WHERE path=?", (path,)).fetchone() == (digest,):
                    continue
                db.execute("DELETE FROM chunks WHERE path=?", (path,))
                db.executemany("INSERT INTO chunks(path,line,title,body) VALUES (?,?,?,?)",
                               ((path, line, title, body) for line, title, body in sections(content)))
                db.execute("INSERT OR REPLACE INTO sources VALUES (?,?)", (path, digest))
            stale = [row[0] for row in db.execute("SELECT path FROM sources") if row[0] not in current]
            for path in stale:
                db.execute("DELETE FROM chunks WHERE path=?", (path,))
                db.execute("DELETE FROM sources WHERE path=?", (path,))
        return db
    except Exception:
        db.close()
        raise


def search(db, query, limit):
    terms = re.findall(r"\w+", query, re.UNICODE)
    if not terms:
        return []
    expression = " AND ".join('"' + term.replace('"', '""') + '"' for term in terms)
    rows = db.execute("SELECT path,line,body FROM chunks WHERE chunks MATCH ? ORDER BY bm25(chunks),path,CAST(line AS INTEGER) LIMIT ?",
                      (expression, limit)).fetchall()
    # unicode61 does not segment Chinese words. Substring fallback also supports
    # short Chinese queries and punctuation without a tokenizer/model dependency.
    if len(rows) < limit:
        clauses = " AND ".join("instr(lower(body), lower(?)) > 0" for _ in terms)
        extra = db.execute(f"SELECT path,line,body FROM chunks WHERE {clauses} ORDER BY path,CAST(line AS INTEGER) LIMIT ?",
                           (*terms, limit)).fetchall()
        seen = {(row[0], row[1]) for row in rows}
        rows += [row for row in extra if (row[0], row[1]) not in seen]
    return rows[:limit]


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="exact project root; defaults to cwd, never searches parents")
    parser.add_argument("--registry", type=Path, default=default_registry(), help="global project registry JSON path")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="create missing templates without overwriting notes")
    context = sub.add_parser("context", help="show bounded summary; refresh index")
    context.add_argument("--max-chars", type=positive, default=6000)
    find = sub.add_parser("search", help="keyword search with source lines; refresh index")
    find.add_argument("query")
    find.add_argument("--limit", type=positive, default=5)
    find.add_argument("--max-chars", type=positive, default=6000)
    sub.add_parser("reindex", help="rebuild disposable SQLite cache from Markdown")
    sub.add_parser("check", help="check required notes and size budgets")
    projects = sub.add_parser("projects", help="list registered memory locations without reading notes")
    projects.add_argument("--json", action="store_true", help="output locations and current availability as JSON")
    projects.add_argument("--prune", action="store_true", help="remove entries whose memory directory is missing or a symlink")
    install = sub.add_parser("install-skill", help="copy explicit-only skill to a specified skills directory")
    install.add_argument("--dest", type=Path, default=Path.home() / ".agents" / "skills")
    args = parser.parse_args(argv)
    try:
        # Do not resolve the final registry component: reject symlinks instead.
        registry = args.registry.expanduser().absolute()
        if args.command == "projects":
            entries = list_projects(registry, prune=args.prune)
            if args.json:
                print(json.dumps({"version": 1, "projects": entries}, ensure_ascii=False, indent=2))
            else:
                print(f"Registry: {registry}")
                print("\n".join(f"[{entry['status']}] {entry['root']} -> {entry['memory_dir']}" for entry in entries)
                      or "No registered project memory.")
            return 0
        if args.command == "install-skill":
            target = args.dest.expanduser() / "simple-codex-memory"
            if target.exists() or target.is_symlink():
                raise ValueError(f"Skill destination already exists: {target}")
            resource = files("simple_codex_memory").joinpath("skill")
            target.mkdir(parents=True)
            (target / "agents").mkdir()
            for name in ("SKILL.md", "agents/openai.yaml"):
                (target / name).write_text(resource.joinpath(*name.split("/")).read_text(encoding="utf-8"), encoding="utf-8")
            print(f"Installed {target}. Invoke $simple-codex-memory explicitly.")
            return 0
        root = args.root.expanduser().resolve()
        directory = memory_dir(root)
        if args.command == "init":
            directory.mkdir(parents=True, exist_ok=True)
            for name, text in {**TEMPLATES, ".gitignore": "index.sqlite*\n"}.items():
                path = directory / name
                if path.is_symlink():
                    raise ValueError(f"Template path must not be a symlink: {name}")
                try:
                    with path.open("x", encoding="utf-8") as stream:
                        stream.write(text)
                except FileExistsError:
                    pass
            register(registry, root)
            print(f"Initialized {directory}; existing files preserved. Memory remains opt-in.")
            return 0
        if args.command == "check":
            if not directory.is_dir():
                raise ValueError("No project memory. Run memory init first.")
            problems = []
            all_notes = {name: text for _, name, text in notes(directory)}
            for name in TEMPLATES:
                if name not in all_notes:
                    problems.append(f"Missing regular file: {name}")
            for name, content in all_notes.items():
                cap = 100 if name == "summary.md" else 300
                if len(content.splitlines()) > cap or len(content) > cap * 100:
                    problems.append(f"{name}: exceeds {cap} lines or {cap * 100} characters; condense or split")
            print("\n".join(problems) if problems else "Memory checks passed.")
            if not problems:
                register(registry, root)
            return 1 if problems else 0
        db = connect(directory, rebuild=args.command == "reindex")
        try:
            if args.command == "reindex":
                print("Index rebuilt from Markdown.")
            elif args.command == "context":
                available = {name: text for _, name, text in notes(directory)}
                if "summary.md" not in available:
                    raise ValueError("Missing summary.md; run memory init to restore the template.")
                output = available["summary.md"]
                output += "\nAvailable notes: " + ", ".join(available)
                print(bounded(output, args.max_chars))
            else:
                rows = search(db, args.query, args.limit)
                # Per-result excerpts ensure one long section cannot hide every other hit.
                per_hit = max(1, args.max_chars // max(1, len(rows)) - 100)
                output = "\n\n".join(f".memory/{path}:{line}\n{bounded(body, per_hit)}" for path, line, body in rows)
                print(bounded(output or "No matching memory.", args.max_chars))
        finally:
            db.close()
        register(registry, root)
        return 0
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"memory: {exc}", file=sys.stderr)
        return 2


def bounded(text, cap):
    marker = "\n[truncated]"
    if len(text) <= cap:
        return text
    return text[:max(0, cap - len(marker))] + marker[:cap]


if __name__ == "__main__":
    raise SystemExit(main())
