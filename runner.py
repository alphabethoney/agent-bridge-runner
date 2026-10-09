#!/usr/bin/env python3
"""agent-bridge-runner —— 在 Windows 上接收任务、驱动 CLI agent、把事件落盘。

设计要点：
1. 任务不挂在 SSH 会话下：Windows 的 sshd 在会话结束时会终结整棵子进程树，
   所以这里为每个任务写一个 run.cmd，交给计划任务启动，进程便独立存活。
2. 所有事件按 seq 追加到 tasks/<id>/events.jsonl，watch 只按 seq 增量取，可重入。
3. 任务文本通过标准输入以 UTF-8 JSON 传入，绝不拼进命令行。
"""
import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TASKS = ROOT / "tasks"
sys.path.insert(0, str(ROOT))
from adapters import codex as ad_codex  # noqa: E402
from adapters import dsh as ad_dsh  # noqa: E402

ADAPTERS = {"dsh": ad_dsh, "codex": ad_codex}
NEW_GROUP = 0x00000200

# 强制标准输出为 UTF-8。否则中文经 cmd 会按系统代码页编码，取事件的一端解析不了。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def now():
    return time.time()


def policy():
    return json.loads((ROOT / "policy.json").read_text(encoding="utf-8"))


_TID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def td(tid):
    tid = str(tid)
    if not _TID_RE.match(tid):
        raise ValueError(f"非法的 task_id：{tid!r}")
    return TASKS / tid


def read_json(p, default=None):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(p, obj):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(p) + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def say(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _err(r):
    try:
        return (r.stderr or r.stdout or b"").decode("utf-8", "replace").strip()
    except Exception:
        return str(r.returncode)


def check_cwd(cwd, pol):
    cwd = os.path.expandvars(str(cwd or ""))
    if not cwd:
        raise ValueError("缺少 cwd")
    p = Path(cwd).resolve()
    parts = [part.lower() for part in p.parts]
    for r in pol.get("allowed_roots", []):
        base = Path(os.path.expandvars(r)).resolve()
        bparts = [part.lower() for part in base.parts]
        if bparts and parts[:len(bparts)] == bparts:
            return str(p)
    raise ValueError(f"目录不在白名单内：{p}")


# ---------------------------------------------------------------- submit
def cmd_submit(_args):
    try:
        raw = sys.stdin.buffer.read().decode("utf-8", "replace")
        req = json.loads(raw or "{}")
    except Exception as e:
        say({"ok": False, "error": f"请求不是合法 JSON：{e}"})
        return 1
    pol = policy()
    agent = str(req.get("agent") or "dsh")
    if agent not in pol.get("agents", {}):
        say({"ok": False, "error": f"未知的 agent：{agent}"})
        return 1
    try:
        cwd = check_cwd(req.get("cwd"), pol)
    except ValueError as e:
        say({"ok": False, "error": str(e)})
        return 1
    tid = req.get("task_id")
    if not tid:
        tid = "t" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    tid = str(tid)
    try:
        d = td(tid)
    except ValueError as e:
        say({"ok": False, "error": str(e)})
        return 1
    d.mkdir(parents=True, exist_ok=True)
    req.update({"task_id": tid, "agent": agent, "cwd": cwd})
    req.setdefault("timeout_s", pol.get("default_timeout_s", 600))
    write_json(d / "request.json", req)
    write_json(d / "state.json", {"status": "queued", "created": now()})
    (d / "events.jsonl").write_text("", encoding="utf-8")

    runcmd = d / "run.cmd"
    lines = [
        "@echo off",
        "chcp 65001 >nul",
        "set PYTHONUTF8=1",
        f'cd /d "{ROOT}"',
        f'"{sys.executable}" "{ROOT / "runner.py"}" _run {tid} >> "{d / "worker.log"}" 2>&1',
    ]
    runcmd.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")

    tn = f"qqagent-{tid}"
    cr = subprocess.run(["schtasks", "/create", "/f", "/tn", tn, "/tr", str(runcmd),
                         "/sc", "once", "/st", "00:00"], capture_output=True)
    if cr.returncode != 0:
        say({"ok": False, "error": f"计划任务创建失败：{_err(cr)}"})
        return 1
    rr = subprocess.run(["schtasks", "/run", "/tn", tn], capture_output=True)
    if rr.returncode != 0:
        subprocess.run(["schtasks", "/delete", "/f", "/tn", tn], capture_output=True)
        say({"ok": False, "error": f"计划任务启动失败：{_err(rr)}"})
        return 1
    # 等任务进程真正起来再删除，否则可能在它被调度之前就把任务定义删掉
    deadline = now() + 10
    while now() < deadline and not (d / "worker.log").exists():
        time.sleep(0.1)
    subprocess.run(["schtasks", "/delete", "/f", "/tn", tn], capture_output=True)
    say({"ok": True, "task_id": tid})
    return 0


# ---------------------------------------------------------------- worker
def make_emitter(d):
    f = open(d / "events.jsonl", "a", encoding="utf-8")
    seq = 0

    def emit(etype, text="", **meta):
        nonlocal seq
        seq += 1
        rec = {"seq": seq, "ts": now(), "type": etype, "text": text, "meta": meta}
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        f.flush()
        return rec

    return emit


def cmd_run(args):
    tid = args.task_id
    d = td(tid)
    req = read_json(d / "request.json")
    if not req:
        say({"ok": False, "error": "找不到 request.json"})
        return 1
    pol = policy()
    spec = pol["agents"][req["agent"]]
    ad = ADAPTERS.get(spec.get("kind") or req["agent"], ad_dsh)
    emit = make_emitter(d)
    started = now()
    emit("started", f"{req['agent']} 开始执行", cwd=req["cwd"])
    write_json(d / "state.json", {"status": "running", "started": started})

    cmd = [os.path.expandvars(spec["exe"])] + list(spec.get("args", []))
    if req.get("session_id") and spec.get("kind") == "dsh":
        cmd += ["--session-id", str(req["session_id"])]
    cmd.append(str(req.get("prompt") or ""))

    env = dict(os.environ)
    if spec.get("kind") == "dsh":
        mode = str(req.get("permission") or spec.get("default_permission") or "workspace-write")
        if mode not in ("read-only", "workspace-write", "danger-full-access"):
            mode = "workspace-write"
        env["DSH_PERMISSION_MODE"] = mode
        emit("permission", mode)

    errf = open(d / "stderr.log", "a", encoding="utf-8", errors="replace")
    try:
        proc = subprocess.Popen(
            cmd, cwd=req["cwd"], stdout=subprocess.PIPE, stderr=errf,
            stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
            errors="replace", bufsize=1, creationflags=NEW_GROUP, env=env,
        )
    except Exception as e:
        emit("error", f"启动失败：{e}")
        write_json(d / "state.json", {"status": "failed", "error": str(e)})
        return 1

    write_json(d / "state.json", {"status": "running", "pid": proc.pid, "started": started})
    timed_out = {"v": False}

    def _kill():
        timed_out["v"] = True
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)

    timer = threading.Timer(int(req.get("timeout_s") or 600), _kill)
    timer.daemon = True
    timer.start()

    final_text, buf = "", []
    try:
        for line in proc.stdout:
            ev = ad.parse(line)
            if not ev:
                continue
            etype, text, meta = ev
            if etype == "text":
                buf.append(text)
            emit(etype, text, **meta)
        code = proc.wait()
    finally:
        timer.cancel()
        errf.close()

    if timed_out["v"]:
        emit("error", f"超时（{req.get('timeout_s')} 秒），进程已终止")
    if buf:
        final_text = "\n".join(buf)
    (d / "result.txt").write_text(final_text, encoding="utf-8")
    status = "timed_out" if timed_out["v"] else ("succeeded" if code == 0 else "failed")
    emit("exit", f"进程退出码 {code}", code=code)
    write_json(d / "state.json", {"status": status, "exit_code": code,
                                  "started": started, "ended": now(),
                                  "session_id": req.get("session_id"),
                                  "final": final_text[:2000]})
    return 0


