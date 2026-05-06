#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
git-auto-sync — Drop-in automatic Git sync for any repo.

Usage:
  python sync.py              # Install (right-click to run, no args needed)
  python sync.py install      # Same as above
  python sync.py uninstall    # Remove scheduled tasks / launch agents
  python sync.py status       # Show current status
  python sync.py run          # Start daemon (called by system service)
  python sync.py once         # Run one sync cycle and exit
  python sync.py check        # Manual health check

After install:
  - Completely silent — no windows, no popups, no interruptions
  - Starts on boot, auto-restarts on crash, independent of any editor
  - File save → 5s debounce → auto commit + push
  - Pulls remote every 30 min (probation) or 2 min (graduated)
  - Daily 9:00 health check, notification only on failure

Works on Windows (x64) and macOS (Apple Silicon / Intel).
Requires: Python 3.10+, Git. Installs watchdog automatically.
License: MIT
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import platform
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

# ── Handle pythonw (no console → stdout/stderr are None) ──
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

# ── Windows: prevent subprocess console windows ──────
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# ── Locate repo ─────────────────────────────────────
SCRIPT_PATH = Path(__file__).resolve()


def find_repo_root() -> Path:
    """Walk up from script location to find .git directory."""
    for p in [SCRIPT_PATH.parent, *SCRIPT_PATH.parent.parents]:
        if (p / ".git").exists():
            return p
    raise RuntimeError("Cannot find Git repository")


def _repo_label(repo: Path) -> str:
    """Derive a human-readable label from the repo folder name."""
    return repo.name or "repo"


# ── Dependency check ────────────────────────────────
def _pip_is_locked() -> bool:
    """Detect PEP 668 externally-managed Python (uv, Homebrew, etc.)"""
    r = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--dry-run", "watchdog"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=_NO_WINDOW,
    )
    return "externally-managed" in (r.stderr + r.stdout).lower()


def _gitexclude_venv(venv_dir: Path):
    """Add venv path to .git/info/exclude so git add -A won't stage it."""
    try:
        repo = find_repo_root()
        exclude_file = repo / ".git" / "info" / "exclude"
        exclude_file.parent.mkdir(parents=True, exist_ok=True)
        rel = venv_dir.relative_to(repo)
        entry = str(rel).replace("\\", "/") + "/"
        if exclude_file.exists():
            content = exclude_file.read_text(encoding="utf-8")
            if entry in content:
                return
        with open(exclude_file, "a", encoding="utf-8") as f:
            f.write(f"\n# auto-sync venv (do not commit)\n{entry}\n")
    except Exception:
        pass


def _ensure_venv() -> Path:
    """Create a venv next to the script if pip is locked."""
    venv_dir = SCRIPT_PATH.parent / ".venv"
    if venv_dir.exists():
        python = venv_dir / ("Scripts" if os.name == "nt" else "bin") / "python"
        if python.exists() or (python.parent / "python.exe").exists():
            return venv_dir
    subprocess.run(
        [sys.executable, "-m", "venv", str(venv_dir)],
        check=True, creationflags=_NO_WINDOW,
    )
    _gitexclude_venv(venv_dir)
    return venv_dir


def ensure_watchdog():
    try:
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler, FileSystemEvent
        return Observer, FileSystemEventHandler, FileSystemEvent
    except ImportError:
        pass

    if _pip_is_locked():
        venv_dir = _ensure_venv()
        venv_python = venv_dir / ("Scripts" if os.name == "nt" else "bin") / "python"
        if os.name == "nt":
            venv_python = venv_python.with_suffix(".exe")
        subprocess.run(
            [str(venv_python), "-m", "pip", "install", "watchdog", "-q"],
            check=True, creationflags=_NO_WINDOW,
        )
        os.execv(str(venv_python), [str(venv_python), *sys.argv])

    print("Installing dependency: watchdog ...", flush=True)
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "watchdog", "-q"],
        check=True, creationflags=_NO_WINDOW,
    )
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler, FileSystemEvent
    print("Done.", flush=True)
    return Observer, FileSystemEventHandler, FileSystemEvent


