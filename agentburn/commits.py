"""`agentburn commits` — what a commit cost, in your own window.

Claude Code records the working directory and git branch of every session.
Your repositories record when each commit landed. Joining the two attributes
every call to the commit that followed it in that repository: the weighted
usage between two consecutive commits is what the second one cost.

Read-only on both sides: `git log` in the repositories the transcripts name,
nothing written, nothing sent. Sessions whose directory is not a git
repository (or has no commits in the window) are reported as such.
"""

from __future__ import annotations

import os
import statistics
import subprocess
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from .limits import cell_weight
from .model import Snapshot, agent_label
from .report import fmt_tokens


@dataclass
class CommitCost:
    repo: str
    sha: str
    subject: str
    ts: float
    weight: float
    calls: int
    branch: Optional[str] = None


@dataclass
class RepoStat:
    repo: str
    commits: int
    median: float
    total: float
    uncommitted: float  # usage after the last commit in the window


@dataclass
class CommitsReport:
    agent: str
    top: list = field(default_factory=list)  # CommitCost, costliest first
    repos: list = field(default_factory=list)  # RepoStat, by total
    skipped: list = field(default_factory=list)  # (project, reason)
    total_attributed: float = 0.0
    total_weight: float = 0.0
    unsupported: str = ""


def _git(args: list, cwd: str, timeout: float = 10) -> Optional[str]:
    try:
        r = subprocess.run(
            ["git"] + args, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def _repo_root(cwd: str) -> Optional[str]:
    out = _git(["rev-parse", "--show-toplevel"], cwd)
    return out.strip() if out else None


def _commits(root: str, since: Optional[float]) -> list:
    """[(ts, sha, subject)] across all refs, oldest first."""
    args = ["log", "--all", "--no-merges", "--format=%H%x1f%ct%x1f%s"]
    if since:
        args.append(f"--since={int(since)}")
    out = _git(args, root, timeout=30)
    if not out:
        return []
    rows = []
    for line in out.splitlines():
        parts = line.split("\x1f", 2)
        if len(parts) == 3 and parts[1].isdigit():
            rows.append((float(parts[1]), parts[0], parts[2]))
    rows.sort()
    return rows


def build_commits(snap: Snapshot, since: Optional[float] = None, git_ok: bool = True) -> CommitsReport:
    rep = CommitsReport(agent=snap.agent)
    if not snap.usage_cells or not any(s.project for s in snap.sessions):
        rep.unsupported = (
            f"{agent_label(snap.agent)}'s adapter does not record working directories per "
            "session, so calls can't be joined to commits."
        )
        return rep
    if git_ok and _git(["--version"], os.getcwd()) is None:
        rep.unsupported = "git is not on PATH; `agentburn commits` needs it to read your repositories."
        return rep

    project_of = {s.id: s.project for s in snap.sessions}
    branch_of = {s.id: s.branch for s in snap.sessions}
    for s in snap.sessions:
        if s.parent_id and not project_of.get(s.id):
            project_of[s.id] = project_of.get(s.parent_id)
            branch_of[s.id] = branch_of.get(s.parent_id)

    # cwd → repo root (one git call per distinct directory)
    roots: dict = {}
    for cwd in {p for p in project_of.values() if p}:
        if not os.path.isdir(cwd):
            rep.skipped.append((cwd, "directory no longer exists"))
            roots[cwd] = None
            continue
        roots[cwd] = _repo_root(cwd)
        if roots[cwd] is None:
            rep.skipped.append((cwd, "not a git repository"))

    # repo → [(ts, weight, calls, branch)]
    usage: dict = defaultdict(list)
    for c in snap.usage_cells:
        w = cell_weight(c)
        rep.total_weight += w
        if w <= 0 or not c.session:
            continue
        root = roots.get(project_of.get(c.session) or "")
        if not root:
            continue
        usage[root].append((c.start, w, c.calls, branch_of.get(c.session)))

    for root, cells in usage.items():
        commits = _commits(root, since)
        if not commits:
            rep.skipped.append((root, "no commits in the window"))
            continue
        cells.sort()
        costs = {sha: [0.0, 0, None] for _, sha, _ in commits}
        uncommitted = 0.0
        i = 0
        for start, w, n, br in cells:
            while i < len(commits) and commits[i][0] < start:
                i += 1
            if i >= len(commits):
                uncommitted += w
                continue
            entry = costs[commits[i][1]]
            entry[0] += w
            entry[1] += n
            entry[2] = entry[2] or br
        # A commit with nothing between it and the previous one (a rebase, a
        # merge of someone else's work) costs nothing and is not listed.
        priced = []
        for ts, sha, subj in commits:
            w, n, br = costs[sha]
            if w > 0:
                cc = CommitCost(repo=os.path.basename(root), sha=sha[:8], subject=subj[:60], ts=ts,
                                weight=w, calls=n, branch=br)
                priced.append(cc)
                rep.top.append(cc)
        if priced:
            total = sum(c.weight for c in priced)
            rep.total_attributed += total
            rep.repos.append(RepoStat(
                repo=os.path.basename(root), commits=len(priced),
                median=statistics.median(c.weight for c in priced), total=total,
                uncommitted=uncommitted,
            ))
        else:
            rep.skipped.append((root, "usage recorded, but after the last commit"))
    rep.top.sort(key=lambda c: -c.weight)
    rep.top = rep.top[:8]
    rep.repos.sort(key=lambda r: -r.total)
    return rep


def render_commits(rep: CommitsReport, color: bool = True) -> str:
    from .report import P

    p = P(color)
    out = ["", p.b(f"🧾 agentburn commits — {rep.agent} · what a commit cost you")]
    out.append(p.dim("   usage between two consecutive commits in a repository is what the second one cost"))
    out.append("")
    if rep.unsupported:
        out.append(f"   {rep.unsupported}")
        out.append("")
        return "\n".join(out)
    if not rep.top:
        out.append("   No commits could be priced in this window.")
        for proj, why in rep.skipped[:6]:
            out.append(p.dim(f"   {proj}: {why}"))
        out.append("")
        return "\n".join(out)

    out.append(p.b("   COSTLIEST COMMITS"))
    for c in rep.top:
        when = time.strftime("%b %d", time.localtime(c.ts))
        out.append(
            f"   {fmt_tokens(c.weight):>8}   {c.repo[:18]:<18} {c.sha}  {when}  {c.subject}"
        )
    out.append("")
    out.append(p.b("   BY REPOSITORY"))
    out.append(p.dim("   median cost of a commit · commits · usage after the last commit"))
    for r in rep.repos[:8]:
        out.append(
            f"   {r.repo[:24]:<24} {fmt_tokens(r.median):>8} median · {r.commits:>4} commits · "
            f"{fmt_tokens(r.total):>8} total" + (f" · {fmt_tokens(r.uncommitted)} uncommitted" if r.uncommitted else "")
        )
    out.append("")
    if rep.total_weight:
        out.append(
            p.dim(f"   {rep.total_attributed / rep.total_weight:.0%} of weighted usage landed in a priced commit; "
                  f"{len(rep.skipped)} director{'y' if len(rep.skipped) == 1 else 'ies'} skipped.")
        )
    out.append(p.dim("   Read-only: `git log` in the repositories your sessions name. Weighted tokens, as in `limits`."))
    out.append("")
    return "\n".join(out)


def commits_json(rep: CommitsReport) -> dict:
    return {
        "agent": rep.agent,
        "unsupported": rep.unsupported or None,
        "top": [
            {"repo": c.repo, "sha": c.sha, "subject": c.subject, "ts": c.ts, "weight": round(c.weight),
             "calls": c.calls, "branch": c.branch}
            for c in rep.top
        ],
        "repos": [
            {"repo": r.repo, "commits": r.commits, "median": round(r.median), "total": round(r.total),
             "uncommitted": round(r.uncommitted)}
            for r in rep.repos
        ],
        "skipped": [{"path": p_, "why": w} for p_, w in rep.skipped],
        "attributed_share": round(rep.total_attributed / rep.total_weight, 4) if rep.total_weight else None,
    }
