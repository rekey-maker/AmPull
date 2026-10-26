# -*- coding: utf-8 -*-
"""hls_fetch —— 分片级并行取数（设计 §7.2 状态机、§7.4 重试）

并发单位是**分片**（不是分片内的分块）：实测分块模型 0.35 文件/s，
分片并行 2.5 文件/s。网络取数由调用方注入 fetch 函数（走 AmPull 的 opener），
本模块只管调度、解密、校验、落盘与账本，便于离线桩测。
"""

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import crypto
from . import plan as hplan
from .parse import HlsError

_RETRY_BACKOFF = (0.5, 1.0, 2.0)
# 只有鉴权/签名类值得"重拉播放列表"；410 是 URL 级消失，重拉也没用
_AUTH_CODES = ("http_401", "http_403")


def _write_atomic(path, data):
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def _as_hls_error(e):
    """把外部异常（尤其 AmPull 的 Fail）归一到 HlsError，保住 error 码。

    审查实锤：`_get` 抛的是主流程 Fail，worker 里 `except HlsError` 不命中，
    403 被包成 network → 「整批 403 重拉列表」永不触发。这里统一归一。
    """
    if isinstance(e, HlsError):
        return e
    code = getattr(e, "code", None)
    if code:
        return HlsError(code, str(e))
    return HlsError("network", "%s: %s" % (type(e).__name__, e))


def _segment_worker(item, ctx):
    """单段：取 → 解 → 校验 → 落盘 → 记账。返回 (idx, bytes, retries)。"""
    idx = item["idx"]
    last_err = None
    for attempt in range(len(_RETRY_BACKOFF) + 1):
        if ctx["abort"].is_set():
            raise HlsError("cancelled", "已中止（前序失败过多）")
        try:
            raw = ctx["fetch"](item["url"], ctx["headers"], item.get("byterange"))
            if raw is None:
                raise HlsError("network", "分片返回空")
            if item.get("key_uri"):
                key = ctx["key_cache"].get(item["key_uri"])
                if key is None:
                    key = crypto.load_key(item["key_uri"], ctx["fetch"],
                                          ctx["headers"], ctx["key_cache"])
                iv = (crypto.iv_from_hex(item["iv_hex"])
                      if item.get("iv_hex")
                      else crypto.iv_from_sequence(ctx["media_sequence"] + idx))
                plain = crypto.decrypt_segment(raw, key, iv)
            else:
                plain = raw
            crypto.validate_ts(plain)
            _write_atomic(item["file"], plain)
            hplan.append_journal(ctx["parts_dir"], {
                "idx": idx, "ok": True, "bytes": len(plain), "retry": attempt,
                "ts": int(time.time())})
            return idx, len(plain), attempt
        except Exception as e:                                  # noqa: BLE001
            last_err = _as_hls_error(e)
            if last_err.code in _AUTH_CODES or last_err.code == "cancelled":
                raise last_err
        if attempt < len(_RETRY_BACKOFF):
            time.sleep(_RETRY_BACKOFF[attempt])
    raise last_err or HlsError("network", "未知失败")


