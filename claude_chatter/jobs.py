"""
Read-only view of Claude Code background/daemon jobs (~/.claude/jobs/<id>/).

This is the data behind the fleet list: done and blocked jobs have no live
process, so they are invisible to the session scan in protocol.py.
"""
import os
import json
import glob
from typing import Any, Dict, List, Optional

# Dropped on purpose: respawnFlags/providerEnv can carry launch settings, linkScanOffset is an internal cursor (linkScanPath is exposed as transcript_path).
_JOB_FIELDS = (
    "name", "state", "detail", "output", "needs", "tempo", "intent", "cwd", "template",
    "sessionId", "createdAt", "updatedAt", "firstTerminalAt", "lastTerminalAt",
    "worktreePath", "worktreeBranch", "originCwd", "tokens", "suggestedReply",
)


def _read_json(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _last_timeline_entry(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 65536))
            tail = f.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    for line in reversed(tail.splitlines()):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            entry.pop("text", None)
            return entry
    return None


def _worker_alive(worker: Dict[str, Any]) -> bool:
    """True if the roster worker's pid exists and has the recorded start time (pid reuse guard)."""
    try:
        with open("/proc/%d/stat" % int(worker["pid"]), "r", encoding="utf-8") as f:
            start = f.read().rsplit(")", 1)[1].split()[19]
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return False
    return start == str(worker.get("procStart"))


def _mtime(path: Optional[str]) -> Optional[float]:
    try:
        return os.stat(path).st_mtime if path else None
    except OSError:
        return None


def list_jobs(claude_dir: Optional[str] = None, live_sessions: Optional[Dict[str, Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """
    One entry per job dir, newest update first. `running` is true when the daemon
    roster has a worker for the job whose pid is alive with the recorded start time;
    PR `pr_state` comes from Claude Code's own gh-pr-status-cache.json (may be stale or absent). `live_sessions` is the
    verify_and_purge_sessions() result; matching it by jobId/parkedJobId adds
    the live pid/kind/status of the job's process, if any.
    """
    claude_dir = claude_dir or os.path.join(os.path.expanduser("~"), ".claude")

    live_by_job: Dict[str, Dict[str, Any]] = {}
    for name, peer in (live_sessions or {}).items():
        for key in ("jobId", "parkedJobId"):
            job_id = peer.get(key)
            if job_id:
                live_by_job[job_id] = peer

    roster = _read_json(os.path.join(claude_dir, "daemon", "roster.json")) or {}
    workers = roster.get("workers") if isinstance(roster.get("workers"), dict) else {}
    pr_cache = _read_json(os.path.join(claude_dir, "gh-pr-status-cache.json")) or {}

    jobs: List[Dict[str, Any]] = []
    for state_path in glob.glob(os.path.join(claude_dir, "jobs", "*", "state.json")):
        state = _read_json(state_path)
        if state is None:
            continue
        job_dir = os.path.dirname(state_path)
        job_id = state.get("daemonShort") or os.path.basename(job_dir)

        job: Dict[str, Any] = {"id": job_id}
        for field in _JOB_FIELDS:
            if field in state:
                job[field] = state[field]
        children = []
        for c in (state.get("children") or []):
            if not isinstance(c, dict):
                continue
            cached = pr_cache.get(c.get("href")) if c.get("kind") == "pr" else None
            children.append({
                "id": c.get("id"), "kind": c.get("kind"), "href": c.get("href"),
                "pr_state": cached.get("state") if isinstance(cached, dict) else None,
            })
        job["children"] = children
        worker = workers.get(job_id)
        job["running"] = isinstance(worker, dict) and _worker_alive(worker)
        job["last_timeline"] = _last_timeline_entry(os.path.join(job_dir, "timeline.jsonl"))
        job["transcript_path"] = state.get("linkScanPath")
        job["transcript_mtime"] = _mtime(state.get("linkScanPath"))

        live = live_by_job.get(job_id)
        job["live"] = None if live is None else {
            "pid": live.get("pid"), "kind": live.get("kind"), "status": live.get("status"),
        }
        jobs.append(job)

    jobs.sort(key=lambda j: j.get("updatedAt") or "", reverse=True)
    return jobs
