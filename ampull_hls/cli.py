# -*- coding: utf-8 -*-
"""ampull_hls.cli —— `ampull.py hls` / `ampull.py tools` 子命令

为什么不用 add_subparsers：主解析走 `parse_intermixed_args()`，CPython 对
nargs=PARSER 直接 TypeError。由 AmPull.main 在构建主 Parser 前剥掉 token，
用这里自建的 Parser 解析，再回到既有 if/elif 链首分支——Fail / emit /
journal / 退出码全部沿用主流程。

ctx 注入：Fail、Parser、extra_headers、resolve_ops、user_agent、
default_opener（带 _SafeRedirect）、json_mode。
"""

import hashlib
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from . import crypto, fetch as hfetch, mux as hmux, parse as hparse, plan as hplan
from . import tools as htools
from .parse import HlsError

_DEF_TIMEOUT = 20
# 跨 host 时保留的凭证白名单（与 AmPull 主流程 _XHOST_KEEP 同语义）
_XHOST_KEEP = ("accept", "accept-language", "referer", "user-agent",
               "authorization", "range")


# ------------------------------------------------------------------ 参数
def _add_common(p, json_mode):
    p.add_argument("-h", "--help", action="store_true", help="显示帮助")
    p.add_argument("--json", action="store_true", help="stdout 只输出一行 JSON 结果")
    p.add_argument("--proxy", metavar="URL", help="显式 HTTP 代理（默认自动检测）")
    p.add_argument("--no-proxy", action="store_true", help="强制直连")
    p.add_argument("--header", action="append", metavar="K:V", help="附加请求头（可多次）")
    p.add_argument("--timeout", type=int, default=_DEF_TIMEOUT,
                   help="单请求超时秒数（默认 20）")


def build_parser(sub, Parser, json_mode=False):
    """子命令解析器（复用主 Parser 子类，保证 usage 错误退出码=1）。"""
    if sub == "hls":
        p = Parser(prog="ampull.py hls", allow_abbrev=False, add_help=False,
                   description="HLS（m3u8）视频下载：分片并行 + 断点续传 + "
                               "AES-128 解密 + 可选 mp4 封装")
        _add_common(p, json_mode)
        p.add_argument("url", metavar="m3u8链接", help="HLS 播放列表 URL（主/子列表均可）")
        p.add_argument("out_dir", metavar="输出目录", help="落盘目录（不存在会创建）")
        p.add_argument("--quality", default="best", metavar="best|worst|高度",
                       help="选档：best/worst 按带宽；数字按分辨率高度（如 720），"
                            "取 >= 该高度里最接近的一档，达不到则退最高档")
        p.add_argument("--workers", type=int, default=8, metavar="N",
                       help="并发分片数（默认 8，1~16）")
        p.add_argument("--keep-parts", action="store_true",
                       help="成功后保留分片与账本（排障用）")
        p.add_argument("--no-mux", action="store_true", help="只落 .ts，不调 ffmpeg 封装")
        p.add_argument("--as", dest="as_name", metavar="名字", help="指定输出文件名")
        p.add_argument("--resume-ignore", action="store_true",
                       help="断点指纹不符时强制复用旧分片（危险）")
        p.add_argument("--ffmpeg", metavar="路径", help="显式指定 ffmpeg")
        p.add_argument("--probe", action="store_true",
                       help="只看不下载：输出档位/段数/时长/加密/可下载性")
        return p
    p = Parser(prog="ampull.py tools", allow_abbrev=False, add_help=False,
               description="依赖自举：按需安装外部工具（只写本地 tools 目录，"
                           "不改 PATH、不写系统目录）")
    _add_common(p, json_mode)
    p.add_argument("action", metavar="动作", choices=["list", "path", "install", "remove"],
                   help="list=支持表与本机现状；path=打印会选用的路径；"
                        "install=安装（默认 dry-run）；remove=删本地副本")
    p.add_argument("tool", metavar="工具", nargs="?", default="ffmpeg",
                   help="工具名（当前只支持 ffmpeg）")
    p.add_argument("-y", "--yes", action="store_true", help="真正执行（install/remove）")
    p.add_argument("--force", action="store_true",
                   help="install 时即使已发现可用的 ffmpeg 也强制装一份到程序目录")
    p.add_argument("--scan", action="store_true",
                   help="tools list 时额外扫描“别处自带的 ffmpeg”（有限深度 + 烟测前候选）")
    return p


