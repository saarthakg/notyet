"""Which files use which, in the working tree: for choosing the tests that
exercise a change.

Besides import statements, these count as using a module, because tests reach
code through them all the time:
  - a string that names it: importlib.import_module("pkg.mod"), a mock.patch
    target, `pytest_plugins`, a dotted path in a settings dict;
  - a string ending in a dot, or an f-string starting with one, that names a
    package ("pkg.backends." + name): every module in that package;
  - ["...", "-m", "pkg"]: running it as a program;
  - a bare module name that matches one file in the repo, for tests that put a
    scripts directory on sys.path.
And a conftest.py is used by every test module under its directory.

Imports are parsed with `ast` and resolved with notyet.pyresolve
(source roots, relative imports, submodule names), and cached in
.git/notyet/cache/ per file content. On a repo the size of Django the first
build takes about ten seconds; later checks only read what changed.
"""
import ast
import json
import os
import re
from collections import deque
from pathlib import Path, PurePosixPath
from typing import Iterable, Optional

from notyet.pyresolve import find_python_source_roots, resolve_python_import
from notyet import snapshot, store
from notyet.source import parse


def python_blobs(root: str, tree: str) -> dict[str, str]:
    """path → blob id for every .py file in `tree`."""
    out = snapshot.git(root, "ls-tree", "-r", "-z", tree)
    blobs = {}
    for entry in out.split("\0"):
        if not entry:
            continue
        meta, path = entry.split("\t", 1)
        if path.endswith(".py") and meta.split()[1] == "blob":
            blobs[path] = meta.split()[2]
    return blobs


_DOTTED = re.compile(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)+\.?$|[A-Za-z_]\w*\.$")


