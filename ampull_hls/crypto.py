# -*- coding: utf-8 -*-
"""hls_crypto —— HLS AES-128 解密与 TS 校验（RFC 8216 §5.2）

只做三件事：取 key、解一段、判一段是不是好 TS。
不依赖 AmPull 主文件；网络取 key 由调用方注入 fetch 函数，便于离线单测。
"""

import struct

from .parse import HlsError

# TS 包长：188 = 标准；192 = M2TS（每包前 4 字节时间戳）；204 = 带 RS 纠错
_TS_PACKET = 188
_TS_PACKET_M2TS = 192
_TS_PACKET_RS = 204
_SYNC = 0x47


def backend_available():
    """cryptography 是否可用。明文流不需要它。"""
    try:
        import cryptography  # noqa: F401
        return True
    except Exception:
        return False


def _aes_cbc_decrypt(data, key, iv):
    """AES-128-CBC 解密。长度非 16 的整数倍属实现错误（上层会先校验）。"""
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except Exception:
        raise HlsError(
            "hls_dep_missing",
            "需要 cryptography 才能解 AES-128 —— 安装：python -m pip install cryptography")
    if len(data) % 16 != 0:
        raise HlsError("hls_seg_corrupt",
                       "密文长度 %d 不是 16 的整数倍，不是合法 AES-CBC 数据" % len(data))
    dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    return dec.update(data) + dec.finalize()


def iv_from_sequence(seq):
    """媒体序号 → 16 字节大端 IV（RFC 8216 §5.2）。"""
    if seq < 0:
        raise HlsError("hls_parse_failed", "媒体序号为负（%d）" % seq)
    return struct.pack(">QQ", 0, int(seq) & 0xFFFFFFFFFFFFFFFF)


def iv_from_hex(text):
    """EXT-X-KEY 的 IV 属性（32 位十六进制）→ 16 字节。

    宽容处理：允许 0x 前缀；位数不足左补零（部分站点写 16 位短 IV）。
    超过 32 位视为非法。
    """
    s = (text or "").strip().lower()
    if s.startswith("0x"):
        s = s[2:]
    if not s or any(c not in "0123456789abcdef" for c in s) or len(s) > 32:
        raise HlsError("hls_key_failed", "EXT-X-KEY 的 IV 不是合法十六进制：%r" % text)
    return bytes.fromhex(s.rjust(32, "0"))


def strip_pkcs7(data):
    """去 PKCS7 填充——**用对齐反推，而不是信任末字节**。

    为什么不能只看末字节（r1 审查实锤）：零填充/不填充的编码器并不罕见，
    按"末字节 n 且尾部全 n"判定会把填充留在明文里，长度必然不是 188 的
    整数倍，于是 validate_ts 报 seg_corrupt——把"填充不规范"误诊成"数据坏"。

    可算性：CBC 密文长度 L 恒为 16 的倍数，gcd(188,16)=4，故在 n∈[0,16] 里
    最多只有一个 n 满足 (L-n)%188==0，且必然 n≡L (mod 4)。这个唯一解就是
    该去的字节数；末字节只用来交叉验证（不符时仍按对齐解，不判失败）。
    """
    if not data:
        return data
    L = len(data)
    cands = [n for n in range(0, 17) if (L - n) % _TS_PACKET == 0]
    if len(cands) == 1:
        n = cands[0]
        return data[:L - n] if n else data
    # 没有唯一解（例如 L 本身既不整除 188 也不接近）：回退标准 PKCS7 判据
    n = data[-1]
    if 1 <= n <= 16 and L >= n and data[-n:] == bytes([n]) * n:
        return data[:-n]
    return data


def load_key(uri, fetch, headers=None, cache=None):
    """取 16 字节密钥。cache 为 dict（按绝对 URI 复用，同一个 key 只取一次）。

    fetch 签名：fetch(url, headers) -> bytes（由调用方注入，内部走 AmPull 的 opener）。
    """
    if cache is not None and uri in cache:
        return cache[uri]
    if not uri:
        raise HlsError("hls_key_failed", "EXT-X-KEY 缺少 URI")
    data = fetch(uri, headers or {})
    if data is None:
        raise HlsError("hls_key_failed", "取 key 失败：%s" % uri)
    if len(data) != 16:
        raise HlsError(
            "hls_key_failed",
            "key 长度 %d != 16（%s）——可能返回了错误页/需要 Referer" % (len(data), uri))
    if cache is not None:
        cache[uri] = data
    return data


def decrypt_segment(cipher_bytes, key, iv):
    """密文 → 明文（已去填充）。"""
    return strip_pkcs7(_aes_cbc_decrypt(cipher_bytes, key, iv))


def validate_ts(plain):
    """校验明文是不是可拼的 TS。

    通过：首字节 0x47 且长度 %188 == 0。
    不通过：抛 HlsError("hls_seg_corrupt", 具体原因)——绝不把可疑数据写进成品。
    """
    if not plain:
        raise HlsError("hls_seg_corrupt", "解密后为空")
    if len(plain) % _TS_PACKET == 0 and plain[0] == _SYNC:
        # 多点同步字节确认：只查首字节有 1/256 假阳性，前 8 个包边界都要对
        for off in range(_TS_PACKET, min(len(plain), _TS_PACKET * 8), _TS_PACKET):
            if plain[off] != _SYNC:
                raise HlsError(
                    "hls_seg_corrupt",
                    "第 %d 字节不是同步字节（0x%02x）——填充残留或流被截断"
                    % (off, plain[off]))
        return True
    # 给出可诊断的具体形态，而不是笼统说"坏数据"
    if len(plain) % _TS_PACKET_M2TS == 0 and len(plain) >= _TS_PACKET_M2TS \
            and plain[4] == _SYNC:
        raise HlsError("hls_seg_corrupt",
                       "疑似 192 字节 M2TS 变体（时间戳前缀），一期不支持")
    if len(plain) % _TS_PACKET_RS == 0 and plain[0] == _SYNC:
        raise HlsError("hls_seg_corrupt",
                       "疑似 204 字节 TS 变体（带 RS 纠错），一期不支持")
    raise HlsError(
        "hls_seg_corrupt",
        "首字节 0x%02x / 长度 %d（%%188=%d）——key 不对或分片被截断"
        % (plain[0], len(plain), len(plain) % _TS_PACKET))