# ---------------------------------------------------------------- watch / cancel / list
def cmd_watch(args):
    f = td(args.task_id) / "events.jsonl"
    if not f.exists():
        say({"ok": False, "error": "任务不存在"})
        return 1
    for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if int(rec.get("seq", 0)) > args.after:
            say(rec)
    say({"type": "_state", "seq": 10 ** 9,
         "state": read_json(td(args.task_id) / "state.json", {})})
    return 0


def cmd_cancel(args):
    st = read_json(td(args.task_id) / "state.json", {}) or {}
    pid = st.get("pid")
    if pid:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
    write_json(td(args.task_id) / "state.json",
               {**st, "status": "cancelled", "ended": now()})
    say({"ok": True, "task_id": args.task_id})
    return 0


def cmd_list(_args):
    out = []
    if TASKS.exists():
        for d in sorted(TASKS.iterdir(), reverse=True)[:50]:
            st = read_json(d / "state.json", {}) or {}
            req = read_json(d / "request.json", {}) or {}
            out.append({"task_id": d.name, "agent": req.get("agent"),
                        "cwd": req.get("cwd"), "status": st.get("status"),
                        "prompt": (req.get("prompt") or "")[:60]})
    say({"ok": True, "tasks": out})
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("submit").set_defaults(fn=cmd_submit)
    p_run = sub.add_parser("_run")
    p_run.add_argument("task_id")
    p_run.set_defaults(fn=cmd_run)
    p_w = sub.add_parser("watch")
    p_w.add_argument("task_id")
    p_w.add_argument("--after", type=int, default=0)
    p_w.set_defaults(fn=cmd_watch)
    p_c = sub.add_parser("cancel")
    p_c.add_argument("task_id")
    p_c.set_defaults(fn=cmd_cancel)
    sub.add_parser("list").set_defaults(fn=cmd_list)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
