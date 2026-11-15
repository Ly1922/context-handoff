#!/usr/bin/env python3
"""context-handoff: survive Antigravity auto-compaction.

Antigravity has no PreCompact hook, so this script watches the live transcript
from lifecycle hooks instead:

  pre      PreInvocation hook. Estimates context usage; at the soft/hard
           threshold it asks the agent to update its handoff note, and right
           after a compaction it tells the agent to read the note back.
  stop     Stop hook. After a long turn with a stale note, asks for one
           refresh (once per turn).
  session  SessionStart hook. Points a new conversation at recent notes.
  calibrate [brain_dir]   Learn compaction thresholds from old transcripts.
  status   <conversationId>   Print the tracked state (debugging).

Hooks must never break the agent loop, so every entry point fails open: on any
error it prints "{}" and exits 0.
"""
import datetime
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

DEFAULT_CFG = {
    "enabled": True,
    "notes_dirname": ".handoff",          # created inside the first workspace
    "soft_ratio": 0.65,                    # ask for a handoff note update
    "hard_ratio": 0.80,                    # insist, repeat until the note is fresh
    "hard_repeat_every": 3,                # invocations between hard reminders
    "default_compaction_tokens": 230000,   # used until calibration data exists
    "default_compaction_bytes": 500000,    # transcript bytes, fallback signal
    "bytes_per_token": 3.0,
    "stop_refresh": True,
    "stop_min_ratio": 0.40,
    "stop_min_tool_calls": 8,
    "session_hint_days": 7,
    "session_hint_max": 5,
    "auto_snapshot_min_tool_calls": 5,
}

EDIT_TOOLS = {"write_file", "edit_file", "write_to_file", "replace_file_content",
              "multi_replace_file_content", "code_action"}
PATH_KEYS = ("Path", "TargetFile", "AbsolutePath", "path", "file_path")
COMPACTION_MARK = "Resuming from a compaction"


# ---------------------------------------------------------------- utilities
def load_cfg():
    cfg = dict(DEFAULT_CFG)
    try:
        with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    return cfg


def data_dir():
    d = os.environ.get("PLUGIN_DATA") or os.path.join(ROOT, ".state")
    os.makedirs(os.path.join(d, "conv"), exist_ok=True)
    return d


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def out(obj):
    sys.stdout.buffer.write(json.dumps(obj, ensure_ascii=True).encode("ascii"))
    sys.stdout.flush()


def to_int(v):
    try:
        return int(v)
    except Exception:
        return 0


def iso_to_epoch(s):
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def short(s, n):
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


_SECRET_PATTERNS = [
    (re.compile(r"(sk-|sk_live_|ghp_|gho_|github_pat_|xox[bpas]-|AKIA|AIza|hf_)[A-Za-z0-9_\-]{8,}"), "[REDACTED]"),
    (re.compile(r"(?i)((?:password|passwd|pwd|token|secret|api[_-]?key)\s*[:=]\s*)\S+"), r"\1[REDACTED]"),
    (re.compile(r"((?:密码|口令|密钥)(?:是|为)?\s*[:=：]?\s*)[\x21-\x7e]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{12,}"), r"\1[REDACTED]"),
    (re.compile(r"(://[^/\s:@]+:)[^@\s/]+@"), r"\1[REDACTED]@"),
]


def redact(s):
    """Snapshots land in the workspace (maybe a git repo): mask obvious secrets."""
    for pat, rep in _SECRET_PATTERNS:
        s = pat.sub(rep, s)
    # a bare one-word message mixing letters and digits is probably a credential
    if re.fullmatch(r"(?=\S*[A-Za-z])(?=\S*\d)\S{8,64}", s.strip()) and "/" not in s and "\\" not in s:
        return "[REDACTED: 疑似凭据]"
    return s


def push_unique(lst, item, keep):
    if item in lst:
        lst.remove(item)
    lst.append(item)
    del lst[:-keep]


