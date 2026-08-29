"""ATLAS Oracle Tracker — GitHub Actions runner.

One job = one ~4h session:
  1. clone the private engine repo (token via header auth, never stored)
  2. run the model loop (fetch candles -> score -> paper/live decisions)
  3. persist the ledger back to the private repo
  4. render the public oracle showcase (charts + README) and push it
  5. self-dispatch the next session

Printed output is restricted to progress summaries; model internals,
config contents and secrets never reach the Actions log.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

REPO = os.environ.get("GITHUB_REPOSITORY", "")
GH_TOKEN = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN", "")
ATHENA_TOKEN = os.environ.get("ATHENA_TOKEN", "")
ATHENA_REPO = os.environ.get("ATHENA_REPO", "")
API = "https://api.github.com"

PUBLIC_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENGINE_DIR = "/tmp/engine_run"


def log(msg: str) -> None:
    print(msg, flush=True)


def _secret_mask() -> None:
    for name in ("ATHENA_TOKEN", "GH_TOKEN", "GITHUB_TOKEN",
                 "HL_MASTER_ADDR", "HL_MASTER_SECRET"):
        val = os.environ.get(name, "")
        if val:
            print(f"::add-mask::{val}")


def api(path: str, method: str = "GET", body: dict | None = None,
        token: str = "") -> tuple[int, dict | list]:
    req = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Authorization": f"token {token}",
                 "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
            return resp.status, (json.loads(data) if data else {})
    except urllib.error.HTTPError as e:
        return e.code, {}


def already_running() -> bool:
    """True if another workflow run is active or queued right now."""
    status, data = api(
        f"/repos/{REPO}/actions/runs?per_page=10", token=GH_TOKEN)
    if status != 200:
        return False
    me = os.environ.get("GITHUB_RUN_ID", "")
    for run in data.get("workflow_runs", []):
        if str(run.get("id")) == me:
            continue
        if run.get("status") in ("in_progress", "queued", "waiting"):
            log("[runner] another session is already alive — exiting")
            return True
    return False


def clone_engine() -> bool:
    auth = base64.b64encode(
        f"x-access-token:{ATHENA_TOKEN}".encode()).decode()
    env = dict(os.environ,
               GIT_TERMINAL_PROMPT="0",
               GIT_CONFIG_COUNT="1",
               GIT_CONFIG_KEY_0="http.extraheader",
               GIT_CONFIG_VALUE_0=f"AUTHORIZATION: basic {auth}")
    try:
        subprocess.run(["rm", "-rf", ENGINE_DIR], check=True)
        subprocess.run(
            ["git", "clone", "--depth", "1", "--quiet",
             f"https://github.com/{ATHENA_REPO}.git", ENGINE_DIR],
            env=env, check=True)
        log("[runner] engine repo synced")
        reqs = os.path.join(ENGINE_DIR, "live", "requirements.txt")
        subprocess.run([sys.executable, "-m", "pip", "install", "--quiet",
                        "-r", reqs], check=True)
        log("[runner] engine deps installed")
        return True
    except Exception:
        log("[runner] engine sync failed — watchdog will retry")
        return False


def run_engine() -> int:
    cfg = json.load(open(os.path.join(ENGINE_DIR, "live", "config.json")))
    minutes = int(cfg.get("run_minutes", 215))
    proc = subprocess.Popen(
        [sys.executable, "engine.py"],
        cwd=os.path.join(ENGINE_DIR, "live"),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        env=dict(os.environ))
    assert proc.stdout is not None
    tail: list[str] = []
    for line in proc.stdout:
        line = line.rstrip()
        tail.append(line)
        tail = tail[-25:]
        if line.startswith("[engine]"):
            log(line)
    proc.wait()
    if proc.returncode != 0:
        try:
            with open(os.path.join(ENGINE_DIR, "last_tail.log"),
                      "w") as fh:
                fh.write("\n".join(tail))
        except Exception:
            pass
        log(f"[runner] engine stopped (rc={proc.returncode})")
    else:
        log("[runner] engine finished rc=0")
    return proc.returncode


def persist_state() -> None:
    state = os.path.join(ENGINE_DIR, "live", "state.json")
    if not os.path.exists(state):
        log("[runner] no state.json to persist")
        return
    env_git = _git_env(ATHENA_TOKEN)
    subprocess.run(["git", "-C", ENGINE_DIR, "add", "-f",
                    "live/state.json"], check=True)
    staged = subprocess.run(
        ["git", "-C", ENGINE_DIR, "diff", "--cached", "--quiet"],
        capture_output=True).returncode != 0
    if not staged:
        log("[runner] ledger unchanged")
        return
    subprocess.run(["git", "-C", ENGINE_DIR, "commit", "-q", "-m",
                    "state update"], env=env_git, check=True)
    for attempt in range(3):
        push = subprocess.run(
            ["git", "-C", ENGINE_DIR, "push", "--quiet", "origin", "main"],
            env=env_git, capture_output=True, text=True)
        if push.returncode == 0:
            log("[runner] ledger persisted to private repo")
            return
        log(f"[runner] push rejected (attempt {attempt + 1}) — rebasing")
        subprocess.run(["git", "-C", ENGINE_DIR, "add", "-f",
                        "live/state.json"], check=True)
        rebase = subprocess.run(
            ["git", "-C", ENGINE_DIR, "pull", "--rebase", "-q", "origin",
             "main"], env=env_git, capture_output=True, text=True)
        if rebase.returncode != 0:
            log("[runner] ledger rebase conflict — will retry next cycle")
            subprocess.run(["git", "-C", ENGINE_DIR, "rebase", "--abort"],
                           env=env_git, capture_output=True)
            return
    log("[runner] ledger push failed after retries")


def _git_env(token: str) -> dict:
    auth = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return dict(os.environ,
                GIT_TERMINAL_PROMPT="0",
                GIT_AUTHOR_NAME="oracle-bot",
                GIT_AUTHOR_EMAIL="bot@users.noreply.github.com",
                GIT_COMMITTER_NAME="oracle-bot",
                GIT_COMMITTER_EMAIL="bot@users.noreply.github.com",
                GIT_CONFIG_COUNT="1",
                GIT_CONFIG_KEY_0="http.extraheader",
                GIT_CONFIG_VALUE_0=f"AUTHORIZATION: basic {auth}")


def push_showcase() -> None:
    subprocess.run(
        ["git", "-C", PUBLIC_ROOT, "add", "-A", "README.md", "charts"],
        check=True)
    staged = subprocess.run(
        ["git", "-C", PUBLIC_ROOT, "diff", "--cached", "--quiet"],
        capture_output=True).returncode != 0
    if not staged:
        log("[runner] showcase unchanged")
        return
    subprocess.run(["git", "-C", PUBLIC_ROOT, "commit", "-q", "-m",
                    "oracle showcase update"], env=_git_env(GH_TOKEN),
                   check=True)
    subprocess.run(
        ["git", "-C", PUBLIC_ROOT, "push", "--quiet", "origin",
         os.environ.get("GITHUB_REF_NAME", "main")],
        env=_git_env(GH_TOKEN), check=True)
    log("[runner] showcase pushed")


def dispatch_next() -> None:
    status, _ = api(f"/repos/{REPO}/dispatches", method="POST",
                    body={"event_type": "tick"}, token=GH_TOKEN)
    log(f"[runner] next session dispatched (HTTP {status})")


def main() -> None:
    _secret_mask()
    event = os.environ.get("GITHUB_EVENT_NAME", "workflow_dispatch")
    log(f"[runner] session start (event={event})")
    if not ATHENA_REPO or not ATHENA_TOKEN:
        log("[runner] configuration incomplete — watchdog will retry")
        return
    if event in ("schedule", "repository_dispatch") and already_running():
        return
    if not clone_engine():
        return
    rc = run_engine()
    persist_state()
    if rc != 0:
        # engine failed: do NOT chain-respawn (runaway loop guard).
        # the */30 schedule watchdog revives the chain instead.
        log("[runner] session ended with engine failure — watchdog "
            "will retry")
        return
    db = ""
    data_dir = os.path.join(ENGINE_DIR, "data")
    if os.path.isdir(data_dir):
        cands = [os.path.join(data_dir, f) for f in os.listdir(data_dir)
                 if f.endswith(".db")]
        if cands:
            db = max(cands, key=os.path.getsize)
    if db and os.environ.get("SHOWCASE", "1") == "1":
        r = subprocess.run(
            [sys.executable, os.path.join(PUBLIC_ROOT, "runner",
                                          "showcase.py"), db],
            cwd=PUBLIC_ROOT, text=True, capture_output=True)
        if r.returncode != 0:
            log("[showcase] render failed — retrying next session")
        else:
            log("[showcase] rendered")
            push_showcase()
    dispatch_next()
    log("[runner] session complete")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        log("[runner] session aborted (internal error) — watchdog "
            "will retry")

