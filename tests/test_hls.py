# -*- coding: utf-8 -*-
"""test_hls.py —— ampull_hls 离线仿真套件（零外网）

覆盖：解析/选码率/指纹/解密/PKCS7/TS 校验/断点/播放列表变更/并行取数/403 刷新。
手法与 test_proxy_scan.py 一致：本地真 socket 桩 + 纯文本 fixture。
Usage: python tests/test_hls.py -> ALL GREEN
"""
import base64
import http.server
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from ampull_hls import crypto, fetch as hfetch, mux, parse as hparse, plan as hplan  # noqa: E402
from ampull_hls.parse import HlsError  # noqa: E402

PASSED = []
FAILED = []


def check(name, cond, detail=""):
    if cond:
        PASSED.append(name)
        print("  [ok] %s" % name)
    else:
        FAILED.append((name, detail))
        print("  [FAIL] %s  %s" % (name, detail))


def expect_error(name, code, fn, *a, **kw):
    try:
        fn(*a, **kw)
    except HlsError as e:
        check(name, e.code == code, "期望 %s，实得 %s（%s）" % (code, e.code, e.msg))
    except Exception as e:  # noqa: BLE001
        check(name, False, "抛了非 HlsError：%s: %s" % (type(e).__name__, e))
    else:
        check(name, False, "没有抛错（期望 %s）" % code)


# ---------------------------------------------------------------- fixtures
def media_text(n=3, key=True, endlist=True, extra=""):
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:6",
             "#EXT-X-MEDIA-SEQUENCE:0"]
    if key:
        lines.append('#EXT-X-KEY:METHOD=AES-128,URI="key.key"')
    if extra:
        lines.extend(extra.split("\n"))
    for i in range(n):
        lines.append("#EXTINF:6.000,")
        lines.append("seg%d.ts" % i)
    if endlist:
        lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines)


MASTER = "\n".join([
    "#EXTM3U",
    '#EXT-X-STREAM-INF:PROGRAM-ID=1,BANDWIDTH=500000,RESOLUTION=854x480,CODECS="avc1.4d401f,mp4a.40.2"',
    "480/index.m3u8",
    "#EXT-X-STREAM-INF:BANDWIDTH=1000000,RESOLUTION=1280x720",
    "720/index.m3u8",
    "#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1920x1080",
    "1080/index.m3u8",
])


def aes_encrypt(plain, key, iv):
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    padder = padding.PKCS7(128).padder()
    data = padder.update(plain) + padder.finalize()
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return enc.update(data) + enc.finalize()


def fake_ts(npackets=3, fill=b"\x00"):
    """造合法 TS：npackets × 188，每包首字节 0x47。"""
    out = bytearray()
    for _ in range(npackets):
        out += b"\x47" + fill * 187
    return bytes(out)


# ---------------------------------------------------------------- 解析
def test_parse():
    print("\n[1] 解析与选码率")
    p = hparse.parse(MASTER, "https://x.com/v/index.m3u8")
    check("master 识别", p["kind"] == "master")
    check("master 三档", len(p["variants"]) == 3)
    check("相对路径解析", p["variants"][0]["url"] == "https://x.com/v/480/index.m3u8")
    check("CODECS 引号内逗号不被切", "avc1" in p["variants"][0]["codecs"])

    # RESOLUTION 是 宽x高：720x406 的"高"是 406 不是 720（当日样本就是这种标法）
    odd = hparse.parse("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1,RESOLUTION=720x406\na.m3u8",
                       "https://x.com/v/index.m3u8")
    check("RESOLUTION 取第二个数为高", odd["variants"][0]["height"] == 406
          and odd["variants"][0]["width"] == 720, str(odd["variants"][0]))

    v, warn = hparse.pick_variant(p, "720")
    check("--quality 720 命中 720", v["height"] == 720 and warn is None, str(v))
    v, warn = hparse.pick_variant(p, "900")
    check("--quality 900 → 最接近的 1080", v["height"] == 1080 and warn is None)
    v, warn = hparse.pick_variant(p, "best")
    check("best 取带宽最高", v["height"] == 1080)
    v, warn = hparse.pick_variant(p, "worst")
    check("worst 取带宽最低", v["height"] == 480)
    v, warn = hparse.pick_variant(p, "4000")
    check("超出上限退最高档 + quality_fallback", v["height"] == 1080 and warn == "quality_fallback")

    # 站方以**宽度**标 720（真实样本 720x406 的 16:9 片源）：高度不达标时按宽度兜底
    width_only = hparse.parse("\n".join([
        "#EXTM3U",
        "#EXT-X-STREAM-INF:BANDWIDTH=500000,RESOLUTION=480x270", "a.m3u8",
        "#EXT-X-STREAM-INF:BANDWIDTH=1000000,RESOLUTION=720x406", "b.m3u8",
    ]), "https://x.com/v/index.m3u8")
    v, warn = hparse.pick_variant(width_only, "720")
    check("高度不达标→按宽度命中 720 档（quality_matched_by_width）",
          v["width"] == 720 and warn == "quality_matched_by_width",
          "%s / %s" % (v, warn))
    v, warn = hparse.pick_variant(width_only, "2000")
    check("宽高都不达标 → fallback 最高档", v["width"] == 720 and warn == "quality_fallback")
    expect_error("非法 quality 报 hls_variant_missing", "hls_variant_missing",
                 hparse.pick_variant, p, "ultra")

    m = hparse.parse(media_text(4), "https://x.com/v/720/index.m3u8")
    check("media 识别", m["kind"] == "media" and len(m["segments"]) == 4)
    check("分片相对路径", m["segments"][0]["url"] == "https://x.com/v/720/seg0.ts")
    check("总时长累加", abs(m["duration"] - 24.0) < 0.01, str(m["duration"]))
    check("AES-128 识别", m["encrypted"] == "aes-128")
    check("单 key", len(m["keys"]) == 1)
    check("key URI 相对路径被解析成绝对",
          m["keys"][0]["uri"] == "https://x.com/v/720/key.key",
          str(m["keys"][0]["uri"]))
    check("endlist", m["endlist"] is True)

    # BOM + CRLF + 行尾空白
    weird = "\ufeff" + media_text(2).replace("\n", "\r\n") + "\r\n   \r\n"
    w = hparse.parse(weird, "https://x.com/a/")
    check("BOM/CRLF/空行容忍", len(w["segments"]) == 2)

    # 透明流（无 KEY）
    plain = hparse.parse(media_text(2, key=False), "https://x.com/a/")
    check("无 KEY → encrypted=None", plain["encrypted"] is None)

    # 截断
    expect_error("EXTINF 无 URI 报错", "hls_parse_failed",
                 hparse.parse, "#EXTM3U\n#EXTINF:6,\n", "https://x/")
    expect_error("首行不是 #EXTM3U 报错", "hls_parse_failed",
                 hparse.parse, "<html>login</html>", "https://x/")
    expect_error("0 段报错", "hls_parse_failed",
                 hparse.parse, "#EXTM3U\n#EXT-X-ENDLIST\n", "https://x/")

    # DRM 形态
    drm = hparse.parse(media_text(2, extra='#EXT-X-KEY:METHOD=SAMPLE-AES,URI="k"'),
                       "https://x/")
    check("SAMPLE-AES → drm", drm["encrypted"] == "drm")
    drm2 = hparse.parse(
        media_text(2, extra='#EXT-X-KEY:METHOD=AES-128,URI="k",'
                            'KEYFORMAT="urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"'),
        "https://x/")
    check("非 identity KEYFORMAT → drm", drm2["encrypted"] == "drm")

    # fMP4 / BYTERANGE 识别
    fmp4 = hparse.parse(media_text(2, extra='#EXT-X-MAP:URI="init.mp4"'), "https://x/")
    check("EXT-X-MAP → fmp4", fmp4["fmp4"] is True)
    br = hparse.parse(media_text(2, extra="#EXT-X-BYTERANGE:1000@0"), "https://x/")
    check("BYTERANGE 解析", br["segments"][0]["byterange"] == (0, 1000))