# ------------------------------------------------------------------ 名字安全
_BAD_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {"COM%d" % i for i in range(1, 10)} \
            | {"LPT%d" % i for i in range(1, 10)}


def _safe_name(name):
    """--as 清洗（与主流程同语义）：去目录、去非法字符、拒保留名与 ..。"""
    base = os.path.basename((name or "").replace("\\", "/"))
    base = _BAD_NAME.sub("_", base).strip(" .")
    if not base or base in (".", ".."):
        raise HlsError("bad_path", "--as 不是合法文件名：%r" % name)
    stem = os.path.splitext(base)[0].upper()
    if stem in _RESERVED:
        raise HlsError("bad_path", "--as 命中 Windows 保留名：%r" % base)
    return base


def _unique_path(path):
    """撞名保护：base(N).ext（与 AmPull 既有习惯一致）。"""
    if not os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    for i in range(2, 1000):
        cand = "%s(%d)%s" % (root, i, ext)
        if not os.path.exists(cand):
            return cand
    return path


def _fmt_mb(n):
    return "%.1f MB" % (n / 1048576.0)


# ------------------------------------------------------------------ 网络
def _headers_for(ctx, args, url, base_host=None):
    """请求头：跨 host 只保留白名单（与主流程 _XHOST_KEEP 同语义，防凭据泄漏）。"""
    extra = dict(ctx.get("extra_headers") or {})
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    hdrs = {}
    for k, v in extra.items():
        if base_host is None or host == base_host.lower() or k.lower() in _XHOST_KEEP:
            hdrs[k] = v
    if ctx.get("user_agent") and "User-Agent" not in hdrs:
        hdrs["User-Agent"] = ctx["user_agent"]
    return hdrs


def _opener_for(ctx, args, url):
    host = urllib.parse.urlsplit(url).hostname or ""
    primary, _alt = ctx["resolve_ops"](args, host=host)
    # 关键：默认 opener 必须带 _SafeRedirect（302 跨 host 剥凭据），
    # 裸 build_opener() 会把 Cookie/Authorization 跟到异己 host（审查实锤）
    return primary or ctx.get("default_opener") or urllib.request.build_opener()


def _get(ctx, args, url, base_host=None):
    """GET → bytes。所有失败归一成 HlsError（错误码进词表，含 http_XXX）。"""
    opener = _opener_for(ctx, args, url)
    req = urllib.request.Request(url, headers=_headers_for(ctx, args, url, base_host))
    timeout = getattr(args, "timeout", None) or _DEF_TIMEOUT
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        raise HlsError("http_%d" % e.code,
                       "HTTP %d：%s（分片签名可能已过期，或需要 Referer/登录态）"
                       % (e.code, url))
    except urllib.error.URLError as e:
        raise HlsError("network", "网络错误 %s：%s" % (e.reason, url))
    except Exception as e:                                     # noqa: BLE001
        raise HlsError("network", "%s: %s" % (type(e).__name__, e))


def _download_to(ctx, args, url, dest):
    """流式下载到文件（100MB+ 不进内存），返回 (sha256_hex, size)。"""
    opener = _opener_for(ctx, args, url)
    req = urllib.request.Request(url, headers=_headers_for(ctx, args, url))
    timeout = getattr(args, "timeout", None) or _DEF_TIMEOUT
    h = hashlib.sha256()
    size = 0
    try:
        with opener.open(req, timeout=timeout) as r, open(dest, "wb") as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                h.update(chunk)
                size += len(chunk)
    except urllib.error.HTTPError as e:
        raise HlsError("tools_install_failed",
                       "下载失败 HTTP %d：%s" % (e.code, url))
    except Exception as e:                                     # noqa: BLE001
        raise HlsError("tools_install_failed",
                       "下载失败 %s：%s" % (type(e).__name__, e))
    return h.hexdigest(), size


