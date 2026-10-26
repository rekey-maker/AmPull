# -*- coding: utf-8 -*-
"""hls_parse —— HLS 播放列表解析（一期：VOD / 明文 / AES-128）

设计依据：docs/design/功能设计-HLS视频下载-2026-10-26.md（§5 契约、§7.1 解密、§7.3 指纹）
RFC 8216：Master/Media Playlist、EXT-X-KEY、EXT-X-MEDIA-SEQUENCE、EXT-X-MAP、BYTERANGE

对外：parse() / pick_variant() / fingerprint()。纯文本进、dict 出，可独立单测。
"""

import hashlib
import re
from urllib.parse import urljoin

_AES128 = "AES-128"
_DRM_METHODS = ("SAMPLE-AES", "SAMPLE-AES-CTR")


class HlsError(Exception):
    """HLS 领域错误：code 进 AmPull 的 error 词表，msg 是人话一行。"""

    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code
        self.msg = msg


def _clean_lines(text):
    """去 BOM / 统一换行 / 去首尾空白 / 丢空行。保留 '#' 开头的结构性行。"""
    if text.startswith("\ufeff"):
        text = text[1:]
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    out = []
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            continue
        out.append(line)
    return out


_ATTR_RE = re.compile(r'([A-Za-z0-9_-]+)=("[^"]*"|[^,]*)')


def _parse_attrs(s):
    """KEY=VALUE,KEY="quoted,value"。正则整表扫描（CODECS 里带逗号，不能 split）。"""
    out = {}
    for k, v in _ATTR_RE.findall(s or ""):
        v = v.strip()
        if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
            v = v[1:-1]
        out[k.upper()] = v
    return out


def _split_tag(line):
    if ":" in line:
        name, _, rest = line.partition(":")
        return name.strip().upper(), rest.strip()
    return line.strip().upper(), ""


def _to_float(s, default=0.0):
    try:
        return float(s)
    except (TypeError, ValueError):
        return default


def _to_int(s, default=0):
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return default


def _resolution(attrs):
    """RESOLUTION=1280x720 → (w, h)。大写 X 也认（非标站点实测）。"""
    raw = attrs.get("RESOLUTION") or ""
    parts = re.split("[xX]", raw)
    if len(parts) == 2:
        return _to_int(parts[0], 0), _to_int(parts[1], 0)
    return 0, _to_int(attrs.get("HEIGHT"), 0)


def parse(text, base_url):
    """解析一份播放列表。返回 dict（见设计 §6.2）。

    出错抛 HlsError("hls_parse_failed", ...)。
    """
    lines = _clean_lines(text)
    if not lines or not lines[0].startswith("#EXTM3U"):
        raise HlsError("hls_parse_failed",
                       "不是 m3u8（首行不是 #EXTM3U，可能是 HTML 错误页或登录墙）")

    variants, segments, keys = [], [], []
    media_sequence = 0
    duration = 0.0
    endlist = False
    fmp4 = False
    version = target_duration = None
    playlist_type = None

    cur_key_id = None
    drm_reason = None
    pending_variant = None
    pending_inf = None
    pending_range = None
    last_range = {}
    seen_segment = False

    for lineno, line in enumerate(lines[1:], start=2):
        if line.startswith("#"):
            tag, body = _split_tag(line)

            if tag == "#EXT-X-STREAM-INF":
                pending_variant = _parse_attrs(body)
            elif tag == "#EXT-X-ENDLIST":
                endlist = True
            elif tag == "#EXT-X-MAP":
                fmp4 = True
            elif tag == "#EXT-X-MEDIA-SEQUENCE":
                if seen_segment:
                    # 出现在分片之后会被错误地"回填"到前面已产出的段（IV 全错）
                    raise HlsError(
                        "hls_parse_failed",
                        "第 %d 行：EXT-X-MEDIA-SEQUENCE 出现在分片之后（RFC 要求"
                        "在第一个分片之前），IV 无法确定" % lineno)
                media_sequence = _to_int(body, 0)
            elif tag == "#EXT-X-VERSION":
                version = _to_int(body, None)
            elif tag == "#EXT-X-TARGETDURATION":
                target_duration = _to_float(body, None)
            elif tag == "#EXT-X-PLAYLIST-TYPE":
                playlist_type = (body or "").strip().upper() or None
            elif tag == "#EXTINF":
                pending_inf = _to_float(body.split(",")[0], 0.0)
            elif tag == "#EXT-X-BYTERANGE":
                pending_range = body.strip()
            elif tag == "#EXT-X-KEY":
                attrs = _parse_attrs(body)
                method = (attrs.get("METHOD") or "").upper()
                keyformat = (attrs.get("KEYFORMAT") or "identity")
                if method == "NONE":
                    # METHOD=NONE 时其他属性 MUST NOT 存在；脏行直接忽略 URI
                    cur_key_id = None
                elif method == _AES128 and keyformat == "identity":
                    # key 的 URI 可能是相对路径（实站常见 URI="key.key"），
                    # 必须相对播放列表解析，否则下游把 'key.key' 当 URL 用
                    keys.append({"id": len(keys), "method": method,
                                 "uri": urljoin(base_url, attrs.get("URI") or ""),
                                 "iv_hex": attrs.get("IV") or None})
                    cur_key_id = len(keys) - 1
                else:
                    if method in _DRM_METHODS or keyformat != "identity":
                        drm_reason = "%s/%s" % (method or "?", keyformat)
                        cur_key_id = None
                    else:
                        raise HlsError(
                            "hls_parse_failed",
                            "第 %d 行：无法识别的加密方法 %r（一期仅支持 AES-128/NONE）"
                            % (lineno, method))
            # 其它 tag 忽略
            continue

        # ---- URI 行 ----
        url = urljoin(base_url, line)
        if pending_variant is not None:
            w, h = _resolution(pending_variant)
            variants.append({
                "url": url,
                "bandwidth": _to_int(pending_variant.get("BANDWIDTH"), 0),
                "height": h, "width": w,
                "codecs": pending_variant.get("CODECS") or "",
            })
            pending_variant = None
            continue

        if pending_inf is None:
            raise HlsError("hls_parse_failed",
                           "第 %d 行出现无 #EXTINF/#EXT-X-STREAM-INF 前置的 URI"
                           % lineno)
        byterange = None
        if pending_range:
            byterange = _parse_byterange(pending_range, url, last_range)
        segments.append({
            "idx": len(segments), "url": url, "duration": pending_inf,
            "byterange": byterange, "key_id": cur_key_id,
        })
        duration += pending_inf
        pending_inf = None
        pending_range = None
        seen_segment = True

    if pending_variant is not None:
        raise HlsError("hls_parse_failed",
                       "#EXT-X-STREAM-INF 后面缺少 URI 行（播放列表被截断？）")
    if pending_inf is not None:
        raise HlsError("hls_parse_failed",
                       "#EXTINF 后面缺少分片 URI（播放列表被截断？）")

    if variants and segments:
        variants = []          # 两种形态混排：以分片为准

    kind = "master" if variants else "media"
    if kind == "media" and not segments:
        raise HlsError("hls_parse_failed", "播放列表里没有任何分片（0 段）")

    encrypted = None
    if drm_reason:
        encrypted = "drm"
    elif keys:
        encrypted = "aes-128"

    return {
        "kind": kind, "variants": variants, "segments": segments, "keys": keys,
        "media_sequence": media_sequence, "duration": round(duration, 3),
        "endlist": endlist, "playlist_type": playlist_type,
        "encrypted": encrypted, "drm_reason": drm_reason, "fmp4": fmp4,
        "version": version, "target_duration": target_duration, "base_url": base_url,
    }