# ---------------------------------------------------------------- 指纹
def test_fingerprint():
    print("\n[2] 内容指纹")
    a = hparse.parse(media_text(3), "https://a.com/x/index.m3u8")
    b = hparse.parse(media_text(3).replace("seg", "other"),
                     "https://a.com/x/index.m3u8")
    check("仅 URI 变化 → 指纹不变", hparse.fingerprint(a) == hparse.fingerprint(b))
    c = hparse.parse(media_text(3).replace("#EXTINF:6.000", "#EXTINF:8.000"),
                     "https://a.com/x/index.m3u8")
    check("时长变化 → 指纹变化", hparse.fingerprint(a) != hparse.fingerprint(c))
    d = hparse.parse(media_text(4), "https://a.com/x/index.m3u8")
    check("段数变化 → 指纹变化", hparse.fingerprint(a) != hparse.fingerprint(d))


# ---------------------------------------------------------------- 解密
def test_crypto():
    print("\n[3] 解密与 TS 校验")
    key = b"83d620fcc8ecafc8"
    iv = crypto.iv_from_sequence(7)
    check("IV 序列 → 16 字节", len(iv) == 16 and iv[-1] == 7)
    check("IV hex 32 位", crypto.iv_from_hex("00" * 15 + "ff") == bytes(15) + b"\xff")
    check("IV hex 短位左补零", crypto.iv_from_hex("ff") == bytes(15) + b"\xff")
    expect_error("IV 非法十六进制", "hls_key_failed", crypto.iv_from_hex, "zz")

    ts = fake_ts(3)
    enc = aes_encrypt(ts, key, iv)
    out = crypto.decrypt_segment(enc, key, iv)
    check("AES-128 往返一致", out == ts, "%d → %d" % (len(enc), len(out)))
    crypto.validate_ts(out)

    # PKCS7 用段级数据测（16 字节小数据会撞对齐启发式的边界，不代表真实场景）
    plain376 = fake_ts(2)                                   # 376 = 188*2
    check("PKCS7 标准填充（8 字节）", crypto.strip_pkcs7(plain376 + b"\x08" * 8) == plain376)
    check("PKCS7 零填充（8 个 0x00）", crypto.strip_pkcs7(plain376 + b"\x00" * 8) == plain376)
    check("PKCS7 未填充但已对齐", crypto.strip_pkcs7(plain376) == plain376)

    expect_error("空数据 → hls_seg_corrupt", "hls_seg_corrupt", crypto.validate_ts, b"")
    expect_error("长度不对 → hls_seg_corrupt", "hls_seg_corrupt",
                 crypto.validate_ts, b"\x47" + b"\x00" * 100)
    m2ts = b"\x00\x00\x00\x00" + b"\x47" + b"\x00" * 187
    expect_error("192 字节 M2TS 变体明确报错", "hls_seg_corrupt",
                 crypto.validate_ts, m2ts * 2)
    rs = b"\x47" + b"\x00" * 203
    expect_error("204 字节变体明确报错", "hls_seg_corrupt", crypto.validate_ts, rs * 2)


