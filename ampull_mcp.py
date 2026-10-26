#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ampull_mcp.py —— AmPull 的 MCP 协议壳（stdio，零依赖）

定位：把 AmPull 的 CLI 能力按 `SKILL.md` 的覆盖面暴露成 MCP 工具，**不复制业务逻辑**——
每次调用都是子进程跑 `ampull.py ... --json`，取最后一行收据原样回传。CLI 与 MCP 同源，
不会出现两面口径不一致。

协议：JSON-RPC 2.0，换行分帧（MCP stdio transport）。与 awsearch_mcp.py 同族实现。

工具（12）：
  ampull_download   单/多 URL 下载（= CLI 位置参数语义）
  ampull_manifest   清单批量（--manifest）
  ampull_repo       HuggingFace 整仓（--repo + --include）
  ampull_hls        HLS 下载（同步或异步）
  ampull_hls_probe  HLS 探针：只看档位/段数/时长/加密，不下载
  ampull_job        查异步任务状态与收据
  ampull_stop       停掉本进程发起的异步任务（含 serve）
  ampull_tools      ffmpeg 依赖自举（list/path/install/remove）
  ampull_status     下载产物状态（只读）
  ampull_clean      残留清理（默认只列不删）
  ampull_journal    下载台账最近 N 条（只读）
  ampull_serve      回环 Range 服务（后台起，返回 URL）
  ampull_schema     完整契约（= CLI --schema）