# ── Config ───────────────────────────────────────────
DEFAULT_CONFIG = {
    "debounce_seconds": 5,
    "pull_interval_seconds": 1800,      # probation: 30 min
    "graduated_pull_interval": 120,     # graduated: 2 min
    "commit_message_prefix": "auto-sync",
    "ignore_patterns": [
        ".git", ".venv", "__pycache__", "*.pyc",
        ".DS_Store", "Thumbs.db", "*.tmp", "*.swp", "~$*",
    ],
}


# ── Git operations ───────────────────────────────────
class GitRepo:
    def __init__(self, path: Path):
        self.path = path.resolve()
        if not (self.path / ".git").exists():
            raise ValueError(f"Not a Git repository: {self.path}")

    def run(self, args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["git", "-C", str(self.path), *args],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            creationflags=_NO_WINDOW,
        )
        if check and result.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
        return result

    @property
    def default_branch(self) -> str:
        probe = self.run(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], check=False)
        if probe.returncode == 0 and "/" in probe.stdout.strip():
            return probe.stdout.strip().split("/", 1)[1]
        for c in ("main", "master"):
            if self.run(["show-ref", "--verify", f"refs/remotes/origin/{c}"], check=False).returncode == 0:
                return c
        return "main"

    @property
    def current_branch(self) -> str:
        return self.run(["rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()

    @property
    def has_changes(self) -> bool:
        return bool(self.run(["status", "--porcelain", "--ignore-submodules"]).stdout.strip())

    @property
    def has_remote(self) -> bool:
        return bool(self.run(["remote"], check=False).stdout.strip())

    def pull(self) -> tuple[bool, str]:
        if not self.has_remote:
            return False, "no remote"
        fetch = self.run(["fetch", "--all", "--prune"], check=False)
        if fetch.returncode != 0:
            return False, f"fetch failed: {fetch.stderr.strip()}"
        branch = self.default_branch
        local = self.run(["rev-parse", "HEAD"], check=False).stdout.strip()
        remote = self.run(["rev-parse", f"origin/{branch}"], check=False).stdout.strip()
        if local == remote:
            return False, "up to date"
        if self.has_changes:
            self.run(["stash", "push", "--include-untracked", "-m", "autosync-stash"])
            pull_r = self.run(["pull", "--rebase", "origin", branch], check=False)
            pop_r = self.run(["stash", "pop"], check=False)
            if pull_r.returncode != 0:
                return False, f"pull conflict: {pull_r.stderr.strip()}"
            if pop_r.returncode != 0:
                return False, "pulled but local conflict — changes saved in stash"
        else:
            pull_r = self.run(["pull", "--rebase", "origin", branch], check=False)
            if pull_r.returncode != 0:
                self.run(["rebase", "--abort"], check=False)
                return False, f"pull conflict: {pull_r.stderr.strip()}"
        return True, "pulled new changes"

    def commit_and_push(self, message: str) -> tuple[bool, str]:
        if not self.has_changes:
            return False, "nothing to commit"
        if self.current_branch != self.default_branch:
            return False, f"not on default branch ({self.current_branch}), skipped"
        self.run(["add", "-A", "--ignore-errors"])
        staged = self.run(["diff", "--cached", "--quiet"], check=False)
        if staged.returncode == 0:
            return False, "nothing to commit"
        commit = self.run(["commit", "-m", message], check=False)
        if commit.returncode != 0:
            detail = (commit.stdout + commit.stderr).strip()
            if "nothing to commit" in detail.lower():
                return False, "nothing to commit"
            return False, f"commit failed: {detail}"
        if not self.has_remote:
            return True, "committed (no remote)"
        push = self.run(["push", "origin", self.default_branch], check=False)
        if push.returncode != 0:
            return False, f"push failed: {push.stderr.strip()}"
        return True, "committed and pushed"


# ══════════════════════════════════════════════════════
#  Daemon
# ══════════════════════════════════════════════════════

class AutoSync:
    def __init__(self, repo_path: Path):
        self.repo = GitRepo(repo_path)
        self.config = dict(DEFAULT_CONFIG)
        self.logger = self._setup_logger()
        self._change_detected = threading.Event()
        self._last_change_time: float = 0
        self._lock = threading.Lock()
        self._running = False

    def _log_dir(self) -> Path:
        d = self.repo.path / ".git" / "autosync-logs"
        d.mkdir(exist_ok=True)
        return d

    def _setup_logger(self) -> logging.Logger:
        logger = logging.getLogger(f"autosync-{self.repo.path.name}")
        logger.setLevel(logging.INFO)
        if not logger.handlers:
            try:
                utf8_stream = io.TextIOWrapper(
                    sys.stdout.buffer, encoding="utf-8",
                    errors="replace", line_buffering=True,
                )
            except AttributeError:
                utf8_stream = sys.stdout
            sh = logging.StreamHandler(utf8_stream)
            sh.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", datefmt="%H:%M:%S"))
            logger.addHandler(sh)
            fh = logging.FileHandler(
                self._log_dir() / "autosync.log",
                encoding="utf-8", mode="a",
            )
            fh.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
            logger.addHandler(fh)
        return logger

    def _get_pull_interval(self) -> int:
        state_file = self._log_dir() / "graduation.json"
        try:
            if state_file.exists():
                state = json.loads(state_file.read_text(encoding="utf-8"))
                if state.get("graduated"):
                    return self.config["graduated_pull_interval"]
        except Exception:
            pass
        return self.config["pull_interval_seconds"]

    def _on_file_change(self):
        self._last_change_time = time.time()
        self._change_detected.set()

    def _debounce_and_push(self):
        debounce = self.config["debounce_seconds"]
        while self._running:
            self._change_detected.wait(timeout=1)
            if not self._change_detected.is_set():
                continue
            elapsed = time.time() - self._last_change_time
            if elapsed < debounce:
                time.sleep(debounce - elapsed)
                if time.time() - self._last_change_time < debounce:
                    continue
            self._change_detected.clear()
            with self._lock:
                try:
                    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
                    msg = f"{self.config['commit_message_prefix']}: {stamp}"
                    ok, desc = self.repo.commit_and_push(msg)
                    if ok:
                        self.logger.info(f"↑ {desc}")
                    elif "nothing" not in desc:
                        self.logger.warning(f"⚠ push: {desc}")
                        # push rejected → pull immediately and retry
                        if "push failed" in desc or "non-fast-forward" in desc:
                            try:
                                pulled, pdesc = self.repo.pull()
                                if pulled:
                                    self.logger.info(f"↓ {pdesc}")
                                ok2, desc2 = self.repo.commit_and_push(msg)
                                if ok2:
                                    self.logger.info(f"↑ retry: {desc2}")
                            except Exception as pe:
                                self.logger.error(f"✗ pull-retry error: {pe}")
                except Exception as e:
                    self.logger.error(f"✗ push error: {e}")

    def _pull_loop(self):
        while self._running:
            interval = self._get_pull_interval()
            time.sleep(interval)
            with self._lock:
                try:
                    ok, desc = self.repo.pull()
                    if ok:
                        self.logger.info(f"↓ {desc}")
                except Exception as e:
                    self.logger.error(f"✗ pull error: {e}")

    def run_once(self):
        self.logger.info(f"one-time sync: {self.repo.path.name}")
        ok, desc = self.repo.pull()
        self.logger.info(f"↓ {desc}")
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        ok, desc = self.repo.commit_and_push(f"{self.config['commit_message_prefix']}: {stamp}")
        self.logger.info(f"↑ {desc}")

    def start(self):
        Observer, FileSystemEventHandler, FileSystemEvent = ensure_watchdog()

        class _Handler(FileSystemEventHandler):
            def __init__(inner, callback, ignore):
                super().__init__()
                inner._callback = callback
                inner._ignore = ignore

            def on_any_event(inner, event):
                if event.is_directory:
                    return
                parts = Path(event.src_path).parts
                name = Path(event.src_path).name
                for p in inner._ignore:
                    if p.startswith("*") and event.src_path.endswith(p[1:]):
                        return
                    elif p.startswith("~$") and name.startswith("~$"):
                        return
                    elif p in parts or name == p:
                        return
                inner._callback()

        self._running = True

        pid_file = self._log_dir() / "autosync.pid"
        pid_file.write_text(str(os.getpid()), encoding="utf-8")

        pull_interval = self._get_pull_interval()
        mode = "graduated" if pull_interval <= 120 else "probation"
        self.logger.info(f"▶ start: {self.repo.path.name} | pull: {pull_interval}s | {mode}")

        # Initial sync
        try:
            with self._lock:
                self.repo.pull()
                stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
                ok, desc = self.repo.commit_and_push(f"{self.config['commit_message_prefix']}: {stamp}")
                if ok:
                    self.logger.info(f"↑ startup push: {desc}")
        except Exception as e:
            self.logger.warning(f"⚠ startup sync failed: {e}")

        handler = _Handler(self._on_file_change, self.config["ignore_patterns"])
        observer = Observer()
        observer.schedule(handler, str(self.repo.path), recursive=True)
        observer.start()

        threading.Thread(target=self._debounce_and_push, daemon=True).start()
        threading.Thread(target=self._pull_loop, daemon=True).start()

        try:
            while self._running:
                time.sleep(1)
        except KeyboardInterrupt:
            self.logger.info("■ stopped")
        finally:
            self._running = False
            observer.stop()
            observer.join()
            self.logger.info("■ exited")


# ══════════════════════════════════════════════════════
#  Health check + Graduation
# ══════════════════════════════════════════════════════

GRADUATION_DAYS = 5


def _state_path(repo: Path) -> Path:
    return repo / ".git" / "autosync-logs" / "graduation.json"


def _load_state(repo: Path) -> dict:
    p = _state_path(repo)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"graduated": False, "clean_dates": [], "last_check": None, "last_result": None}


def _save_state(repo: Path, state: dict):
    p = _state_path(repo)
    p.parent.mkdir(exist_ok=True)
    p.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _check_process(repo: Path) -> tuple[bool, str]:
    pid_file = repo / ".git" / "autosync-logs" / "autosync.pid"
    if not pid_file.exists():
        return False, "no PID file found"
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return False, "PID file corrupted"
    if os.name == "nt":
        r = subprocess.run(
            ["tasklist", "/fi", f"pid eq {pid}", "/fo", "csv", "/nh"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=_NO_WINDOW,
        )
        if str(pid) in r.stdout:
            return True, f"daemon running (PID {pid})"
    else:
        try:
            os.kill(pid, 0)
            return True, f"daemon running (PID {pid})"
        except ProcessLookupError:
            pass
        except PermissionError:
            return True, f"daemon running (PID {pid})"
    return False, f"daemon stopped (PID {pid})"


def _check_last_sync(repo: Path) -> tuple[bool, str]:
    log_file = repo / ".git" / "autosync-logs" / "autosync.log"
    if not log_file.exists():
        return False, "no sync log"
    with open(log_file, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()[-100:]
    last_ok = None
    today = datetime.now().strftime("%Y-%m-%d")
    ok_count = 0
    for line in lines:
        m = re.match(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]", line)
        if not m:
            continue
        ts, date = m.group(1), m.group(1)[:10]
        if "↑" in line or "↓" in line:
            last_ok = ts
            if date == today:
                ok_count += 1
    if not last_ok:
        return False, "no successful sync found"
    try:
        age_h = (datetime.now() - datetime.strptime(last_ok, "%Y-%m-%d %H:%M:%S")).total_seconds() / 3600
        if age_h > 2:
            return False, f"last success: {last_ok} ({age_h:.1f}h ago)"
    except ValueError:
        pass
    msg = f"last success: {last_ok}"
    if ok_count:
        msg += f" | today: {ok_count}x"
    return True, msg


def _check_remote(repo: Path) -> tuple[bool, str]:
    def git(*a):
        return subprocess.run(
            ["git", "-C", str(repo), *a],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=_NO_WINDOW,
        )
    git("fetch", "--all", "--prune")
    local = git("rev-parse", "HEAD").stdout.strip()
    # Try main first, then master
    for branch in ("main", "master"):
        r = git("rev-parse", f"origin/{branch}")
        if r.returncode == 0:
            remote = r.stdout.strip()
            break
    else:
        return False, "cannot determine remote branch"
    if local == remote:
        return True, f"local=remote ({local[:7]})"
    ahead = git("rev-list", "--count", f"origin/{branch}..HEAD").stdout.strip()
    behind = git("rev-list", "--count", f"HEAD..origin/{branch}").stdout.strip()
    return False, f"ahead {ahead} / behind {behind}"


def _check_pending(repo: Path) -> tuple[bool, str]:
    r = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain", "--ignore-submodules"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=_NO_WINDOW,
    )
    lines = [l for l in r.stdout.strip().split("\n") if l.strip()]
    if not lines:
        return True, "working tree clean"
    return False, f"{len(lines)} uncommitted file(s)"


def _notify(title: str, message: str):
    try:
        if os.name == "nt":
            t = title.replace('"', '`"')
            m = message.replace('"', '`"').replace("\n", "`n")
            ps = (
                "Add-Type -AssemblyName System.Windows.Forms;"
                "$n=New-Object System.Windows.Forms.NotifyIcon;"
                "$n.Icon=[System.Drawing.SystemIcons]::Information;"
                f'$n.BalloonTipTitle="{t}";'
                f'$n.BalloonTipText="{m}";'
                "$n.Visible=$true;$n.ShowBalloonTip(10000);"
                "Start-Sleep 5;$n.Dispose()"
            )
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                capture_output=True, timeout=15, creationflags=_NO_WINDOW,
            )
        else:
            subprocess.run(
                ["osascript", "-e", f'display notification "{message}" with title "{title}"'],
                capture_output=True, timeout=5,
            )
    except Exception:
        pass


def run_check(repo: Path) -> bool:
    label = _repo_label(repo)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{'=' * 50}")
    print(f"  git-auto-sync health check  {now_str}")
    print(f"  repo: {repo}")
    print(f"{'=' * 50}\n")

    checks = [
        ("daemon", _check_process(repo)),
        ("last sync", _check_last_sync(repo)),
        ("remote", _check_remote(repo)),
        ("pending", _check_pending(repo)),
    ]

    all_ok = True
    for name, (ok, msg) in checks:
        print(f"  {'✅' if ok else '❌'} {name}: {msg}")
        if not ok:
            all_ok = False

    # Graduation logic
    state = _load_state(repo)
    today = datetime.now().strftime("%Y-%m-%d")
    if state["graduated"]:
        grad_msg = "graduated (high-frequency: every 2 min)"
    else:
        clean = state.get("clean_dates", [])
        cutoff = (datetime.now() - timedelta(days=GRADUATION_DAYS + 2)).strftime("%Y-%m-%d")
        if all_ok:
            if today not in clean:
                clean.append(today)
        else:
            clean = []
        clean = sorted(d for d in clean if d >= cutoff)
        state["clean_dates"] = clean
        if len(clean) >= GRADUATION_DAYS:
            state["graduated"] = True
            grad_msg = f"🎓 Graduated! {len(clean)} consecutive clean days → 2 min pull"
        else:
            grad_msg = f"probation: {len(clean)}/{GRADUATION_DAYS} clean days"

    state["last_check"] = now_str
    state["last_result"] = "healthy" if all_ok else "unhealthy"
    _save_state(repo, state)

    print(f"\n  📊 {grad_msg}\n")

    # Only notify on failure
    if not all_ok:
        fails = [msg for _, (ok, msg) in checks if not ok]
        _notify(f"⚠ {label} sync issue", "\n".join(fails))

    # Warning file
    wf = repo / "⚠sync-error.md"
    if all_ok:
        if wf.exists():
            wf.unlink()
    else:
        content = f"# ⚠ Auto-sync error\n\nChecked: {now_str}\n\n"
        for name, (ok, msg) in checks:
            if not ok:
                content += f"- **{name}**: {msg}\n"
        content += f"\n{grad_msg}\n\n---\n*This file disappears automatically when the issue is resolved.*\n"
        wf.write_text(content, encoding="utf-8")

    return all_ok


# ══════════════════════════════════════════════════════
#  Install / Uninstall (system-level, editor-independent)
# ══════════════════════════════════════════════════════

def _task_names(repo: Path) -> tuple[str, str]:
    label = _repo_label(repo)
    return f"GitAutoSync-{label}", f"GitAutoSync-{label}-check"


def _find_pythonw() -> Path:
    """Find pythonw.exe for silent execution. Prefers venv if present."""
    venv_dir = SCRIPT_PATH.parent / ".venv"
    if venv_dir.exists():
        venv_pw = venv_dir / "Scripts" / "pythonw.exe"
        if venv_pw.exists():
            return venv_pw
        venv_p = venv_dir / "Scripts" / "python.exe"
        if venv_p.exists():
            return venv_p
    python = Path(sys.executable).resolve()
    pythonw = python.parent / "pythonw.exe"
    if pythonw.exists():
        return pythonw
    return python


def _pre_install_env():
    """Pre-install: ensure watchdog is available, create venv if pip is locked."""
    try:
        import watchdog  # noqa: F401
        return
    except ImportError:
        pass
    if _pip_is_locked():
        venv_dir = _ensure_venv()
        venv_pip = venv_dir / ("Scripts" if os.name == "nt" else "bin") / "pip"
        if os.name == "nt":
            venv_pip = venv_pip.with_suffix(".exe")
        subprocess.run(
            [str(venv_pip), "install", "watchdog", "-q"],
            check=True, creationflags=_NO_WINDOW,
        )
        print("   env: PEP 668 detected, created .venv and installed watchdog")
    else:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "watchdog", "-q"],
            check=True, creationflags=_NO_WINDOW,
        )