# ---------------------------------------------------------------- 计划/断点
def test_plan():
    print("\n[4] 计划与断点")
    tmp = tempfile.mkdtemp(prefix="hlsplan_")
    try:
        out = os.path.join(tmp, "v.mp4")
        p = hparse.parse(media_text(3), "https://x.com/v/index.m3u8")
        pl = hplan.build_plan(p, out)
        check("parts 目录命名", pl["parts_dir"].endswith("v.parts"))
        check("meta 落盘", os.path.isfile(os.path.join(pl["parts_dir"], "meta.json")))
        check("3 段计划", len(pl["segments"]) == 3 and pl["skipped"] == 0)

        # 模拟第一段完成 → 重建应跳过
        seg0 = pl["segments"][0]
        with open(seg0["file"], "wb") as f:
            f.write(fake_ts(2))
        hplan.append_journal(pl["parts_dir"], {"idx": 0, "ok": True,
                                               "bytes": 376, "retry": 0})
        pl2 = hplan.build_plan(p, out)
        check("断点跳过已 done", pl2["skipped"] == 1 and 0 in pl2["done"])

        # 列表变化（时长变）→ 报错 + 归档旧断点
        p3 = hparse.parse(media_text(3).replace("#EXTINF:6.000", "#EXTINF:9.000"),
                          "https://x.com/v/index.m3u8")
        expect_error("指纹不符 → hls_playlist_changed", "hls_playlist_changed",
                     hplan.build_plan, p3, out)
        stale = [d for d in os.listdir(tmp) if ".parts.stale-" in d]
        check("旧断点被归档而非删除", len(stale) == 1, str(os.listdir(tmp)))

        # --resume-ignore 强续不报错
        pl4 = hplan.build_plan(p3, out, resume_ignore=True)
        check("resume-ignore 可强续", pl4["segments"][0]["idx"] == 0)

        # 「不洗白指纹」要单开目录测：上面的旧断点已被归档，新目录没有旧 meta
        out2 = os.path.join(tmp, "u.mp4")
        hplan.build_plan(p, out2)
        hplan.build_plan(p3, out2, resume_ignore=True)
        with open(os.path.join(hplan.parts_dir_for(out2), "meta.json"),
                  encoding="utf-8") as f:
            meta_after = json.load(f)
        check("resume-ignore 不洗白旧指纹",
              meta_after.get("fingerprint") == hparse.fingerprint(p),
              "%s vs %s" % (meta_after.get("fingerprint"),
                            hparse.fingerprint(p)))

        # 边界：live / fmp4 / byterange / drm
        live = hparse.parse(media_text(2, endlist=False), "https://x/")
        expect_error("无 ENDLIST → live 拒", "hls_live_unsupported",
                     hplan.build_plan, live, os.path.join(tmp, "a.mp4"))
        fmp4 = hparse.parse(media_text(2, extra='#EXT-X-MAP:URI="i.mp4"'), "https://x/")
        expect_error("fMP4 → 一期拒", "hls_parse_failed",
                     hplan.build_plan, fmp4, os.path.join(tmp, "b.mp4"))
        br = hparse.parse(media_text(2, extra="#EXT-X-BYTERANGE:1000@0"), "https://x/")
        expect_error("BYTERANGE → 一期拒", "hls_parse_failed",
                     hplan.build_plan, br, os.path.join(tmp, "c.mp4"))
        drm = hparse.parse(media_text(2, extra='#EXT-X-KEY:METHOD=SAMPLE-AES,URI="k"'),
                           "https://x/")
        expect_error("DRM → hls_drm", "hls_drm",
                     hplan.build_plan, drm, os.path.join(tmp, "d.mp4"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 本地 HTTP 桩
class _Stub:
    """可控分片服务器：data{path: bytes}，behaviour(path) -> 'ok'|'403'|'500'。"""

    def __init__(self, data, behaviour=None):
        self.data = dict(data)
        self.behaviour = behaviour or (lambda p: "ok")
        self.hits = {}
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                path = self.path.split("?")[0].lstrip("/")
                outer.hits[path] = outer.hits.get(path, 0) + 1
                mode = outer.behaviour(path)
                if mode == "403":
                    self.send_response(403)
                    self.end_headers()
                    return
                if mode == "500":
                    self.send_response(500)
                    self.end_headers()
                    return
                body = outer.data.get(path)
                if body is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def url(self, path=""):
        return "http://127.0.0.1:%d/%s" % (self.port, path.lstrip("/"))

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def _make_fetcher():
    import urllib.request

    def fetch(url, headers=None, byterange=None):
        req = urllib.request.Request(url, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            raise HlsError("http_%d" % e.code, "HTTP %d: %s" % (e.code, url))
        except urllib.error.URLError as e:
            raise HlsError("network", str(e)[:100])

    return fetch


def test_fetch_e2e():
    print("\n[5] 并行取数（本地桩，AES-128 端到端）")
    old_backoff = hfetch._RETRY_BACKOFF
    hfetch._RETRY_BACKOFF = (0, 0, 0)
    tmp = tempfile.mkdtemp(prefix="hlsfetch_")
    stub = None
    try:
        key = b"0123456789abcdef"
        plain_segs = [fake_ts(3, bytes([i])) for i in range(6)]
        seg_data, key_ok = {}, True
        for i, plain in enumerate(plain_segs):
            iv = crypto.iv_from_sequence(i)
            seg_data["seg%d.ts" % i] = aes_encrypt(plain, key, iv)
        seg_data["key.key"] = key

        stub = _Stub(seg_data)
        out = os.path.join(tmp, "v.mp4")
        p = hparse.parse(
            media_text(6).replace('URI="key.key"', 'URI="%s"' % stub.url("key.key")),
            stub.url("index.m3u8"))
        pl = hplan.build_plan(p, out)
        for seg in pl["segments"]:
            seg["url"] = stub.url("seg%d.ts" % seg["idx"])
        progress = []
        res = hfetch.fetch_all(pl, _make_fetcher(), workers=3,
                               on_progress=lambda d, t, b, f: progress.append((d, t, b)))
        check("6 段全部完成", res["done"] == 6 and not res["failed"], str(res))
        check("字节数与明文一致", res["bytes"] == sum(len(x) for x in plain_segs))
        check("进度回调有值且终态 6/6", progress and progress[-1][0] == 6, str(progress[-1:]))
        for i in range(6):
            with open(pl["segments"][i]["file"], "rb") as f:
                check("第 %d 段解密正确" % i, f.read() == plain_segs[i])

        ts_path = os.path.join(tmp, "v.ts")
        concat_bytes = mux.concat_parts([s["file"] for s in pl["segments"]], ts_path)
        check("拼接字节数", concat_bytes == sum(len(x) for x in plain_segs))

        # 账本：fetch 成功后 journal 必须落盘，且重跑能跳过（续传不是假实现）
        jpath = os.path.join(pl["parts_dir"], "journal.jsonl")
        check("journal 已写入", os.path.isfile(jpath))
        if os.path.isfile(jpath):
            with open(jpath, encoding="utf-8") as f:
                ok_lines = [l for l in f if '"ok": true' in l]
            check("journal 6 条成功记录", len(ok_lines) == 6, str(len(ok_lines)))
        pl_again = hplan.build_plan(p, out)
        check("重跑计划跳过已下 6 段", pl_again["skipped"] == 6, str(pl_again["skipped"]))

        # 403 刷新：第一次全 403，on_refresh 把开关拨回 ok
        allow = {"ok": False}
        stub2 = _Stub(seg_data, behaviour=lambda p: "ok" if allow["ok"] or p == "key.key" else "403")
        out2 = os.path.join(tmp, "w.mp4")
        p2 = hparse.parse(
            media_text(6).replace('URI="key.key"', 'URI="%s"' % stub2.url("key.key")),
            stub2.url("index.m3u8"))
        pl2 = hplan.build_plan(p2, out2)
        refreshed = {"n": 0}

        def on_refresh():
            refreshed["n"] += 1
            allow["ok"] = True
            return {s["idx"]: stub2.url("seg%d.ts" % s["idx"]) for s in pl2["segments"]}

        res2 = hfetch.fetch_all(pl2, _make_fetcher(), workers=2,
                                on_refresh=on_refresh)
        check("403 触发一次刷新后成功", res2["done"] == 6 and refreshed["n"] == 1,
              "refreshed=%s res=%s" % (refreshed["n"], res2))
        stub2.close()

        # 连续失败中止
        stub3 = _Stub(seg_data, behaviour=lambda p: "500")
        out3 = os.path.join(tmp, "x.mp4")
        p3 = hparse.parse(
            media_text(6).replace('URI="key.key"', 'URI="%s"' % stub3.url("key.key")),
            stub3.url("index.m3u8"))
        pl3 = hplan.build_plan(p3, out3)
        expect_error("连续失败 → hls_segment_failed", "hls_segment_failed",
                     hfetch.fetch_all, pl3, _make_fetcher(), None, 2,
                     None, None, 2)
        stub3.close()
    finally:
        hfetch._RETRY_BACKOFF = old_backoff
        if stub:
            stub.close()
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 封装降级
def test_mux():
    print("\n[6] 封装与降级")
    path, source = mux.find_ffmpeg(explicit=None)
    check("ffmpeg 搜索链不抛异常",
          source in ("env", "local", "shared", "path", None), str(source))
    check("找不到时不报错，返回 None", mux.find_ffmpeg(explicit="/no/such/ffmpeg")[0] is None)
    check("ffprobe 缺失容忍", mux.probe_duration("x.mp4", None) is None)


def test_cli():
    """子命令接线：真起进程跑 `ampull.py hls`，验证收据/退出码/落盘。"""
    import json
    import subprocess
    print("\n[7] CLI 端到端（子命令接线）")
    old_backoff = hfetch._RETRY_BACKOFF
    tmp = tempfile.mkdtemp(prefix="hlscli_")
    stub = None
    try:
        key = b"0123456789abcdef"
        data = {"key.key": key}
        plains = []
        for i in range(3):
            plain = fake_ts(2, bytes([i]))
            plains.append(plain)
            data["s%d.ts" % i] = aes_encrypt(plain, key, crypto.iv_from_sequence(i))
        play = "\n".join([
            "#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:6",
            "#EXT-X-MEDIA-SEQUENCE:0",
            '#EXT-X-KEY:METHOD=AES-128,URI="KEYURL"',
            "#EXTINF:6.0,", "s0.ts",
            "#EXTINF:6.0,", "s1.ts",
            "#EXTINF:6.0,", "s2.ts",
            "#EXT-X-ENDLIST",
        ])
        stub = _Stub(data)
        # _Stub 内部对 data 做了浅拷贝，所以这里改 stub.data（不是外面的 data）
        stub.data["index.m3u8"] = play.replace(
            "KEYURL", stub.url("key.key")).encode("utf-8")
        out_dir = os.path.join(tmp, "out")
        cmd = [sys.executable, os.path.join(ROOT, "ampull.py"), "hls",
               stub.url("index.m3u8"), out_dir, "--json", "--no-mux",
               "--workers", "2"]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=120)
        lines = [l for l in proc.stdout.decode("utf-8", "replace").splitlines() if l.strip()]
        rec = json.loads(lines[-1]) if lines else {}
        check("CLI 退出码 0", proc.returncode == 0,
              "rc=%s stderr=%s" % (proc.returncode,
                                   proc.stderr.decode("utf-8", "replace")[-300:]))
        check("CLI 收据 ok", rec.get("ok") is True, str(rec)[:200])
        check("CLI 只有一行 JSON", len(lines) == 1, "lines=%d" % len(lines))
        check("hls 子对象段数", (rec.get("hls") or {}).get("segments_done") == 3,
              str(rec.get("hls")))
        check("产物为 .ts（--no-mux）", (rec.get("path") or "").endswith(".ts"),
              str(rec.get("path")))
        check("产物存在且非空", rec.get("path") and os.path.getsize(rec["path"]) > 0)
        check("mp4 未被创建", not os.path.exists(os.path.join(out_dir, "index_6p.mp4")))

        # 参数缺失 → usage（退出码 1，JSON 有 error）
        bad = subprocess.run([sys.executable, os.path.join(ROOT, "ampull.py"),
                              "hls", "--json"], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, timeout=60)
        blines = [l for l in bad.stdout.decode("utf-8", "replace").splitlines() if l.strip()]
        brec = json.loads(blines[-1]) if blines else {}
        check("缺参与 → 退出码 1", bad.returncode == 1, "rc=%s" % bad.returncode)
        check("缺参与 → error=usage", brec.get("error") == "usage", str(brec)[:160])

        def _run(*av, **kw):
            p = subprocess.run([sys.executable, os.path.join(ROOT, "ampull.py")] + list(av),
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               timeout=60, **kw)
            ls = [l for l in p.stdout.decode("utf-8", "replace").splitlines() if l.strip()]
            return p.returncode, (json.loads(ls[-1]) if ls else {}), ls

        # P0 回归：tools --yes 曾被主流程"必须配 --clean"拦死
        rc, rec_t, _ = _run("tools", "list", "--yes", "--json")
        check("tools --yes 不再被 --clean 校验拦死", rc == 0 and rec_t.get("ok"),
              "rc=%s %s" % (rc, str(rec_t)[:160]))

        # --json 写在子命令前面 → 明确 usage，不许静默退化成批量下载
        rc, rec_j, _ = _run("--json", "hls", "nosuch://x", tmp)
        check("--json 前置 → usage 而不是批量", rc == 1 and rec_j.get("error") == "usage",
              "rc=%s %s" % (rc, str(rec_j)[:160]))

        # 已有可用 ffmpeg 时，install 不该再装第二份（用户明确偏好共用）
        from ampull_hls import mux as _m
        rc, rec_i, _l = _run("tools", "install", "ffmpeg", "--json")
        if _m.find_ffmpeg()[0]:
            check("已发现 ffmpeg 时 install 复用不下载",
                  rc == 0 and rec_i.get("source") == "reused"
                  and rec_i.get("bytes") == 0, str(rec_i)[:200])
        else:
            check("本机无 ffmpeg，跳过复用断言", True)

        # 子命令 help 契约：JSON 模式走 stderr + 单行收据 + rc=1
        rc, rec_h, hl = _run("hls", "--json", "--help")
        check("hls --json --help → error=help + rc1",
              rc == 1 and rec_h.get("error") == "help" and len(hl) == 1,
              "rc=%s %s" % (rc, str(rec_h)[:120]))
    finally:
        hfetch._RETRY_BACKOFF = old_backoff
        if stub:
            stub.close()
        shutil.rmtree(tmp, ignore_errors=True)


def test_as_name():
    """--as 清洗（安全回归：不许写出输出目录）。"""
    print("\n[8] --as 清洗")
    from ampull_hls.cli import _safe_name
    check("穿越被收窄为 basename", _safe_name("..\\..\\evil") == "evil",
          _safe_name("..\\..\\evil"))
    # ntpath.basename 会把 "a:" 当盘符前缀剥掉 → 得到 b_c_.mp4（结果仍在 out_dir 内）
    check("非法字符替换", _safe_name('a:b*c?.mp4') == "b_c_.mp4",
          _safe_name("a:b*c?.mp4"))
    check("绝对路径被收窄", _safe_name("C:\\Windows\\x.mp4") == "x.mp4",
          _safe_name("C:\\Windows\\x.mp4"))
    try:
        _safe_name("CON")
    except Exception as e:  # noqa: BLE001
        check("Windows 保留名被拒", getattr(e, "code", "") == "bad_path", str(e))
    else:
        check("Windows 保留名被拒", False, "没有抛错")


def test_cli_master_and_keep():
    """CLI：主列表选档 + --keep-parts 保留断点。"""
    import json
    import subprocess
    print("\n[9] CLI 主列表选档与 --keep-parts")
    tmp = tempfile.mkdtemp(prefix="hlsm_")
    stub = None
    try:
        key = b"0123456789abcdef"
        data = {"key.key": key}
        for i in range(2):
            data["s%d.ts" % i] = aes_encrypt(fake_ts(2, bytes([i])), key,
                                             crypto.iv_from_sequence(i))
        media = "\n".join([
            "#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-MEDIA-SEQUENCE:0",
            '#EXT-X-KEY:METHOD=AES-128,URI="key.key"',
            "#EXTINF:6.0,", "s0.ts", "#EXTINF:6.0,", "s1.ts", "#EXT-X-ENDLIST",
        ])
        master = "\n".join([
            "#EXTM3U",
            "#EXT-X-STREAM-INF:BANDWIDTH=500000,RESOLUTION=854x480", "v480.m3u8",
            "#EXT-X-STREAM-INF:BANDWIDTH=1000000,RESOLUTION=1280x720", "v720.m3u8",
        ])
        stub = _Stub(data)
        stub.data["v480.m3u8"] = media.encode()
        stub.data["v720.m3u8"] = media.encode()
        stub.data["master.m3u8"] = master.encode()
        out_dir = os.path.join(tmp, "o")
        p = subprocess.run([sys.executable, os.path.join(ROOT, "ampull.py"), "hls",
                            stub.url("master.m3u8"), out_dir, "--quality", "720",
                            "--json", "--no-mux", "--keep-parts"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
        lines = [l for l in p.stdout.decode("utf-8", "replace").splitlines() if l.strip()]
        rec = json.loads(lines[-1]) if lines else {}
        h = rec.get("hls") or {}
        check("主列表：选到 720 档", rec.get("ok") and h.get("quality") == "720p",
              str(h)[:160])
        check("主列表：文件名带档位", "_720p" in (rec.get("path") or ""),
              str(rec.get("path")))
        check("--keep-parts：断点目录保留", os.path.isdir(h.get("parts_dir") or ""),
              str(h.get("parts_dir")))
        check("--keep-parts：分片文件仍在",
              bool(h.get("parts_dir")) and len(os.listdir(h["parts_dir"])) >= 2)
    finally:
        if stub:
            stub.close()
        shutil.rmtree(tmp, ignore_errors=True)


def test_mux_fallback_cli():
    """CLI：ffmpeg 存在但执行失败 → 降级 .ts、清半截 mp4、保留断点。"""
    import subprocess
    print("\n[10] CLI 封装失败降级")
    tmp = tempfile.mkdtemp(prefix="hlsfx_")
    stub = None
    try:
        key = b"0123456789abcdef"
        data = {"key.key": key}
        for i in range(2):
            data["s%d.ts" % i] = aes_encrypt(fake_ts(2, bytes([i])), key,
                                             crypto.iv_from_sequence(i))
        media = "\n".join([
            "#EXTM3U", "#EXT-X-MEDIA-SEQUENCE:0",
            '#EXT-X-KEY:METHOD=AES-128,URI="key.key"',
            "#EXTINF:6.0,", "s0.ts", "#EXTINF:6.0,", "s1.ts", "#EXT-X-ENDLIST",
        ])
        stub = _Stub(data)
        stub.data["index.m3u8"] = media.encode()
        fake_ffmpeg = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                                   "System32", "cmd.exe")
        out_dir = os.path.join(tmp, "o")
        p = subprocess.run([sys.executable, os.path.join(ROOT, "ampull.py"), "hls",
                            stub.url("index.m3u8"), out_dir, "--json",
                            "--ffmpeg", fake_ffmpeg],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
        lines = [l for l in p.stdout.decode("utf-8", "replace").splitlines() if l.strip()]
        rec = json.loads(lines[-1]) if lines else {}
        warn = (rec.get("warning") or "")
        check("封装失败仍 ok", rec.get("ok") is True, str(rec)[:160])
        check("warning=mux_skipped", "mux_skipped" in warn, warn)
        check("container=ts", (rec.get("hls") or {}).get("container") == "ts",
              str(rec.get("hls"))[:120])
        check("成品是 .ts", (rec.get("path") or "").endswith(".ts"), str(rec.get("path")))
        check("半截 mp4 已清掉",
              not any(f.endswith(".mp4") for f in os.listdir(out_dir)))
        check("断点保留（装好 ffmpeg 可秒过重封装）",
              os.path.isdir((rec.get("hls") or {}).get("parts_dir") or ""))
    finally:
        if stub:
            stub.close()
        shutil.rmtree(tmp, ignore_errors=True)


def test_tools_root_relative():
    """依赖落盘必须相对程序目录（便携），不许落在用户家目录/AppData。"""
    print("\n[11] tools 落盘位置（相对程序目录）")
    from ampull_hls import mux, tools
    root = tools.tools_root("ffmpeg")
    expect = os.path.join(mux.program_dir(), "tools", "ffmpeg")
    check("落盘根 = <程序目录>/tools/ffmpeg", os.path.normcase(root) == os.path.normcase(expect),
          "%s vs %s" % (root, expect))
    low = os.path.abspath(root).lower()
    check("不落在 AppData", "appdata" not in low, low)
    check("不落在用户家目录的 .local", ".local" not in low, low)
    check("搜索链包含该目录", mux.tools_dir("ffmpeg") in mux._local_tools_dirs(),
          str(mux._local_tools_dirs()))


def test_ffmpeg_discovery():
    """别处自带的 ffmpeg：有限扫描能发现、深度受限、坏文件被烟测拦下。"""
    print("\n[12] ffmpeg 发现（别处自带 / 环境变量直指）")
    from ampull_hls import mux
    exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    tmp = tempfile.mkdtemp(prefix="ffdisc_")
    try:
        deep = os.path.join(tmp, "AppA", "bin")
        os.makedirs(deep)
        dummy = os.path.join(deep, exe)
        with open(dummy, "wb") as f:
            f.write(b"not a real exe")
        found = mux._discover_candidates(roots=[tmp], max_depth=3, deadline=5)
        check("发现别处自带的 ffmpeg", any(p == dummy for p in found), str(found))
        found_shallow = mux._discover_candidates(roots=[tmp], max_depth=1, deadline=5)
        check("深度上限生效（不会无限下钻）",
              not any(p == dummy for p in found_shallow), str(found_shallow))

        appb = os.path.join(tmp, "AppB")
        os.makedirs(appb)
        with open(os.path.join(appb, exe), "wb") as f:
            f.write(b"x")
        cands = mux._candidates_in([deep, appb])
        check("AMPULL_FFMPEG_DIRS：目录本身与 bin/ 都认", len(cands) == 2, str(cands))

        # 兄弟程序目录（用户桌面 ALrec 的真实形态）：Other/tools/ffmpeg/bin/ffmpeg.exe
        parent = os.path.join(tmp, "Desktop")
        sib = os.path.join(parent, "ALrec", "tools", "ffmpeg", "bin")
        os.makedirs(sib)
        sib_exe = os.path.join(sib, exe)
        with open(sib_exe, "wb") as f:
            f.write(b"x")
        found_sib = mux._discover_candidates(roots=[parent], max_depth=4, deadline=5)
        check("兄弟程序目录（ALrec 形态）被发现", sib_exe in found_sib, str(found_sib))
        roots = mux._discovery_roots(platform="win32", program_parent=parent)
        check("发现根包含兄弟目录", parent in roots, str(roots))
        mac_roots = mux._discovery_roots(platform="darwin")
        check("mac 发现根含 brew 路径",
              "/opt/homebrew/bin" in mac_roots and "/usr/local/bin" in mac_roots,
              str(mac_roots))

        check("假 exe 过不了烟测（不会被选中）", mux._smoke(dummy) is False)

        # 共享安装位（C:\ffmpeg 这类多工具共用位置）：定点探测、不递归
        shared_base = os.path.join(tmp, "C_drive")
        shared_dir = os.path.join(shared_base, "ffmpeg", "bin")
        os.makedirs(shared_dir)
        shared_exe = os.path.join(shared_dir, exe)
        with open(shared_exe, "wb") as f:
            f.write(b"x")
        shared = mux._shared_candidates(bases=[shared_base])
        check("共享安装位（<盘>/ffmpeg/bin）被识别", shared == [shared_exe], str(shared))

        path, source = mux.find_ffmpeg(discover=False)
        check("discover=False 仍能走原有链",
              source in ("env", "env_dir", "local", "shared", "path", None),
              "%s/%s" % (path, source))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_mcp():
    """MCP 壳（stdio JSON-RPC）：握手 / 工具表 / 调用 / 异步 job 靠磁盘持久化。"""
    import subprocess
    print("\n[13] MCP 壳")
    mcp = os.path.join(ROOT, "ampull_mcp.py")
    if not os.path.isfile(mcp):
        check("ampull_mcp.py 存在", False, mcp)
        return

    def call(reqs):
        payload = "\n".join(json.dumps(r, ensure_ascii=False) for r in reqs) + "\n"
        p = subprocess.run([sys.executable, mcp], input=payload.encode("utf-8"),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180)
        out = []
        for line in p.stdout.decode("utf-8", "replace").splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
        return out

    res = call([
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                    "clientInfo": {"name": "t", "version": "0"}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "ampull_tools", "arguments": {"action": "path"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "nosuch", "arguments": {}}},
    ])
    by_id = {r.get("id"): r for r in res}
    init = (by_id.get(1) or {}).get("result") or {}
    check("握手返回 serverInfo.name=ampull",
          (init.get("serverInfo") or {}).get("name") == "ampull", str(init)[:120])
    # 版本必须跟 CLI 的 --schema 一致（MCP 侧以 CLI 为准，避免两边漂移）
    sch = subprocess.run([sys.executable, os.path.join(ROOT, "ampull.py"), "--schema"],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    cli_ver = ""
    try:
        cli_ver = json.loads(sch.stdout.decode("utf-8", "replace"))["version"]
    except Exception:
        pass
    check("MCP 版本与 CLI schema 一致",
          (init.get("serverInfo") or {}).get("version") == cli_ver and bool(cli_ver),
          "%s vs %s" % ((init.get("serverInfo") or {}).get("version"), cli_ver))
    tools = [t.get("name") for t in ((by_id.get(2) or {}).get("result") or {}).get("tools", [])]
    expect_tools = ["ampull_download", "ampull_manifest", "ampull_repo", "ampull_hls",
                    "ampull_hls_probe", "ampull_job", "ampull_stop", "ampull_tools",
                    "ampull_status", "ampull_clean", "ampull_journal", "ampull_serve",
                    "ampull_schema"]
    check("工具表 13 个（覆盖 SKILL 全部能力面）", sorted(tools) == sorted(expect_tools),
          str(tools))
    path_res = ((by_id.get(3) or {}).get("result") or {}).get("structuredContent") or {}
    check("tools/call 能拿到 path 收据", path_res.get("action") == "path", str(path_res)[:160])
    check("未知工具返回 JSON-RPC 错误", (by_id.get(4) or {}).get("error") is not None,
          str(by_id.get(4))[:120])

    # 异步：新进程提交 → 另一个新进程查（靠 jobs/<id>/out.json，不靠内存）
    job_res = call([{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                     "params": {"name": "ampull_hls",
                                "arguments": {"url": "https://127.0.0.1:9/x.m3u8",
                                              "out_dir": os.path.join(tempfile.gettempdir(),
                                                                      "ampull_mcp_t"),
                                              "wait": False}}}])
    job = ((job_res[0].get("result") or {}).get("structuredContent") or {}).get("job")
    check("异步调用返回 job_id", bool(job), str(job_res)[:160])
    if job:
        import time as _t
        _t.sleep(3)
        q = call([{"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                   "params": {"name": "ampull_job", "arguments": {"job_id": job}}}])
        qr = ((q[0].get("result") or {}).get("structuredContent") or {})
        check("新进程能查到该 job（磁盘持久化）",
              qr.get("job") == job and qr.get("state") in ("done", "running"),
              str(qr)[:160])


class _MCPProc:
    """常驻 MCP 进程（测试 serve/stop 这类需要同一进程状态的操作）。"""

    def __init__(self, script):
        import subprocess as _sp
        self.p = _sp.Popen([sys.executable, script], stdin=_sp.PIPE,
                           stdout=_sp.PIPE, stderr=_sp.PIPE, cwd=ROOT)
        self.i = 0

    def call(self, name, args):
        self.i += 1
        req = {"jsonrpc": "2.0", "id": self.i, "method": "tools/call",
               "params": {"name": name, "arguments": args}}
        self.p.stdin.write((json.dumps(req) + "\n").encode("utf-8"))
        self.p.stdin.flush()
        line = self.p.stdout.readline()
        if not line:
            return {}
        try:
            r = json.loads(line.decode("utf-8", "replace"))
        except Exception:
            return {}
        return (r.get("result") or {}).get("structuredContent") or {}

    def close(self):
        try:
            self.p.stdin.close()
        except Exception:
            pass
        try:
            self.p.terminate()
        except Exception:
            pass


def test_mcp_ops():
    """MCP 的下载/探针/台账/清理/serve+stop（本地桩，零外网）。"""
    import random
    import urllib.request
    print("\n[14] MCP 能力面（下载/探针/清理/serve）")
    tmp = tempfile.mkdtemp(prefix="mcpops_")
    stub = None
    m = None
    try:
        blob = bytes(random.getrandbits(8) for _ in range(200 * 1024))
        media = "\n".join([
            "#EXTM3U", "#EXT-X-MEDIA-SEQUENCE:0",
            "#EXTINF:6.0,", "s0.ts", "#EXTINF:6.0,", "s1.ts", "#EXT-X-ENDLIST",
        ])
        master = "\n".join([
            "#EXTM3U",
            "#EXT-X-STREAM-INF:BANDWIDTH=500000,RESOLUTION=854x480", "v480.m3u8",
            "#EXT-X-STREAM-INF:BANDWIDTH=1000000,RESOLUTION=1280x720", "v720.m3u8",
        ])
        stub = _Stub({"file.bin": blob})
        stub.data["v480.m3u8"] = media.encode()
        stub.data["v720.m3u8"] = media.encode()
        stub.data["master.m3u8"] = master.encode()

        # journal 走 DL_JOURNAL：必须在起 MCP 进程**之前**设好（子进程继承环境）
        alt_journal = os.path.join(tmp, "alt.jsonl")
        with open(alt_journal, "w", encoding="utf-8") as f:
            f.write('{"t": "2026-01-01T00:00:00", "url": "x", "ok": true}\n')
        os.environ["DL_JOURNAL"] = alt_journal
        m = _MCPProc(os.path.join(ROOT, "ampull_mcp.py"))

        # 1) 探针（不下载）
        pr = m.call("ampull_hls_probe", {"url": stub.url("master.m3u8"),
                                         "quality": "720"})
        probe = ((pr.get("hls") or {}).get("probe") or {})
        check("MCP 探针：两档 + 选中 720",
              pr.get("ok") and len(probe.get("variants") or []) == 2
              and (probe.get("selected") or {}).get("height") == 720,
              str(pr)[:200])
        check("MCP 探针：可下载/段数", probe.get("downloadable") is True
              and probe.get("segments_total") == 2, str(probe)[:160])

        # 2) 普通下载（同步）
        out = os.path.join(tmp, "dl")
        dr = m.call("ampull_download", {"urls": [stub.url("file.bin")],
                                        "out_dir": out, "wait": True,
                                        "timeout_s": 120})
        check("MCP download ok", dr.get("ok") is True, str(dr)[:200])
        fpath = dr.get("path") or ""
        check("MCP download 落成目录里的文件（不是叫 dl 的文件）",
              os.path.isdir(out) and os.path.isfile(fpath),
              "out=%s path=%s" % (out, fpath))
        check("MCP download 字节一致",
              os.path.isfile(fpath) and os.path.getsize(fpath) == len(blob),
              "%s / %s" % (fpath, dr.get("bytes")))

        # 3) 台账与清理（只列）
        jr = m.call("ampull_journal", {"limit": 3})
        check("MCP journal 可读", jr.get("ok") is True and "entries" in jr,
              str(jr)[:160])
        cr = m.call("ampull_clean", {"paths": out})
        check("MCP clean 默认只列不删", cr.get("ok") is True
              and os.path.isfile(fpath), str(cr)[:160])

        # 4) serve + stop（同一进程内，靠内存句柄）
        sr = m.call("ampull_serve", {"path": fpath, "port": 0})
        url = sr.get("url") or ""
        job = sr.get("job")
        check("MCP serve 返回 url", bool(url) and "127.0.0.1" in url, str(sr)[:200])
        if url:
            try:
                with urllib.request.urlopen(url, timeout=10) as r:
                    body = r.read()
                check("serve 能取到内容", body == blob, "%d vs %d" % (len(body), len(blob)))
            except Exception as e:                                  # noqa: BLE001
                check("serve 能取到内容", False, "%s: %s" % (type(e).__name__, e))
        if job:
            st = m.call("ampull_stop", {"job_id": job})
            check("MCP stop 能停掉 serve", st.get("ok") is True, str(st)[:160])

        # 5) 未给 out_dir → 默认落 <程序目录>/downloads/（不污染程序根目录）
        dr2 = m.call("ampull_download", {"urls": [stub.url("file.bin")],
                                         "wait": True, "timeout_s": 120})
        default_dir = os.path.join(ROOT, "downloads")
        check("未给 out_dir 时落到 downloads/",
              dr2.get("ok") is True
              and os.path.dirname(os.path.abspath(dr2.get("path") or ""))
              == os.path.abspath(default_dir), str(dr2.get("path")))

        # 6) prune：只删"已结束且够老"的任务目录
        old_job = os.path.join(ROOT, "jobs", "19990101-000000-oldold")
        os.makedirs(old_job, exist_ok=True)
        with open(os.path.join(old_job, "out.json"), "w", encoding="utf-8") as f:
            f.write('{"ok": true}\n')
        old = 10 * 86400
        os.utime(old_job, (time.time() - old, time.time() - old))
        jr2 = m.call("ampull_job", {"prune_days": 7})
        check("prune 删掉 7 天前的已结束任务",
              jr2.get("ok") is True
              and "19990101-000000-oldold" in (jr2.get("pruned") or []),
              str(jr2)[:200])

        # 7) journal 认 DL_JOURNAL（与 CLI 同源；环境在起进程前已设）
        jr3 = m.call("ampull_journal", {"limit": 5})
        check("journal 认 DL_JOURNAL",
              (jr3.get("file") or "").endswith("alt.jsonl")
              and jr3.get("count", 0) >= 1, str(jr3)[:200])
    finally:
        os.environ.pop("DL_JOURNAL", None)
        if m:
            m.close()
        if stub:
            stub.close()
        shutil.rmtree(tmp, ignore_errors=True)


def test_mcp_extra():
    pass


def main():
    test_parse()
    test_fingerprint()
    test_crypto()
    test_plan()
    test_fetch_e2e()
    test_mux()
    test_cli()
    test_as_name()
    test_cli_master_and_keep()
    test_mux_fallback_cli()
    test_tools_root_relative()
    test_ffmpeg_discovery()
    test_mcp()
    test_mcp_ops()
    print("\n" + "=" * 60)
    if FAILED:
        print("FAILED: %d / %d" % (len(FAILED), len(PASSED) + len(FAILED)))
        for name, detail in FAILED:
            print("  - %s  %s" % (name, detail))
        return 1
    print("ALL GREEN (%d checks)" % len(PASSED))
    return 0


if __name__ == "__main__":
    sys.exit(main())