# ---------------------------------------------------------------- transcript
def transcript_file(payload):
    p = payload.get("transcriptPath") or ""
    full = os.path.join(os.path.dirname(p), "transcript_full.jsonl") if p else ""
    if full and os.path.exists(full):
        return full
    return p if p and os.path.exists(p) else ""


def new_state():
    return {
        "offset": 0, "initial": True, "last_step": -1,
        "bytes_since_ckpt": 0, "bytes_since_resp": 0, "ctx_tokens": 0,
        "last_ckpt_step": -1, "compactions": 0, "pending_compaction": False,
        "requests": [], "files": [], "commands": [], "last_model_msg": "",
        "tool_calls_total": 0, "turn_tool_calls": 0, "turn_start_ts": 0.0,
        "warned": [], "warn_ts": 0.0, "last_hard_inv": -99, "inv": 0,
        "notes_path": "",
    }


def record_calibration(tokens, nbytes, model):
    path = os.path.join(data_dir(), "calibration.json")
    cal = read_json(path, {"tokens": [], "bytes": [], "by_model": {}})
    if tokens > 50000:
        cal["tokens"] = (cal.get("tokens", []) + [tokens])[-30:]
        if model:
            bm = cal.setdefault("by_model", {})
            bm[model] = (bm.get(model, []) + [tokens])[-30:]
    if nbytes > 100000:
        cal["bytes"] = (cal.get("bytes", []) + [nbytes])[-30:]
    write_json(path, cal)


def process_step(st, d, nbytes, model, learn=True):
    typ = d.get("type", "")
    step = to_int(d.get("step_index", -1))
    st["last_step"] = max(st["last_step"], step)
    content = d.get("content") or ""

    if typ == "CHECKPOINT" and COMPACTION_MARK in content[:200]:
        if step > st["last_ckpt_step"]:
            if learn:
                record_calibration(st["ctx_tokens"], st["bytes_since_ckpt"], model)
            st["last_ckpt_step"] = step
            st["compactions"] += 1
            st["pending_compaction"] = True
        st["bytes_since_ckpt"] = nbytes
        st["bytes_since_resp"] = nbytes
        st["ctx_tokens"] = 0
        st["warned"] = []
        return

    st["bytes_since_ckpt"] += nbytes
    st["bytes_since_resp"] += nbytes

    if typ == "USER_INPUT":
        m = re.search(r"<USER_REQUEST>(.*?)</USER_REQUEST>", content, re.S)
        text = (m.group(1) if m else content).strip()
        if text:
            st["requests"] = (st["requests"] + [redact(short(text, 300))])[-30:]
        st["turn_tool_calls"] = 0
        st["turn_start_ts"] = iso_to_epoch(d.get("created_at", "")) or time.time()
    elif typ == "PLANNER_RESPONSE":
        tok = (to_int(d.get("input_tokens")) + to_int(d.get("cache_read_tokens"))
               + to_int(d.get("cache_write_tokens")) + to_int(d.get("cache_creation_tokens")))
        if tok:
            st["ctx_tokens"] = tok + to_int(d.get("output_tokens"))
            st["bytes_since_resp"] = 0
        if content.strip():
            st["last_model_msg"] = redact(short(content, 800))
        for tc in d.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            name = tc.get("name", "")
            args = tc.get("args") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {}
            st["tool_calls_total"] += 1
            st["turn_tool_calls"] += 1
            if name in EDIT_TOOLS:
                for k in PATH_KEYS:
                    if args.get(k):
                        push_unique(st["files"], str(args[k]), 40)
                        break
            elif name == "run_command" and args.get("CommandLine"):
                st["commands"] = (st["commands"] + [redact(short(args["CommandLine"], 220))])[-12:]


