"""The Project Locator: deterministic filesystem discovery.

The Manager performs semantic selection; this component produces the
candidates (Invariant 6). The boundary is strict: the Manager never receives
or searches the filesystem tree - it gets a small ranked set of bounded
project metadata (names, paths, remotes, manifests), and source-file
discovery starts only after a project is resolved, inside the coding
worker's workspace.

Two separate concepts:

    Project Index     locally discovered folders that *look* like projects,
                      cached in <home>/index.json across runs
    Project Registry  projects the Conductor has actually identified and
                      used (ProjectStore)

Discovery is incremental: searches hit the registry and index first, and
the filesystem is rescanned only when a name cannot be resolved from either
(or on explicit request). Fingerprints (git remote, package name) let a
moved repository be recognised as the same project rather than silently
registered twice.
"""

from __future__ import annotations

import configparser
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .projects import Project, ProjectStore
from .storage import atomic_write_json, now_iso, read_json

MAX_CANDIDATES = 5           # the Manager sees a small ranked set, never more

MARKERS = (".git", "package.json", "pyproject.toml", "Cargo.toml", "go.mod",
           "Gemfile", "pom.xml", "build.gradle")
_SKIP_DIRS = {"node_modules", ".venv", "venv", "__pycache__", "dist",
              "build", "target", ".next", ".cache"}
_MAX_DEPTH = 4


@dataclass
class ProjectCandidate:
    path: str
    name: str
    markers: list[str] = field(default_factory=list)
    repo_root: str | None = None
    remote_url: str | None = None
    package_name: str | None = None
    registered_id: str | None = None      # already-known project, if any
    score: int = 0

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v not in (None, [])}


def _git_remote(repo_root: Path) -> str | None:
    config = repo_root / ".git" / "config"
    try:
        parser = configparser.ConfigParser()
        parser.read_string(config.read_text())
        for section in parser.sections():
            if section.startswith('remote "') and \
                    parser.has_option(section, "url"):
                return parser.get(section, "url")
    except (OSError, configparser.Error):
        pass
    return None


def _package_name(path: Path) -> str | None:
    try:
        return json.loads((path / "package.json").read_text()).get("name")
    except (OSError, json.JSONDecodeError):
        pass
    try:
        match = re.search(r'(?m)^name\s*=\s*"([^"]+)"',
                          (path / "pyproject.toml").read_text())
        if match:
            return match.group(1)
    except OSError:
        pass
    return None


def _fold(text: str) -> str:
    """A name as the ear hears it: lowercase, punctuation collapsed to a
    space. Queries arrive spoken - "voice agent" - and must find the
    voice-agent directory and the voice_agent package alike."""
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _is_linked_worktree(path: Path) -> bool:
    """A linked git worktree has a .git *file*, not a directory. Those are
    task workspaces (ours or a provider's), never independent projects."""
    dotgit = path / ".git"
    return dotgit.exists() and dotgit.is_file()


class ProjectIndex:
    """The discovery cache: candidates that looked like projects, persisted
    so the filesystem is not rescanned for every request.

    This is not the registry - indexed entries carry no identity and no
    tasks; they are raw material for the Manager's semantic selection.
    """

    def __init__(self, home: Path) -> None:
        self.path = home / "index.json"
        self._data = read_json(self.path) or {"scanned_at": None,
                                              "entries": {}}

    @property
    def scanned_at(self) -> str | None:
        return self._data["scanned_at"]

    def entries(self) -> list[ProjectCandidate]:
        """Indexed candidates, with entries whose paths vanished pruned."""
        stale = [p for p in self._data["entries"] if not Path(p).is_dir()]
        if stale:
            for path in stale:
                del self._data["entries"][path]
            self._save()
        return [ProjectCandidate(**entry)
                for entry in self._data["entries"].values()]

    def replace(self, candidates: list[ProjectCandidate]) -> None:
        self._data["entries"] = {c.path: self._entry(c) for c in candidates}
        self._data["scanned_at"] = now_iso()
        self._save()

    def upsert(self, candidate: ProjectCandidate) -> None:
        self._data["entries"][candidate.path] = self._entry(candidate)
        self._save()

    @staticmethod
    def _entry(candidate: ProjectCandidate) -> dict:
        return {"path": candidate.path, "name": candidate.name,
                "markers": candidate.markers,
                "repo_root": candidate.repo_root,
                "remote_url": candidate.remote_url,
                "package_name": candidate.package_name}

    def _save(self) -> None:
        atomic_write_json(self.path, self._data)


