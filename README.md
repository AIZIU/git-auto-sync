# git-auto-sync

**One file. One command. Your Git repo syncs itself.**

Drop `sync.py` into any Git repo, run it, done. Files are committed and pushed automatically on save, pulled from remote on a schedule. Works on Windows and Mac. No config files, no dependencies beyond Python and Git.

## Why

| You want | Existing options | This |
|----------|-----------------|------|
| Dropbox-like sync for Git repos | gitwatch (Linux only, needs inotifywait) | ✅ Windows + Mac |
| Auto-commit on file save | git-sync (no file watcher, manual/cron only) | ✅ Real-time file watcher |
| Set and forget | Most tools need systemd/cron/launchd setup | ✅ One command installs everything |
| No editor lock-in | VS Code extensions die with the editor | ✅ System-level daemon, survives reboots |

## Install

```bash
# Clone or download sync.py into your repo, then:
python sync.py
```

That's it. On Windows you can literally right-click → "Run with Python".

### What happens

- **Windows**: Registers two Scheduled Tasks (daemon + daily health check), uses `pythonw.exe` for zero UI flicker
- **Mac**: Creates two LaunchAgents (daemon with `KeepAlive` + daily health check)
- **Daemon starts immediately** and runs in the background

### Requirements

- Python 3.10+
- Git
- `watchdog` (auto-installed on first run)

Works with **any** Python installation — python.org, [uv](https://docs.astral.sh/uv/), Homebrew, pyenv. If your Python has PEP 668 restrictions (externally managed), the script auto-creates a local `.venv` and handles everything transparently.

## How it works

```
File saved → 5s debounce → git add -A → git commit → git push
                          ↑ runs silently in background
Remote changes → git pull --rebase (every 30 min → 2 min after graduation)
Night quiet hours → scheduled pulls pause from 23:00 to 05:00
```

### Graduation

The daemon starts in **probation mode** (pulls every 30 minutes). After 24 cumulative healthy online hours, it **graduates** to high-frequency mode (pulls every 2 minutes). Scheduled pulls pause from 23:00 to 05:00; file-save pushes still work if you happen to be working late.

### Conflict handling

- Local changes + remote changes → `git stash` → `git pull --rebase` → `git stash pop`
- Push rejected (another PC pushed first) → automatic pull + retry push
- If rebase fails → aborts cleanly, notifies you
- Only operates on the default branch (main/master)

## Commands

| Command | What it does |
|---------|-------------|
| `python sync.py` | Install (default action) |
| `python sync.py install` | Same as above |
| `python sync.py uninstall` | Remove all scheduled tasks/agents and stop daemon |
| `python sync.py status` | Show daemon status, task registration, graduation progress |
| `python sync.py check` | Run health check (daemon alive? synced? clean?) |
| `python sync.py once` | Pull + commit + push once, then exit |
| `python sync.py run` | Start daemon (called by system service, not for manual use) |

## Health checks

- **Daily at 9:00** — automatic, silent when healthy
- **Notification only on failure** — Windows balloon tip or macOS notification
- **Daemon heartbeat** — idle repos stay healthy even when there is nothing new to commit or pull
- **Cumulative graduation** — counts healthy online hours, not calendar days
- **Warning file** — `⚠sync-error.md` appears in repo root when unhealthy, disappears when fixed
- 4 checks: daemon alive, recent sync, local=remote, no pending files

## Multi-repo

Task names are derived from the repo folder name (`GitAutoSync-{folder}`), so you can install in multiple repos without conflicts.

## Storage

Sync state lives inside `.git/` — no files added to your working tree:

```
.git/autosync-logs/
├── autosync.log        # sync log
├── autosync.pid        # daemon PID
├── heartbeat.json      # last successful daemon activity
└── graduation.json     # healthy hours + graduation state
```

> If your Python is PEP 668 restricted (uv, Homebrew), a `.venv/` directory is created next to `sync.py` for the `watchdog` dependency. It's automatically excluded from Git tracking via `.git/info/exclude`.

## Uninstall

```bash
python sync.py uninstall
```

Removes scheduled tasks/launch agents and stops the daemon. Your repo is untouched.

## FAQ

**Q: Will it commit secrets/credentials?**
A: It commits everything that `git add -A` would. Use `.gitignore` to exclude sensitive files — same as you would with manual Git.

**Q: What if two machines push at the same time?**
A: The second push will fail, trigger a `pull --rebase` on next cycle, then push again. Designed for low-contention workflows (notes, configs, content).

**Q: Does it work with GitHub/GitLab/Bitbucket?**
A: Any Git remote. Just make sure your SSH key or credential helper is configured.

**Q: Can I use it for code projects?**
A: You can, but it's designed for content repos where atomic commits don't matter. For code, consider proper CI/CD.

## License

MIT