长任务：`wait=False` 时立即返回 job_id，收据由子进程写 `jobs/<id>/out.json`
（磁盘持久化，MCP 重启后仍可查）。除 serve 启动最多等 20s 拿端口外，不做任何轮询。
"""

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
AMPULL = os.path.join(HERE, "ampull.py")
if not os.path.isfile(AMPULL):                       # git 里是小写，Windows 大小写不敏感
    for _cand in ("AmPull.py",):
        _p = os.path.join(HERE, _cand)
        if os.path.isfile(_p):
            AMPULL = _p
            break

JOBS_DIR = os.path.join(HERE, "jobs")
JOBS = {}                                            # id -> {proc,dir,out,log,argv}
JOBS_LOCK = threading.Lock()
TOOLS = {}

VERSION = "5.21"
_VERSION_CACHE = {}


def _cli_version():
    """以 CLI 的 --schema 版本为准（硬编码只作兜底，避免两边版本漂移）。"""
    if "v" not in _VERSION_CACHE:
        rec = _run_cli(["--schema"], timeout=60)
        _VERSION_CACHE["v"] = rec.get("version") or VERSION
    return _VERSION_CACHE["v"]


def _default_out():
    """未给 out_dir 时的默认落盘目录：<程序目录>/downloads（gitignore）。"""
    d = os.path.join(HERE, "downloads")
    os.makedirs(d, exist_ok=True)
    return d


def _tool(fn):
    TOOLS[fn.__name__.replace("tool_", "")] = fn
    return fn


# ------------------------------------------------------------------ 子进程
def _env():
    env = dict(os.environ)
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def _last_json(text):
    for line in reversed([l for l in (text or "").splitlines() if l.strip()]):
        try:
            return json.loads(line)
        except Exception:
            continue
    return None


def _run_cli(argv, timeout=None):
    """同步跑 CLI，返回收据（失败也返回结构化收据）。"""
    try:
        proc = subprocess.run([sys.executable, AMPULL] + [str(x) for x in argv],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=timeout, env=_env(), cwd=HERE)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout",
                "error_msg": "本地超时（%.0fs）；长任务请用 wait=false"
                             % (timeout or 0),
                "command": " ".join(str(x) for x in argv)}
    out = (proc.stdout or b"").decode("utf-8", "replace")
    err = (proc.stderr or b"").decode("utf-8", "replace")
    rec = _last_json(out)
    if rec is None:
        return {"ok": False, "error": "internal",
                "error_msg": "子进程无 JSON 收据（rc=%s）：%s"
                             % (proc.returncode, (err or out)[-300:]),
                "command": " ".join(str(x) for x in argv)}
    return rec


def _spawn(argv):
    """异步起任务：子进程自己把收据写进 jobs/<id>/out.json（磁盘持久化）。"""
    job_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    d = os.path.join(JOBS_DIR, job_id)
    os.makedirs(d, exist_ok=True)
    out_path = os.path.join(d, "out.json")
    log_path = os.path.join(d, "out.log")
    out_f = open(out_path, "wb")
    log_f = open(log_path, "wb")
    proc = subprocess.Popen([sys.executable, AMPULL] + [str(x) for x in argv],
                            stdout=out_f, stderr=log_f, cwd=HERE, env=_env())
    # 父进程不再需要这两个句柄（子进程持有自己的）——不关会随任务数泄漏 fd
    try:
        out_f.close()
        log_f.close()
    except Exception:
        pass
    with JOBS_LOCK:
        JOBS[job_id] = {"proc": proc, "dir": d, "out": out_path, "log": log_path,
                        "argv": argv, "started": time.time()}
    return {"ok": True, "mode": "async", "job": job_id, "state": "running",
            "log": log_path,
            "hint": "ampull_job(job_id=\"%s\") 查状态与收据" % job_id}


def _read_receipt(d):
    p = os.path.join(d, "out.json")
    if not os.path.isfile(p):
        return None
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            return _last_json(f.read())
    except OSError:
        return None


def _job_state(info):
    proc = info.get("proc")
    if proc is None:
        return "unknown"
    if proc.poll() is None:
        return "running"
    return "done" if _read_receipt(info["dir"]) else "failed"


# ------------------------------------------------------------------ 参数拼装
def _flags(conns=None, chunk_mb=None, as_name=None, sha256=None, mirror=None,
           header=None, proxy=None, no_proxy=False, hf_token=None, include=None):
    argv = []
    if conns:
        argv += ["-c", str(int(conns))]
    if chunk_mb:
        argv += ["--chunk-mb", str(int(chunk_mb))]
    if as_name:
        argv += ["--as", as_name]
    if sha256:
        argv += ["--sha256", sha256]
    for m in (mirror or []):
        argv += ["--mirror", m]
    for h in (header or []):
        argv += ["--header", h]
    if no_proxy:
        argv.append("--no-proxy")
    elif proxy:
        argv += ["--proxy", proxy]
    if hf_token:
        argv += ["--hf-token", hf_token]
    for i in (include or []):
        argv += ["--include", i]
    return argv


def _sync_or_async(argv, wait, timeout_s):
    argv = list(argv) + ["--json"]
    if wait:
        return _run_cli(argv, timeout=timeout_s)
    return _spawn(argv)


# ------------------------------------------------------------------ 下载类
@_tool
def tool_download(urls, out_dir=None, conns=None, chunk_mb=None, as_name=None,
                  sha256=None, mirror=None, header=None, proxy=None,
                  no_proxy=False, hf_token=None, reset_proxy=False,
                  wait=True, timeout_s=3600):
    """下载一个或多个 URL。out_dir **一律按目录处理**（内部补尾部分隔符）。

    单 URL 时 CLI 的第二个位置参数默认是"文件路径"，这里强制成目录语义，
    避免 `ampull_download(url, out_dir="D:/x/dl")` 真的生成一个叫 `dl` 的文件
    （实锤过的坑）。要指定文件名请用 as_name。
    不给 out_dir 时落到 `<程序目录>/downloads/`（不会污染程序根目录）。
    """
    if isinstance(urls, str):
        urls = [urls]
    out_dir = out_dir or _default_out()
    argv = list(urls)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        argv.append(out_dir + os.sep)          # 尾部分隔符 = CLI 认作目录
    elif as_name:
        argv.append("." + os.sep)
    argv += _flags(conns=conns, chunk_mb=chunk_mb, as_name=as_name, sha256=sha256,
                   mirror=mirror, header=header, proxy=proxy, no_proxy=no_proxy,
                   hf_token=hf_token)
    if reset_proxy:
        argv.append("--reset-proxy")
    return _sync_or_async(argv, wait, timeout_s)


@_tool
def tool_manifest(file, out_dir, conns=None, chunk_mb=None, mirror=None,
                  header=None, proxy=None, no_proxy=False, wait=True,
                  timeout_s=3600):
    """清单批量：每行 `URL [sha256]`，`#` 是注释；结果落到 out_dir（自动创建）。"""
    os.makedirs(out_dir, exist_ok=True)
    argv = ["--manifest", file, out_dir]
    argv += _flags(conns=conns, chunk_mb=chunk_mb, mirror=mirror, header=header,
                   proxy=proxy, no_proxy=no_proxy)
    return _sync_or_async(argv, wait, timeout_s)


@_tool
def tool_repo(repo, out_dir, include=None, conns=None, chunk_mb=None, mirror=None,
              header=None, proxy=None, no_proxy=False, hf_token=None,
              wait=True, timeout_s=7200):
    """HuggingFace 整仓下载（默认 hf-mirror.com）。include 如 ["UD-IQ4_XS/*.gguf"]。

    **不给 include 会整仓全下（可能几百 GB）**——先探清单或先给 include。
    """
    os.makedirs(out_dir, exist_ok=True)
    argv = ["--repo", repo, out_dir]
    argv += _flags(conns=conns, chunk_mb=chunk_mb, mirror=mirror, header=header,
                   proxy=proxy, no_proxy=no_proxy, hf_token=hf_token,
                   include=include)
    return _sync_or_async(argv, wait, timeout_s)


@_tool
def tool_hls(url, out_dir=".", quality="best", workers=8, no_mux=False,
             keep_parts=False, as_name=None, ffmpeg=None, resume_ignore=False,
             timeout_s=3600, wait=True, proxy=None, no_proxy=False, header=None):
    """HLS（m3u8）视频下载：分片并行 + 断点续传 + AES-128 解密 + 可选 mp4 封装。

    quality：数字按高度，高度不达标时按宽度兜底（站方常以宽度标 720）。
    长任务建议 wait=False；想先看档位/段数/时长，用 ampull_hls_probe（不下载）。
    """
    argv = ["hls", url, out_dir or _default_out()]
    if quality and quality != "best":
        argv += ["--quality", str(quality)]
    if workers and int(workers) != 8:
        argv += ["--workers", str(int(workers))]
    if no_mux:
        argv.append("--no-mux")
    if keep_parts:
        argv.append("--keep-parts")
    if as_name:
        argv += ["--as", as_name]
    if ffmpeg:
        argv += ["--ffmpeg", ffmpeg]
    if resume_ignore:
        argv.append("--resume-ignore")
    if no_proxy:
        argv.append("--no-proxy")
    elif proxy:
        argv += ["--proxy", proxy]
    for h in (header or []):
        argv += ["--header", h]
    return _sync_or_async(argv, wait, timeout_s)


@_tool
def tool_hls_probe(url, quality="best", proxy=None, no_proxy=False, header=None):
    """只看不下载：返回档位表、选中档、段数、时长、加密方式、是否可下载。"""
    argv = ["hls", url, ".", "--probe"]
    if quality and quality != "best":
        argv += ["--quality", str(quality)]
    if no_proxy:
        argv.append("--no-proxy")
    elif proxy:
        argv += ["--proxy", proxy]
    for h in (header or []):
        argv += ["--header", h]
    return _run_cli(argv + ["--json"], timeout=120)


# ------------------------------------------------------------------ 任务
@_tool
def tool_job(job_id=None, prune_days=None):
    """查异步任务：给 job_id 返回状态/收据；不给则列出全部（含历史目录）。

    prune_days=N：删掉 N 天前已结束的任务目录（jobs/ 会随任务数增长）。
    """
    if prune_days:
        cutoff = time.time() - float(prune_days) * 86400
        pruned, kept = [], []
        if os.path.isdir(JOBS_DIR):
            for name in sorted(os.listdir(JOBS_DIR)):
                d = os.path.join(JOBS_DIR, name)
                try:
                    if os.path.getmtime(d) > cutoff:
                        kept.append(name)
                        continue
                    with JOBS_LOCK:
                        running = name in JOBS and JOBS[name]["proc"].poll() is None
                    if running:
                        kept.append(name)
                        continue
                    shutil.rmtree(d, ignore_errors=True)
                    pruned.append(name)
                except OSError:
                    kept.append(name)
        return {"ok": True, "pruned": pruned, "count": len(pruned),
                "kept": len(kept), "jobs_dir": JOBS_DIR}
    with JOBS_LOCK:
        items = dict(JOBS)
    if not job_id:
        rows = [{"job": j, "state": _job_state(i)} for j, i in sorted(items.items())]
        hist = []
        if os.path.isdir(JOBS_DIR):
            for name in sorted(os.listdir(JOBS_DIR)):
                if name in items:
                    continue
                rec = _read_receipt(os.path.join(JOBS_DIR, name))
                hist.append({"job": name, "state": "done" if rec else "unknown",
                             "ok": (rec or {}).get("ok")})
        return {"ok": True, "jobs": rows + hist}
    info = items.get(job_id)
    if info:
        return {"ok": True, "job": job_id, "state": _job_state(info),
                "receipt": _read_receipt(info["dir"]), "log": info["log"],
                "argv": " ".join(str(x) for x in info.get("argv") or [])}
    d = os.path.join(JOBS_DIR, job_id)
    rec = _read_receipt(d)
    if rec is None:
        return {"ok": False, "error": "bad_args", "job": job_id,
                "error_msg": "未知任务 id（没有 receipt，也没有在跑）"}
    return {"ok": True, "job": job_id, "state": "done", "receipt": rec,
            "log": os.path.join(d, "out.log")}


@_tool
def tool_stop(job_id):
    """停掉本进程发起的异步任务（下载中途停：断点保留，重跑即续）。"""
    with JOBS_LOCK:
        info = JOBS.get(job_id)
    if not info:
        return {"ok": False, "error": "bad_args", "job": job_id,
                "error_msg": "只能停本 MCP 进程发起、且仍在跑的任务"
                             "（历史任务请直接在系统里结束进程）"}
    proc = info["proc"]
    if proc.poll() is not None:
        return {"ok": True, "job": job_id, "state": "already-exited",
                "receipt": _read_receipt(info["dir"])}
    try:
        proc.terminate()
        for _ in range(20):
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        if proc.poll() is None:
            proc.kill()
    except Exception as e:                                    # noqa: BLE001
        return {"ok": False, "error": "internal", "job": job_id,
                "error_msg": "%s: %s" % (type(e).__name__, e)}
    return {"ok": True, "job": job_id, "state": "stopped",
            "note": "断点已保留（分片与账本在 <产物>.parts/），重跑同一条命令即续传",
            "log": info["log"]}


# ------------------------------------------------------------------ 其它
@_tool
def tool_tools(action, tool="ffmpeg", yes=False, force=False, scan=False):
    """ffmpeg 依赖自举：list/path/install/remove。先复用已有（PATH/共享位/别处自带），
    确实没有再装到程序目录（需 yes=true）。"""
    argv = ["tools", action]
    if tool and action in ("path", "install", "remove"):
        argv.append(tool)
    if yes:
        argv.append("--yes")
    if force:
        argv.append("--force")
    if scan:
        argv.append("--scan")
    return _run_cli(argv + ["--json"],
                    timeout=1800 if (yes and action == "install") else 120)


@_tool
def tool_status(paths):
    """下载产物状态（只读）：state/progress/速度。可一次给多个路径。"""
    if isinstance(paths, str):
        paths = [paths]
    return _run_cli(["--status"] + list(paths) + ["--json"], timeout=60)


@_tool
def tool_clean(paths=None, yes=False, all=False, older_than=None):
    """清理下载残留。**默认只列不删**；yes=true 才真删，all=true 连断点一起删。"""
    argv = ["--clean"]
    if paths:
        argv += (list(paths) if not isinstance(paths, str) else [paths])
    if yes:
        argv.append("-y")
    if all:
        argv.append("--all")
    if older_than:
        argv += ["--older-than", older_than]
    return _run_cli(argv + ["--json"], timeout=300)


@_tool
def tool_journal(limit=20):
    """下载台账最近 N 条（downloads.jsonl，只读；URL 已脱敏）。

    认 `DL_JOURNAL` 环境变量（与 CLI 同源），否则读程序目录下的默认台账。
    """
    p = os.environ.get("DL_JOURNAL") or os.path.join(HERE, "downloads.jsonl")
    if not os.path.isfile(p):
        return {"ok": True, "entries": [], "note": "还没有台账文件：%s" % p}
    rows = []
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            lines = [l for l in f if l.strip()]
        for line in lines[-max(1, int(limit)):]:
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    except OSError as e:
        return {"ok": False, "error": "io", "error_msg": str(e)}
    rows.reverse()
    return {"ok": True, "count": len(rows), "entries": rows, "file": p}


@_tool
def tool_serve(path, port=0, host="127.0.0.1"):
    """回环 Range 服务：后台起进程，返回 url/port/job（播放器可边下边用）。

    用 ampull_stop(job_id) 停止。未完成区间挂起等待（不是 404）。
    """
    boot = _spawn(["--serve", path, "--port", str(int(port)),
                   "--host", host, "--json"])
    job_id = boot["job"]
    with JOBS_LOCK:
        info = dict(JOBS.get(job_id) or {})
    deadline = time.time() + 20
    rec = None
    while time.time() < deadline:                  # 只等启动那一行 JSON
        rec = _read_receipt(info.get("dir", ""))
        if rec:
            break
        time.sleep(0.3)
    if not rec:
        return {"ok": False, "error": "internal", "job": job_id,
                "error_msg": "20 秒内没拿到 serve 启动收据（看 log）",
                "log": boot.get("log")}
    rec = dict(rec)
    rec["job"] = job_id
    return rec


@_tool
def tool_schema():
    """AmPull 机器可读契约（与 CLI --schema 同一份）。"""
    return _run_cli(["--schema"], timeout=60)


# ------------------------------------------------------------------ 工具 schema
_SCHEMAS = {
    "download": {
        "type": "object",
        "properties": {
            "urls": {"type": ["string", "array"], "items": {"type": "string"},
                     "description": "一个或多个 URL；多个=分片批量"},
            "out_dir": {"type": "string",
                        "description": "落盘目录（总会按目录处理，自动创建；文件名用 as_name）"},
            "conns": {"type": "integer", "description": "连接数 1~64，默认 16"},
            "chunk_mb": {"type": "integer", "description": "分块 MB，默认 16"},
            "as_name": {"type": "string", "description": "落盘文件名（仅单 URL）"},
            "sha256": {"type": "string", "description": "期望 SHA256，不符即判废"},
            "mirror": {"type": "array", "items": {"type": "string"},
                       "description": "镜像候选（URL / @文件 / @auto）"},
            "header": {"type": "array", "items": {"type": "string"}},
            "proxy": {"type": "string"},
            "no_proxy": {"type": "boolean"},
            "hf_token": {"type": "string", "description": "HF 访问令牌"},
            "reset_proxy": {"type": "boolean",
                            "description": "清除代理扫描当日标记（换代理端口后）"},
            "wait": {"type": "boolean", "description": "false=返回 job_id（大文件推荐）"},
            "timeout_s": {"type": "integer", "description": "同步模式本地超时，默认 3600"},
        },
        "required": ["urls"],
    },
    "manifest": {
        "type": "object",
        "properties": {
            "file": {"type": "string", "description": "清单文件：每行 URL [sha256]"},
            "out_dir": {"type": "string"},
            "conns": {"type": "integer"}, "chunk_mb": {"type": "integer"},
            "mirror": {"type": "array", "items": {"type": "string"}},
            "header": {"type": "array", "items": {"type": "string"}},
            "proxy": {"type": "string"}, "no_proxy": {"type": "boolean"},
            "wait": {"type": "boolean"}, "timeout_s": {"type": "integer"},
        },
        "required": ["file", "out_dir"],
    },
    "repo": {
        "type": "object",
        "properties": {
            "repo": {"type": "string", "description": "HF 仓库名或页面链接"},
            "out_dir": {"type": "string"},
            "include": {"type": "array", "items": {"type": "string"},
                        "description": "路径通配过滤；不给会整仓全下（可能几百 GB）"},
            "conns": {"type": "integer"}, "chunk_mb": {"type": "integer"},
            "mirror": {"type": "array", "items": {"type": "string"}},
            "header": {"type": "array", "items": {"type": "string"}},
            "proxy": {"type": "string"}, "no_proxy": {"type": "boolean"},
            "hf_token": {"type": "string"},
            "wait": {"type": "boolean"}, "timeout_s": {"type": "integer"},
        },
        "required": ["repo", "out_dir"],
    },
    "hls": {
        "type": "object",
        "properties": {
            "url": {"type": "string"}, "out_dir": {"type": "string"},
            "quality": {"type": "string",
                        "description": "best|worst|数字（按高度，不达标按宽度兜底）"},
            "workers": {"type": "integer"},
            "no_mux": {"type": "boolean"}, "keep_parts": {"type": "boolean"},
            "as_name": {"type": "string"}, "ffmpeg": {"type": "string"},
            "resume_ignore": {"type": "boolean"},
            "wait": {"type": "boolean"}, "timeout_s": {"type": "integer"},
            "proxy": {"type": "string"}, "no_proxy": {"type": "boolean"},
            "header": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["url"],
    },
    "hls_probe": {
        "type": "object",
        "properties": {
            "url": {"type": "string"}, "quality": {"type": "string"},
            "proxy": {"type": "string"}, "no_proxy": {"type": "boolean"},
            "header": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["url"],
    },
    "job": {"type": "object", "properties": {
        "job_id": {"type": "string"},
        "prune_days": {"type": "number",
                       "description": "删掉 N 天前已结束的任务目录（jobs/ 清理）"},
    }},
    "stop": {"type": "object", "properties": {"job_id": {"type": "string"}},
             "required": ["job_id"]},
    "tools": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "path", "install", "remove"]},
            "tool": {"type": "string"}, "yes": {"type": "boolean"},
            "force": {"type": "boolean"}, "scan": {"type": "boolean"},
        },
        "required": ["action"],
    },
    "status": {
        "type": "object",
        "properties": {"paths": {"type": ["string", "array"], "items": {"type": "string"}}},
        "required": ["paths"],
    },
    "clean": {
        "type": "object",
        "properties": {
            "paths": {"type": ["string", "array"], "items": {"type": "string"}},
            "yes": {"type": "boolean", "description": "真删（默认只列）"},
            "all": {"type": "boolean", "description": "连断点 .part 一起删"},
            "older_than": {"type": "string", "description": "如 7d / 24h / 30m"},
        },
    },
    "journal": {"type": "object", "properties": {"limit": {"type": "integer"}}},
    "serve": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "下载产物路径（.part 或最终文件）"},
            "port": {"type": "integer", "description": "0=临时端口"},
            "host": {"type": "string", "description": "默认 127.0.0.1"},
        },
        "required": ["path"],
    },
    "schema": {"type": "object", "properties": {}},
}

_DESCS = {
    "download": "下载一个或多个 URL（单文件或分片批量）。大文件用 wait=false + job 查。",
    "manifest": "按清单文件批量下载（每行 URL [sha256]）。",
    "repo": "HuggingFace 整仓下载（默认 hf-mirror.com）；建议给 include 限定范围。",
    "hls": "下载 HLS（m3u8）视频：分片并行 + 断点 + AES-128 + 可选 mp4 封装。",
    "hls_probe": "HLS 探针：不下载，只回档位/段数/时长/加密/可下载性，用于选档与预判。",
    "job": "查异步任务状态与收据（不给 job_id 列全部，含历史目录）。",
    "stop": "停掉本进程发起的异步任务（含 serve）；断点保留，重跑即续。",
    "tools": "ffmpeg 依赖自举：list/path/install/remove；先复用已有，需 yes=true 才装。",
    "status": "下载产物状态只读查询（state/progress/速度）。",
    "clean": "清理下载残留（默认只列不删；yes=true 才删，all=true 连断点）。",
    "journal": "下载台账最近 N 条（只读）。",
    "serve": "回环 Range 服务：后台起，返回 URL，播放器可边下边用；ampull_stop 停止。",
    "schema": "AmPull 机器可读契约（参数/错误码/说明）。",
}

_ORDER = ("download", "manifest", "repo", "hls", "hls_probe", "job", "stop",
          "tools", "status", "clean", "journal", "serve", "schema")


# ------------------------------------------------------------------ JSON-RPC
def _ok(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _err(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _text_result(obj, is_error=False):
    return {"content": [{"type": "text",
                         "text": json.dumps(obj, ensure_ascii=False, indent=1)}],
            "structuredContent": obj,
            "isError": bool(is_error)}


def handle(req):
    mid = req.get("id")
    method = req.get("method")
    params = req.get("params") or {}

    if method == "initialize":
        return _ok(mid, {
            "protocolVersion": params.get("protocolVersion") or "2024-11-05",
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "ampull", "version": _cli_version()},
            "instructions": "AmPull 下载器。下载用 ampull_download/ampull_hls；"
                            "HLS 先 ampull_hls_probe 看档位；长任务 wait=false 再用 "
                            "ampull_job；ffmpeg 缺失用 ampull_tools（先复用、需同意才装）。",
        })
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "ping":
        return _ok(mid, {})
    if method == "tools/list":
        return _ok(mid, {"tools": [
            {"name": "ampull_" + n, "description": _DESCS[n], "inputSchema": _SCHEMAS[n]}
            for n in _ORDER]})
    if method == "tools/call":
        name = (params.get("name") or "").replace("ampull_", "")
        args = params.get("arguments") or {}
        fn = TOOLS.get(name)
        if fn is None:
            return _err(mid, -32602, "未知工具：%s" % name)
        try:
            result = fn(**args)
        except TypeError as e:
            return _ok(mid, _text_result({"ok": False, "error": "bad_args",
                                          "error_msg": "参数不匹配：%s" % e}, True))
        except Exception as e:                              # noqa: BLE001
            return _ok(mid, _text_result({"ok": False, "error": "internal",
                                          "error_msg": "%s: %s"
                                                       % (type(e).__name__, e)}, True))
        is_err = isinstance(result, dict) and result.get("ok") is False
        return _ok(mid, _text_result(result, is_err))
    if method in ("resources/list", "prompts/list"):
        return _ok(mid, {method.split("/")[0]: []})
    return _err(mid, -32601, "未实现的方法：%s" % method)


def main():
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue
        if isinstance(req, list):
            out = [r for r in (handle(x) for x in req) if r]
        else:
            out = handle(req)
        if out:
            sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