def fetch_all(plan, fetch, headers=None, workers=8, on_progress=None,
              on_refresh=None, max_consecutive_fail=5, key_cache=None):
    """并行取全部未完成分片。

    fetch(url, headers, byterange) -> bytes   # 由 cmd 层包装 AmPull 的 opener
    on_progress(done, total, bytes, failed)   # 进度回调（5s 节流由调用方做）
    on_refresh() -> {idx: new_url} | None     # 分片签名过期时刷新播放列表（只调一次）
    key_cache 传入已预取的密钥（避免 worker 首轮并发各取一次 key）
    """
    workers = max(1, min(16, int(workers or 8)))
    segs = [s for s in plan["segments"] if s["idx"] not in plan["done"]]
    total = len(plan["segments"])
    skipped = len(plan["done"])
    if not segs:
        return {"done": total, "failed": [], "skipped": skipped,
                "bytes": 0, "refreshed": False, "seconds": 0.0, "aborted": None}

    ctx = {"fetch": fetch, "headers": headers or {},
           "key_cache": dict(key_cache or {}),
           "media_sequence": plan.get("media_sequence") or 0,
           "parts_dir": plan["parts_dir"], "abort": threading.Event()}

    lock = threading.Lock()
    state = {"done": skipped, "bytes": 0, "failed": [], "consecutive": 0,
             "hard": 0, "refreshed": False, "aborted": None}
    for s in plan["segments"]:
        if s["idx"] in plan["done"]:
            try:
                state["bytes"] += os.path.getsize(s["file"])
            except OSError:
                pass
    t0 = time.time()

    def _report():
        if on_progress:
            try:
                on_progress(state["done"], total, state["bytes"], len(state["failed"]))
            except Exception:
                pass

    def _abort_reason():
        """中止判据：连续失败达阈值，或**非鉴权类**失败过半且至少 5 段。

        鉴权类（401/403）不计入：整批 403 是"签名过期"的典型形态，正确处置
        是重拉播放列表一次（on_refresh），而不是中止。
        """
        if state["consecutive"] >= max_consecutive_fail:
            return "连续 %d 段失败" % state["consecutive"]
        if state["hard"] >= 5 and state["failed_rate_base"] \
                and state["hard"] * 2 >= state["failed_rate_base"]:
            return "失败率过高（%d/%d）" % (state["hard"], state["failed_rate_base"])
        return None

    def _run(batch):
        if not batch:
            return
        state["failed_rate_base"] = len(batch)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(_segment_worker, s, ctx): s for s in batch}
            try:
                for fut in as_completed(futs):
                    seg = futs[fut]
                    try:
                        idx, nbytes, _retries = fut.result()
                    except Exception as e:                      # noqa: BLE001
                        err = _as_hls_error(e)
                        with lock:
                            state["failed"].append((seg["idx"], err.code, err.msg))
                            if err.code in _AUTH_CODES:
                                pass        # 鉴权类不计连续/速率，交给刷新分支
                            else:
                                state["hard"] += 1
                                state["consecutive"] += 1
                                if not state["aborted"]:
                                    reason = _abort_reason()
                                    if reason:
                                        state["aborted"] = reason
                                        ctx["abort"].set()
                        if state["aborted"]:
                            for f in futs:
                                f.cancel()
                            break
                        continue
                    with lock:
                        state["done"] += 1
                        state["bytes"] += nbytes
                        state["consecutive"] = 0
                    _report()
            finally:
                # 中止时不再等剩余任务跑完 3 次退避（worker 会在下一轮循环开头看到 abort）
                try:
                    pool.shutdown(wait=True, cancel_futures=True)
                except TypeError:
                    # Python 3.8 没有 cancel_futures（README 声明 3.8+，不能破）
                    pool.shutdown(wait=True)

    _report()
    _run(segs)

    # 签名过期：整批 403 时刷新播放列表一次，换 URL 重试失败段
    if on_refresh and not state["refreshed"] and state["failed"] and not state["aborted"]:
        if any(f[1] in _AUTH_CODES for f in state["failed"]):
            state["refreshed"] = True
            try:
                new_urls = on_refresh() or {}
            except Exception:                                    # noqa: BLE001
                new_urls = {}
            if new_urls:
                retry = []
                for seg in plan["segments"]:
                    if seg["idx"] in new_urls and seg["idx"] not in plan["done"] \
                            and not os.path.isfile(seg["file"]):
                        seg["url"] = new_urls[seg["idx"]]
                        retry.append(seg)
                state["failed"] = []
                state["consecutive"] = 0
                state["hard"] = 0
                state["aborted"] = None
                ctx["abort"].clear()
                _run(retry)

    result = {
        "done": state["done"], "failed": state["failed"], "skipped": skipped,
        "bytes": state["bytes"], "refreshed": state["refreshed"],
        "seconds": round(time.time() - t0, 2), "aborted": state["aborted"],
    }
    if result["failed"]:
        idx, code, msg = result["failed"][0]
        raise HlsError("hls_segment_failed",
                       "%d 段失败（首例第 %d 段 %s：%s）%s"
                       % (len(result["failed"]), idx, code, msg[:80],
                          ("；已中止：%s" % result["aborted"])
                          if result["aborted"] else ""))
    return result