def _parse_byterange(body, url, last_range):
    """`<n>[@<o>]` → (offset, length)。省略 offset 时接同 URI 上一段末尾。"""
    text = body.strip()
    if "@" in text:
        n, _, o = text.partition("@")
        off, length = _to_int(o, 0), _to_int(n, 0)
    else:
        length = _to_int(text, 0)
        prev = last_range.get(url)
        off = (prev[0] + prev[1]) if prev else 0
    last_range[url] = (off, length)
    return (off, length)


def pick_variant(parsed, quality="best"):
    """从 master 挑一档。返回 (variant|None, warning|None)。

    quality：数字=目标高度，取 >= 该高度里最接近的；达不到退最高档 + quality_fallback。
    best/worst 按带宽；height 缺失的档不参与数字匹配。
    """
    if parsed["kind"] != "master":
        return None, None
    variants = parsed.get("variants") or []
    if not variants:
        raise HlsError("hls_variant_missing", "master 里没有可用档位")

    q = (quality or "best").strip().lower()
    if q in ("best", "worst"):
        pool = sorted(variants, key=lambda v: (v.get("bandwidth") or 0,
                                               v.get("height") or 0))
        return (pool[-1] if q == "best" else pool[0]), None

    try:
        target = int(q)
    except ValueError:
        raise HlsError("hls_variant_missing",
                       "--quality 只接受 best/worst/数字高度（可再按宽度兜底），收到 %r"
                       % quality)

    with_height = [v for v in variants if (v.get("height") or 0) > 0]
    if not with_height:
        pool = sorted(variants, key=lambda v: v.get("bandwidth") or 0)
        return pool[-1], "quality_fallback"

    ge = [v for v in with_height if v["height"] >= target]
    if ge:
        return min(ge, key=lambda v: (v["height"], -(v.get("bandwidth") or 0))), None

    # 高度全不达标时再按**宽度**匹配一次：站方常以宽度标 720/1080
    # （本机实测：`RESOLUTION=720x406` 的 16:9 片源，站方就叫它 720）
    ge_w = [v for v in variants if (v.get("width") or 0) >= target]
    if ge_w:
        return min(ge_w, key=lambda v: (v.get("width") or 0,
                                        -(v.get("height") or 0))), \
            "quality_matched_by_width"

    return max(with_height, key=lambda v: v["height"]), "quality_fallback"


def variant_key(variant):
    """档位身份：宽度x高度|带宽|CODECS。指纹与断点目录都靠它区分档位。"""
    if not variant:
        return ""
    return "%dx%d|%s|%s" % (variant.get("width") or 0, variant.get("height") or 0,
                            variant.get("bandwidth") or 0,
                            (variant.get("codecs") or "")[:40])


def fingerprint(parsed, vkey=""):
    """内容指纹（设计 §7.3）：段数 + 各段时长（0.1s 取整）+ 加密 + 容器 + 档位。

    分片 URI **不参与**（服务端周期性重签 URL 时内容没变，指纹必须不变）；
    但**档位必须参与**——同视频 480p/720p 的段数与 EXTINF 序列通常完全一致，
    不加档位就会把两档的段混拼成一条废片（审查实锤）。
    """
    segs = parsed.get("segments") or []
    durations = ",".join("%.1f" % (s.get("duration") or 0) for s in segs)
    raw = "%d|%s|%s|%s|%s|%d|%s" % (
        len(segs), durations, parsed.get("encrypted") or "-",
        "fmp4" if parsed.get("fmp4") else "ts",
        vkey or "-", parsed.get("media_sequence") or 0,
        (parsed.get("playlist_type") or "-"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
