# -*- coding: utf-8 -*-
"""hls_plan —— 断点计划与账本（设计 §7.3）

目录布局：
  <out>.parts/
    meta.json            指纹/段数/创建时间（续传一致性判据）
    journal.jsonl        每段一行 {idx, ok, bytes, retry, ts}
    00000.ts …           已下并已解密的分片
"""

import json
import os
import threading
import time

from .parse import HlsError, fingerprint

META = "meta.json"
JOURNAL = "journal.jsonl"
# 多线程 worker 会并发记账：不加锁时 open("a")+write 可能交错，
# 造成丢行（load_journal 跳坏行 → 该段重下，正确性无损但续传变差）
_JOURNAL_LOCK = threading.Lock()


def parts_dir_for(out_path):
    """<dir>/<name>.mp4 → <dir>/<name>.parts"""
    root, _ = os.path.splitext(out_path)
    return root + ".parts"


def _read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path, obj):
    """原子写：先 .tmp 再 os.replace，避免断电留下半截 JSON。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def load_journal(parts_dir):
    """读账本 → {idx: record}。坏行跳过（日记格式，容忍半行）。"""
    out = {}
    path = os.path.join(parts_dir, JOURNAL)
    if not os.path.isfile(path):
        return out
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if isinstance(rec, dict) and "idx" in rec:
                out[int(rec["idx"])] = rec
    return out


def append_journal(parts_dir, rec):
    """追加一行账本（进程内串行，防并发写交错）。"""
    os.makedirs(parts_dir, exist_ok=True)
    path = os.path.join(parts_dir, JOURNAL)
    line = json.dumps(rec, ensure_ascii=False) + "\n"
    with _JOURNAL_LOCK:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)


def build_plan(parsed, out_path, resume_ignore=False, vkey=""):
    """生成任务计划；处理断点一致性（设计 §7.3 的指纹规则）。

    vkey = 档位身份（宽度x高度|带宽|CODECS，见 parse.variant_key）——
    必须进指纹，否则 480p 的断点会被 720p 的段"续"进去（审查实锤）。

    返回 plan：
      out_path, parts_dir, segments[{idx,url,file,key_uri,iv_hex,duration}],
      done(set[int]), skipped(int), stale_parts(str|None), fingerprint
    指纹不一致且未 --resume-ignore 时抛 HlsError("hls_playlist_changed")，
    并把旧 parts 目录改名归档（数据不丢）。
    """
    if parsed["kind"] != "media":
        raise HlsError("hls_parse_failed", "build_plan 需要 media 列表")
    if parsed.get("fmp4"):
        raise HlsError("hls_parse_failed",
                       "检测到 EXT-X-MAP（fMP4 初始化段），一期不支持")
    if parsed.get("encrypted") == "drm":
        raise HlsError("hls_drm",
                       "DRM 加密（%s），不支持" % (parsed.get("drm_reason") or "SAMPLE-AES"))
    key_uris = {(k.get("uri") or "") for k in (parsed.get("keys") or [])}
    if len(key_uris) > 1:
        # ADR-8：一期单 KEY；多 KEY 轮换明确拒绝，不猜（猜出来的是错片）
        raise HlsError("hls_parse_failed",
                       "多 KEY 轮换（%d 个不同 key URI），一期不支持" % len(key_uris))
    if not parsed.get("endlist") and (parsed.get("playlist_type") or "") != "VOD":
        raise HlsError("hls_live_unsupported",
                       "没有 #EXT-X-ENDLIST 且不是 PLAYLIST-TYPE:VOD，疑似直播/滑动"
                       "窗口，一期不支持")

    fp = fingerprint(parsed, vkey)
    parts_dir = parts_dir_for(out_path)
    stale_parts = None
    meta = _read_json(os.path.join(parts_dir, META))

    if meta and meta.get("fingerprint") and meta["fingerprint"] != fp:
        if not resume_ignore:
            # 归档而不是删除：用户只是拿错链接时，已下的几百 MB 不能白白消失
            stale_parts = _archive_parts(parts_dir)
            raise HlsError(
                "hls_playlist_changed",
                "播放列表内容已变（指纹 %s → %s），旧断点已归档到 %s；"
                "确认要重下就删掉该归档目录后重跑（或 --resume-ignore 强制复用旧分片）"
                % (meta["fingerprint"], fp, stale_parts))
        # resume_ignore：继续用旧账本，收据里由上层打 resume_forced

    os.makedirs(parts_dir, exist_ok=True)
    # 指纹不一致且用户用 --resume-ignore 强续时，**保留旧 meta**：
    # 否则把新指纹写进去，下次就"看起来一致"了，等于把混拼隐患永久洗白
    if not meta:
        _write_json(os.path.join(parts_dir, META), {
            "fingerprint": fp,
            "segments_total": len(parsed["segments"]),
            "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        })
    elif not meta.get("fingerprint"):
        _write_json(os.path.join(parts_dir, META), {
            "fingerprint": fp,
            "segments_total": len(parsed["segments"]),
            "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        })

    journal = load_journal(parts_dir)
    segments, done = [], set()
    widths = max(4, len(str(len(parsed["segments"]) - 1)))
    for seg in parsed["segments"]:
        idx = seg["idx"]
        fname = ("%0*d.ts" % (widths, idx))
        path = os.path.join(parts_dir, fname)
        key_uri = None
        iv_hex = None
        if seg.get("key_id") is not None:
            k = parsed["keys"][seg["key_id"]]
            key_uri = k.get("uri")
            iv_hex = k.get("iv_hex")
        item = {
            "idx": idx,
            "url": seg["url"],
            "file": path,
            "key_uri": key_uri,
            "iv_hex": iv_hex,
            "duration": seg.get("duration"),
            "byterange": seg.get("byterange"),
        }
        if seg.get("byterange"):
            raise HlsError("hls_parse_failed",
                           "检测到 EXT-X-BYTERANGE 分片，一期不支持（二期）")
        rec = journal.get(idx)
        if rec and rec.get("ok") and os.path.isfile(path) \
                and os.path.getsize(path) > 0:
            done.add(idx)
        segments.append(item)

    return {
        "out_path": out_path,
        "parts_dir": parts_dir,
        "segments": segments,
        "done": done,
        "skipped": len(done),
        "stale_parts": stale_parts,
        "fingerprint": fp,
        "media_sequence": parsed.get("media_sequence") or 0,
    }


def _archive_parts(parts_dir):
    """把旧断点目录改名成 .parts.stale-<ts>，返回新路径（失败则返回原路径）。"""
    if not os.path.isdir(parts_dir):
        return parts_dir
    dest = "%s.stale-%s" % (parts_dir, time.strftime("%Y%m%d%H%M%S"))
    try:
        os.replace(parts_dir, dest)
        return dest
    except OSError:
        return parts_dir


def cleanup_parts(plan, keep=False):
    """成功后清理：删分片与账本（keep=True 时保留）。"""
    import shutil
    if keep:
        return False
    path = plan.get("parts_dir")
    if path and os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)
        return True
    return False