def _install_win(repo: Path):
    _pre_install_env()
    pythonw = _find_pythonw()
    sync_task, check_task = _task_names(repo)

    # Register daemon (every 5 min, script deduplicates via PID)
    sync_cmd = f'"{pythonw}" "{SCRIPT_PATH}" run --repo "{repo}"'
    subprocess.run(["schtasks", "/Delete", "/TN", sync_task, "/F"],
                   capture_output=True, creationflags=_NO_WINDOW)
    r = subprocess.run(
        ["schtasks", "/Create", "/TN", sync_task, "/TR", sync_cmd,
         "/SC", "MINUTE", "/MO", "5", "/F"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=_NO_WINDOW,
    )
    if r.returncode != 0:
        print(f"❌ Failed to register daemon: {r.stderr.strip()}")
        return False

    # Register daily health check
    subprocess.run(["schtasks", "/Delete", "/TN", check_task, "/F"],
                   capture_output=True, creationflags=_NO_WINDOW)
    check_cmd = f'"{pythonw}" "{SCRIPT_PATH}" check --repo "{repo}"'
    r2 = subprocess.run(
        ["schtasks", "/Create", "/TN", check_task, "/TR", check_cmd,
         "/SC", "DAILY", "/ST", "09:00", "/F"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=_NO_WINDOW,
    )
    if r2.returncode != 0:
        print(f"⚠ Failed to register health check: {r2.stderr.strip()}")

    # Kill old process, start new one
    pid_file = repo / ".git" / "autosync-logs" / "autosync.pid"
    if pid_file.exists():
        try:
            old_pid = int(pid_file.read_text(encoding="utf-8").strip())
            subprocess.run(["taskkill", "/PID", str(old_pid), "/F"],
                           capture_output=True, creationflags=_NO_WINDOW)
        except (ValueError, OSError):
            pass

    subprocess.Popen(
        [str(pythonw), str(SCRIPT_PATH), "run", "--repo", str(repo)],
        creationflags=0x08000000,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    print("✅ Installed")
    print("   daemon: kept alive every 5 min (max 5 min recovery)")
    print("   health check: daily at 9:00")
    print("   daemon started in background")
    return True


def _install_mac(repo: Path):
    _pre_install_env()
    venv_dir = SCRIPT_PATH.parent / ".venv"
    venv_py = venv_dir / "bin" / "python"
    python = venv_py if venv_py.exists() else Path(sys.executable).resolve()
    label = _repo_label(repo)
    log_dir = Path.home() / "Library" / "Logs" / f"git-auto-sync-{label}"
    log_dir.mkdir(parents=True, exist_ok=True)
    agents_dir = Path.home() / "Library" / "LaunchAgents"
    agents_dir.mkdir(parents=True, exist_ok=True)

    label_sync = f"com.git-auto-sync.{label}"
    plist_sync = agents_dir / f"{label_sync}.plist"
    plist_sync_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>{label_sync}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python}</string>
        <string>{SCRIPT_PATH}</string>
        <string>run</string>
        <string>--repo</string>
        <string>{repo}</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>{log_dir}/stdout.log</string>
    <key>StandardErrorPath</key><string>{log_dir}/stderr.log</string>
    <key>WorkingDirectory</key><string>{repo}</string>
</dict>
</plist>"""

    label_check = f"com.git-auto-sync.{label}.check"
    plist_check = agents_dir / f"{label_check}.plist"
    plist_check_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>{label_check}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python}</string>
        <string>{SCRIPT_PATH}</string>
        <string>check</string>
        <string>--repo</string>
        <string>{repo}</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key><integer>9</integer>
        <key>Minute</key><integer>0</integer>
    </dict>
    <key>StandardOutPath</key><string>{log_dir}/check.log</string>
    <key>StandardErrorPath</key><string>{log_dir}/check-err.log</string>
    <key>WorkingDirectory</key><string>{repo}</string>
</dict>
</plist>"""

    for plist_path, content in [(plist_sync, plist_sync_content), (plist_check, plist_check_content)]:
        subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
        plist_path.write_text(content, encoding="utf-8")
        subprocess.run(["launchctl", "load", str(plist_path)], capture_output=True)

    print("✅ Installed")
    print("   daemon: auto-start + crash recovery (KeepAlive)")
    print("   health check: daily at 9:00")
    print(f"   logs: {log_dir}")
    return True


def _uninstall_win(repo: Path):
    sync_task, check_task = _task_names(repo)
    for name in (sync_task, check_task):
        subprocess.run(["schtasks", "/Delete", "/TN", name, "/F"],
                       capture_output=True, creationflags=_NO_WINDOW)
    # Also clean up legacy task names
    for name in ("Holo自动同步", "Holo同步健康检查", "GitAutoSync"):
        subprocess.run(["schtasks", "/Delete", "/TN", name, "/F"],
                       capture_output=True, creationflags=_NO_WINDOW)
    pid_file = repo / ".git" / "autosync-logs" / "autosync.pid"
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True, creationflags=_NO_WINDOW)
        except (ValueError, OSError):
            pass
    print("✅ Uninstalled")


def _uninstall_mac(repo: Path):
    label = _repo_label(repo)
    for suffix in ("", ".check"):
        plist = Path.home() / "Library" / "LaunchAgents" / f"com.git-auto-sync.{label}{suffix}.plist"
        if plist.exists():
            subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)
            plist.unlink()
    print("✅ Uninstalled")