def _imports_of(source: str) -> tuple[list[tuple[str, list[str]]], set[str]]:
    """([(module, names)] for import statements, dotted names that appear in strings)."""
    tree = parse(source)
    if tree is None:
        return [], set()
    specs, strings = [], set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            specs += [(alias.name, []) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            specs.append(("." * node.level + (node.module or ""), [a.name for a in node.names]))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if len(node.value) < 200 and _DOTTED.match(node.value):
                strings.add(node.value)
        elif isinstance(node, (ast.List, ast.Tuple, ast.Call)):
            items = node.args if isinstance(node, ast.Call) else node.elts
            for flag, value in zip(items, items[1:]):
                program = getattr(value, "value", None)
                if getattr(flag, "value", None) == "-m" and isinstance(program, str):
                    strings |= {program + ".__main__", program + "."}
    return specs, strings


def _mentions(path: str) -> list[str]:
    """Regexes for what a use of this file has to contain: its module name (its package's, for an
    __init__.py), or its package's name at the end of a string ("pkg.backends." + name)."""
    p = PurePosixPath(path)
    package = re.escape(p.parent.name)
    out = [rf"\b{package if p.stem == '__init__' else re.escape(p.stem)}\b"]
    return out + ([rf"\b{package}\.[\"'{{]"] if package else [])


class ImportGraph:
    """`checkout`: where the files of `tree` can be read, when that isn't the working tree."""
    VERSION = 4      # bump when edges are computed differently, so cached ones aren't reused

    def __init__(self, root: str, tree: str, checkout: Optional[str] = None):
        self.root, self.files_root = root, checkout or root
        self.blobs = python_blobs(root, tree)
        self.by_stem: dict[str, list[str]] = {}
        for p in self.blobs:
            self.by_stem.setdefault(PurePosixPath(p).stem, []).append(p)
        self.edges: dict[str, list[str]] = self._load_or_build()
        self.reverse: dict[str, set[str]] = {}
        for src, targets in self.edges.items():
            for t in targets:
                self.reverse.setdefault(t, set()).add(src)
        for p in self.blobs:     # pytest loads a conftest.py for every module under its directory
            if PurePosixPath(p).name == "conftest.py":
                folder = os.path.dirname(p)
                self.reverse.setdefault(p, set()).update(
                    q for q in self.blobs if q != p and (not folder or q.startswith(folder + "/")))

    def _load_or_build(self) -> dict[str, list[str]]:
        """From the cache where possible: a file's edges depend on its content (its blob id), and on which
        files exist. When files come or go, only those whose text mentions one of their names can resolve
        differently, so only they are read again."""
        path = store.state_dir(self.root) / "cache" / f"imports-v{self.VERSION}.json"
        path.parent.mkdir(exist_ok=True)
        try:
            cached = json.loads(path.read_text())
        except (OSError, ValueError):
            cached = {}
        moved = {m for p in cached.keys() ^ self.blobs.keys() for m in _mentions(p)} if cached else set()
        mentions = re.compile("|".join(sorted(moved))) if moved else None
        roots = None
        edges, fresh = {}, {}
        for rel, blob in self.blobs.items():
            hit = cached.get(rel)
            if hit and hit[0] == blob and not (mentions and mentions.search(self._read(rel))):
                edges[rel] = hit[1]
            else:
                if roots is None:
                    roots = find_python_source_roots(self.files_root)
                edges[rel] = self._resolve(rel, roots)
            fresh[rel] = [blob, edges[rel]]
        if fresh != cached:
            path.write_text(json.dumps(fresh))
        return edges

    def _read(self, rel: str) -> str:
        try:
            return Path(os.path.join(self.files_root, rel)).read_text(encoding="utf-8", errors="surrogateescape")
        except OSError:
            return ""

    def _resolve(self, rel: str, roots) -> list[str]:
        full = os.path.join(self.files_root, rel)
        targets = set()
        specs, strings = _imports_of(self._read(rel))
        for module, names in specs:
            hits = self._files(module, full, names, roots)
            if not hits and not module.startswith("."):
                hits = self._by_name(module)
            targets |= hits
        for text in strings:
            targets |= self._named(text, full, roots)
        for t in list(targets):       # importing pkg.sub.mod runs pkg/__init__.py and pkg/sub/__init__.py first
            folder = os.path.dirname(t)
            while folder:
                if f"{folder}/__init__.py" in self.blobs:
                    targets.add(f"{folder}/__init__.py")
                folder = os.path.dirname(folder)
        targets.discard(rel)
        return sorted(targets)

    def _files(self, module: str, full: str, names: list[str], roots) -> set[str]:
        hits = resolve_python_import(module, full, self.files_root, names=names, source_roots=roots)
        return {t for t in (os.path.relpath(h, os.path.realpath(self.files_root)) for h in hits) if t in self.blobs}

    def _named(self, text: str, full: str, roots) -> set[str]:
        """The modules a dotted string names. Only names that start in the repo resolve to anything."""
        if text.endswith("."):
            package = self._files(text.rstrip("."), full, [], roots)
            folders = {os.path.dirname(p) for p in package if p.endswith("/__init__.py")}
            return package | {p for p in self.blobs if os.path.dirname(p) in folders}
        parts = text.split(".")
        for cut in range(len(parts), 0, -1):
            hits = self._files(".".join(parts[:cut]), full, [], roots)
            if hits:
                return hits
        return set()

    def _by_name(self, module: str) -> set[str]:
        """An absolute import that resolves nowhere: the file it can only mean, if there's exactly one
        outside any package (a scripts directory put on sys.path)."""
        stem = module.split(".")[0]
        found = [p for p in self.by_stem.get(stem, [])
                 if f"{os.path.dirname(p)}/__init__.py" not in self.blobs and stem != "conftest"]
        return set(found) if len(found) == 1 else set()

    def importers(self, targets: Iterable[str], max_depth: Optional[int] = None) -> dict[str, int]:
        """Files that use any of `targets`, directly (depth 1) or through
        other files, with the smallest depth for each."""
        seen: dict[str, int] = {}
        queue = deque((t, 0) for t in targets)
        while queue:
            node, depth = queue.popleft()
            if max_depth is not None and depth >= max_depth:
                continue
            for src in self.reverse.get(node, ()):
                if src not in seen or seen[src] > depth + 1:
                    seen[src] = depth + 1
                    queue.append((src, depth + 1))
        return seen