def scan(st, path, model):
    """Incrementally consume complete lines appended since the last call."""
    if not path:
        return
    size = os.path.getsize(path)
    if size < st["offset"]:          # transcript rewritten: start over
        keep = st.get("notes_path", "")
        st.clear()
        st.update(new_state())
        st["notes_path"] = keep
    with open(path, "rb") as f:
        f.seek(st["offset"])
        data = f.read()
    end = data.rfind(b"\n")
    if end < 0:
        return
    initial = st.get("initial", False)
    for raw in data[: end + 1].split(b"\n"):
        if not raw.strip():
            continue
        try:
            d = json.loads(raw)
        except Exception:
            st["bytes_since_ckpt"] += len(raw)
            continue
        process_step(st, d, len(raw), model, learn=not initial)
    st["offset"] += end + 1
    if initial:
        # First look at an existing conversation: only announce a compaction
        # that is the most recent thing in the transcript.
        if st["pending_compaction"] and st["last_ckpt_step"] < st["last_step"] - 3:
            st["pending_compaction"] = False
        st["initial"] = False


# ---------------------------------------------------------------- thresholds
def limits(cfg, model):
    cal = read_json(os.path.join(data_dir(), "calibration.json"), {})
    toks = (cal.get("by_model", {}).get(model) or cal.get("tokens") or [])[-10:]
    byts = (cal.get("bytes") or [])[-10:]
    tok_lim = cfg.get("compaction_tokens") or (min(toks) if toks else cfg["default_compaction_tokens"])
    byt_lim = cfg.get("compaction_bytes") or (min(byts) if byts else cfg["default_compaction_bytes"])
    return tok_lim, byt_lim


def usage(st, cfg, model):
    tok_lim, byt_lim = limits(cfg, model)
    if st["ctx_tokens"]:
        est = st["ctx_tokens"] + st["bytes_since_resp"] / cfg["bytes_per_token"]
        return est / tok_lim, int(est), int(tok_lim), "tokens"
    return st["bytes_since_ckpt"] / byt_lim, st["bytes_since_ckpt"], int(byt_lim), "bytes"


# ---------------------------------------------------------------- notes
def notes_path(st, payload, cfg):
    if st.get("notes_path"):
        return st["notes_path"]
    ws = [w for w in payload.get("workspacePaths") or [] if w and os.path.isdir(w)]
    base = ws[0] if ws else (payload.get("artifactDirectoryPath") or data_dir())
    conv = payload.get("conversationId", "unknown")
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M")
    st["notes_path"] = os.path.join(base, cfg["notes_dirname"], f"{stamp}_{conv[:8]}.md")
    return st["notes_path"]


def mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def find_git_root(path):
    d = path if os.path.isdir(path) else os.path.dirname(path)
    for _ in range(40):
        if not d:
            return ""
        if os.path.exists(os.path.join(d, ".git")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return ""
        d = parent
    return ""


def run_git(root, *args, max_lines=30):
    try:
        r = subprocess.run(["git", "-C", root, *args], capture_output=True, timeout=4,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        lines = r.stdout.decode("utf-8", "replace").rstrip().splitlines()
    except Exception:
        return ""
    if len(lines) > max_lines:
        lines = lines[:max_lines] + [f"... (+{len(lines) - max_lines} 行)"]
    return "\n".join(lines)


def ensure_git_exclude(st, cfg):
    """Keep .handoff/ out of `git status` via the repo-local .git/info/exclude
    (never committed, unlike .gitignore)."""
    if not cfg.get("git_exclude", True) or st.get("git_excluded"):
        return
    st["git_excluded"] = True
    ndir = os.path.dirname(st["notes_path"])
    root = find_git_root(os.path.dirname(ndir))
    if not root or not os.path.isdir(os.path.join(root, ".git")):
        return
    rel = os.path.relpath(ndir, root).replace("\\", "/").strip("/") + "/"
    path = os.path.join(root, ".git", "info", "exclude")
    try:
        existing = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
        if rel not in existing.split("\n") and "/" + rel not in existing.split("\n"):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(("" if existing.endswith("\n") or not existing else "\n")
                        + f"# context-handoff notes\n/{rel}\n")
    except Exception:
        pass


def git_state(st, payload, force=False):
    """git status / diff --stat for repos the conversation touched.
    Cached for 60s because snapshots can be written on every invocation."""
    cache = st.get("git_cache") or {}
    if not force and time.time() - cache.get("ts", 0) < 60:
        return cache.get("repos", [])
    roots = []
    candidates = list(reversed(st["files"])) + list(payload.get("workspacePaths") or [])
    for p in candidates:
        r = find_git_root(p) if p else ""
        if r and r not in roots:
            roots.append(r)
        if len(roots) >= 3:
            break
    repos = []
    for r in roots:
        status = run_git(r, "status", "--short", "--branch")
        stat = run_git(r, "diff", "--stat", "HEAD", max_lines=15)
        repos.append([r, status, stat])
    st["git_cache"] = {"ts": time.time(), "repos": repos}
    return repos


def archive(src, dst_dir, name, keep_prefix, keep=20):
    """Copy src into history/ and prune old copies with the same prefix."""
    if not os.path.exists(src):
        return ""
    os.makedirs(dst_dir, exist_ok=True)
    dst = os.path.join(dst_dir, name)
    shutil.copy2(src, dst)
    olds = sorted(glob.glob(os.path.join(dst_dir, keep_prefix + "*")), key=mtime)
    for p in olds[:-keep]:
        try:
            os.remove(p)
        except OSError:
            pass
    return dst


def history_dir(st):
    return os.path.join(os.path.dirname(st["notes_path"]), "history")


def archive_notes_if_changed(st):
    """Every distinct version of the agent-written note is kept, so one bad
    rewrite (or a summary of a summary) never destroys the earlier detail."""
    m = mtime(st["notes_path"])
    if not m or m == st.get("notes_seen_mtime"):
        return
    st["notes_seen_mtime"] = m
    st["notes_versions"] = st.get("notes_versions", 0) + 1
    stem = os.path.basename(st["notes_path"])[:-3]
    stamp = datetime.datetime.fromtimestamp(m).strftime("%H%M%S")
    archive(st["notes_path"], history_dir(st), f"{stem}.v{st['notes_versions']:02d}_{stamp}.md", stem + ".v")


def write_auto_snapshot(st, payload, ratio, cur, lim, unit):
    """Mechanical, model-free snapshot so something survives even if the agent
    ignores every reminder."""
    path = st["notes_path"][:-3] + ".auto.md"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    ensure_git_exclude(st, load_cfg())
    lines = [
        "# 自动快照（由 context-handoff hook 生成，勿手改）",
        "",
        f"- 会话: `{payload.get('conversationId', '')}`",
        f"- 更新时间: {datetime.datetime.now():%Y-%m-%d %H:%M:%S}",
        f"- 上下文估算: {ratio:.0%}（{cur:,} / {lim:,} {unit}），已压缩 {st['compactions']} 次",
        f"- 人工交接文件: `{st['notes_path']}`",
        "", "## 用户请求（按时间）",
    ]
    lines += [f"{i}. {r}" for i, r in enumerate(st["requests"], 1)] or ["（无）"]
    lines += ["", "## 改动过的文件（最近在后）"]
    lines += [f"- `{p}`" for p in st["files"]] or ["（无）"]
    lines += ["", "## 最近命令"]
    lines += [f"- `{c}`" for c in st["commands"]] or ["（无）"]
    for root, status, stat in git_state(st, payload):
        lines += ["", f"## Git: `{root}`", "", "```text", status or "(工作区干净)"]
        if stat:
            lines += ["", stat]
        lines += ["```"]
    if st["last_model_msg"]:
        lines += ["", "## 最近一条回复摘录", "", st["last_model_msg"]]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


TEMPLATE = """# <任务标题>
## 目标 / 验收标准
## 当前状态（一句话）
## 已完成（含关键文件路径）
## 进行中 / 下一步（按顺序、可直接执行）
## 关键决策与原因
## 坑 / 已排除的方案
## 关键路径·命令·配置值"""


def sys_msg(text):
    return {"systemMessage": {"systemMessage": text}}


# ---------------------------------------------------------------- hooks
def hook_pre(payload, cfg):
    conv = payload.get("conversationId")
    if not conv:
        return {}
    model = payload.get("modelName", "")
    spath = os.path.join(data_dir(), "conv", f"{conv}.json")
    st = new_state()
    st.update(read_json(spath, {}))
    st["inv"] += 1
    scan(st, transcript_file(payload), model)
    npath = notes_path(st, payload, cfg)
    ratio, cur, lim, unit = usage(st, cfg, model)
    inject = []
    try:
        archive_notes_if_changed(st)
    except Exception:
        pass

    if st["pending_compaction"]:
        st["pending_compaction"] = False
        # Freeze what existed right before the compaction.
        stem = os.path.basename(npath)[:-3]
        hist = history_dir(st)
        n = st["compactions"]
        try:
            archive(npath, hist, f"{stem}.c{n}.md", stem + ".c")
            archive(npath[:-3] + ".auto.md", hist, f"{stem}.c{n}.auto.md", stem + ".c")
        except Exception:
            pass
        st["git_cache"] = {}
        auto = write_auto_snapshot(st, payload, ratio, cur, lim, unit)
        have = os.path.exists(npath)
        msg = [f"[context-handoff] 刚刚发生了第 {st['compactions']} 次自动压缩，压缩摘要可能丢失细节。"]
        if have:
            msg.append(f"继续工作之前，先用 read_file 读取交接文件 {npath}"
                       f"（最后更新 {datetime.datetime.fromtimestamp(mtime(npath)):%H:%M:%S}），"
                       "以其中的“下一步”和“坑”为准补全摘要。")
        else:
            msg.append("本会话还没有人工交接文件。")
        msg.append(f"机械快照（用户请求、改过的文件、命令、git 状态）在 {auto}，需要时读取。"
                   f"交接文件的历史版本在 {hist}（若当前版本信息不全可查旧版）。读完直接继续原任务，不必向用户汇报这一步。")
        if st["files"]:
            msg.append("最近改动的文件: " + "; ".join(st["files"][-8:]))
        inject.append(sys_msg("\n".join(msg)))
    elif ratio >= cfg["hard_ratio"]:
        stale = mtime(npath) < st["warn_ts"] if "hard" in st["warned"] else True
        if stale and st["inv"] - st["last_hard_inv"] >= cfg["hard_repeat_every"]:
            st["last_hard_inv"] = st["inv"]
            st["git_cache"] = {}
            if "hard" not in st["warned"]:
                st["warned"].append("hard")
                st["warn_ts"] = time.time()
            inject.append(sys_msg(
                f"[context-handoff] 紧急：上下文约 {ratio:.0%}（{cur:,}/{lim:,} {unit}），随时可能自动压缩。"
                f"现在立即用 write_file（Overwrite=true）把完整进展写入 {npath}，然后再继续原任务。"
                f"格式:\n{TEMPLATE}\n要求：具体到文件路径、命令、数值；“下一步”写成新会话可直接执行的步骤；控制在 150 行内；不要向用户汇报这一步。"))
    elif ratio >= cfg["soft_ratio"] and "soft" not in st["warned"]:
        st["warned"].append("soft")
        st["warn_ts"] = time.time()
        inject.append(sys_msg(
            f"[context-handoff] 上下文约 {ratio:.0%}（{cur:,}/{lim:,} {unit}）。在下一个工具调用前，"
            f"先用 write_file 把当前进展写入交接文件 {npath}（已存在则整体覆盖更新），之后继续原任务。"
            f"格式:\n{TEMPLATE}\n不要向用户汇报这一步。"))

    if ratio >= cfg["soft_ratio"] or inject:
        try:
            write_auto_snapshot(st, payload, ratio, cur, lim, unit)
        except Exception:
            pass
    write_json(spath, st)
    return {"injectSteps": inject} if inject else {}


def hook_stop(payload, cfg):
    conv = payload.get("conversationId")
    if not conv:
        return {}
    model = payload.get("modelName", "")
    spath = os.path.join(data_dir(), "conv", f"{conv}.json")
    st = new_state()
    st.update(read_json(spath, {}))
    scan(st, transcript_file(payload), model)
    npath = notes_path(st, payload, cfg)
    ratio, cur, lim, unit = usage(st, cfg, model)
    result = {}
    try:
        archive_notes_if_changed(st)
    except Exception:
        pass
    if st["tool_calls_total"] >= cfg["auto_snapshot_min_tool_calls"]:
        try:
            write_auto_snapshot(st, payload, ratio, cur, lim, unit)
        except Exception:
            pass
    if (cfg["stop_refresh"]
            and payload.get("terminationReason") == "NO_TOOL_CALL"
            and not payload.get("continuationActive")
            and ratio >= cfg["stop_min_ratio"]
            and st["turn_tool_calls"] >= cfg["stop_min_tool_calls"]
            and mtime(npath) < st["turn_start_ts"]):
        result = {"decision": "continue", "reason": (
            f"[context-handoff] 本轮工作量较大且上下文已约 {ratio:.0%}，交接文件尚未更新。"
            f"请用 write_file 把最新进展写入 {npath}（格式同之前的模板），写完后只回复一句“已更新交接文件”。")}
    write_json(spath, st)
    return result


def hook_session(payload, cfg):
    ws = [w for w in payload.get("workspacePaths") or [] if w and os.path.isdir(w)]
    if not ws:
        return {}
    cutoff = time.time() - cfg["session_hint_days"] * 86400
    notes = []
    for w in ws:
        for p in glob.glob(os.path.join(w, cfg["notes_dirname"], "*.md")):
            if p.endswith(".auto.md") or mtime(p) < cutoff:
                continue
            title = ""
            try:
                with open(p, encoding="utf-8") as f:
                    for line in f:
                        if line.strip():
                            title = short(line.lstrip("# "), 80)
                            break
            except Exception:
                pass
            notes.append((mtime(p), p, title))
    if not notes:
        return {}
    notes.sort(reverse=True)
    lines = ["[context-handoff] 本工作区有近期交接文件。如果用户要继续其中某个任务，先用 read_file 读取对应文件再动手；无关则忽略："]
    for t, p, title in notes[: cfg["session_hint_max"]]:
        lines.append(f"- {datetime.datetime.fromtimestamp(t):%m-%d %H:%M} {title} → {p}")
    return {"injectSteps": [sys_msg("\n".join(lines))]}


# ---------------------------------------------------------------- tools
def calibrate(brain_dir):
    cal = {"tokens": [], "bytes": [], "by_model": {}}
    write_json(os.path.join(data_dir(), "calibration.json"), cal)
    files = glob.glob(os.path.join(brain_dir, "*", ".system_generated", "logs", "transcript_full.jsonl"))
    files.sort(key=mtime)
    for p in files:
        st = new_state()
        st["initial"] = False
        scan(st, p, "")
    cal = read_json(os.path.join(data_dir(), "calibration.json"), {})
    print(json.dumps({"transcripts": len(files), "tokens": cal.get("tokens"),
                      "bytes": cal.get("bytes")}, indent=1))


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "pre"
    if cmd == "calibrate":
        default = os.path.join(os.path.expanduser("~"), ".gemini", "antigravity", "brain")
        calibrate(sys.argv[2] if len(sys.argv) > 2 else default)
        return
    if cmd == "status":
        print(json.dumps(read_json(os.path.join(data_dir(), "conv", f"{sys.argv[2]}.json"), {}),
                         ensure_ascii=False, indent=1))
        return
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8") or "{}")
        cfg = load_cfg()
        if not cfg.get("enabled", True):
            out({})
            return
        fn = {"pre": hook_pre, "stop": hook_stop, "session": hook_session}.get(cmd)
        out(fn(payload, cfg) if fn else {})
    except Exception as e:  # fail open, but leave a trace
        try:
            with open(os.path.join(data_dir(), "error.log"), "a", encoding="utf-8") as f:
                f.write(f"{datetime.datetime.now().isoformat()} {cmd}: {e!r}\n")
        except Exception:
            pass
        out({})


if __name__ == "__main__":
    main()