def show_status(repo: Path):
    label = _repo_label(repo)
    sync_task, check_task = _task_names(repo)
    print(f"{'=' * 50}")
    print(f"  git-auto-sync status — {label}")
    print(f"{'=' * 50}\n")

    ok, msg = _check_process(repo)
    print(f"  {'✅' if ok else '❌'} {msg}")

    if os.name == "nt":
        for name in (sync_task, check_task):
            r = subprocess.run(
                ["schtasks", "/Query", "/TN", name, "/FO", "LIST"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                creationflags=_NO_WINDOW,
            )
            status = "registered" if r.returncode == 0 else "not registered"
            print(f"  {'✅' if r.returncode == 0 else '❌'} task [{name}]: {status}")

    state = _load_state(repo)
    grad = "graduated (2 min pull)" if state.get("graduated") else f"probation ({len(state.get('clean_dates', []))}/{GRADUATION_DAYS} days)"
    print(f"  📊 {grad}")
    if state.get("last_check"):
        print(f"  📅 last check: {state['last_check']} → {state.get('last_result', '?')}")
    print()


# ══════════════════════════════════════════════════════
#  Entry point
# ══════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="git-auto-sync — automatic Git sync daemon",
    )
    parser.add_argument(
        "action", nargs="?", default="install",
        choices=["install", "uninstall", "run", "once", "check", "status"],
        help="install|uninstall|run|once|check|status (default: install)",
    )
    parser.add_argument("--repo", type=Path, help="repo path (auto-detected if omitted)")
    args = parser.parse_args()

    repo = (args.repo or find_repo_root()).resolve()

    if args.action == "install":
        if os.name == "nt":
            _install_win(repo)
        else:
            _install_mac(repo)
        if len(sys.argv) == 1 and sys.stdin and sys.stdin.isatty():
            input("\nPress Enter to close...")

    elif args.action == "uninstall":
        if os.name == "nt":
            _uninstall_win(repo)
        else:
            _uninstall_mac(repo)

    elif args.action == "run":
        pid_file = repo / ".git" / "autosync-logs" / "autosync.pid"
        if pid_file.exists():
            try:
                old_pid = int(pid_file.read_text(encoding="utf-8").strip())
                if old_pid != os.getpid():
                    if os.name == "nt":
                        r = subprocess.run(
                            ["tasklist", "/fi", f"pid eq {old_pid}", "/fo", "csv", "/nh"],
                            capture_output=True, text=True, encoding="utf-8", errors="replace",
                            creationflags=_NO_WINDOW,
                        )
                        if str(old_pid) in r.stdout:
                            sys.exit(0)
                    else:
                        try:
                            os.kill(old_pid, 0)
                            sys.exit(0)
                        except ProcessLookupError:
                            pass
                        except PermissionError:
                            sys.exit(0)
            except (ValueError, OSError):
                pass

        backoff = 5
        while True:
            try:
                sync = AutoSync(repo)
                sync.start()
                break
            except KeyboardInterrupt:
                break
            except Exception as e:
                print(f"[crash] {e}, retrying in {backoff}s...", file=sys.stderr, flush=True)
                time.sleep(backoff)
                backoff = min(backoff * 2, 300)

    elif args.action == "once":
        sync = AutoSync(repo)
        sync.run_once()

    elif args.action == "check":
        ok = run_check(repo)
        sys.exit(0 if ok else 1)

    elif args.action == "status":
        show_status(repo)


if __name__ == "__main__":
    main()