class ProjectLocator:
    def __init__(self, store: ProjectStore,
                 search_roots: list[str | Path] | None = None) -> None:
        self.store = store
        self.search_roots = [Path(r).expanduser() for r in search_roots or []]
        self.index = ProjectIndex(store.home)

    # -- inspection ---------------------------------------------------------
    def inspect_path(self, path: str | Path) -> ProjectCandidate | None:
        path = Path(path).resolve()
        if not path.is_dir():
            return None
        markers = [m for m in MARKERS if (path / m).exists()]
        if not markers or _is_linked_worktree(path):
            return None
        # Monorepos: a project may be a subdirectory of its repository
        # (rootPath = apps/web, repoRoot = the enclosing .git). Walk up so
        # workspaces are worktreed from the real repo.
        repo_root = None
        for ancestor in (path, *path.parents):
            if (ancestor / ".git").is_dir():
                repo_root = str(ancestor)
                break
        return ProjectCandidate(
            path=str(path), name=path.name, markers=markers,
            repo_root=repo_root,
            remote_url=_git_remote(Path(repo_root)) if repo_root else None,
            package_name=_package_name(path))

    # -- discovery ------------------------------------------------------------
    def _walk(self) -> list[ProjectCandidate]:
        home = self.store.home.resolve()
        seen: set[str] = set()
        found: list[ProjectCandidate] = []

        def visit(directory: Path, depth: int) -> None:
            if depth > _MAX_DEPTH or directory.name in _SKIP_DIRS or \
                    directory.name.startswith("."):
                return
            resolved = directory.resolve()
            # Never inspect our own home: managed worktrees under
            # <home>/workspaces/ must not appear as projects (spec 18).
            if resolved == home or home in resolved.parents:
                return
            candidate = self.inspect_path(resolved)
            if candidate and candidate.path not in seen:
                seen.add(candidate.path)
                found.append(candidate)
                return                     # a project root; don't nest scan
            try:
                children = sorted(directory.iterdir())
            except OSError:
                return                     # unreadable: skip it, not the walk
            for child in children:
                # Guard each child separately. Wrapping the loop meant one
                # unreadable sibling aborted every sibling after it.
                try:
                    if child.is_dir():
                        visit(child, depth + 1)
                except OSError:
                    continue

        for root in self.search_roots:
            if root.is_dir():
                visit(root, 0)
        return found

    def scan(self) -> int:
        """Full discovery pass over the search roots; refreshes the index.
        Runs on demand (setup, explicit request, unresolved name), never
        per-utterance."""
        candidates = self._walk()
        self.index.replace(candidates)
        return len(candidates)

    # -- search ---------------------------------------------------------------
    def _match_registered(self, query_l: str) -> list[ProjectCandidate]:
        matches = []
        for project in self.store.list():
            names = [_fold(name) for name in project.names()]
            score = 0
            if query_l == _fold(project.display_name):
                score += 4
            if query_l in names:
                score += 3
            elif any(query_l in name for name in names):
                score += 2
            if score:
                if project.id in self.store.recent_ids()[:3]:
                    score += 1
                matches.append(ProjectCandidate(
                    path=project.root_path, name=project.display_name,
                    registered_id=project.id, score=score))
        return matches

    def _match_indexed(self, query_l: str,
                       known_paths: set[str]) -> list[ProjectCandidate]:
        matches = []
        for candidate in self.index.entries():
            if candidate.path in known_paths:
                continue
            texts = [_fold(candidate.name)]
            if candidate.package_name:
                texts.append(_fold(candidate.package_name))
            if candidate.remote_url:
                texts.append(_fold(candidate.remote_url))
            score = 0
            if any(query_l == t for t in texts):
                score += 3
            elif any(query_l in t for t in texts):
                score += 2
            if score:
                candidate.score = score
                matches.append(candidate)
        return matches

    def search(self, query: str,
               limit: int = MAX_CANDIDATES) -> list[ProjectCandidate]:
        """A small ranked candidate set for the Manager to choose from.

        Registry first, then the cached index; the filesystem is rescanned
        only when neither resolves the name. The result is bounded metadata -
        never a tree, never file contents. The model chooses; it never
        fabricates candidates.
        """
        query_l = _fold(query)
        registered = self._match_registered(query_l)
        known_paths = {c.path for c in registered}
        matches = registered + self._match_indexed(query_l, known_paths)
        if not matches:
            self.scan()                    # expand only when unresolved
            matches = registered + self._match_indexed(query_l, known_paths)
        return sorted(matches, key=lambda c: -c.score)[:limit]

    # -- registration ----------------------------------------------------------
    def register(self, path: str | Path, display_name: str | None = None,
                 aliases: list[str] | None = None) -> Project:
        candidate = self.inspect_path(path)
        if candidate is None:
            raise ValueError(f"{path} does not look like a project")
        for project in self.store.list():
            if project.root_path == candidate.path:
                return project             # idempotent
        return self.store.register(
            display_name=display_name or candidate.name,
            root_path=candidate.path, aliases=aliases or [],
            repo_root=candidate.repo_root, remote_url=candidate.remote_url,
            package_name=candidate.package_name)

    # -- health -----------------------------------------------------------------
    def refresh(self, project_id: str) -> Project:
        project = self.store.get(project_id)
        if project is None:
            raise KeyError(f"no such project: {project_id}")
        status = "available" if Path(project.root_path).is_dir() else "missing"
        if status != project.status:
            project = self.store.update(project_id, status=status)
        return project

    def find_moved(self, project_id: str) -> list[ProjectCandidate]:
        """Fingerprint search: same remote or package name elsewhere.
        A moved repo is by definition not where the index last saw it, so
        this refreshes the index first."""
        project = self.store.get(project_id)
        if project is None:
            raise KeyError(f"no such project: {project_id}")
        self.scan()
        matches = []
        for candidate in self.index.entries():
            if candidate.path == project.root_path:
                continue
            same_remote = (project.remote_url and
                           candidate.remote_url == project.remote_url)
            same_package = (project.package_name and
                            candidate.package_name == project.package_name)
            if same_remote or same_package:
                candidate.score = 3 if same_remote else 2
                matches.append(candidate)
        return sorted(matches, key=lambda c: -c.score)
