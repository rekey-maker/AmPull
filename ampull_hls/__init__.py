# -*- coding: utf-8 -*-
"""ampull_hls —— AmPull 的 HLS（m3u8）下载子包。

设计文档：docs/design/功能设计-HLS视频下载-2026-10-26.md
一期范围：VOD + 明文 TS + AES-128 + 分片并行 + 断点 + 可选 mp4 封装。

模块（用法：ampull_hls.parse.parse(...)）：
  parse   播放列表解析 / 选码率 / 内容指纹
  plan    断点计划、账本、指纹一致性
  crypto  AES-128 解密、TS 校验
  fetch   分片级并行调度
  mux     拼接、ffmpeg 封装、时长核对

注意：这里**不**把函数直接提到包级（否则 `ampull_hls.parse` 会变成函数、
盖掉同名子模块，调用方 `from ampull_hls import parse` 后拿不到模块）。
"""

from .parse import HlsError                     # noqa: F401
from . import crypto, fetch, mux, parse, plan   # noqa: F401

__all__ = ["HlsError", "crypto", "fetch", "mux", "parse", "plan"]