def _get_json(ctx, args, url):
    import json
    raw = _get(ctx, args, url)
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception as e:                                     # noqa: BLE001
        raise HlsError("tools_install_failed", "GitHub API 返回非 JSON：%s" % e)


def _get_text(ctx, args, url, base_host=None):
    raw = _get(ctx, args, url, base_host)
    head = raw[:64].lstrip().lower()
    if head.startswith(b"<html") or head.startswith(b"<!doctype"):
        raise HlsError("hls_parse_failed",
                       "拿到 HTML 而不是 m3u8（可能是登录墙/错误页）：%s" % url)
    return raw.decode("utf-8", "replace")


# ------------------------------------------------------------------ hls
def run_hls(args, res, ctx):
    t0 = time.time()
    warnings = []

    def warn(code):
        if code and code not in warnings:
            warnings.append(code)

    if args.workers < 1 or args.workers > 16:
        warn("workers_clamped")
        args.workers = max(1, min(16, args.workers))
    os.makedirs(args.out_dir, exist_ok=True)

    plan = None
    stats = None
    try:
        # 参数前置校验（都不付网费）：--as 安全、--quality 语法
        as_name = _safe_name(args.as_name) if args.as_name else None
        q = (args.quality or "best").strip().lower()
        if q not in ("best", "worst") and not q.isdigit():
            raise HlsError("hls_variant_missing",
                           "--quality 只接受 best/worst/数字高度，收到 %r" % args.quality)
        args.quality = q
        url = args.url
        base_host = urllib.parse.urlsplit(url).hostname or ""
        parsed = None
        picked = None
        master_variants = []
        for _depth in range(4):
            parsed = hparse.parse(_get_text(ctx, args, url, base_host), url)
            if parsed["kind"] != "master":
                break
            if not master_variants:
                master_variants = list(parsed.get("variants") or [])
            picked, pick_warn = hparse.pick_variant(parsed, args.quality)
            warn(pick_warn)
            print("  选中档位：%s（带宽 %skbps）%s"
                  % (("%dp" % picked["height"]) if picked.get("height") else "?",
                     (picked.get("bandwidth") or 0) // 1000, picked["url"]))
            url = picked["url"]
        if not parsed or parsed["kind"] != "media":
            raise HlsError("hls_parse_failed", "播放列表层级过深或始终没有分片")

        # --probe：只看不下载（MCP 的 hls_probe 靠它；也方便人先看档位再决定）
        if getattr(args, "probe", False):
            variants = []
            for v in master_variants:
                variants.append({"width": v.get("width"), "height": v.get("height"),
                                 "bandwidth": v.get("bandwidth"),
                                 "codecs": v.get("codecs"), "url": v["url"]})
            res.update({"ok": True, "path": None, "bytes": 0,
                        "seconds": round(time.time() - t0, 2)})
            res["hls"] = {
                "variant_url": url,
                "quality": ("%dp" % picked["height"]) if (picked or {}).get("height") else None,
                "encrypted": parsed.get("encrypted"),
                "key_count": len(parsed.get("keys") or []),
                "probe": {
                    "kind": parsed["kind"],
                    "variants": variants,
                    "selected": picked,
                    "segments_total": len(parsed["segments"]),
                    "duration": parsed.get("duration"),
                    "encrypted": parsed.get("encrypted"),
                    "drm_reason": parsed.get("drm_reason"),
                    "fmp4": parsed.get("fmp4"),
                    "endlist": parsed.get("endlist"),
                    "playlist_type": parsed.get("playlist_type"),
                    "media_sequence": parsed.get("media_sequence"),
                    "key_count": len(parsed.get("keys") or []),
                    "quality_warning": (warnings[0] if warnings else None),
                    "downloadable": not (parsed.get("fmp4")
                                         or parsed.get("encrypted") == "drm"),
                },
            }
            if warnings:
                res["warning"] = ",".join(warnings)
            print("  探针：%d 段 / %.1fs / %s / 可下载=%s"
                  % (len(parsed["segments"]), parsed.get("duration") or 0,
                     parsed.get("encrypted") or "plain",
                     res["hls"]["probe"]["downloadable"]))
            return 0

        if parsed.get("encrypted") == "drm":
            raise HlsError("hls_drm", "DRM 加密（%s），不支持"
                           % (parsed.get("drm_reason") or "SAMPLE-AES"))
        if parsed.get("encrypted") == "aes-128" and not crypto.backend_available():
            raise HlsError("hls_dep_missing",
                           "解 AES-128 需要 cryptography：python -m pip install cryptography")

        height = (picked or {}).get("height") or 0
        vkey = hparse.variant_key(picked) if picked else ""
        stem = os.path.splitext(os.path.basename(
            urllib.parse.urlsplit(url).path or ""))[0] or "video"
        if as_name:
            name = as_name
            if not os.path.splitext(name)[1]:
                name += ".mp4"
            elif name.lower().endswith((".ts", ".m4s")) and not args.no_mux:
                # 中间 .ts 与成品同名会互相覆盖（拼完还要喂给 ffmpeg）
                raise HlsError("bad_path",
                               "--as 不要用 .ts/.m4s 作为成品名（封装需要独立的中间文件）；"
                               "想只落 .ts 请加 --no-mux")
        else:
            name = ("%s_%dp.mp4" % (stem, height)) if height else ("%s.mp4" % stem)
        mp4_path = _unique_path(os.path.join(args.out_dir, name))
        ts_path = os.path.splitext(mp4_path)[0] + ".ts"

        plan = hplan.build_plan(parsed, mp4_path, resume_ignore=args.resume_ignore,
                                vkey=vkey)
        if args.resume_ignore:
            warn("resume_forced")
        if plan["skipped"]:
            print("  断点续传：跳过 %d / %d 段" % (plan["skipped"], len(plan["segments"])))

        # ---- key 与 IV 预检（审查实锤：坏 key 不该先下 924 段再报错）----
        key_cache = {}
        for item in plan["segments"]:
            if item.get("key_uri") is None:
                continue
            if not item["key_uri"]:
                raise HlsError("hls_key_failed",
                               "第 %d 段引用的 EXT-X-KEY 缺少 URI" % item["idx"])
            if item.get("iv_hex"):
                crypto.iv_from_hex(item["iv_hex"])
            if item["key_uri"] not in key_cache:
                key_cache[item["key_uri"]] = crypto.load_key(
                    item["key_uri"],
                    lambda u, h=None: _get(ctx, args, u, base_host),
                    None, key_cache)
        if key_cache:
            print("  key 校验通过（%d 个）" % len(key_cache))

        last = {"t": 0.0}

        def on_progress(done, total, nbytes, failed):
            now = time.time()
            if now - last["t"] < 5 and done != total:
                return
            last["t"] = now
            rate = nbytes / max(1e-6, now - t0) / 1048576.0
            print("  seg %d/%d · %s · %.1f MB/s%s"
                  % (done, total, _fmt_mb(nbytes), rate,
                     (" · 失败 %d" % failed) if failed else ""))
            try:
                sys.stderr.flush()
            except Exception:
                pass

        def on_refresh():
            fresh = hparse.parse(_get_text(ctx, args, url, base_host), url)
            if hparse.fingerprint(fresh, vkey) != hparse.fingerprint(parsed, vkey):
                raise HlsError("hls_playlist_changed",
                               "刷新后的播放列表与断点指纹不符，停止（不出错片）")
            warn("playlist_refreshed")
            return {s["idx"]: s["url"] for s in fresh["segments"]}

        stats = hfetch.fetch_all(
            plan, lambda u, h=None, br=None: _get(ctx, args, u, base_host),
            headers=None, workers=args.workers, on_progress=on_progress,
            on_refresh=on_refresh, key_cache=key_cache)

        total_ts = hmux.concat_parts([s["file"] for s in plan["segments"]], ts_path)
        ffmpeg, source = hmux.find_ffmpeg(args.ffmpeg)
        if args.ffmpeg and not ffmpeg:
            raise HlsError("bad_path",
                           "--ffmpeg 指定的路径不存在：%s" % args.ffmpeg)
        muxed, container, final_path = False, "ts", ts_path
        if args.no_mux:
            pass
        elif not ffmpeg:
            warn("ffmpeg_missing")
            print("  未找到 ffmpeg：已保留 .ts 与断点；装好后重跑可秒过并封装")
        else:
            ok, err = hmux.mux_to_mp4(ts_path, mp4_path, ffmpeg)
            if ok:
                muxed, container, final_path = True, "mp4", mp4_path
                try:
                    os.remove(ts_path)
                except OSError:
                    pass
            else:
                # 半截 mp4 必须清掉：否则下次 _unique_path 改名 → 全量重下
                try:
                    if os.path.isfile(mp4_path):
                        os.remove(mp4_path)
                except OSError:
                    pass
                warn("mux_skipped")
                print("  封装失败，已保留 .ts：%s" % (err or "")[:200])

        dur_media = None
        if muxed:
            dur_media = hmux.probe_duration(final_path, hmux.find_ffprobe(ffmpeg))
            if dur_media and parsed.get("duration") \
                    and abs(dur_media - parsed["duration"]) > 2.0:
                warn("duration_mismatch")

        # 只有成品已产出（mp4 或显式 --no-mux）才清断点；
        # ffmpeg 缺失/封装失败时保留，兑现"装好 ffmpeg 重跑可秒过"
        if (muxed or args.no_mux) and not args.keep_parts:
            hplan.cleanup_parts(plan, keep=False)

        res.update({
            "ok": True,
            "path": final_path,
            "bytes": os.path.getsize(final_path) if os.path.isfile(final_path) else total_ts,
            "seconds": round(time.time() - t0, 2),
        })
        res["hls"] = _hls_block(parsed, url, height, plan, stats, muxed, container,
                                dur_media, args, source)
        if warnings:
            res["warning"] = ",".join(warnings)
        return 0

    except HlsError as e:
        res["error"], res["error_msg"] = e.code, e.msg[:200]
        res["hls"] = _hls_block(None, getattr(args, "url", None), 0, plan, stats,
                                False, "ts", None, args, None, error=e.code)
        print("\nHLS 下载失败：%s" % e.msg)
        return 1


def _hls_block(parsed, url, height, plan, stats, muxed, container, dur_media,
               args, source, error=None):
    block = {
        "variant_url": url,
        "quality": ("%dp" % height) if height else None,
        "segments_total": len(plan["segments"]) if plan else None,
        "segments_done": (stats or {}).get("done"),
        "segments_failed": len((stats or {}).get("failed") or []),
        "segments_skipped_resume": plan["skipped"] if plan else None,
        "encrypted": (parsed or {}).get("encrypted"),
        "key_count": len((parsed or {}).get("keys") or []),
        "muxed": muxed,
        "container": container,
        "duration_playlist": (parsed or {}).get("duration"),
        "duration_media": dur_media,
        "parts_dir": (plan or {}).get("parts_dir"),
        "ffmpeg_source": source,
        "install_command": (None if muxed or getattr(args, "no_mux", False)
                            else "ampull.py tools install ffmpeg --yes"),
    }
    if error:
        block["error_stage"] = error
    return block


# ------------------------------------------------------------------ tools
def run_tools(args, res, ctx):
    t0 = time.time()
    action, tool = args.action, args.tool or "ffmpeg"
    if tool not in htools.PINS:
        res.update({"ok": False, "mode": "tools", "action": action, "tool": tool,
                    "error": "tools_not_found",
                    "error_msg": "不支持的工具 %r（可选：%s）"
                                 % (tool, ", ".join(sorted(htools.PINS))),
                    "seconds": round(time.time() - t0, 2)})
        return 1
    try:
        if action == "list":
            rows = []
            path, source = hmux.find_ffmpeg()
            for name in sorted(htools.PINS):
                row = {
                    "tool": name,
                    "supported_platforms": sorted(htools.PINS[name]["assets"]),
                    "installed": htools.installed_versions(name),
                    "in_use": path if name == "ffmpeg" else None,
                    "source": source if name == "ffmpeg" else None,
                    "tools_root": htools.tools_root(name),
                }
                if args.scan and name == "ffmpeg":
                    # 只列候选（未烟测），让用户看到"别处自带的那份能不能借"
                    row["discovered"] = hmux._discover_candidates()[:10]
                rows.append(row)
            res.update({"ok": True, "mode": "tools", "action": "list",
                        "tool": None, "tools": rows,
                        "seconds": round(time.time() - t0, 2)})
            for r in rows:
                print("  %s：本地 %d 份；当前使用 %s（来源 %s）"
                      % (r["tool"], len(r["installed"]),
                         r["in_use"] or "未找到", r["source"] or "-"))
                for cand in (r.get("discovered") or []):
                    print("    候选（未探活）：%s" % cand)
            return 0

        if action == "path":
            path, source = hmux.find_ffmpeg()
            if path:
                res.update({"ok": True, "mode": "tools", "action": "path",
                            "tool": tool, "path": path, "source": source,
                            "seconds": round(time.time() - t0, 2)})
                print("  %s" % path)
                return 0
            res.update({"ok": False, "mode": "tools", "action": "path",
                        "tool": tool, "path": None, "source": None,
                        "error": "tools_not_found",
                        "error_msg": "未找到 ffmpeg（搜索链：--ffmpeg / "
                                     "AMPULL_FFMPEG / 本地 tools / PATH）；"
                                     "可跑 tools install 安装",
                        "seconds": round(time.time() - t0, 2)})
            return 1

        fetch_json = lambda u: _get_json(ctx, args, u)          # noqa: E731
        if action == "install":
            # 先看有没有"现成可用的"（PATH/共享位/自己 tools/其他程序自带）：
            # 有就不装第二份——多工具共用一份 ffmpeg 是用户明确偏好（2026-10-26）
            existing = hmux.find_ffmpeg()
            if existing[0] and not getattr(args, "force", False):
                note = ("已发现可用 ffmpeg（来源 %s）：%s —— 未安装第二份；"
                        "确实要装到程序目录请加 --force" % (existing[1], existing[0]))
                res.update({"ok": True, "mode": "tools", "action": "install",
                            "tool": tool, "path": existing[0],
                            "source": "reused", "ffmpeg_source": existing[1],
                            "dry_run": not args.yes, "note": note, "bytes": 0,
                            "seconds": round(time.time() - t0, 2)})
                print("  " + note)
                return 0
            if not args.yes:
                plan_txt = htools.plan_text(tool, fetch_json=fetch_json)
                res.update({"ok": True, "mode": "tools", "action": "install",
                            "tool": tool, "dry_run": True, "plan": plan_txt,
                            "seconds": round(time.time() - t0, 2)})
                print(plan_txt)
                return 0
            got = htools.install(tool, yes=True,
                                 fetch_bytes=lambda u: _get(ctx, args, u),
                                 fetch_json=fetch_json,
                                 fetch_to_file=lambda u, d: _download_to(ctx, args, u, d))
            res.update(got)
            print("  已安装 %s %s → %s" % (tool, got.get("version"), got.get("path")))
            return 0

        if action == "remove":
            got = htools.remove(tool, yes=args.yes)
            res.update(got)
            if got.get("dry_run"):
                print("  dry-run：将删除 %s；加 --yes 执行"
                      % (got.get("would_remove") or "（无本地副本）"))
            return 0

        res.update({"ok": False, "mode": "tools", "action": action, "tool": tool,
                    "error": "usage", "error_msg": "未知动作 %r" % action})
        return 1
    except HlsError as e:
        res.update({"ok": False, "mode": "tools", "action": action, "tool": tool,
                    "error": e.code, "error_msg": e.msg[:200],
                    "seconds": round(time.time() - t0, 2)})
        print("\n工具操作失败：%s" % e.msg)
        return 1


def run(sub, args, res, ctx):
    return run_hls(args, res, ctx) if sub == "hls" else run_tools(args, res, ctx)
