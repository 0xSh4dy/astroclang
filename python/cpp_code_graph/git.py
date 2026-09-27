"""Reading a git diff as a set of changed line ranges.

This module knows about git and nothing about the index.  Its whole job is to
turn a revision or a working tree into "these lines of this file are new",
which is a question git can answer exactly and a question the index cannot
answer at all.

The line ranges are the *new* file's, because that is the file the index was
built from and the file a reader will open.  A pure deletion has no new lines
- it names the line the deleted text used to occupy, so the change still
attaches to whatever contains that point rather than to nothing.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

# `@@ -old_start,old_count +new_start,new_count @@`, with the counts optional
# when they are 1.  A hunk header can also end with the enclosing function,
# which is git's guess from the source text and is not used here: the index
# knows the real answer.
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")

STATUS_WORDS = {
    "A": "added",
    "M": "modified",
    "D": "deleted",
    "R": "renamed",
    "C": "copied",
    "T": "type changed",
}


class GitError(RuntimeError):
    """git could not answer, which is different from answering 'no changes'."""


@dataclass
class FileChange:
    path: str            # repo-relative, in the new tree where there is one
    old_path: str = ""   # set for a rename or copy
    status: str = "modified"
    lines: List[Tuple[int, int]] = field(default_factory=list)

    def describe(self) -> Dict[str, object]:
        out: Dict[str, object] = {"file": self.path, "status": self.status}
        if self.old_path:
            out["was"] = self.old_path
        if self.lines:
            out["lines"] = collapse_ranges(self.lines)
            out["changed_lines"] = sum(e - s + 1 for s, e in self.lines)
        return out


@dataclass
class Diff:
    root: Path
    revision: str = "worktree"
    subject: str = ""
    files: List[FileChange] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.files

    def for_index(self) -> List[Tuple[str, List[Tuple[int, int]]]]:
        """(absolute path, ranges) for every changed file still on disk."""
        out = []
        for change in self.files:
            if change.status == "deleted" or not change.lines:
                continue
            out.append((str(self.root / change.path), change.lines))
        return out


def run_git(root: Path, args: Sequence[str],
            check: bool = True) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True, timeout=60,
        )
    except FileNotFoundError as exc:
        raise GitError("git is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {' '.join(args)} timed out") from exc
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise GitError(detail[-1] if detail else f"git exited {proc.returncode}")
    return proc.stdout


def repository_root(path: Path) -> Optional[Path]:
    """The working tree containing `path`, or None when it is not in one."""
    try:
        out = run_git(Path(path), ["rev-parse", "--show-toplevel"],
                      check=False)
    except GitError:
        return None
    out = out.strip()
    return Path(out) if out else None


def describe_revision(root: Path, revision: str) -> Tuple[str, str]:
    """The abbreviated commit id and its subject line."""
    if revision == "worktree":
        return "worktree", "uncommitted changes"
    try:
        out = run_git(root, ["log", "-1", "--format=%h%x00%s", revision,
                             "--"], check=False)
    except GitError:
        return revision, ""
    parts = out.strip().split("\x00", 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return revision, ""


def diff(root: Path, revision: str = "HEAD",
         staged: bool = False) -> Diff:
    """Changed line ranges between `revision` and the working tree.

    `revision` may be a commit, a range such as ``main...HEAD``, or the word
    ``worktree`` for everything not yet committed.  `staged` restricts that to
    the index, which is what a pre-commit check wants.
    """
    root = Path(root)
    if repository_root(root) is None:
        raise GitError(f"{root} is not inside a git working tree")

    if revision == "worktree":
        args = ["diff"]
        if staged:
            args.append("--cached")
    else:
        args = ["diff", revision]

    status_text = run_git(root, [*args, "--name-status", "-M",
                                 "--no-color", "--no-ext-diff"])
    changes = _parse_status(status_text)

    # `--unified=0` is what makes this exact: with no context lines every hunk
    # header is the changed range itself, and no arithmetic is needed to
    # recover it from padding.
    patch = run_git(root, [*args, "--unified=0", "-M", "--no-color",
                           "--no-ext-diff", "-U0"])
    _attach_ranges(patch, changes)
    if _ends_at_worktree(revision, staged):
        _add_untracked(root, changes)

    revision_id, subject = describe_revision(root, revision)
    return Diff(root=root, revision=revision_id, subject=subject,
                files=[changes[k] for k in sorted(changes)])


def _ends_at_worktree(revision: str, staged: bool) -> bool:
    """Whether this diff compares something *to* the working tree.

    Untracked files belong to the working tree, but git leaves them out of
    `diff` however it is invoked, because they are in no commit and no index.
    So they are added back by hand - but only when the working tree is one
    side of the comparison.  A diff between two commits is a question about
    history, and an unsaved file has no business in the answer.
    """
    return not staged and ".." not in revision


def _add_untracked(root: Path, changes: Dict[str, FileChange]) -> None:
    """New files nobody has staged yet.

    `git diff` shows tracked changes only, so a file just written is invisible
    to it - and a review of the working tree that omits the new file omits the
    part most likely to be wrong.  Ignored files stay out, which is what
    `.gitignore` is for.
    """
    out = run_git(root, ["ls-files", "--others", "--exclude-standard"],
                  check=False)
    for name in out.splitlines():
        name = name.strip()
        if not name or name in changes:
            continue
        change = FileChange(path=name, status="added")
        try:
            # The whole file is new, so every line of it is a changed line.
            with open(root / name, "r", encoding="utf-8", errors="replace") as f:
                count = len(f.read().splitlines())
            change.lines = [(1, max(1, count))]
        except OSError:
            pass
        changes[name] = change


def _parse_status(text: str) -> Dict[str, FileChange]:
    """One FileChange per path, keyed by the path in the new tree."""
    out: Dict[str, FileChange] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        code = parts[0]
        letter = code[0]
        status = STATUS_WORDS.get(letter, code)
        if letter in ("R", "C") and len(parts) >= 3:
            change = FileChange(path=parts[2], old_path=parts[1],
                                status=status)
        elif len(parts) >= 2:
            change = FileChange(path=parts[1],
                                status=status if letter != "R" else "renamed")
        else:
            continue
        out[change.path] = change
    return out


def _attach_ranges(patch: str, changes: Dict[str, FileChange]) -> None:
    current: Optional[FileChange] = None
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            current = changes.get(_path_from_header(line))
            continue
        if line.startswith("+++ "):
            # Authoritative for renames and for paths whose `diff --git`
            # header is quoted; `/dev/null` means the file is gone.
            path = _strip_prefix(line[4:])
            if path:
                current = changes.get(path)
            continue
        if current is None or not line.startswith("@@"):
            continue
        m = _HUNK.match(line)
        if not m:
            continue
        start = int(m.group(1))
        count = int(m.group(2)) if m.group(2) is not None else 1
        if count == 0:
            # A deletion: no new lines exist.  Name the point the removed text
            # occupied, so the change still attaches to the code around it.
            current.lines.append((max(1, start), max(1, start)))
        else:
            current.lines.append((start, start + count - 1))


def _path_from_header(line: str) -> str:
    """The new path from `diff --git a/x b/x`, for files without a +++ line."""
    rest = line[len("diff --git "):]
    if rest.startswith('"'):
        return ""
    marker = " b/"
    at = rest.rfind(marker)
    if at < 0:
        return ""
    return rest[at + len(marker):]


def _strip_prefix(field: str) -> str:
    field = field.strip()
    if not field or field == "/dev/null":
        return ""
    if field.startswith('"') and field.endswith('"'):
        field = field[1:-1]
    if field.startswith("a/") or field.startswith("b/"):
        return field[2:]
    return field


def collapse_ranges(ranges: List[Tuple[int, int]]) -> str:
    """Ranges as a compact `12-14, 31` string, which is what a reader wants."""
    merged: List[Tuple[int, int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return ", ".join(str(s) if s == e else f"{s}-{e}" for s, e in merged)
