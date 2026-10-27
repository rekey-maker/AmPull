# -*- coding: utf-8 -*-
"""AmPull = Agent Multi-link Downloader（agent 多链接下载器）· 单文件 · 零依赖 · 断点续传 · 分片批量 · 面向编码 agent

把一个文件分成 16MB 的块，用 16 个连接同时下载。中断后重跑同一条命令即可续传。
模型仓库的分片（GGUF 等）可一条命令批量下载：多链接、--manifest 清单或 --repo 整仓。

用法：
    python ampull.py <下载链接> [保存位置] [-c 连接数] [--sha256 校验值] [--json]
    python ampull.py <链接1> <链接2> ... [目标目录]      多个分片，逐片下载
    python ampull.py --manifest 清单.txt [目标目录]       清单每行: URL [sha256]
    python ampull.py --repo 仓库ID [--include 通配] [目标目录]   整仓自动列清单
    python ampull.py --serve 保存位置 [--port 0] [--host 127.0.0.1]
                                                回环 Range 服务（下载中即可边下边用）
    python ampull.py --status 保存位置 [保存位置...]  查询下载状态（只读，可与下载进程并存）

示例：
    python ampull.py "https://example.com/big.zip"
    python ampull.py "https://example.com/big.zip" "downloads/big.zip" -c 32
    python ampull.py "https://example.com/big.zip" --json --sha256 abcd1234...
    python ampull.py --repo unsloth/Qwen3.8-Flash-Next-GGUF --include "UD-IQ4_XS/*.gguf" "models/UD-IQ4_XS"
    python ampull.py "https://github.com/OWNER/REPO/archive/refs/heads/main.zip"
    （v5.18：GitHub 归档 404 自动试默认分支/master/main/develop + codeload 直链，
     命中即改用新 URL 下载，warning=gh_branch_fallback；gh-proxy 等包装前缀保留）

────────────── 给编码 agent 的接口速览（机器可读完整契约：跑 --schema） ──────────────
· --json：stdout 只有最后一行 JSON（ok/path/bytes/seconds/sha256/verified/error/
  warning 八字段，取最后一行解析）；批量模式额外多 files 数组（每分片一个对象，
  同八字段+url 源链接）。进度与提示全走 stderr。warning 可为逗号连接的多个：
  html_response=服务器返回的是网页而非文件（只警告，退出码仍 0）；
  proxy_fallback=发生过代理通道切换；
  gh_branch_fallback=GitHub 归档 404 后自动换分支重试（URL 已改写，收据外的
  原链接可弃用）。
· error 词表闭合（权威清单以 --schema 输出为准）：bad_args / bad_sha256 / bad_url /
  bad_path / usage / help / network / incomplete / size_mismatch / verify_failed /
  etag_mismatch / disk / io / internal / interrupted / cancelled / locked /
  waste，v5.20 新增 hls_*（HLS 子命令：parse/variant/key/drm/segment/seg_corrupt/
  live/playlist_changed/dep_missing/mux）与 tools_*（依赖自举），及
  http_XXX（XXX=HTTP 状态码）。locked=另一实例正下载同一文件（双开保护，等它
  结束或重跑）。etag_mismatch=下载响应与探测的 ETag 不符（服务器中途换内容），
  重跑同一条命令即可按账本判废旧断点后全量重下。
  waste=浪费熔断：失败重试累计吞掉的流量超过 max(64MB, 2 倍文件大小)，判定
  源站不稳定；断点已保留，更换源或网络后重跑即可续传。
  批量 error=第一个失败分片，明细看 files[].error。失败时 JSON 另带 error_msg
  （人话一行，兜底码的排查线索）。退出码 0 成功 / 130 中断 / 其余 1。conflict_rename
  不是错误码：是目标名被占时静默改名另存（ok=true，收据 path 为实际落盘名）。
· 避坑：URL 一律双引号包起来（含 & 的链接不加引号，PowerShell 会前台报错却把
  下载丢后台、cmd 会截断参数——S3/OSS 预签名和 GitHub release 全是这种形式）；
  用 python 或 py 启动，python3 在很多 Windows 上是 Microsoft Store 占位程序；
  失败或中断后重跑同一条命令即可续传（已完成分片秒过，断点分片续传）。
· 批量语义：末尾参数是本地路径=目标目录（不存在会创建）；逐片顺序下载（每片内部
  仍多连接，跨片并行正对镜像限流打）；一个分片失败不拦后面，磁盘满除外（确定性
  失败，跳过剩余分片）；批量不支持 --sha256（校验值逐片写在清单或由 --repo 自动
  获取）。--repo 不给 --include 会整仓全下（几百 GB），先看清打印的清单再放行；
  LFS 文件 oid 即内容 sha256，--repo 自带逐片校验。
· 多源池（--mirror，v5.0）：候选并行探测（每候选单请求 20s）后按双门筛选——大小一致+
  支持 Range；ETag 不一致需 --sha256 兜底否则出局；ETag 单侧缺失不判废只警告
  （v5.16，建议配 --sha256 终检）。镜像请求按白名单剥凭据（v5.16：跨 host 重定向
  与落点再发起同样白名单式，Cookie/X-Api-Key 等不发往异己 host，
  Authorization/Range 保留）。--mirror
  @文件（每行 URL，# 注释）/@auto（读运行历史，默认关）；运行历史 mirrors.jsonl
  （与 ampull.py 同目录，超 2MB 自动截尾保最近 1000 行，v5.16）；--json 多
  mirrors 字段（host/url/bytes/fails/ewma）。批量模式每片独立建池（v5.10）。
· 限流韧性：429/503 服从 Retry-After（上限 120s），否则指数退避 ±30% 抖动；并发
  自适应收缩（对半砍）与缓慢恢复。HEAD/Range 探测自带退避重试。不支持续传的服务器
  自动降级单连接/流式（不可续传，但 --sha256 仍可校验；0 字节文件按已知大小处理）。
· 代理（v4.2 起自动检测，端口无关）：环境变量 HTTP(S)_PROXY > --proxy/--no-proxy >
  注册表 + 本机端口并行探针（仅认 HTTPS CONNECT 实测通过者）；github/hf 系代理
  优先，国内 CDN 直连优先，首选失败自动切备用（每块都有 206 校验，混用安全）。
  v5.1：分流按请求目标域名逐个判定——镜像探测与分块请求按镜像自己的域名
  走（主源 HF 走代理时 hf-mirror 等国内镜像仍直连），--proxy/--no-proxy 强制
  语义不变（显式指定=全部连接照办）。
· 每次下载（无论成败）追加一行到与 ampull.py 同目录的 downloads.jsonl；
  --clean [目录...] 默认只列不删，-y 删垃圾（孤儿账本+残片），-y --all 连断点删
  （v5.16 起断点的配对账本同轮一并删，不再留孤儿等二次收敛），10 分钟内动过的
  不删，.bad 校验失败证据永不删。
· --status 路径 [路径...]：只读查询下载状态——active（另一进程下载中）/resumable（断点
  可续传，重跑原命令接上）/done/foreign（外部 .part，不判不碰）/missing；收据含
  downloaded_bytes/progress_pct，active/resumable 另带 speed_bytes_s（账本最近 16 次
  写入窗口实测速度，相邻正增益段累计；null=无采样/无正增益段/断点窗口陈旧>120s）
  与 eta_s（仅 active，按当前速度折算；正增益采样段 <2 时 null——单段外推误导）。
  分块下载 stderr 进度条空闲 3s 强制刷一帧（v5.12，不再随块静默数分钟）；v5.13
  百分比/速度/ETA 画「接收进度」（fetch 1MB 步进读累加，含未完成块的已收部分，
  aria2 同款）——首块完成前不再 0.0% 定格；真终端
  （isatty）另随帧发 OSC 9;4 进度环（任务栏/标签页），agent 管道不发。
  v5.15：stderr 接管道/重定向时进度自动切整行输出（5 秒一帧节流，首帧与 100%
  必出）——| tee 与 2> 日志可实时 tail 跟踪，不再 \r 糊成一条巨线；真终端保持
  \r 单行刷新不变。
  给产物路径即可（.part/.part.done/.part.lock 自动归一），missing 也是有效结果
  （退出码 0）；可一次给多个路径（收据 {count, results:[单路径收据...]}）。状态
  人话行恒走 stderr（--json 下 stdout 只有最后一行 JSON）。agent 下载标准姿势
  （v5.18 约定，完成靠推送不靠轮询）：下载命令一律**前台非阻塞**跑（--json）——
  宿主卡片实时流式进度给用户看，预计超 10 分钟也一样；平台 10 分钟超时把它
  强转后台时进程不死，此后退出推送照达。agent 启动后即返回做别的事，任务
  退出通知即完成信号——读收据核对 ok/error 再回报；用户问进度才单查一次
  --status（<1s）报数；明显超时仍无推送才单查一次对账（resumable=进程死了，
  重跑续传）。任何 sleep 轮询循环与阻塞式等待一律禁止——阻塞会卡死整个会话。
· --serve 路径 [--port 0] [--host 127.0.0.1]：回环 Range 服务，对本工具的下载
  产物（.part 断点或最终文件，给最终名自动识别）提供标准 HTTP 文件服务；未完成
  区间挂起等待而非 404，播放器/校验工具可边下边用；下载停止推进超 300 秒连接
  会被服务端断开（重连即可续流）。Range 按 RFC 7233：非法/多区间回 200 全文件，
  416 只用于起点超界。--json 语义特例：启动即输出
  一行 JSON（ok/mode/path/port/url/size/complete/warning）且进程保持前台，
  Ctrl+C 退出码 130，退出时不再有第二行 JSON。warning=partial_content 表示下载
  进行中（重跑下载会重置 .part 内容，读到什么以当时账本为准）。
  v5.20 起支持 HLS：`ampull.py hls <m3u8> <目录>`——VOD + 明文 TS + AES-128，
  分片级并行 + 断点续传 + 可选 mp4 封装；fMP4 / EXT-X-BYTERANGE / 多 KEY 轮换 /
  DRM / 直播一律明确拒绝（不猜、不出错片）。缺 ffmpeg 时落 .ts，可用
  `ampull.py tools install ffmpeg` 自举（只写本地 tools 目录，需用户同意）。
  v5.16 起校验 Host 头（只认 loopback 与
  监听地址，DNS rebinding 防线），异己 Host 直接 403（附人话提示）；绑定
  0.0.0.0/:: 时跳过校验（局域网客户端 Host 是各自真实 IP，且外网绑定无
  rebinding 威胁，v5.17 文档补记）。管道/重定向输出字节恒 UTF-8（v5.17，
  真终端跟随控制台代码页）。
· 输出不含 emoji 与箭头（GBK 控制台安全）。被 taskkill /F 强杀无 JSON，但账本已
  原子保存，重跑续传。设计全记录见《AmPull-Agent化改造方案.md》。
────────────────────────────────────────────────────────────────────────────
"""
import argparse
import collections
import concurrent.futures as cf
import fnmatch
import hashlib
import http.client
import json
import os
import random
import re
import shutil
import select
import socket
import socketserver
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    import winreg              # Windows 系统代理注册表（代理自动检测用）
except ImportError:
    winreg = None              # 非 Windows：跳过注册表，端口扫描照常
try:
    import msvcrt              # Windows 文件锁（双开互斥用）
except ImportError:
    msvcrt = None              # 非 Windows：跳过双开锁（POSIX 可用 fcntl，暂不做）

CHUNK = 16 << 20                              # 默认分块 16MB

# ---- v5.15 管道友好进度：\r 同行刷新只在真终端有意义；stderr 接了管道/重定向
# （| tee、2> 日志、agent 捕获）时 \r 会把全部进度糊成一条巨线，且旧内容被覆盖
# 无从解析。助手按 isatty 分流：真终端行为与历史版本逐字节一致；非 tty 自动切
# 「行进度」——每帧整行输出（带换行），5 秒节流（首帧与 100% 必出，中段只留
# 采样点），tail -f / CI 日志即可实时跟踪，量级也可控（1 小时下载 ≈ 720 行）。
_STDERR_IS_TTY = None          # main() 里判一次（v5.15：延迟到调用时惰性判定，
                               # 供测试打桩 sys.stderr 后仍取到正确形态）
_LINE_PROGRESS_LAST = 0.0      # 非 tty 行进度的上次输出时刻（单调节流）


def _progress_print(text):
    """进度帧统一出口。text 为不含换行的帧内容；真终端 \\r 回行首覆盖刷新，
    非 tty 整行输出（append 语义，日志友好）。"""
    global _STDERR_IS_TTY, _LINE_PROGRESS_LAST
    if _STDERR_IS_TTY is None:
        try:
            _STDERR_IS_TTY = sys.stderr.isatty()
        except Exception:
            _STDERR_IS_TTY = False
    if _STDERR_IS_TTY:
        sys.stderr.write("\r" + text)
        sys.stderr.flush()
        return
    # 非 tty：节流 5s。首帧（last==0）与满进度（text 含 100.0%）永不节流——
    # 前者让 agent 日志立刻有「开始下载」的锚点，后者保证收尾帧不丢。
    now = time.monotonic()
    if _LINE_PROGRESS_LAST and now - _LINE_PROGRESS_LAST < 5.0 \
            and "100.0%" not in text:
        return
    _LINE_PROGRESS_LAST = now
    sys.stderr.write(text + "\n")
    sys.stderr.flush()


def _progress_end():
    """进度段收尾：真终端补换行（下一行输出不再黏在进度条尾），非 tty 无操作
    （行进度本就自带换行）。每处 print("\\r...") 进度循环结束后调用。"""
    if _STDERR_IS_TTY:
        sys.stderr.write("\n")
        sys.stderr.flush()
# v5.13 显示层接收计数：fetch 步进读时累加本会话物理接收字节（含未完成块的已收
# 部分），show_progress 画「接收进度」——账本 done 只有块完成粒度，16MB 块在
# 2MB/s 下 8s 才跳一拍、首块完成前 0.0%/0.0 定格（llama.cpp 真机实锤）。纯显示
# 不进账本；download() 每任务在 submit 前重置，批量跨条目不串账。
_rx_lock = threading.Lock()
_rx_bytes = 0
# 版本线=交付轮次（v4.7 起 UA 与设计文档版本号同线，schema 的 version 字段
# 反射此处，勿单独改——历史上 UA 走 2.x、文档走 v4.x 两套叙事，agent 缓存的
# 契约会跨版本对不上）
UA = "ampull/5.21"   # v5.21：MCP 层（ampull_mcp.py：hls/tools/status/异步 job）+ 共享 ffmpeg 通道 + quality 宽度兜底
# error 词表（单一事实来源，--schema 输出它）。闭合=JSON 的 error 字段不会出现
# 词表外的值（http_XXX 除外，XXX 是任意 HTTP 状态码）。cancelled=批量模式下其他
# 分片已失败时被提前放弃的分片错误码——手写清单曾漏过它（docstring 漂移实测），
# 故常量化；改 Fail 词表必须同步这里。
ERROR_CODES = ("bad_args", "bad_sha256", "bad_url", "bad_path", "usage", "help",
               "network", "incomplete", "size_mismatch", "verify_failed",
               "etag_mismatch",
               "disk", "io", "internal", "interrupted", "cancelled", "locked",
               "waste",
               # ---- v5.20 HLS（m3u8）子命令 ----
               "hls_parse_failed", "hls_variant_missing", "hls_key_failed",
               "hls_drm", "hls_segment_failed", "hls_seg_corrupt",
               "hls_live_unsupported", "hls_playlist_changed", "hls_dep_missing",
               "hls_mux_failed",
               # ---- v5.20 tools 依赖自举 ----
               "tools_install_failed", "tools_not_found")
# 这些 HTTP 状态是永久性失败，重试没有意义（403 尤其：对限流站点重试等于延长封禁）
FATAL_HTTP = (400, 401, 403, 404, 405, 416, 451)
# HEAD 被【方法层面】拒绝时才改用 GET 探测。405/501 是铁证；403 也收进来——
# 反派 B 评审：WAF/CDN 常只拦 HEAD 不拦 GET，能下的链接不该被 HEAD 一票判死；
# 真正的鉴权/防盗链 403 会同样死于 GET 探测，代价只是多一个 0-0 字节请求。
# 404 同理（v4.7）：个别 WAF/CDN 对 HEAD 回 404 而 GET 正常，收进来代价只是
# 多一个 GET 探测，与收 403 的逻辑同源。
# 401 不收（鉴权明确，换方法无意义）；503 是暂时不可用，属于"该重试"而不是
# "该换方法"。
HEAD_REJECTED = (405, 501, 403, 404)
PROBE_TRIES = 4                               # 探测阶段重试次数（与分块下载保持一致）
PROBE_BUDGET = 30                             # 探测阶段的总时长上限（秒），含退避等待
# 探测/流式的单次 socket 超时。原来用 60，比 Windows 的 TCP 连接超时（21s）还长，
# 于是"脚本里的超时根本没机会生效"；而实测 hf-mirror 的 HEAD 最慢到过 17.09s，
# 所以下限不能低于 ~25s，取 30 兼顾两端。
PROBE_TIMEOUT = 30
STREAM_TIMEOUT = 60                           # 流式下载的单次读超时（慢链路要宽一些）
CLEAN_ACTIVE_SEC = 600                        # --clean 活跃保护阈值（与 C4 并发提示同值）


def _is_disk_error(e):
    """磁盘写满类错误：要报 disk，不能报成 network 或笼统的 io。"""
    codes = (getattr(e, "errno", None), getattr(e, "winerror", None))
    return any(c in (28, 39, 112, 1450, 1455) for c in codes if c)


def _acquire_part_lock(out):
    """双开互斥（反派 B 严重项）：对 <out>.part.lock 独占锁第 1 字节。

    专用锁文件而非锁 .part 的字节范围：Windows 字节锁会挡住同进程 worker 对
    被锁范围的写入，且流式下载文件增长会让锁区间从 EOF 外滑进文件内部。
    锁随进程存活——崩溃/被杀时句柄关闭锁自动释放；残留的 .lock 文件没有锁
    语义，重跑直接接管，--clean 按 tmp 归类。返回锁句柄，download 的局部
    变量回收（CPython 引用计数）即释放，无需显式 close；worker 失败路径在
    as_completed except 里显式释放（future 持异常 traceback 钉死本帧，引用
    计数不可靠，v5.0 实锤）；其余 Fail 路径（探测/locked/usage 等本帧抛出）
    无引用环，引用计数照旧。
    复审 N1：锁文件**创建**失败（只读目录/权限）降级为警告并放弃保护——原本
    能秒退成功的"文件已存在"场景不能被一把锁拦死；真写不进去时后续 io 错误
    照常兜底。锁**冲突**（另一实例持锁）仍是硬失败 locked。
    """
    lp = out + ".part.lock"
    if msvcrt is None:
        # B-P2-1：POSIX 无字节锁、双开互斥缺席（模块头已声明），但仍建标记锁文件
        # ——--status/--clean 的活跃判定（锁文件 mtime 新鲜≈进行中）此前结构性
        # 不可达（下载进程从不创建 .lock，mtime 分支是死代码）。局限：超过
        # CLEAN_ACTIVE_SEC 的长下载中途会失真为可续传；残留标记无锁语义，
        # 与 Windows 残锁同判。
        try:
            return open(lp, "a+b")
        except OSError as e:
            print("警告：无法创建断点锁文件 %s（%s），活跃探测标记不可用" % (lp, e))
            return None
    try:
        fh = open(lp, "a+b")
    except OSError as e:
        print("警告：无法创建断点锁文件 %s（%s），双开保护不可用" % (lp, e))
        return None
    for i in range(3):
        # A/C P3：--status 探针的加锁尝试可能落在本函数 open→locking 的微秒窗口
        # 里（探针锁成功→真下载误判 locked 被杀）。短退避等探针自释；真双开
        # 三次全败（+0.6s）仍报 locked。
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            break
        except OSError:
            if i == 2:
                fh.close()
                raise Fail("locked", "另一个下载进程正持有此文件的断点锁（双开保护）；"
                                     "等它结束或重跑即可，必要时删除 %s" % lp)
            time.sleep(0.3)
    return fh


def _release_part_lock(fh):
    """完成/秒退/worker 失败路径释放并清掉锁文件。sweep 删不掉自己开着的句柄
    文件（Windows 拒删被打开文件），必须走这里：先 close 释放锁再 remove。
    本帧直接抛出的 Fail（探测/usage/locked/incomplete）不调用，靠引用计数
    释放，残留 .lock 无锁语义，交给下一次的 sweep / --clean。"""
    if fh is None:
        return
    try:
        lp = fh.name
        fh.close()
        os.remove(lp)
    except OSError:
        pass


# ---- 浪费熔断（v4.9，对标迅雷"重取字节超限即判失败"的单源版）----
# 只统计【运行期内】失败 attempt 吞掉的流量：分块路径 fetch 失败时已收但被丢弃
# 的字节（不完整响应/ETag 拒收/连接中途断）。刻意不持久化：判废重跑=新进程=计数
# 归零，跨轮次的浪费由 T3 账本身份判定与用户自觉兜底（迅雷 cfg 持久化浪费账目
# 是它的多轮任务模型，不学）。流式路径不统计：不可续传的小文件本身有 4 次重试
# 上限，浪费天然有界（<=4 倍文件大小），熔断无增益。统计刻意保守（只计有精确
# 数字的部分，header 阶段失败计 0）——低估只延迟熔断，不会误杀慢源。
_WASTE_BYTES = [0]         # [累计浪费字节数]；list 绕 global，锁保护下读写
_WASTE_GEN = [0]           # [代际号]；download() 每片开头 +1。批量失败片 ex.shutdown
                           # (wait=False) 的滞留 worker 会在下一片归零后追加计数→假
                           # 熔断（v5.4 P2-5）——fetch 持本片代际，_add_waste 过代即弃
_WASTE_LOCK = threading.Lock()
WASTE_MIN = 64 << 20       # 阈值下限：max(64MB, 2*size)；小文件防坏源，大文件防无限


def _add_waste(n, gen=None):
    # v5.14：判等收进锁内——原代码锁外读 _WASTE_GEN 与换代（download 片头）存在
    # TOCTOU：读到旧 gen 后片头换代，加锁时旧片字节记到新片头上（假熔断）。
    with _WASTE_LOCK:
        if n > 0 and (gen is None or gen == _WASTE_GEN[0]):
            _WASTE_BYTES[0] += n


def _waste_over(size):
    """返回 (是否超限, 已浪费字节, 阈值)。size 为 None（流式）按 0 计。"""
    limit = max(WASTE_MIN, 2 * (size or 0))
    with _WASTE_LOCK:
        return _WASTE_BYTES[0] > limit, _WASTE_BYTES[0], limit


def _norm_etag(e):
    """归一化 ETag。

    实测同一份内容在 `?download=true` 下返回弱校验 `W/"…"`、否则返回强校验 `"…"`；
    GitHub 的又是 `"0x8DBF73607E168A6"` 这种十六进制大小写混排。
    不归一化会把同一份内容误判成「服务器文件已变」而白白丢弃进度。
    """
    if not e:
        return None
    return re.sub(r"^[Ww]/", "", e.strip()).strip('"').lower() or None


def _retryable(exc):
    """哪些异常值得重试。

    永久性 HTTP 错误（403/404/416…）不重试——否则会把鉴权失败当网络抖动，
    对限流站点等于延长封禁。
    **确定性的网络错误也不重试**（实测：连接被拒白等 15.3 秒，黑洞地址 91.3 秒），
    因为重试只会吃掉 agent 那一轮的调用预算。也不能拿 OSError 一把兜住——
    那会把 PermissionError / 磁盘写满也当成网络抖动重试 7 秒。
    """
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in (408, 429) or 500 <= exc.code < 600
    if isinstance(exc, urllib.error.URLError):
        reason = getattr(exc, "reason", None)
        # ---- 会审 P2：urlopen 把 ConnectionRefused 包进 URLError.reason，
        # 下面那个裸 isinstance 判定因此是死代码——实测照样重试 4 次白烧 7-15 秒
        if isinstance(reason, (socket.gaierror, ssl.SSLError, ConnectionRefusedError)):
            return False                          # DNS 失败 / 证书问题 / 端口没人听：确定性
        return True
    if isinstance(exc, ConnectionRefusedError):
        return False                              # 端口没人听：重试无意义（直抛形态兜底）
    if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, TimeoutError,
                        socket.timeout, http.client.HTTPException)):
        return True
    return isinstance(exc, ConnectionError)


def _retry_after_sec(e, cap=120):
    """解析 Retry-After（秒）。只认数字形式——HTTP-date 太少见且各站乱给，不猜。

    上限 cap：真见过 Retry-After: 86400 的站，照单全收会让一次下载静默挂一天，
    agent 的调用预算早烧完了。超上限就按上限等；等不起就失败，重跑续传。
    """
    h = getattr(e, "headers", None)
    v = h.get("Retry-After") if h else None
    if v is not None and str(v).strip().isdigit():
        return min(int(str(v).strip()), cap)
    return 0


def _attempt(fn, what, tries=PROBE_TRIES, budget=PROBE_BUDGET):
    """指数退避重试，带**总时长预算**。

    探测阶段原本一次重试都没有，而它恰恰是最容易失败的一步：实测同一个 GitHub
    release 链接会出现「几分钟前 6.9 秒下完 10MB、随后连续 3 次各 21 秒超时」的交替。

    但纯次数上限在"每次 21 秒"和"每次 1 秒"下含义完全不同 —— 对 agent 来说真正
    重要的是**这一轮调用最多花多久**，所以这里同时卡次数和总时长。
    """
    t0 = time.time()
    for i in range(tries):
        try:
            return fn()
        except BaseException as e:
            wait = 2 ** i
            if not _retryable(e) or i == tries - 1:
                raise
            if time.time() - t0 + wait > budget:
                raise                                 # 再等下去会超出探测总预算
            print("  %s 失败（%s: %s），%.0f 秒后重试（第 %d/%d 次）"
                  % (what, type(e).__name__, str(e)[:60], wait, i + 2, tries))
            time.sleep(wait)


# ---- v4.5 鉴权透传（--header / --hf-token）：进程级注入三咽喉 ----
# 三咽喉 = _open（探测/流式/list_repo 全走它）+ fetch（分块）+ 流式重试。
# 探针 _probe_proxy 不注入：用户凭据只应发给下载目标，不应发给探测目标。
EXTRA_HEADERS = {}   # main() 解析参数后填充（CLI 一次进程一个任务，模块级足够）


# v4.7 P1：协议头由工具接管，--header 注入会破坏语义（外部评审：fetch 的头字典
# 里 **EXTRA_HEADERS 排在 Range/UA 之后，用户传 Range 会覆盖分块区间产出坏文件
# 且无警告）。直接拒绝并说明原因，不静默丢弃。User-Agent 不拒——防盗链站传
# 浏览器 UA 是合法需求，改为"用户赢"优先级（见 _open/fetch）。
_RESERVED_HEADERS = {
    "range": "分块下载的 Range 区间由工具按块生成，注入会产出错位的坏文件",
    "host": "Host 由 HTTP 层按目标 URL 生成",
    "content-length": "Content-Length 由 HTTP 层管理",
    "connection": "连接管理由 urllib 处理",
    "accept-encoding": "urllib 不自动解压，声明 gzip 会把压缩体原样落盘",
}


def _parse_headers(pairs):
    """"Key: Value" 列表 -> dict。按第一个冒号分割（值里可含冒号，如 Bearer a:b）。
    非法项报 bad_args——静默丢弃会让 agent 以为鉴权带上了，401 死循环排查无门。
    保留头（协议语义头）直接拒绝；两条请求路径（_open/fetch）对 User-Agent 统一
    "用户赢"，避免改前两条路径优先级相反的不一致。"""
    out = {}
    for it in pairs or []:
        k, _, v = it.partition(":")
        k, v = k.strip(), v.strip()
        if not k or not v:
            raise Fail("bad_args", "--header 格式应为 'Key: Value'（收到 %r）" % it)
        # v5.5 复审（A-P1/B-P2 同根）：控制字符与非 latin-1 在边界拦截。值中段
        # "\r "+文本是 obs-fold，宽容服务器折叠成第二个头（折叠注入）；裸 \n/
        # 控制字符让 http.client putheader 抛 ValueError → internal（把输入形状
        # 错报成运行期错）；中文等非 latin-1 出站必炸。HTTP 头合法字符集本就是
        # latin-1，提前 bad_args——CLI --header 直接受益。
        for label, part in (("键", k), ("值", v)):
            if any(ord(c) < 0x20 or ord(c) == 0x7f for c in part):
                raise Fail("bad_args", "--header %s含控制字符（收到 %.40r）" % (label, part))
            try:
                part.encode("latin-1")
            except UnicodeEncodeError:
                raise Fail("bad_args", "--header %s含非 latin-1 字符，HTTP 头不支持"
                           "（收到 %.40r）" % (label, part))
        why = _RESERVED_HEADERS.get(k.lower())
        if why:
            raise Fail("bad_args", "--header 不接受保留头 %r：%s" % (k, why))
        # v5.4 P1-2/P2-8：键统一规范化到 urllib 出站键空间（Request.add_header
        # 对键做 capitalize()=首字母大写其余小写）。不规范化则 "authorization"
        # 小写键让凭据剥离的精确 pop 落空（urllib 出站会把它变回 Authorization
        # 发给镜像 host——18.8-F 红线泄漏）；"user-agent" 与工具 UA 同键坍缩
        # 后被反杀。HTTP 头键大小写不敏感，规范化对服务器零影响。
        out[k.capitalize()] = v
    return out


# v5.16 P2-4 收口：302 落点白名单（模块级，重定向链与再发起两路同构引用）。
# Authorization 保留——HF resolve 302 到 cdn-lfs 后带 Bearer 是 huggingface_hub
# 同款行为（实测链路依赖）；Cookie 域作用域严格、自定义 X-* 鉴权头不该跨 host，
# 一律剥离。Range 不在此列：用户传入解析层已拒，工具生成的 Range 在剥离之后追加。
_XHOST_KEEP = frozenset(("accept", "accept-language", "referer", "user-agent",
                         "authorization"))


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    """跨 host 重定向凭据剥离——白名单式（v5.16 P2-4）。

    旧黑名单只剥 Cookie/cookie2，--header "X-Api-Key: SECRET" 会原样跟到 302
    落点（与发起侧 _open 的白名单不对称，zcode 双审实锤）。改白名单：只保留
    _XHOST_KEEP + range。Range 必须保留——续传请求 302 到 CDN 是主链路，剥了
    续传直接断。"""

    def redirect_request(self, req, fp, code, msg, hdrs, newurl):
        old = urllib.parse.urlsplit(req.full_url).netloc
        new = super().redirect_request(req, fp, code, msg, hdrs, newurl)
        if new is not None and old != urllib.parse.urlsplit(new.full_url).netloc:
            new.headers = {k: v for k, v in new.headers.items()
                           if k.lower() in _XHOST_KEEP or k.lower() == "range"}
        return new


_DEFAULT_OPENER = urllib.request.build_opener(_SafeRedirect())


def _open(url, method="GET", rng=None, timeout=PROBE_TIMEOUT, op=None,
          main_host=None, origin_host=None):
    # P1：用户头先铺底，User-Agent"用户赢"（缺省回落工具 UA）；Range 由工具
    # 生成（解析层已拒 Range，这里是防御纵深）。与 fetch 同语义。
    h = dict(EXTRA_HEADERS)
    if main_host:
        # v5.0 镜像凭据剥离（18.8-F）在探针路径的落点：head 的请求构造全走
        # _open，不剥的话镜像候选探测会把主源凭据发给镜像 host。语义与 fetch
        # 一致：请求发起时按目标 host 剥，redirect 链（_SafeRedirect）不动。
        # v5.9 P1/P2-1（zcode 审查）：① 名单式 pop 防不住自定义鉴权头——
        # --header "X-Api-Key: SECRET" 原样外发镜像；改白名单式放行，跨主源
        # 体系只保留非鉴权通用头，其余（Authorization/Cookie*/任意 X-* 凭据）
        # 一律不发。302 链的 _SafeRedirect 保留 Authorization（HF cdn-lfs 依赖）
        # 不受影响——镜像请求在发起头里就没有凭据，redirect 链自然无可泄漏。
        # ② main_host 升级为 host 集合 {原始, 落点}：同源镜像（host==原始）
        # 不再被 302 落点判据误剥自家凭据 401 出局；str 传参兼容旧形态。
        mhs = main_host if isinstance(main_host, (set, frozenset)) else (
            {main_host} if main_host else set())
        tgt = (urllib.parse.urlsplit(url).hostname or "").lower()
        if mhs and tgt not in mhs:
            keep = {"accept", "accept-language", "referer"}
            h = {k: v for k, v in h.items() if k.lower() in keep}
    if origin_host:
        # v5.16 P2-4 收口（r5 测试实测）：探测/下载期对 302 落点 URL 的**再发起**
        # 不经重定向链，链上剥离够不着这里——探测时 head() 跟随 302 后 final 已
        # 是落点 URL，后续 Range 探测/单连接/流式/fetch 全部直连落点，EXTRA_
        # HEADERS（X-Api-Key 等）原样直达，与 --header help 承诺矛盾。修法与
        # _SafeRedirect 同构：请求目标 host ≠ 原始 URL host（即落点请求）时，
        # 应用落点白名单（Authorization 保留——HF cdn-lfs 直连依赖；Cookie/X-*
        # 剥离）。与 main_host 剥离正交：镜像剥离先走（更严），这里只管落点。
        tgt = (urllib.parse.urlsplit(url).hostname or "").lower()
        if tgt and tgt != origin_host:
            h = {k: v for k, v in h.items()
                 if k.lower() in _XHOST_KEEP}
    # setdefault 键必须与 _parse_headers 规范化后的键空间一致（capitalize 形态）：
    # 用 "User-Agent" 会被规范化后的 "User-agent" 绕开命中，反杀回归（v5.4 P2-8）
    h.setdefault("User-agent", UA)
    if rng:
        h["Range"] = rng
    return _urlopen(urllib.request.Request(url, method=method, headers=h), timeout, op)


# ===================== 代理自动检测（v4.2） =====================
# 决策优先级：
#   1. 环境变量 HTTP(S)_PROXY / NO_PROXY —— urllib 原生行为，设了就完全尊重
#   2. --proxy URL 强制 / --no-proxy 强制直连
#   3. 自动检测：Windows 系统代理注册表 + 本机 127.0.0.1 监听端口扫描，
#      候选逐个探针验证"是不是真转发代理"——端口无关，谁用谁的端口都能发现
#      （SOCKS-only 口探针必然失败被排除；主流客户端同时开 HTTP 口）
# 按域名分流：GitHub/HF 系代理优先；hf-mirror/魔搭等国内 CDN 直连优先（绕美国线
# 纯浪费，v4.1 冒烟实测）。首选通道探测失败自动切备用——猜错只损失一次探测，
# 正确性不依赖域名表。SOCKS 不支持（urllib 限制），探针会自然排除。

PROXY_FIRST_HOSTS = ("github.com", "githubusercontent.com", "github.io",
                     "githubassets.com", "huggingface.co", "hf.co")
# 强制直连用的 opener：ProxyHandler({}) 会屏蔽环境变量里的代理
# （复审 B：剥 Cookie 的 redirect 策略必须挂满全部 opener——
#  --no-proxy 直连与代理回退通道若不挂，302 跨 host 照样带 Cookie）
_NOENV_OPENER = urllib.request.build_opener(_SafeRedirect(),
                                            urllib.request.ProxyHandler({}))
_OPENER_NOTE_DONE = set()      # 已打印过分流说明的域名（批量 62 片不刷屏）
_PROXY_CACHE = None            # 探测结果缓存（一次进程只扫一轮）


def _urlopen(req, timeout, op=None):
    """op=None 走 _DEFAULT_OPENER（等价 urlopen + 跨 host 剥 Cookie）；
    否则用指定 opener——自定义 ProxyHandler 会无视环境变量，这正是
    --proxy / --no-proxy 的强制语义。"""
    return (op or _DEFAULT_OPENER).open(req, timeout=timeout)


def _make_opener(purl):
    return urllib.request.build_opener(
        _SafeRedirect(),
        urllib.request.ProxyHandler({"http": purl, "https": purl}))


def _env_proxy_active():
    return bool(os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
                or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy"))


def _host_matches(host, suffix):
    return host == suffix or host.endswith("." + suffix)


def _is_loopback(host):
    """本机回环地址。代理进程在另一网络命名空间里，访问不到本机的 127.0.0.1
    ——实测 IDE 网关对 http://127.0.0.1:<port> 的转发请求直接黑洞挂起。
    loopback 目标永远不该碰代理（既定裁决：显式 --proxy 除外，用户意图优先）。"""
    h = (host or "").lower().strip("[]")
    return h in ("127.0.0.1", "localhost", "::1") or h.startswith("127.")


def _parse_proxy_server(s):
    """解析 WinINET ProxyServer。三种形态：host:port /
    http=host:port;https=host:port;ftp=... / 含 socks=...（跳过，urllib 不支持）。"""
    out = []
    s = (s or "").strip()
    if not s:
        return out
    if ";" in s:
        for part in s.split(";"):
            if "=" in part:
                scheme, addr = part.split("=", 1)
                addr = addr.strip()
                if scheme.strip().lower() in ("http", "https") and addr:
                    out.append(addr)
    elif "://" in s:
        scheme, addr = s.split("://", 1)
        if scheme.lower() in ("http", "https") and addr:
            out.append(addr)
    elif "=" in s:
        # 无分号的单条目形态也可能带 scheme 前缀（如 socks=1.2.3.4:1080）
        scheme, addr = s.split("=", 1)
        if scheme.strip().lower() in ("http", "https") and addr.strip():
            out.append(addr.strip())
    else:
        out.append(s)
    return out


def _registry_proxies():
    """Windows 系统代理（Clash 系"系统代理"模式写这里）。ProxyEnable=0 视为未设。"""
    if winreg is None:
        return []
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
        try:
            enable = winreg.QueryValueEx(k, "ProxyEnable")[0]
            if not enable:
                return []
            srv = winreg.QueryValueEx(k, "ProxyServer")[0]
        finally:
            k.Close()
    except OSError:
        return []
    return _parse_proxy_server(srv)


def _listen_addrs():
    """本机 LISTENING (host, port) 列表（netstat 解析，零依赖的通用做法）。
    r38（对齐 awsearch r37++）：放宽到 **局域网 IPv4 绑定**（部分客户端只
    bind 具体内网 IP，旧回环正则会漏）与 **127/8 全段**（仿真测试实锤
    127.0.0.2 可绑定）；0.0.0.0/[::] 全接口归一为 127.0.0.1；[::1] 补上；
    >=1024（代理不会占低位）；不截断——真代理端口无规律，耗时由并行探针
    兜住。"""
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True,
                             text=True, errors="replace", timeout=10).stdout
    except Exception:
        return []
    addrs = []
    seen = set()
    for m in re.finditer(r"^\s*TCP\s+(\S+):(\d+)\s+\S+\s+LISTENING",
                         out, re.M):
        host, p = m.group(1), int(m.group(2))
        if p < 1024:
            continue
        if host in ("0.0.0.0", "[::]", "::"):
            key = ("127.0.0.1", p)
        elif host == "[::1]":
            key = ("::1", p)
        elif host == "::1" or host.startswith("127."):
            key = (host, p)
        elif re.match(r"^(?:192\.168\.|10\.|172\.(?:1[6-9]|2\d|3[01])\.)", host):
            key = (host, p)
        else:
            continue
        if key not in seen:
            seen.add(key)
            addrs.append(key)
    return addrs


def _probe_proxy(purl, timeout=4.0, target="www.baidu.com:443"):
    """是不是一个能用的转发代理：经它对 https 目标发起 **CONNECT 隧道**，200 才算。
    必须测 CONNECT 而非明文 http 转发——实测本机有些服务（IDE 网关，8731/8732）
    肯转发明文 http 但 CONNECT 回 501，而我们的下载目标全是 https。SOCKS 口也会
    在这里握手失败被自然排除。r38：改为 socket 直发 CONNECT（更轻，不再真 GET
    favicon）；target 可换境外域做境外连通性验证；回环 RTT 毫秒级，timeout 1.5s
    足够判定。**CONNECT 目标必须 host:port 形态**——带路径（如 /favicon.ico）
    会被代理拒绝、真代理口被误分类为 socks（首跑实锤）。"""
    try:
        parts = urllib.parse.urlsplit(
            purl if "://" in purl else "http://" + purl)
        host = parts.hostname or ""
        port = parts.port or 80
        if not host:
            return False
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.sendall(("CONNECT %s HTTP/1.1\r\nHost: %s\r\n\r\n"
                       % (target, target)).encode("ascii"))
            buf = b""
            while b"\r\n\r\n" not in buf and len(buf) < 8192:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
            head = buf.split(b"\r\n", 1)[0].decode("latin-1", "replace")
            return head.split(" ")[1:2] == ["200"]
    except Exception:
        return False


def _probe_socks(purl, timeout=1.5):
    """疑似 SOCKS5 无认证口：握手 05 01 00 -> 05 00 即认定。**只做发现
    提示，不做转发**——urllib 不支持 socks，零依赖红线不引 PySocks；
    提示用户让客户端开 HTTP/混合口（v2rayN 默认同时开、Clash 用 mixed）。"""
    try:
        parts = urllib.parse.urlsplit(
            purl if "://" in purl else "http://" + purl)
        host = parts.hostname or ""
        port = parts.port or 80
        if not host:
            return False
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.sendall(b"\x05\x01\x00")
            r = s.recv(2)
            return len(r) == 2 and r[0] == 5 and r[1] == 0
    except Exception:
        return False


def _classify_port(purl, timeout=1.5):
    """单口分类（r38）：vpn=境外连通 HTTP 代理（baidu+google 双 CONNECT 过，
    入扫描候选）；http=国内 HTTP 口（baidu 过 google 拒——如宿主注入
    网关，不入选仅提示）；socks=SOCKS5 无认证口（提示用户开 HTTP/混合
    口）；other=死口/非代理服务。"""
    if _probe_proxy(purl, timeout=timeout):
        if _probe_proxy(purl, timeout=timeout,
                        target="www.google.com:443"):
            return "vpn"
        return "http"
    if _probe_socks(purl, timeout=timeout):
        return "socks"
    return "other"


def _state_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "ampull.state.json")


def _state_load():
    try:
        with open(_state_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _state_save(d):
    p = _state_path()
    tmp = p + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f)
        os.replace(tmp, p)
    except Exception:
        pass


def _detect_proxies():
    """注册表 + 本机监听端口 → **并行分类探针**（r38，对齐 awsearch r37++）。
    当日语义标记（ampull.state.json）：成功记 scan_ok_date 当日复用零重扫，
    失败记 scan_fail_date 当日退避，次日重扫（监听端口可能变）。分类：
    vpn 口（baidu+google 双 CONNECT 过）入候选——"仅境内可达"的口
    （IDE/宿主注入网关）永不入选；socks 口/国内口落提示。进程内缓存一次
    进程只扫一轮（批量 62 片不会扫 62 次）。"""
    global _PROXY_CACHE
    if _PROXY_CACHE is not None:
        return _PROXY_CACHE
    today = time.strftime("%Y-%m-%d")
    st = _state_load()
    if st.get("scan_ok_date") == today:
        _PROXY_CACHE = [_scan_pick(st)]
        if _PROXY_CACHE[0]:
            return _PROXY_CACHE
        _PROXY_CACHE = None  # 标记在但值为空：当失败处理，走重扫
    if st.get("scan_fail_date") == today:
        _PROXY_CACHE = []
        print("  今日扫描未发现境外代理口（已当日退避）——直连模式")
        return _PROXY_CACHE
    cands = ["http://%s" % a for a in _registry_proxies()]
    src = "系统代理注册表" if cands else ""
    for h, p in _listen_addrs():
        a = "http://%s:%d" % (h, p)
        if a not in cands:
            cands.append(a)
    if not src:
        src = "本机端口扫描"
    found = []
    socks_hint = []
    http_hint = []
    if cands:
        purls = [c if "://" in c else "http://%s" % c for c in cands]
        with cf.ThreadPoolExecutor(min(32, len(purls))) as ex:
            for purl, kind in zip(purls, ex.map(_classify_port, purls)):
                if kind == "vpn":
                    found.append(purl)
                elif kind == "http":
                    http_hint.append(purl)
                elif kind == "socks":
                    socks_hint.append(purl)
    _PROXY_CACHE = found[:3]          # 最多记 3 个：主 + 备用足够，列表太长没意义
    st = _state_load()
    st["scan_ok_date" if found else "scan_fail_date"] = today
    st["scan_proxy"] = found[0] if found else None
    if socks_hint:
        st["socks_hint"] = ", ".join(socks_hint[:3])
    if http_hint:
        st["http_hint"] = ", ".join(http_hint[:3])
    _state_save(st)
    if found:
        print("  检测到本机境外代理 %s（%s，当日复用）——github/hf 走代理优先；"
              "--no-proxy 可强制直连" % (", ".join(found), src))
    if socks_hint:
        print("  检测到 SOCKS 口 [%s]——AmPull 需要 HTTP/混合口，"
              "请在客户端中开启 HTTP 端口" % "; ".join(socks_hint))
    elif http_hint:
        print("  检测到国内 HTTP 口 [%s]（无境外连通性，仅提示）"
              % "; ".join(http_hint))
    if not found and not socks_hint and not http_hint:
        print("  未检测到可用代理口——github/hf 将直连尝试")
    return _PROXY_CACHE


def _scan_pick(st):
    """当日标记值规整：scan_proxy 为空视作无（走重扫）。"""
    p = (st.get("scan_proxy") or "").strip()
    return p if p else None


def _resolve_ops(a, host=None):
    """按 URL 域名决定通道。返回 (primary, alt) opener；None=urllib 默认
    （=无环境变量时直连）。alt 是首选通道探测失败时的备用。
    host 缺省取 a.url 的域名；--repo 的清单 API 走 HF_ENDPOINT 的域名
    （P2：清单与下载必须同一套分流，否则无环境变量时清单直连失败而下载
    却能走代理），由 list_repo 显式传入。"""
    if host is None:
        host = (urllib.parse.urlsplit(getattr(a, "url", "")).hostname or "")
    host = (host or "").lower()
    if getattr(a, "no_proxy", False):
        return _NOENV_OPENER, None
    forced = getattr(a, "proxy", None)
    if forced:
        if not forced.lower().startswith(("http://", "https://")):
            raise Fail("bad_args", "--proxy 仅支持 http/https 代理（收到 %r；"
                       "SOCKS 代理请用支持协议转换的客户端开 HTTP 口）" % forced[:60])
        return _make_opener(forced), None
    if _is_loopback(host):
        # 结构事实优先于环境变量猜测：代理进程访问不到本机回环，走代理只会黑洞挂死
        # （环境变量代理同样如此）。显式 --proxy 除外（用户意图照办）。
        if host not in _OPENER_NOTE_DONE:
            _OPENER_NOTE_DONE.add(host)
            print("  %s 本机回环：钉死直连（代理无法访问本机回环，不设备用）" % host)
        return _NOENV_OPENER, None
    if _env_proxy_active():
        # r38（awsearch r37 裁决回移植）：agent 宿主可能注入受限网关 env
        # （实测 WorkBuddy 子进程 127.0.0.1:54154——国内可达、境外失败），
        # 原生行为失败后切扫描口（当日标记复用）备用，而非裸直连
        cands = _detect_proxies()
        if cands:
            return None, _make_opener(cands[0])
        return None, None
    cands = _detect_proxies()
    if not cands:
        return None, None
    p = _make_opener(cands[0])
    if any(_host_matches(host, d) for d in PROXY_FIRST_HOSTS):
        if host not in _OPENER_NOTE_DONE:
            _OPENER_NOTE_DONE.add(host)
            print("  %s 走代理 %s（域名策略），失败自动改直连" % (host, cands[0]))
        return p, _NOENV_OPENER
    if host not in _OPENER_NOTE_DONE:
        _OPENER_NOTE_DONE.add(host)
        print("  %s 直连优先（检测到的代理 %s 仅作备用）" % (host, cands[0]))
    return None, p


class Fail(Exception):
    """带机器可读错误码的失败，用于填充 JSON 的 error 字段。"""

    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


def _ident_safe(u):
    """journal 是追加式历史文件，url 字段形态不可信（手改/null/怪串）——
    ident_of 只吃字符串，撞异常按"不同源"处理（v5.2 复审 P2）。"""
    if not isinstance(u, str):
        return None
    try:
        return ident_of(u)
    except Exception:
        return None


def journal_same_source(out_path, url):
    """journal 反查：out_path 是否由本工具从同一身份（ident_of 去掉 query）成功产出。

    v5.2 A11 防假完成的身份证据。journal 默认常开（写失败静默），同一命令重跑
    时这里必然命中 → A11 秒退契约保持；查无记录（journal 被删/截断/文件是别的
    程序放的）→ 保守改名，宁可重下不可静默拿错文件。
    """
    try:
        ident = ident_of(url)
        with open(journal_path(), encoding="utf-8") as f:
            for ln in f:
                try:
                    e = json.loads(ln)
                except Exception:
                    continue
                if (e.get("ok") and e.get("path") == out_path
                        and _ident_safe(e.get("url")) == ident):
                    return True
    except OSError:
        pass
    return False


def conflict_rename(out):
    """迅雷 §8.1 同名冲突改名：README.md → README(1).md → …；无扩展名 → 名(1)。

    只在"即将静默覆盖或假完成"的时刻调用（v5.2 dogfood 反馈②）。返回新路径；
    目录部分不变。永不失败（N 递增总有空位），不碰旧文件。
    """
    d, base = os.path.split(out)
    stem, ext = os.path.splitext(base)
    n = 1
    while True:
        cand = os.path.join(d, "%s(%d)%s" % (stem, n, ext))
        if not os.path.exists(cand):
            return cand
        n += 1


def _atomic_replace(src, dst, tries=25, wait=0.4):
    """os.replace 的 Windows 占用族错误有界重试（v4.8 复审 P1，v5.14 扩谱）。

    --serve 会长期持有 .part/账本/最终文件的只读句柄（缺口挂起时也不关），
    CPython 的 open 无 FILE_SHARE_DELETE，下载端的 rename 撞上在读句柄会抛
    PermissionError（sharing violation）——不重试会让"下到 100% 却报 io 失败"。
    v5.14：errmap.h 把 winerror 5/32/33 全映射 EACCES，但 Python 层只有
    winerror 5 落成 PermissionError，32/33 是裸 OSError——只捕 PermissionError
    会漏掉 sharing/lock violation；一并捕获。tries 10→25（窗口 4s→10s），
    杀毒软件扫描句柄的占用窗口实测可超 4 秒。非 Windows 无此问题，helper
    通用无害。
    """
    for i in range(tries):
        try:
            os.replace(src, dst)
            return
        except OSError as e:
            if getattr(e, "winerror", None) not in (5, 32, 33):
                raise
            if i == tries - 1:
                raise
            time.sleep(wait)


def _remove_quiet(p):
    """删除失败静默吞掉（二轮复审 P3：serve/播放器只读句柄的 sharing violation 窗口）。

    只允许用在"删不掉也不破坏一致性"的位置：A7 校验失败作废（数据文件随后
    改名或原样保留，账本与数据文件始终成对，A1 的存在性守卫天然兜底）、旧
    产物清理（内容本就判废，替换失败最终也会诚实报错）。**不能**用在"删完
    账本就截断重写"的路径——残留账本+截断会让下次 ranged 重跑信任旧块号补
    出零洞（静默坏文件），那些位置必须走 _void_ledger。"""
    try:
        os.remove(p)
    except OSError:
        pass


def _void_ledger(p):
    """作废账本，且作废必须实际生效（二轮复审 P3 修正版）。

    用于下游紧跟 open(part,"wb") 截断重写的两处（流式降级/单连接回退）：账本
    若残留，A1 只校验 url/size/chunk/etag 与 .part 存在性，不校验账本块号与
    .part 实际内容一致——残留账本+截断会让下次 ranged 重跑信任旧块号，补出
    来的是零洞（静默坏文件，仅 --sha256 能拦）。所以吞掉不算作废：先删；被
    占（serve 只读句柄/杀毒扫描）则清空内容——CPython 读句柄是 denynone 共享，
    截断写不被 sharing violation 拦，空文件 json.load 必失败，A1 照样整体作废。
    两步都失败（非 CPython 的独占句柄）才报 locked：状态未被破坏（尚未截断），
    诚实报错优于带病继续。"""
    try:
        os.remove(p)
        return
    except OSError:
        pass
    try:
        with open(p, "w"):
            pass
        return
    except OSError:
        pass
    raise Fail("locked", "断点账本被其他程序独占占用且无法作废，关闭占用它的程序后重试")


class Throttle:
    """自适应并发闸门（AIMD）：限流信号对半砍并发，顺风后缓慢恢复。

    背景：镜像站对单 IP 限连接/限频，固定 16 连接撞上后所有 worker 同秒 429、
    同秒重试、再同秒 429（雷群效应）。砍掉名额后**在飞的连接自然跑完**（不强杀，
    已下载到一半的块不作废），新连接在闸门前排队；恢复限速为每 2 秒最多 +0.25
    个名额（约半分钟从 8 回到 16），期间再撞就再砍——最终收敛到服务器能接受的
    水平，而不是靠人肉调 -c。
    """

    def __init__(self, n, abort=None):
        self.max = float(n)
        self.limit = float(n)
        self.inflight = 0
        self._last_reward = 0.0
        self._abort = abort
        self._cv = threading.Condition()

    def _allowed(self):
        return max(1, min(self.max, int(self.limit)))

    def __enter__(self):
        with self._cv:
            while self.inflight >= self._allowed():
                # 排队期间其他分块已把 stop 置位：别再放行新连接，早点收摊
                if self._abort is not None and self._abort.is_set():
                    raise Fail("cancelled", "已取消（排队等闸门时已有其他分块失败）")
                self._cv.wait(timeout=0.5)
            self.inflight += 1
        return self

    def __exit__(self, *exc):
        with self._cv:
            self.inflight -= 1
            self._cv.notify()
        return False

    def penalize(self, why):
        with self._cv:
            before = self._allowed()
            self.limit = max(1.0, self.limit / 2)
            after = self._allowed()
        if after < before:
            print("\n  限流信号（%s）：并发收缩 %d -> %d" % (why, before, after))

    def reward(self):
        with self._cv:
            if self.limit >= self.max:
                return
            now = time.monotonic()
            if now - self._last_reward < 2.0:
                return
            self._last_reward = now
            self.limit = min(self.max, self.limit + 0.25)


def sha256_of(path):
    h = hashlib.sha256()
    total = max(os.path.getsize(path), 1)
    got, t0 = 0, time.time()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 24), b""):
            h.update(block)
            got += len(block)
            # 全文件哈希对几十 GB 的模型要数分钟——100% 之后静默这么久会被当成卡死。
            # v5.15 走 _progress_print：真终端 \r 同行刷新；管道下整行节流输出
            _progress_print("  SHA256 %.1f%%  %.2f GB/s  " % (100 * got / total,
                                                             got / 1e9 / max(time.time() - t0, 1)))
    _progress_end()
    return h.hexdigest()


def ident_of(url):
    """账本身份：原始 URL 去掉 query/fragment。

    绝不能用重定向后的地址：实测 GitHub release 会 302 到带签名和硬过期时间
    (se=...) 的 Azure Blob SAS URL，签名过期后同一文件的地址就变了，
    用它做身份会让跨窗口的续传永远被判「身份不符」而从头下。
    v5.4 P2-12：netloc 一并剥掉 userinfo（user:pass@host 的明文密码不进身份、
    不进账本/历史/收据）。代价：带 userinfo 的 URL 旧账本 ident/fp 失配一次，
    判废重下（诚实升级代价）。
    """
    u = urllib.parse.urlsplit(url)
    netloc = u.netloc.rsplit("@", 1)[-1] if u.netloc else u.netloc
    return "%s://%s%s" % (u.scheme, netloc, u.path)


def _redact_url(u):
    """v5.4 P2-12：URL 内嵌凭据（user:pass@host）只属于请求发起方，落盘/出站
    一律脱敏——工具对 Authorization/Cookie 层层设防，把 netloc 里的明文密码
    写进 journal/收据就全白设了。query 原样保留（SAS 找回链接的既有取舍不变）；
    fetch/池条目仍用原始 URL（带凭据才能下载），只动展示与落盘面。"""
    try:
        s = urllib.parse.urlsplit(u)
        if "@" not in (s.netloc or ""):
            return u
        return urllib.parse.urlunsplit(
            (s.scheme, s.netloc.rsplit("@", 1)[-1], s.path, s.query, s.fragment))
    except ValueError:
        # 畸形 URL（urlsplit 拒收）也要剥——脱敏不因畸形豁免（v5.4 复审 P3）。
        # 只看 authority 段（:// 后第一个 / 之前），query 里的 @ 不受影响
        p = u.find("://")
        if p == -1:
            return u
        h = u.find("/", p + 3)
        seg = u[p + 3:h if h != -1 else len(u)]
        i = seg.rfind("@")
        if i == -1:
            return u
        return u[:p + 3] + seg[i + 1:] + (u[h:] if h != -1 else "")


def fp_of(size, etag, url):
    """账本内容指纹（v5.0 方案 18.3）：归一化(size)+"|"+归一化 ETag。

    取代 url 成为账本主身份：--mirror 下同一内容有多个 URL，URL 已不是内容
    身份，fp 才是——断点跨镜像续传互认、入池混用门都靠它。无 ETag 退化为
    size+ident 路径（与 v4.9 的 url+size 判定强度等价：无 ETag 时兜底用
    ident_of——含 host，跨站同路径同大小不会误续传，复审 P2-2；有 ETag 时
    才是跨镜像内容指纹）。人读注意：fp 是机器字段，别把它当 URL 解析。
    """
    return "%d|%s" % (size, _norm_etag(etag) or ident_of(url))


# ===================== --mirror 多源池（v5.0，方案 §18） =====================
# 职责分工（18.1）：镜像发现归 agent（语义判断+检索候选站），验证与搬运归下载器。
# 一个 flag 两种形态：--mirror URL（可重复）与 --mirror @文件（每行一个 URL，
# # 是注释）；@auto 显式读运行历史（默认关——行为不可见是 agent 调用的大忌）。
# 输入输出分离红线：程序永不写用户指定的 @文件；历史只落固定路径 mirrors.jsonl。
# 历史是候选清单不是信任清单：自动回填照常过入池门+混用门，信任每次重新挣。

MIRROR_TIMEOUT = 20        # 每候选探测单请求超时（hf-mirror 实测 HEAD 最慢 17s）


def mirror_history_path():
    """镜像运行历史：与 ampull.py 同目录的 mirrors.jsonl。
    测试可用 DL_MIRROR_HISTORY 覆盖路径（与 DL_JOURNAL 同款模式）。"""
    p = os.environ.get("DL_MIRROR_HISTORY")
    if p:
        return p
    # 数据文件随程序走（便携）：与 ampull.py 同目录，整文件夹拷走历史不丢
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "mirrors.jsonl")


def _load_mirror_urls(specs):
    """--mirror 参数解析：URL / @文件 / @auto。返回候选 URL 列表（可能重复，调用方去重）。
    @文件 每行一个 URL，# 注释、空行跳过；读失败报 bad_args（静默丢弃会让 agent
    以为镜像生效了）。"""
    urls = []
    for spec in specs or []:
        s = (spec or "").strip()
        if not s:
            continue
        if s == "@auto":
            try:
                with open(mirror_history_path(), encoding="utf-8") as f:
                    for ln in f:
                        try:
                            u = json.loads(ln)
                        except ValueError:
                            continue
                        # v5.14：裸标量行（123/true）与 {"url": 123} 会抛
                        # AttributeError 逃过 except ValueError，整批解析炸掉——
                        # isinstance 双重防护。
                        if not isinstance(u, dict):
                            continue
                        u = u.get("url")
                        if isinstance(u, str) and u.startswith(("http://", "https://")):
                            urls.append(u)   # A-P2-1：历史行同过白名单（防呆过滤）
            except OSError:
                pass                      # 无历史=无候选，不算错误
        elif s.startswith("@"):
            path = s[1:]
            try:
                with open(path, encoding="utf-8") as f:
                    for n, ln in enumerate(f, 1):
                        ln = ln.strip()
                        if ln and not ln.startswith("#"):
                            # A-P2-1：白名单对 @文件 行同样生效——直写规格 v5.9 P3-6
                            # 已拦，@文件 行漏过会带着 ftp:/裸域名白耗 20s 探测超时
                            if not ln.startswith(("http://", "https://")):
                                raise Fail("bad_args", "--mirror @文件 %s 第 %d 行"
                                           "不是 http(s):// URL：%r" % (path, n, ln[:60]))
                            urls.append(ln)
            except OSError as e:
                raise Fail("bad_args", "--mirror @文件读取失败：%s（%s）" % (path, e))
        else:
            # 复审 P3-6：scheme 白名单——裸域名/其他协议（ftp:、file: 等）进探针
            # 只会白耗 20s 超时，早失败优于静默出局
            if not (s.startswith("http://") or s.startswith("https://")):
                raise Fail("bad_args", "--mirror 候选必须是 http(s):// URL 或 "
                                       "@文件/@auto 形态：%r" % s)
            urls.append(s)
    return urls


def _probe_mirrors(cands, a, size, etag, want_sha, main_host=None):
    """并行 head 探测全部候选，按 18.2 双门筛选。返回入池列表
    [{url,final,host,etag,op,alt}]。

    并行探测且全部完成才返回（入池判定在任何字节之前）；每候选单请求
    MIRROR_TIMEOUT 秒、**通道失败自动切备用一次**（v4.2 域名策略的自我修正
    延伸到镜像——猜错通道只损失一次探测）；再失败即出局，候选站本来就允许
    不可靠。opener 解析全部在主线程完成（_resolve_ops），并行段只跑 head——
    不给 worker 线程并发触碰 _detect_proxies 的窗口（v5.1）。
    通道按镜像自己的域名独立分流（v5.1）：hf-mirror/魔搭等国内 CDN 直连
    优先，不继承主源通道；--proxy 强制/--no-proxy/环境变量分支对所有 host
    行为一致（显式指定=用户赢）。
    入池门：size 与主源一致 + 真实支持 Range（能力门，18.8-D）。
    混用门：镜像 ETag 与主源一致放行；不一致但有 --sha256 终检兜底放行
    （镜像 ETag 体系可能与源站不同，不一致不等于内容不同）；否则出局。
    ETag 单侧缺失不判废（T3 原则同构），v5.16 起打印警告（不再静默入池）。
    出局必须说出原因（A5）。池整个 run 固定。
    main_host：探针请求剥凭据用（18.8-F，探测也是请求）。
    """
    def probe(u, hop, halt):
        # 复审 P0：head 把 URLError/OSError 包装成 Fail("network")（L927/L984），
        # 裸网络异常只有 HEAD 回退段的 5xx HTTPError——alt 切换必须两样都接，
        # 否则"猜错通道"的主场景（超时/拒连/DNS）永远切不出去（死代码，测试
        # monkeypatch 抛裸异常曾假阳性）
        try:
            try:
                ms, mr, mf, mn, metag, mct, mlm = head(
                    u, op=hop, timeout=MIRROR_TIMEOUT, tries=1,
                    main_host=main_host,
                    origin_host=(urllib.parse.urlsplit(u).hostname or "").lower())
            except Fail as e:
                if e.code != "network" or halt is None:
                    raise                  # 词表错误直通；无备用通道：出局
                ms, mr, mf, mn, metag, mct, mlm = head(
                    u, op=halt, timeout=MIRROR_TIMEOUT, tries=1,
                    main_host=main_host,
                    origin_host=(urllib.parse.urlsplit(u).hostname or "").lower())
            except (urllib.error.HTTPError, urllib.error.URLError, OSError):
                if halt is None:
                    raise                  # 无备用通道：单请求出局（设计语义）
                ms, mr, mf, mn, metag, mct, mlm = head(
                    u, op=halt, timeout=MIRROR_TIMEOUT, tries=1,
                    main_host=main_host,
                    origin_host=(urllib.parse.urlsplit(u).hostname or "").lower())
            return (u, ms, mr, mf, metag, None, hop, halt)
        except Exception as e:            # 线程池里只传值不传异常（复审 P2：收窄
            return (u, None, None, None, None, e, hop, halt)  # BaseException，
                                              # Ctrl+C 不再被吞成 err 元组）

    # 主线程逐候选解析通道（_detect_proxies 只会在这里首次触发，无并发窗口）
    chan = [(u,) + _resolve_ops(a, host=(urllib.parse.urlsplit(u).hostname or ""))
            for u in cands]
    results = []
    ex = cf.ThreadPoolExecutor(max(1, min(len(cands), 16)))
    futs = [ex.submit(probe, u, hop, halt) for u, hop, halt in chan]
    for f in cf.as_completed(futs):
        results.append(f.result())
    ex.shutdown(wait=False)

    ne = _norm_etag(etag)
    pool = []
    for u, ms, mr, mf, metag, err, hop, halt in sorted(results, key=lambda r: cands.index(r[0])):
        host = (urllib.parse.urlsplit(u).hostname or "?")
        if err is not None:
            print("  镜像出局 %s（探测失败：%s）" % (host, str(err)[:100]))
            continue
        if size is None or ms != size:
            print("  镜像出局 %s（大小不符：远端 %s 而主源 %s）"
                  % (host, ms, size))
            continue
        if not mr:
            print("  镜像出局 %s（不支持 Range，无法参与分块接力）" % host)
            continue
        me = _norm_etag(metag)
        if ne and me and ne != me:
            if want_sha:
                print("  镜像 %s 的 ETag 与主源不一致，已有 --sha256 终检兜底，放行入池" % host)
            else:
                print("  镜像出局 %s（ETag 与主源不一致且未给 --sha256 终检兜底，"
                      "镜像内容可能不同）" % host)
                continue
        elif (ne or me) and not (ne and me):
            # v5.16 P2-5：单侧无 ETag 时混用门比对不了（同大小不同内容拼混合体
            # 且长度校验照过）——此前静默入池，与续传路径的 ETag 警告不对称。
            # 不判废（大量静态源/劣质 CDN 不给 ETag，废掉等于废掉镜像加速），
            # 但必须说出来；要强保证就配 --sha256。
            print("  警告：镜像 %s %s，混用门无法比对内容一致性；同大小不同内容"
                  "有拼坏文件风险，建议配 --sha256 终检"
                  % (host, "未返回 ETag" if not me else "主源未提供 ETag"))
        print("  镜像入池 %s（%s）" % (host, ident_of(u)[:90]))
        # etag 随池条目走：混用门放行的异 ETag 镜像，分块 fetch 校验必须用镜像
        # 自己的 ETag（v5.0 实测：拿主源 ETag 校验镜像块 = 必撞 etag_mismatch，
        # "sha256 兜底放行"名存实亡）；单侧缺失 None → fetch 不做 ETag 判定。
        # op/alt 同样随条目走（v5.1）：分块请求按镜像自己的域名分流
        pool.append({"url": u, "final": mf, "host": host, "etag": me,
                     "op": hop, "alt": halt})
    return pool


def _mirror_log(entries):
    """带 --mirror 的运行按镜像落一行历史（host/url/bytes/fails/ewma/ts），
    供 agent 跨会话学习哪个镜像真的好（--json mirrors 字段是本次运行的实时半边）。
    写失败静默吞掉——日志永远不能影响下载本身。红线：只写固定历史路径，
    永不写用户 @文件（输入归用户，输出归程序）。
    v5.16 P3：与 journal_append 同款 2MB 截断保留最后 1000 行——镜像学习是
    长期资产，但无限增长违背「日志永不影响下载」的体面。"""
    if not entries:
        return
    try:
        p = mirror_history_path()
        d = os.path.dirname(p)
        if d:
            os.makedirs(d, exist_ok=True)
        if os.path.exists(p) and os.path.getsize(p) > (2 << 20):
            with open(p, encoding="utf-8") as f:
                lines = f.readlines()[-1000:]
            with open(p, "w", encoding="utf-8") as f:
                f.writelines(lines)
        with open(p, "a", encoding="utf-8") as f:
            for ent in entries:
                f.write(json.dumps(ent, ensure_ascii=True) + "\n")
    except Exception:
        pass


def _parse_cl(headers):
    """解析 Content-Length。None=服务器不肯告知；0=空文件（N1：必须与"未知"区分，
    否则 0 字节文件会被折进流式分支，永远失败）。非数字头部归为未知——劣质网关
    确实会发这种东西，裸 int() 会把 ValueError 泄漏进 JSON 的 error 字段。"""
    cl = headers.get("Content-Length")
    if cl is None or not cl.strip().isdigit():
        return None
    return int(cl)


def _cd_filename(headers):
    """从 Content-Disposition 提取文件名（RFC 5987 filename* 优先，支持百分号编码）。

    对象存储的 302 落点（魔搭 CDN 实测）URL 路径末段是 LFS oid，真名只存在
    响应头里——不取这里，几百 MB 的 gguf 会落盘成一串 64 位哈希，批量分片
    全部无法对号入座。"""
    cd = headers.get("Content-Disposition") or ""
    m = re.search(r"filename\*=(?:UTF-8''|utf-8'')([^;]+)", cd, re.I) \
        or re.search(r'filename="([^"]+)"', cd) \
        or re.search(r"filename=([^;\s]+)", cd)
    if not m:
        return None
    name = urllib.parse.unquote(m.group(1).strip().strip('"'))
    name = os.path.basename(name.replace("\\", "/")).strip()
    return name or None


# ===================== GitHub 归档 404 分支回退（v5.18） =====================
# 动机（agent 实操复盘）：github.com/OWNER/REPO/archive/refs/heads/main.zip 撞 404
# 多数不是代理坏、也不是链接写错格式，而是默认分支不叫 main（master/develop/…）。
# 原 v5.17 行为：HEAD 404 → Range GET 也 404 → 吞成单连接 → 下载循环再撞 404 →
# http_404，agent 得自己猜分支名再来一轮。回退链：查 API 默认分支 → 常见分支名
# 候选 → 命中即原地改写 URL（保留 gh-proxy 等包装前缀），全败维持 http_404。
# 捕获组（命名）：prefix=可选包装前缀(gh-proxy 等，含尾斜杠) inner=内层 URL
# owner/repo/ref/ext。直连时 prefix=None——所以重构 base 必须用 (prefix or "")。
# 包装前缀是 agent 在国内网络下的既有选择（gh-proxy 等），改写必须只动内层
# ref——剥掉等于替用户换通道。
_GH_ARCHIVE_RE = re.compile(
    r"^(?P<prefix>https?://[^/]+/)?"
    r"(?P<inner>https?://(?:www\.)?github\.com/"
    r"(?P<owner>[^/?#]+)/(?P<repo>[^/?#]+?)/archive/"
    r"(?:refs/(?:heads|tags)/|)?"
    r"(?P<ref>[^/?#]+?)(?P<ext>\.zip|\.tar\.gz|\.tgz))$")
_GH_CANDIDATE_REFS = ("master", "main", "develop")


def _gh_archive_fallback(url, dl_op, alt_op, res):
    """GitHub 归档 URL 404 后的分支回退。命中返回改写后的 URL，否则 None。

    只对 404 触发（403/401 是鉴权/防盗链，分支名救不了）；候选 ref = 仓库默认
    分支（API 权威查询，查询失败降级为纯猜测）+ master/main/develop，去重且
    跳过原 ref。每个候选用 head(tries=1) 快速探测——g3 的双 404 快败让死候选
    立即出局，不烧重试预算。通道语义：None=urllib 默认（直连/环境变量），是
    合法通道不是"无通道"，与 _resolve_ops 返回值同构。"""
    m = _GH_ARCHIVE_RE.match(url)
    if not m:
        return None
    prefix, inner = m.group("prefix") or "", m.group("inner")
    owner, repo, old_ref, ext = m.group("owner", "repo", "ref", "ext")
    ops = [dl_op] + ([alt_op] if alt_op is not None else [])

    def probe(u):
        """单候选探测：通道级故障切下一通道，双 404 由 g3 快败立即出局，
        网络不通且无备用通道时放弃整个回退。"""
        for i, op in enumerate(ops):
            try:
                head(u, op=op, timeout=MIRROR_TIMEOUT, tries=1)
                return True
            except urllib.error.HTTPError:
                continue        # 404/403 等确定性拒绝：该候选出局
            except (Fail, urllib.error.URLError, OSError):
                if i + 1 < len(ops):
                    continue    # 通道问题：切备用通道再试同一候选
                return False
        return False

    # ---- 权威查询：仓库默认分支。api.github.com 与 github.com 同后缀命中
    #      PROXY_FIRST_HOSTS，dl_op 的通道决策天然适用；查询失败不拦回退，
    #      降级为纯候选猜测。----
    api_url = "https://api.github.com/repos/%s/%s" % (owner, repo)
    default_branch = None
    for op in ops:
        try:
            with _open(api_url, "GET", timeout=MIRROR_TIMEOUT, op=op) as r:
                default_branch = (json.loads(r.read().decode(
                    "utf-8", "replace")).get("default_branch") or "").strip()
            break
        except Exception:
            continue
    if default_branch and not re.fullmatch(r"[\w.\-/]+", default_branch):
        default_branch = None               # API 返回怪值不当 ref 用

    cands = []
    for ref in ([default_branch] if default_branch else []) + \
            list(_GH_CANDIDATE_REFS):
        if ref and ref != old_ref and ref not in cands:
            cands.append(ref)
    if not cands:
        return None

    print("  归档 404：分支 %r 不存在，尝试常见分支（%s）…"
          % (old_ref, ", ".join(cands[:6])))
    base = (prefix or "") + inner[:inner.rfind("/archive/")] + "/archive/"
    for ref in cands:
        # 先按原扩展名试归档（refs/heads 与 legacy 解析到同一 ref，单形态即可），
        # 再试 codeload 直链（同 ref 的另一服务器路径；chunked 但通常绕开
        # github.com 侧的怪拦截——agent 实操复盘：早试它可省两轮）
        cands_url = (base + "refs/heads/%s%s" % (ref, ext),
                     "https://codeload.github.com/%s/%s/zip/refs/heads/%s"
                     % (owner, repo, ref))
        for cand in cands_url:
            if probe(cand):
                _warn(res, "gh_branch_fallback")
                print("  命中分支 %r，改用：%s" % (ref, _redact_url(cand)))
                return cand
    return None


def head(url, op=None, timeout=PROBE_TIMEOUT, tries=PROBE_TRIES, main_host=None,
         origin_host=None):
    """探测。返回 (size, ranged, final, name, etag, ctype, lm)。op 指定通道 opener。

    size is None 表示服务器不肯告知体积（chunked 且 Range 也不给 Content-Range），
    调用方应退化为流式下载。**不再把它当成致命错误**：实测 hf-mirror 上的非 LFS
    小文件（config.json / tokenizer.json 等）正是这种形态，而 agent 下一个模型
    需要这一整套配套文件。
    lm 是第三身份因子（v4.9）：Last-Modified 原样出站（弱时间戳，判定同构于 ETag：
    双存在且不等才判废），调用方写进账本做续传身份比对。
    timeout/tries（v5.0）：镜像候选探测用 20s 单请求无重试（hf-mirror 实测 HEAD
    最慢 17s，方案 18.2"每候选超时 20s"）；主源探测保持默认（30s x 4 次预算）。
    main_host（v5.0）：镜像候选探测传入主源 host——目标 host≠主源时剥凭据
    （18.8-F 与 fetch 同构）；主源探测不传，凭据照发。
    origin_host（v5.16 P2-4 收口）：原始 URL host——请求目标（含 302 落点）host
    ≠ 原始 host 时应用落点白名单（Cookie/X-* 剥离，Authorization 保留）。
    """
    size, ranged, etag, ctype, final = None, False, None, "", url

    def head_once():
        with _open(url, "HEAD", timeout=timeout, op=op, main_host=main_host,
                   origin_host=origin_host) as r:
            return r.headers, r.geturl()

    headers = None
    try:
        headers, final = _attempt(head_once, "HEAD 探测", tries=tries)
    except urllib.error.HTTPError as e:
        if e.code not in HEAD_REJECTED:
            raise
        print("  服务器不接受 HEAD（HTTP %d），改用 GET + Range 探测…" % e.code)
    except (urllib.error.URLError, OSError) as e:
        # 重试耗尽。必须收敛成 network：否则 ConnectionResetError 这类 OSError 会
        # 一路冒到 main() 的 except OSError，被报成 "io"+「本地文件操作出错」——
        # 纯网络故障被说成磁盘/权限问题，agent 会去查错方向。
        raise Fail("network", "探测失败（%s: %s）。网络可能不通，稍后重跑同一条命令即可"
                   % (type(e).__name__, e))

    lm = None                 # 第三身份因子（v4.9）：Last-Modified，与 ETag 同构判定
    if headers is not None:
        size = _parse_cl(headers)
        etag = _norm_etag(headers.get("ETag"))
        lm = headers.get("Last-Modified")
        ctype = (headers.get("Content-Type") or "").lower()
    # 不信任 Accept-Ranges 声明，用真实 Range 请求确认；
    # 顺带在 HEAD 不给长度时把 size 补出来。
    def range_once():
        with _open(final, "GET", "bytes=0-0", timeout=timeout, op=op,
                   main_host=main_host, origin_host=origin_host) as r:
            return r.geturl(), r.headers, r.status

    rheaders = None   # Range 探测被 403/404 等确定性拒绝时不定义，取 CDN 文件名前须判空
    try:
        final, rheaders, status = _attempt(range_once, "Range 探测", tries=tries)
        cr = rheaders.get("Content-Range") or ""
        parts = cr.split("/")
        if cr.startswith("bytes 0-0/") and len(parts) == 2 and parts[1].isdigit():
            if status == 206:
                size, ranged = int(parts[1]), True     # Range 响应的总长比 HEAD 更可信
            else:
                # 伪 206（实测魔搭 resolve：200 + 切片 Content-Length + Content-Range）。
                # 总长必须取 Content-Range 的 TOTAL——此时 Content-Length 是"这一片"
                # 的长度（0-0 就是 1），把它当文件大小会把 7MB 当成 1 字节，之后收满
                # 整个文件也判「不完整」，4 次重试全白费。仍 ranged=False：连状态码
                # 都撒谎的服务器不敢用于多连接分块写（fetch 只认 206），按文档承诺
                # 降级单连接流式（不可续传，但完整性与进度都正确）。
                size, ranged = int(parts[1]), False
                print("  服务器用 HTTP %d 而非 206 响应 Range：总大小取自 Content-Range，"
                      "按不支持续传单连接下载" % status)
        if size is None:
            # HEAD 被拒（405/501）时 GET 探测的响应头是体积的唯一来源——不接住它，
            # "有长度但不支持 Range"的正常链接会被白白退化成流式（会审 P2）
            # v5.4 P2-7：206 但 Content-Range 缺失/total=* 时，响应的 Content-Length
            # 是切片 0-0 的长度（=1）而非总长——补进 size 会把文件当 1 字节、后续
            # 4 次重试全 incomplete，把本可降级单连接流式的服务器判死。保持 None
            # 走流式（fail-closed 方向不变，只是不再冤枉可用的源）。
            if status == 206 and not (cr.startswith("bytes 0-0/")
                                      and len(parts) == 2 and parts[1].isdigit()):
                size = None
            else:
                size = _parse_cl(rheaders)
        if etag is None:
            etag = _norm_etag(rheaders.get("ETag"))
        if lm is None:
            lm = rheaders.get("Last-Modified")
        if not ctype:
            ctype = (rheaders.get("Content-Type") or "").lower()
    except urllib.error.HTTPError as e:
        # ---- 会审 P0：5xx 重试耗尽 ≠ "不支持 Range"。判成不支持会走进单连接分支
        # 把 .part 截断重下，账本若还在就是静默零洞坏文件（评审 A 实测复现链：
        # run1 分块记账 → run2 探测撞持续 5xx 走单连接截断 → run3 旧块号被全盘信任）。
        # 必须失败上抛：账本完好，重跑同一条命令即可续传。
        if _retryable(e):
            raise Fail("network", "Range 探测持续失败（HTTP %d），服务器可能暂时不可用；"
                       "断点已保留，稍后重跑同一条命令即可续传" % e.code)
        if e.code == 404 and headers is None:
            # v5.18 死链快败：HEAD 已回 404（headers 保持 None），Range GET 又 404
            # ——两个方法一致判死，这是真死链（页面/分支不存在），不是"WAF 只拦
            # HEAD"的探测形态。原逻辑吞成 ranged=False 白走单连接，多烧一个请求
            # 才在下载循环里撞 404。上抛交给 download() 的 GitHub 分支回退
            # （_gh_archive_fallback）或 main() 兜底 http_404。403/416 不收：
            # 403 可能只拦 HEAD 不拦 GET（与 HEAD_REJECTED 收 403 同源逻辑），
            # 416 在 Range 语义里有分叉，均维持原降级路径。
            raise
        ranged = False                             # 403/404/416 等确定性拒绝 → 单连接
    except (urllib.error.URLError, OSError) as e:
        raise Fail("network", "Range 探测失败（%s: %s）。网络可能不通，稍后重跑即可"
                   % (type(e).__name__, e))

    # 文件名优先级：Content-Disposition（Range 探测响应 → HEAD 响应）> 最终 URL
    # 路径末段 > download.bin。302 落到对象存储时 URL 末段常是 oid（魔搭实测），
    # 真名只在响应头里；原 URL 的 basename 反而是对的，但 final 已经跳走了。
    name = None
    for h in (rheaders, headers):
        if h is not None:
            name = _cd_filename(h)
            if name:
                break
    name = name or os.path.basename(final.split("?")[0].rstrip("/")) or "download.bin"
    # v3.2：Windows 保留设备名（CON/NUL/COM1… 连带扩展名也算）会让自动取名
    # 产出打不开也删不掉的文件。只管自动取名；用户显式指定的 out 尊重其意图。
    if re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\.|$)", name, re.I):
        name = "_" + name
    # v5.14：Content-Disposition 的 filename* 是 URL 编码的，unquote 会把 %3A
    # 还原成 ':'（%2F 已在 _cd_filename 里挡掉，':' 漏网）；URL 末段同理可能带
    # 非法字符。与 name_from_url 同规则清洗，产出一个 NTFS 上创建必然失败的
    # 名字毫无意义。
    name = _ntfs_safe_name(name)
    return size, ranged, final, name, etag, ctype, lm


def fetch(url, start, end, size=None, op=None, expect_etag=None, main_host=None,
          wgen=None, origin_host=None):
    global _rx_bytes                # v5.13 步进读累加接收字节（见收包循环）
    # P1：与 _open 同语义——用户头铺底、UA"用户赢"、Range 工具赢（解析层已拒
    # Range，这里防御纵深）。
    h = dict(EXTRA_HEADERS)
    if main_host:
        # v5.0 镜像凭据剥离（方案 18.8-F）：凭据只属于主源体系，请求发起时剥离。
        # 刻意不动 _SafeRedirect 的 redirect 链保留逻辑——HF cdn-lfs 主源 302 依赖
        # 保留；镜像请求在发起头里就没有凭据，redirect 链自然无可泄漏。
        # v5.9 P1/P2-1（zcode 审查）：白名单式放行 + host 集合判据，语义与 _open
        # 逐字一致（自定义鉴权头不再外发第三方；同源镜像不误剥）。
        mhs = main_host if isinstance(main_host, (set, frozenset)) else (
            {main_host} if main_host else set())
        tgt = (urllib.parse.urlsplit(url).hostname or "").lower()
        if mhs and tgt not in mhs:
            keep = {"accept", "accept-language", "referer"}
            h = {k: v for k, v in h.items() if k.lower() in keep}
    if origin_host:
        # v5.16 P2-4 收口：与 _open 落点过滤同构——直连 302 落点 URL 的分块请求
        # 不经重定向链，应用落点白名单（Authorization 保留，Cookie/X-* 剥离）。
        tgt = (urllib.parse.urlsplit(url).hostname or "").lower()
        if tgt and tgt != origin_host:
            h = {k: v for k, v in h.items()
                 if k.lower() in _XHOST_KEEP}
    h.setdefault("User-agent", UA)   # 键空间对齐见 _open（v5.4 P2-8）
    h["Range"] = "bytes=%d-%d" % (start, end)
    with _urlopen(urllib.request.Request(url, headers=h), 120, op) as r:
        if r.status != 206:
            # v5.14：裸 IOError 丢状态码，work() 的 FATAL_HTTP 短路只认
            # HTTPError——403/404 也会白等 4 轮。包成 HTTPError（fp=None 时
            # CPython 自动 BytesIO 兜底），状态码进入既有分发链。
            raise urllib.error.HTTPError(url, r.status,
                                         getattr(r, "reason", None)
                                         or "HTTP %s" % r.status,
                                         r.headers, None)
        cr = r.headers.get("Content-Range") or ""
        if not cr.startswith("bytes %d-%d/" % (start, end)):
            raise IOError("服务器返回的区间 %r 与请求的 %d-%d 不符" % (cr, start, end))
        # 评审 B：区间对不代表总长对——Content-Range 的 TOTAL 段也要对上探测值，
        # 否则撒谎服务器能产出 ok=true 的截短文件（无 --sha256 时无告警）。
        total = cr.rsplit("/", 1)[-1] if "/" in cr else ""
        if size is not None and total.isdigit() and int(total) != size:
            raise IOError("服务器声称总长 %s 与探测的 %s 不符（区间 %d-%d）"
                          % (total, size, start, end))
        # P5：单次运行内跨连接复核 ETag。16 条连接各自 fetch，若服务器中途换
        # 内容（CI 覆盖同名 asset），会拼出混合体且长度校验照过——只有 --sha256
        # 能兜住。两边都归一化（弱/强前缀已剥，?download=true 的弱 ETag 与强
        # ETag 实测同值）后双存在且不等才拒收。报错作废而非重试：重试拿到的
        # 还是新内容继续拼，正确链条=重跑让 T3 账本 ETag 判废旧断点全量重下。
        if expect_etag:
            resp_etag = _norm_etag(r.headers.get("ETag"))
            if resp_etag and resp_etag != expect_etag:
                # v4.9：整块按策略拒收即计满块（保守上界——头已判废即断开，body
                # 是否已传输不可知，全部计入丢弃；Fail 直通终止本片，批量跨片
                # 由代际隔离兜住不虚增——v5.4 P2-5）
                _add_waste(end - start + 1, wgen)
                raise Fail("etag_mismatch",
                           "分块响应 ETag 与探测值不符（服务器内容可能已更新）；"
                           "重跑同一条命令即可判废旧断点后全量重下")
        # v5.13 步进读：r.read() 整读在收满前 Python 层零观测点，显示只能等整块
        # 到手（16MB 块 2MB/s 下 8s 一跳、首块完成前 0.0%/0.0 定格，llama.cpp
        # 真机实锤）。改 1MB 步进 + _rx_bytes 累加，show_progress 画接收进度。
        # 副作用：步进读短收不抛 IncompleteRead（http.client 只在整读时 enforce
        # CL），原 except 分支删除——已收部分由下方 len(blob) 短收检查照旧兜底
        # 计 waste（e.partial 与此时的 len(blob) 同值），语义等价。
        buf = []
        try:
            while True:
                b = r.read(1 << 20)
                if not b:
                    break
                buf.append(b)
                with _rx_lock:
                    _rx_bytes += len(b)
        except http.client.IncompleteRead as e:
            # v5.13 复审 P2-1：chunked 截断在步进读下仍抛 IncompleteRead
            # （http.client._safe_read 对已知长度 enforce CL），waste 照旧计费
            # 不容逃逸；CL 短收不抛（下方 len 检查兜底）。已收=buf+本次部分收
            # v5.14：_rx_bytes 收据只扣 buf——partial 从未入账（在 e 里，不在
            # buf 里），一并扣会负数（多扣致「剩余 -1:59」复发）。waste 仍按
            # buf+partial 全额计费。
            p = e.partial
            got = sum(map(len, buf))
            with _rx_lock:
                _rx_bytes -= got
            _add_waste(got + (p if isinstance(p, int) else len(p)), wgen)
            raise
        except BaseException:
            # v5.13：本次 fetch 已收字节全废（连接中断/取消），扣回——重试会重新
            # 收，不扣则 _rx_bytes 重复计数（真机实锤：33MB 文件多次重试后
            # rx>size，显示「剩余 -1:59」）
            with _rx_lock:
                _rx_bytes -= sum(map(len, buf))
            raise
        blob = b"".join(buf)
        buf.clear()   # v5.13 复审 P3-1：释放列表，避免与 join 结果双份滞留到函数尾
    if len(blob) != end - start + 1:
        with _rx_lock:
            _rx_bytes -= len(blob)   # v5.13：短收/超长全废，接收计数扣回
        _add_waste(len(blob), wgen)           # EOF 早断收到部分数据：全废（v4.9）
        if len(blob) > end - start + 1:
            # v5.8 真机实测（cdn.jsdelivr.net/Cloudflare）：探测 0-0 回 206 谎报
            # total（bytes 0-0/55004，真实 147534），区间请求声明 0-55003/55004
            # 却以 chunked 暗渡全量 body——声明与 body 结构性矛盾，重试 4 次每次
            # 拿到同样的矛盾（实测撞死 4 连败）。短收可能是瞬时断流值得重试
            # （走下方 IOError→network 重试）；超长是服务器在谎报，立即判废。
            # 词表 incomplete 已存在（单连接短收在用），零新增；语义对齐
            # etag_mismatch 先例（响应与声明矛盾=作废而非重试，重跑重判）。
            raise Fail("incomplete",
                       "分块 %d-%d 服务器声明 %d 字节却实收 %d 字节（body 超出声明区间），"
                       "Content-Range 不可信；该源分块路径无法下载，重试已跳过"
                       % (start, end, end - start + 1, len(blob)))
        raise IOError("数据不完整：收到 %d / %d 字节" % (len(blob), end - start + 1))
    return blob


def download(a, res):
    """主流程。返回进程退出码；res 被就地填充，供 --json 输出。"""
    t_start = time.time()

    # ---- 会审 P1 error 词表闭合：入口先拦住会以异常类名收场的 URL ----
    u0 = urllib.parse.urlsplit(a.url)
    if u0.scheme not in ("http", "https"):
        # v5.16 P3：错误回显过 _redact_url——userinfo 明文密码进 error_msg
        # 等于绕过全套脱敏（journal/收据面早就脱了，报错面不能漏）
        raise Fail("bad_url", "只支持 http/https 链接（收到 %r）"
                   % _redact_url(a.url)[:80])
    try:
        a.url.encode("ascii")
    except UnicodeEncodeError:
        # urllib 不做 percent-encode：中文路径会在 http.client 的 latin-1 请求行
        # 编码处直接 UnicodeEncodeError（用户群是中文环境，真实会踩）。
        # 入口统一编码。v5.9 P2（zcode 审查）：query/fragment 同样编码——此前
        # 只编 path，中文 query 直接炸 internal。quote 仅对非 ASCII 字节与
        # safe 外字符生效，RFC3986 结构符与既有 %XX（safe 含 %）逐字节保留，
        # SAS 之类纯 ASCII 签名参数不受影响。
        _q_safe = "-._~!$&'()*+,;=:@/?%"
        a.url = urllib.parse.urlunsplit(
            (u0.scheme, u0.netloc, urllib.parse.quote(u0.path, safe="/%"),
             urllib.parse.quote(u0.query, safe=_q_safe) if u0.query else u0.query,
             urllib.parse.quote(u0.fragment, safe=_q_safe) if u0.fragment else u0.fragment))

    # ---- A10 参数校验（含联合上限：fetch 是整块读入内存，所以两者相乘才是峰值）----
    if not 1 <= a.conns <= 64:
        raise Fail("bad_args", "连接数应在 1~64（收到 %d）" % a.conns)
    if not 1 <= a.chunk_mb <= 512:
        raise Fail("bad_args", "分块大小应在 1~512 MB（收到 %d）" % a.chunk_mb)
    if a.conns * a.chunk_mb > 1024:
        raise Fail("bad_args", "连接数 × 分块大小过大（峰值内存约 %.1f GB），请降低其一"
                   % (a.conns * a.chunk_mb / 1024.0))

    # ---- A6 --sha256 入参校验（在下载之前就报错，而不是下完 40 分钟后再删文件）----
    want = None
    if a.sha256:
        token = a.sha256.strip().split()[0] if a.sha256.strip() else ""
        if not re.fullmatch(r"[0-9a-fA-F]{64}", token):
            raise Fail("bad_sha256", "--sha256 需要 64 位十六进制（收到 %d 个字符：%.32s）"
                       % (len(token), token))
        want = token.lower()

    # ---- v4.2 代理决策：首选通道探测失败自动切备用。注意 HTTPError 不切——
    #      能收到 4xx 说明通道是通的（直连 403 是服务器态度问题，换通道没意义）。
    #      JSON 的 warning=proxy_fallback 告知 agent 发生过切换。----
    #      v5.18：404 例外——GitHub 归档 URL 的 404 多数是分支名写错（默认分支
    #      不叫 main），先试分支回退再放弃。回退命中则更新 a.url 后整段重探
    #      （重构的 URL 走同一套探测链）。----
    dl_op, alt_op = _resolve_ops(a)
    _fb_used = False                # 分支回退一次性机会（防改写后再 404 死循环）
    while True:
        try:
            size, ranged, final, name, etag, ctype, lm = head(
                a.url, op=dl_op,
                origin_host=(urllib.parse.urlsplit(a.url).hostname or "").lower())
            break
        except Fail as e:
            if e.code != "network" or alt_op is None:
                raise
            print("  首选通道探测失败（%s），切换备用通道重试" % str(e)[:80])
            dl_op, alt_op = alt_op, None
            _warn(res, "proxy_fallback")        # 循环继续：备用通道重探同一 URL
        except urllib.error.HTTPError as e:
            # 仅 404 且 URL 是 GitHub 归档形态才回退；其余（403/401/405…）照旧
            # 上抛走 main() 兜底。head() 内已由 g3 双 404 快败确保此处收到的是
            # 真死链信号，不吞。
            if e.code != 404 or _fb_used:
                raise
            new_url = _gh_archive_fallback(a.url, dl_op, alt_op, res)
            if new_url is None:
                raise
            a.url = new_url
            _fb_used = True
            dl_op, alt_op = _resolve_ops(a)     # 新 URL 按新 host 重决策通道
    # size is None 表示"体积未知"（chunked 小文件），退化为流式下载；
    # size == 0 是真·空文件，按普通分块路径处理（N1）。
    # v5.16 P2-4 收口：原始 host——落点白名单判据（Range 探测/流式/单连接/
    # 分块 fetch 对 302 落点的直连请求全部要用，提前到探测后统一定义）
    origin_host = (urllib.parse.urlsplit(a.url).hostname or "").lower()

    # ---- v5.0 镜像候选探测（方案 18.2）：并行 head 全部候选，入池门+混用门 ----
    # 判定在任何字节之前完成；探测失败的候选只出局不拦主源下载。
    pool_extra = []
    mirror_specs = getattr(a, "mirror", None)
    if mirror_specs and (size is None or not ranged):
        # 复审 P3-10：ranged=False 主源走流式单连接根本不用池，白探候选浪费超时
        print("  主源%s，将走流式单连接，镜像池不适用，--mirror 忽略"
              % ("体积未知" if size is None else "不支持 Range 分块"))
    elif mirror_specs:
        murls = _load_mirror_urls(mirror_specs)
        seen, cands = {ident_of(a.url)}, []
        for u in murls:
            ui = ident_of(u)
            if ui not in seen:
                seen.add(ui)
                cands.append(u)
        if cands:
            print("  --mirror：并行探测 %d 个镜像候选（每候选单请求 %d 秒超时）…"
                  % (len(cands), MIRROR_TIMEOUT))
            # v5.9 P2-1（zcode 审查）：跨 host 判据升级为 {原始, 落点} 集合——
            # 302 后"主源体系"含两台 host；同源镜像（host==原始）不再被落点
            # 单值判据误剥自家凭据 401 出局。
            main_hosts = {(urllib.parse.urlsplit(u).hostname or "").lower()
                          for u in (a.url, final)} - {""}
            pool_extra = _probe_mirrors(cands, a, size, etag, want,
                                        main_host=main_hosts)
        else:
            print("  --mirror：候选与主源相同，无镜像入池")

    # ---- C5 防盗链 / 门户劫持会把 HTML 错误页当文件返回 ----
    # 只警告不改退出码：合法 HTML 数据集也会命中，硬失败会误伤。
    # 但要把它做成**机器可读**的，否则 agent 只看 ok 字段照样上当。
    if ctype.startswith("text/html") and not want:
        _warn(res, "html_response")
        print("警告：服务器返回的是网页（Content-Type: %s），可能并不是你要下载的文件；"
              "结果里的 warning 字段为 html_response" % ctype)

    # ---- v5.2 --as：显式指定落盘文件名（dogfood 反馈③）----
    # basename 剥离防路径注入；Windows 保留名与自动取名同款防护。与显式输出
    # 文件路径互斥的检查已前置到 main()（复审 P2：不付网费）；这里只做名称
    # 清洗。配目录或省略输出路径时生效，覆盖 URL/响应头推导的自动名。
    if getattr(a, "as_name", None):
        nm = os.path.basename(a.as_name.replace("\\", "/")).strip()
        # v5.7 复审 P2：<>:"|?* 是 NTFS 非法字符（冒号还是 ADS 流分隔符，
        # "a:b" 会静默写进 a 的 b 流而不是新文件）；结尾点/空格会被 NTFS
        # 静默剥掉导致实际落盘名与 --as 不符。与 _safe_relpath 同款防护。
        nm = re.sub(r'[<>:"|?*]', "_", nm).rstrip(" .")
        if not nm:
            raise Fail("bad_args", "--as 不能是空串或纯路径分隔符")
        # v5.5 复审 A-P2：basename("..")==".." 原样过校验，abspath 解析成上级
        # 目录后走 isdir 分支拼出 open 必炸的路径（io 报错误导排查）。"--as .."
        # 是 v5.2 起的既有洞，在本轮一并堵上。
        if nm in (".", ".."):
            raise Fail("bad_args", "--as 不能是 . 或 ..（目录别名不是文件名）")
        if re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\.|$)", nm, re.I):
            nm = "_" + nm
        name = nm

    out = os.path.abspath(a.out) if a.out else os.path.abspath(name)
    if a.out and a.out[-1] in "\\/":
        # ---- 会审 P2：以分隔符结尾 = 明确的目录意图；不处理会把 "newdir\"
        # 当成同名文件静默创建 ----
        os.makedirs(out, exist_ok=True)
    if os.path.isdir(out):
        out = os.path.join(out, name)

    # ---- v5.2 裸文件防假完成（dogfood 反馈②）----
    # out 已存在且无本工具断点痕迹（.part/.part.done 都不在）= 裸文件。有
    # --sha256 时走 A11 校验本就安全（内容即正义），不动；无校验时盲信"同大小
    # 即完成"会被异源同名文件骗过（agent 批量拉 README.md 实锤）。身份证据=
    # journal：有同源成功记录 → A11 秒退契约保持；查无记录 → 冲突改名另存，
    # 旧文件一字不动。有 .part 痕迹的续传路径走 T3/fp 校验（安全），一行不碰。
    # 批量（--manifest/--repo/多 URL）不参与：清单 rel 布局唯一，已完成分片
    # 秒过是批量契约，journal 截断后误改名会破布局并整片重下（复审 P1）。
    if (want is None and size is not None
            and not getattr(a, "_batch", False)
            and os.path.exists(out) and os.path.getsize(out) == size
            and not os.path.exists(out + ".part")
            and not os.path.exists(out + ".part.done")
            and not journal_same_source(out, a.url)):
        new_out = conflict_rename(out)
        print("目标已存在同名文件且无法证明是同一来源（journal 无同源记录），"
              "改名另存：%s" % new_out)
        out = new_out

    # ---- P2 失败也让 agent 拿到路径：排查 .part / .bad 时不需要重新探测 ----
    res["path"] = out

    # ---- v4.6 双开互斥：必须在 A11 秒退的 sweep 之前拿锁，否则第二个实例的
    #      sweep 会删掉第一个实例正在写的 .part。秒退路径锁随 return 自动释放 ----
    lock_fh = _acquire_part_lock(out)

    def _sweep_residue():
        # v3.2：A11 秒退=下载已完成，同目录上次中断留下的 .part/.part.done 顺手
        # 清掉，别让 40GB 的死断点永远躺在 --clean 列表里。并发实例正在写同一
        # .part 的场景由 _acquire_part_lock 真锁覆盖（v4.6，先于这里执行）。
        for suf in (".part", ".part.done", ".part.lock"):
            try:
                if os.path.exists(out + suf):
                    os.remove(out + suf)
            except OSError:
                pass

    # ---- A11 已存在文件 ----
    # size 不为 None：同大小即视为完成（传了 --sha256 则必须真校验，不许静默成功）。
    # **0 字节文件走这里**：size==0 且本地也是 0 字节 → 完成（N1：旧代码把它折进
    # "未知体积"，流式分支 got<=0 → 永远 incomplete）。
    # size 未知（None）：只有 --sha256 才能判断，否则无法证明它完整 → 重新下载
    if os.path.exists(out) and (size is None or os.path.getsize(out) == size):
        if want:
            digest = sha256_of(out)
            if digest != want:
                # ---- 会审 P2 与 T4 同原则：校验失败的证据保留为 .bad，不一键删除 ----
                bad = out + ".bad"
                n = 1
                while os.path.exists(bad):
                    bad = "%s.%d" % (out + ".bad", n)
                    n += 1
                try:
                    _atomic_replace(out, bad)
                    print("已存在的文件校验不通过，已保留为 %s，重新下载：%s" % (bad, out))
                except OSError:
                    _remove_quiet(out)
                    print("已存在的文件校验不通过（改名失败且旧文件删除被占用，"
                          "将覆盖重下），重新下载：%s" % out)
            else:
                print("文件已存在且校验通过：%s" % out)
                _release_part_lock(lock_fh)
                _sweep_residue()
                res.update(ok=True, path=out, bytes=os.path.getsize(out),
                           seconds=time.time() - t_start, sha256=digest, verified=True)
                return 0
        elif size is not None:
            # 刻意**不**读全文件：对几十 GB 的产物，重跑一次只为打印一个哈希要等
            # 好几分钟，而"重跑同一条命令又快又安全"正是 agent 恢复策略的支点。
            # 这里如实说不校验，而不是静默宣称成功。
            print("文件已存在且大小一致：%s" % out)
            print("未经校验。源站若提供了校验值，请加 --sha256 复核。")
            _release_part_lock(lock_fh)
            _sweep_residue()
            res.update(ok=True, path=out, bytes=size, seconds=time.time() - t_start,
                       sha256=None, verified=None)   # v5.11 三态：未提供期望=null（无从校验），非"校验失败"
            return 0
        # size 未知又没给 --sha256：无法证明已存在的文件完整，继续往下重新下载

    # ---- A15a 目录预检（磁盘余量检查挪到账本读取之后，见 A15b）----
    parent = os.path.dirname(out) or "."
    if not os.path.isdir(parent):
        raise Fail("bad_path", "保存位置的上级目录不存在：%s" % parent)

    part, donef = out + ".part", out + ".part.done"
    # fp 是账本主身份（v5.0 方案 18.3）：url 字段退出账本，镜像池下同一内容
    # 多个 URL，内容指纹才是跨源身份。urls 记录池成员供人读与续传溯源。
    # v5.3 调用点守卫：size=None（chunked 流式，GitHub codeload 归档全是此形态）
    # 时 fp 无意义——流式路径没有账本，fp 的全部消费者（L1370 续传比对、写账本）
    # 都在分块路径内。无守卫时 "%d|%s" 直接 TypeError → internal，v4.9 能下的
    # 流式路径被 v5.0 的 fp 引入回归（真链路实锤：Motrix master.tar.gz）。
    fp = fp_of(size, etag, a.url) if size is not None else None
    # digest 是**两条下载路径的统一出口**：流式路径边收边算后就地赋值，
    # 分块路径保持 None，由收尾统一 sha256_of(part) 补上。别改成无条件重算。
    digest = None
    chunk = a.chunk_mb << 20
    # ---- 小文件自动缩块：10.9MB 的分片在 16MB 默认块下只切出 1 个任务，
    #      16 连接退化成单连接。块数 < 连接数时缩到 size/conns（下限 1MB），
    #      让小文件也吃上多连接提速。用户显式给 --chunk-mb 时尊重其选择不缩。 ----
    if (size is not None and 0 < size < chunk * a.conns
            and not any(arg.startswith("--chunk-mb") for arg in sys.argv)):
        chunk = max(1 << 20, -(-size // a.conns))
        print("小文件：分块自动缩为 %d MB，%d 连接并发生效" % (chunk >> 20, a.conns))
    # ---- N1 size 三分：None=未知（流式）；0=空文件 → 任务表为空但仍是"分块路径"，
    #      收尾会拿到一个 0 字节的 .part 并直接通过。旧代码 size<=0 一律进流式分支。----
    tasks = [] if size is None else [(i, s, min(s + chunk - 1, size - 1))
                                     for i, s in enumerate(range(0, size, chunk))]
    need = set(t[0] for t in tasks)

    # ---- C4 并发提示（v4.6 更新）：真双开已被 _acquire_part_lock 拦死（正常情况
    #      下能走到这里说明锁已到手；锁文件创建降级的无锁模式在获取时已另行警告）。
    #      此提示只服务剩余场景：上轮刚结束/被强杀不到 10 分钟，提醒 agent 断点
    #      会自动接上。 ----
    if os.path.exists(donef) and time.time() - os.path.getmtime(donef) < 600:
        print("提示：进度文件 %.0f 秒前刚被写过（上一轮刚结束；若是被强杀的，本次自动续传）"
              % (time.time() - os.path.getmtime(donef)))

    # ---- A1 账本读取：带身份且兼容旧格式，任一项不符即整体作废 ----
    have = set()
    hist = []    # v5.11 速度采样窗 [[ts,已收字节],...]：随断点继承，write_ledger 维护
    if size is not None and os.path.exists(donef):
        ledger_ok = False
        try:
            with open(donef, encoding="utf-8") as f:
                m = json.load(f)
            stored = m.get("etag") if isinstance(m, dict) else None
            stored_lm = m.get("lm") if isinstance(m, dict) else None
            # T3：**只有两边都给了 ETag 且不同**才判失效（这是方案 §1 T3 的原意）。
            # 旧账本没有 etag 字段时不该无谓地把进度整份丢掉。
            if (isinstance(m, dict)                       # 旧格式是裸 list，会走到 else
                    and m.get("v", 1) == 1                # v4.9：无 v 视为 v1（不废旧账本）；v!=1 是不认识的格式
                    and m.get("fp") == fp                 # v5.0 内容指纹（方案 18.3）；v4.9 旧账本无 fp 判废一次（诚实升级代价：url 字段已退出账本）
                    and m.get("size") == size
                    and m.get("chunk") == chunk
                    # ETag 不同 = 同一 URL 下内容被换过（镜像站重新打包、CI 覆盖同名
                    # release asset）。只比 url/size 会静默拼接出新旧混合的坏文件，
                    # 而且长度校验照样通过——这是 v1 最后一个已知静默损坏路径。
                    and not (stored is not None and etag is not None and stored != etag)
                    # lm 同构判定（v4.9 第三身份因子）：双存在且不等才判废，单侧缺失不判
                    and not (stored_lm is not None and lm is not None and stored_lm != lm)
                    # 数据文件不在时账本一律不可信：账本记录的是 .part 里的字节，
                    # 文件没了那些字节就没了。这半个守卫本身就是「.part 被删却交付
                    # 全零文件」的修复——去掉它，该 bug 会原样回来（已用变异实验证实）。
                    # v5.4 P2-4：还得查大小——.part 被外部截断（同步盘/复制工具）
                    # 后守卫只查存在会通过，A3 的 truncate(size) 把截掉部分补成
                    # 零洞，交付静默坏文件还打印 SHA256。A3 保证正常续传时 .part
                    # 恒为 size，此检查不误伤。
                    and os.path.exists(part)
                    and os.path.getsize(part) == size):
                # parts 若被外部改成 null/数字等非 list，set()/迭代会 TypeError
                # 逃出下方 except (OSError, ValueError) 直接炸 internal——加守卫：
                # 非 list 或含非 int 元素（不可哈希会让 set() 炸 TypeError，可哈希
                # 垃圾值也非块号语义）一律不置 ledger_ok，走"保存的进度与当前任务
                # 不匹配"判废
                parts = m.get("parts")
                if isinstance(parts, list) and all(isinstance(x, int) for x in parts):
                    have = set(parts) & need
                    ledger_ok = True
                    # v5.11：采样窗随断点继承（旧账本无 hist/结构不对则空，
                    # --status 侧诚实 null）
                    h = m.get("hist")
                    if (isinstance(h, list)
                            and all(isinstance(x, (list, tuple)) and len(x) == 2
                                    and isinstance(x[0], (int, float)) and x[0] > 0
                                    and isinstance(x[1], (int, float)) and x[1] >= 0
                                    for x in h)):
                        hist = [list(x) for x in h[-16:]]   # 复审 C P3：继承也截尾 16
                    # 复审 B 严重项：任一侧 ETag 缺失时，断点身份只剩 url+size+chunk
                    # 三元组——镜像站同尺寸换内容会静默拼接新旧字节且长度校验照过。
                    # 不判废（很多静态源不给 ETag，废掉等于废掉续传），但必须说出来。
                    if not stored or not etag:
                        print("警告：此源未提供 ETag，续传无法确证断点字节属于当前内容；"
                              "如需强保证请配 --sha256")
        except (OSError, ValueError):
            ledger_ok = False
        # ---- A5 丢弃进度必须说出来，否则用户/agent 会读成"程序有 bug" ----
        if not ledger_ok:
            print("保存的进度与当前任务不匹配（链接/大小/分块/ETag/Last-Modified 已变、"
                  "账本版本不受支持或数据文件缺失），已丢弃，从头下载。"
                  "注意：改变 -c/--chunk-mb 会使分块边界变化，旧断点无法对齐，也会走到这里")

    todo = [t for t in tasks if t[0] not in have]

    # ---- A15b 磁盘余量：只按**还要写的量**计费（会审 P1，中立评审独家）----
    # .part 早已占着盘，再要求一整份 free 是重复计费——"40GB + 紧盘 + 续传"这个
    # 最核心的场景会被永久堵死。单连接路径会截断 .part 从头下（P0），已落盘量不计入。
    # v5.16 P2-1（双 agent 审查）：计费基准从「有效账本块」改「.part 物理实占」。
    # 旧式按 have 数块，账本判废（ETag/内容已变）后 have=空 → 按全量计费，但
    # .part 仍预分配占着 size 的盘——原地覆写完全放得下却假报 disk。物理增长
    # 模型天然统一三态：全新（实占 0 计全量）、有效续传（实占 size 计 0）、
    # 判废/截断重下（.part 恒为 size，A3 truncate 后覆写不增长，计 0）。
    if size is not None:
        part_now = os.path.getsize(part) if os.path.exists(part) else 0
        bill = max(0, size - part_now)
        free = shutil.disk_usage(parent).free
        if bill > free:
            raise Fail("disk", "磁盘空间不足：还需 %.2f GB，可用 %.2f GB（%s）"
                       % (bill / 1e9, free / 1e9, parent))

    if size is None:
        # ---- T1 体积未知（chunked、无 Content-Length、Range 也被无视）----
        # 实测 hf-mirror 上的**非 LFS 文件**就是这个形态，而一个模型有 9 个
        # （config.json / tokenizer.json 7MB / vocab.json / merges.txt …）。
        # v1 对未知体积直接 Fail("no_size") 拒下载，agent 下一个模型就卡在这。
        # 只能单连接流式：不可续传、无法预检磁盘、无法比对大小 —— 但可以边收边算
        # sha256，所以 --sha256 依然能校验。
        # 这里**不发 Range**（直接全量 GET），所以不存在"服务器无视 Range 却静默
        # 从头覆盖"的问题。
        if os.path.exists(donef):
            _void_ledger(donef)    # 这种形态下账本没有意义；下游截断重写，残留即零洞
        print("服务器未告知文件大小（chunked），退化为流式下载：单连接、中断后需从头重下")
        for attempt in range(PROBE_TRIES):
            h, got = hashlib.sha256(), 0
            try:
                with _open(final, timeout=STREAM_TIMEOUT, op=dl_op,
                           origin_host=origin_host) as r, open(part, "wb") as f:
                    # P5：流式同样复核 ETag——整文件一次响应虽然不会拼出混合体，
                    # 但探测后内容被换时 sha256 未传就静默收下新内容，agent 无感知
                    rcl = _parse_cl(r.headers)   # v5.4 P2-6：GET 响应自己的 CL（探测无 CL ≠ GET 也没有）
                    if etag:
                        resp_etag = _norm_etag(r.headers.get("ETag"))
                        if resp_etag and resp_etag != etag:
                            raise Fail("etag_mismatch",
                                       "响应 ETag 与探测值不符（服务器内容可能已更新）；"
                                       "重跑同一条命令即可")
                    while True:
                        blob = r.read(1 << 20)
                        if not blob:
                            break
                        f.write(blob)
                        h.update(blob)
                        got += len(blob)
                        # v5.15：管道下整行节流（SHA256 助手同款），tail -f 可跟踪
                        _progress_print("  已下载 %.1f MB  %.1f MB/s"
                                        % (got / 1e6, got / 1e6 / max(time.time() - t_start, 1)))
                _progress_end()
                # ---- v5.4 P2-6：带 CL 的响应提前 FIN，r.read 对短收不抛
                # IncompleteRead（只在整读时 enforce CL）——不校验会产出截短文件且
                # ok=true（收尾 size=got 让大小校验变恒等式）。与单连接路径同款：
                # 短收触发从头重试，耗尽才判 incomplete；chunked 无 CL 不校验。
                if rcl is not None and got != rcl:
                    if attempt == PROBE_TRIES - 1:
                        raise Fail("incomplete",
                                   "服务器提前断开：只收到 %d / %d 字节。该路径不支持"
                                   "断点续传，重跑会从头重下" % (got, rcl))
                    print("\n  数据不完整（%d / %d 字节），%.0f 秒后从头重试（第 %d/%d 次）"
                          % (got, rcl, 2 ** attempt, attempt + 2, PROBE_TRIES))
                    time.sleep(2 ** attempt)
                else:
                    break
            except Fail:
                raise
            except urllib.error.HTTPError as e:
                # ---- N3 瞬时 HTTP 错误（408/429/5xx）与传输错误同等重试 ----
                # v2 只给传输层异常加了重试，HTTPError 被一刀切 raise：一次 503 就整段
                # 作废。而分块路径的 work() 对同样的 503 是重试的——同一故障两种待遇。
                if e.code in FATAL_HTTP or attempt == PROBE_TRIES - 1:
                    raise
                wait = max(2 ** attempt, _retry_after_sec(e))   # 429/503 服从 Retry-After
                print("\n  流式下载被拒（HTTP %d），%.0f 秒后从头重试（第 %d/%d 次）"
                      % (e.code, wait, attempt + 2, PROBE_TRIES))
                time.sleep(wait)
            except Exception as e:
                if _is_disk_error(e):
                    raise Fail("disk", "写盘失败（磁盘空间不足？）：%s（已收 %.1f MB）"
                               % (e, got / 1e6))
                if attempt == PROBE_TRIES - 1:
                    raise Fail("network", "流式下载重试 %d 次仍失败：%s: %s"
                               % (PROBE_TRIES, type(e).__name__, e))
                # 这条路径是一条 3~12 秒的一次性长连接，中途一次 reset 就整段作废，
                # 所以重试只能从头来 —— 但没有重试就完全是"看运气"。
                print("\n  流式下载中断（%s: %s），%.0f 秒后从头重试（第 %d/%d 次）"
                      % (type(e).__name__, str(e)[:50], 2 ** attempt, attempt + 2, PROBE_TRIES))
                time.sleep(2 ** attempt)
        if got <= 0:
            raise Fail("incomplete", "服务器没有返回任何数据")
        digest = h.hexdigest()
        size = got                                 # 事后才知道的大小，交给统一的收尾
    elif not ranged:
        # ---- A14 单连接回退：原来这条路径完全没有异常处理，断流会被当成正常结束 ----
        if os.path.exists(donef):
            # ---- 会审 P0：这条路径会 open(part,"wb") 截断数据文件，账本必须一并
            # 作废（与流式分支对称）。否则账本声称的旧块号会在下一次重跑被全盘
            # 信任，而被截断的 .part 补出来的是零洞——静默坏文件。
            # 二轮复审 P3：serve 只读句柄占用时 _void_ledger 清空内容代替删除，
            # 作废必须实际生效（不能用 _remove_quiet 吞掉，理由见 _void_ledger）。
            _void_ledger(donef)
        print("该链接不支持断点续传，退回单连接下载……")
        print("（该模式下中断后需要从头重下）")
        done = 0
        # ---- N2 补退避重试（与流式/分块路径同待遇）：v2 恰好漏了这条路径 ----
        # 注意：urllib 分块 read(1MB) 对"带 CL 短读后正常关连接"不抛 IncompleteRead
        # （实测它只在整读 r.read() 时 enforce CL），短读会被当正常 EOF 走出循环。
        # 所以 done != size 必须在循环内判定才能让短读也触发重试。
        for attempt in range(PROBE_TRIES):
            done = 0
            try:
                # P1：v5.16 起直接走 _open（用户头铺底、UA"用户赢"、落点白名单
                # 同构生效）——原裸 Request + EXTRA_HEADERS 绕开了落点剥离，
                # X-Api-Key 原样直达 302 落点（r5 测试实测的泄漏路径之一）
                with _open(final, timeout=120, op=dl_op, origin_host=origin_host) as r, \
                        open(part, "wb") as f:
                    # P5：与流式分支同款 ETag 复核
                    if etag:
                        resp_etag = _norm_etag(r.headers.get("ETag"))
                        if resp_etag and resp_etag != etag:
                            raise Fail("etag_mismatch",
                                       "响应 ETag 与探测值不符（服务器内容可能已更新）；"
                                       "重跑同一条命令即可")
                    while True:
                        blob = r.read(1 << 20)
                        if not blob:
                            break
                        f.write(blob)
                        done += len(blob)
                        el = time.time() - t_start
                        # v5.15：管道下整行节流，非 tty 不再糊一条巨线
                        _progress_print("  %.1f%%  %.1f MB/s  已用 %02d:%02d   "
                                        % (100 * done / size, done / 1e6 / max(el, 1),
                                           int(el) // 60, int(el) % 60))
                _progress_end()
                if done == size:
                    break
                if attempt == PROBE_TRIES - 1:
                    raise Fail("incomplete", "服务器提前断开：只收到 %d / %d 字节。"
                               "该链接不支持断点续传，重跑无法从断点恢复，会从头重下"
                               % (done, size))
                print("\n  数据不完整（%d / %d 字节），%.0f 秒后重试（第 %d/%d 次）"
                      % (done, size, 2 ** attempt, attempt + 2, PROBE_TRIES))
                time.sleep(2 ** attempt)
            except urllib.error.HTTPError as e:
                if e.code in FATAL_HTTP or attempt == PROBE_TRIES - 1:
                    raise
                wait = max(2 ** attempt, _retry_after_sec(e))   # 429/503 服从 Retry-After
                print("\n  单连接下载被拒（HTTP %d），%.0f 秒后重试（第 %d/%d 次）"
                      % (e.code, wait, attempt + 2, PROBE_TRIES))
                time.sleep(wait)
            except Fail:
                raise
            except Exception as e:
                # v5.14：对齐流式分支（1675-1678 区）——磁盘满在这里白等重试 3
                # 轮后误报 network，批量「磁盘满跳过剩余分片」只认 disk 码，
                # 保护失效。用本分支变量 done（非 got）。
                if _is_disk_error(e):
                    raise Fail("disk", "写盘失败（磁盘空间不足？）：%s（已收 %.1f MB）"
                               % (e, done / 1e6))
                if attempt == PROBE_TRIES - 1:
                    raise Fail("network", "单连接下载中断：%s（该链接不支持续传，需从头重下）" % e)
                print("\n  单连接下载中断（%s: %s），%.0f 秒后重试（第 %d/%d 次）"
                      % (type(e).__name__, str(e)[:50], 2 ** attempt, attempt + 2, PROBE_TRIES))
                time.sleep(2 ** attempt)
    else:
        print("文件大小 %.2f GB，分 %d 块，已完成 %d 块，%d 连接并发下载剩余 %d 块"
              % (size / 1e9, len(tasks), len(have), a.conns, len(todo)))
        # ---- A3 .part 无条件重建到正确大小（远端变小后不再永久失败）----
        with open(part, "r+b" if os.path.exists(part) else "wb") as f:
            f.truncate(size)

        ok_lock = threading.Lock()
        stop = threading.Event()
        # v4.9 复审 P2：waste 计数每片归零——阈值按单片 size 计，批量模式下前片
        # 泄漏进后片会误杀（前片攒 63MB，后片首败即熔断）
        # v5.14：归零与换代收进同一把锁（与 _add_waste 判等配对），滞留 worker
        # 不可能把旧片 waste 记到已换代的新片头上。
        with _WASTE_LOCK:
            _WASTE_BYTES[0] = 0
            _WASTE_GEN[0] += 1
            wgen = _WASTE_GEN[0]    # 本片代际：fetch/dup 丢弃路径带上，滞留 worker 的
                                    # 旧片 fetch 失败计不到本片头上（v5.4 P2-5）
        # ---- v5.0 多源池（方案 18.4）：每源独立 AIMD 闸门 ----
        # 份额=总量按源均分（余数给主源），只在池构建时算一次：池成员整个 run
        # 固定，逐块重算会造成快慢源边界乒乓（18.8 防抖裁决，对应迅雷
        # FlowRegulationGroup 的 min_volatility/diff_suppress 配置族）。
        # 单源时 share+extra == 旧 gate 值，行为逐字节一致。
        # v5.9 P2-1：{原始, 落点} 集合判据，与镜像探测处同款（1300 行区）
        main_hosts = {(urllib.parse.urlsplit(u).hostname or "").lower()
                      for u in (a.url, final)} - {""}
        eff = max(1, min(a.conns, len(todo) or 1))
        n_src = 1 + len(pool_extra)
        share = max(1, eff // n_src)
        extra = eff - share * n_src          # 余数给主源
        pool = [{"url": final, "ident": ident_of(a.url), "host": "主源",
                 "etag": etag, "gate": Throttle(share + extra, stop),
                 "op": dl_op, "alt": alt_op,
                 "bytes": 0, "fails": 0, "ewma": None}]
        for m in pool_extra:
            pool.append({"url": m["final"] or m["url"], "ident": ident_of(m["url"]),
                         "host": m["host"], "etag": m.get("etag"),
                         "gate": Throttle(share, stop),
                         "op": m["op"], "alt": m["alt"],
                         "bytes": 0, "fails": 0, "ewma": None})
        if len(pool) > 1:
            print("  多源池 %d 个源，每源并发份额 %d（余数给主源）" % (len(pool), share))
        # v5.4 P1-1：多/单源形态在建池时固化——剔除使 pool 只缩不扩，若用实时
        # len(pool)==1 做分界，"镜像被剔光"会误走单源路径 fetch(final)（主源
        # URL），把已死的源 URL 发给唯一存活的镜像分身，套件实锤二次 403 炸全场
        multi = len(pool) > 1
        # 账本 urls=池成员列表（ident 形态：去 query/fragment，同 ident_of 语义防
        # SAS 过期。单源=[本源]；镜像池=全部入池 URL 供人读与溯源）
        pool_urls = [p["ident"] for p in pool]
        state = {"done": sum(e - s + 1 for i, s, e in tasks if i in have), "ok": set(have)}
        t0 = time.time()
        # v5.11：断档作废——上次账本写入距今超 120s（隔夜续传/长停顿）时旧窗口
        # 不再代表当前速度，丢掉重起窗（write_ledger 会以 [t0, base_done] 播种）
        if hist and t0 - hist[-1][0] > 120:
            hist = []

        def write_ledger():
            # ---- A2 原子写：tmp + fsync + replace，任何时刻被杀都不会留下半截账本 ----
            # "v" 是格式版本位（v4.9，对标迅雷 cfg 的 Magic+Version）：--serve 跨进程
            # 读账本后 .part.done 已是两进程契约，将来加字段/改语义时旧程序能靠它
            # 识别"不认识的账本"而不是误信。读取侧 get("v", 1) 兼容无 v 旧账本。
            # v5.0（方案 18.3）：url 字段退出，fp=内容指纹主身份；urls=池成员列表
            # （单源就是 [本源]，镜像池下写入全部入池 URL 供人读与溯源）。旧 v4.9
            # 程序读新账本拿 url=None 判废重下（安全方向不变）；serve 只查
            # v/chunk/size/etag/parts，不碰 fp/urls——旧 serve 继续能服务新账本
            # （这正是"不升 v:2"的承重理由，zcode 核验）。
            tmp = donef + ".tmp"
            # v5.11 采样窗：hist=[时间,已收字节] 最近 16 次写入，--status 据此算
            # 实测速度/ETA（消费端 do_status）。起点播种 [t0, base_done]——续传免费
            # 继承的字节算在起点里，不虚高速度（与主循环显示速度同哲学）。
            if not hist:
                hist.append([t0, base_done])
            hist.append([time.time(),
                         sum(min(chunk, size - i * chunk) for i in state["ok"])])
            del hist[:-16]
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"v": 1, "fp": fp, "urls": pool_urls, "size": size,
                           "chunk": chunk, "etag": etag, "lm": lm,
                           "parts": sorted(state["ok"]), "hist": hist}, f)
                f.flush()
                os.fsync(f.fileno())
            _atomic_replace(tmp, donef)

        def show_progress():
            # v5.0 起进度显示在 worker（拉取队列下 worker 与块不一一对应，
            # "每完成一块刷一行"的时点只存在于 worker 里）；v5.12 提取成闭包，
            # 主循环空闲节流（cf.wait 超时）复用同一画法。v5.13 百分比/速度/ETA
            # 改画「接收进度」：rx = base_done + _rx_bytes（fetch 步进读累加，
            # 含未完成块的已收部分；账本 done 只有块完成粒度，16MB 块 2MB/s 下
            # 8s 一跳、首块前 0.0% 定格，llama.cpp 真机实锤）——aria2 同款语义。
            # done/_rx_bytes 读不加锁：仅供显示，弱一致可接受。_osc=真终端时随
            # 帧发 OSC 9;4（ConEmu 进度序列，Windows Terminal/VS Code 1.97+ 渲
            # 染成标签页进度环+任务栏进度；agent 管道 isatty=False 恒不发，零干扰）。
            dt = max(time.time() - t0, 1)
            # min 钳制：is_dup/stop 就地丢弃的字节已计入 _rx_bytes 且无扣回点
            #（fetch 成功后 worker 层丢弃），显示封顶防越界；真实接收会追平虚高
            rx = min(base_done + _rx_bytes, size)
            # 速度只按本次运行实收字节算（续传免费继承的 base_done 不计入，同
            # v5.12 语义）
            speed = _rx_bytes / 1e6 / dt
            # v5.12 复审 P3：主循环空闲帧可达 speed=0（刚起步无一字节到手），剩余
            # 显示 --:-- 而不是误导性的 00:00
            if speed > 0:
                remain = (size - rx) / 1e6 / speed
                rem_s = "剩余 %02d:%02d" % (int(remain) // 60, int(remain) % 60)
            else:
                rem_s = "剩余 --:--"
            if _osc:
                sys.stderr.write("\x1b]9;4;1;%d\x07"
                                 % min(100, int(100 * rx / size)))
            # v5.15：帧内容交 _progress_print 分流——真终端 \r 覆盖刷新（含 OSC
            # 序列先行的原语义，OSC 非 tty 恒不发）；管道下整行节流（5s 一帧，
            # 100% 必出），| tee / 2> 日志 / agent 捕获可实时跟踪不再糊线
            _progress_print("  %5.1f%%  %6.1f MB/s  已用 %02d:%02d  %s   "
                            % (min(100.0, 100 * rx / size), speed,
                               int(dt) // 60, int(dt) % 60, rem_s))

        def waste_check(i):
            # ---- v4.9 浪费熔断：每次失败 attempt 的重试前检查，超限即任务级失败 ----
            # 复用"第一失败 → stop.set() → 其余 worker A9 提前放弃"的既有收敛通道，
            # 零新增中断机制；attempt==3 的最终 raise 之前不拦（让原有的
            # network 错误信息优先出来，浪费此时已封顶不再增长）。
            over, wasted, limit = _waste_over(size)
            if over:
                raise Fail("waste",
                           "浪费熔断：失败重试已丢弃 %.0f MB 流量（阈值 %.0f MB），"
                           "源站疑似不稳定；断点已保留，更换源或网络后重跑即可续传"
                           % (wasted / 1048576.0, limit / 1048576.0))

        def pick_source(exclude=None):
            # 负载最少源优先（快源先空闲先拿块——速度即调度器，18.4 明确不做源
            # 亲和）；同分按池序（主源在前）。exclude=刚失败的源（重试自动换源，
            # "换源是队列送的"，不造显式切换逻辑）。inflight 是启发式，读不加锁。
            # v5.4 复审 P2：池可能在本 worker 判空之后、这里执行之前被并发剔空
            # ——min([]) 会抛裸 ValueError 泄漏词表且丢失原异常。返回 None 由
            # 调用方按各自上下文上抛（except 内 bare raise 重抛原异常；领块处
            # 收敛 cancelled）。
            cands = [p for p in pool if p is not exclude]
            if not cands:
                cands = pool
            return (min(cands, key=lambda p: p["gate"].inflight) if cands else None)

        def work(t, is_dup=False):
            i, s, e = t
            src = pick_source()
            if src is None:
                # 领块与选源之间池被并发剔空：主循环即将失败终止，这里诚实收敛
                raise Fail("cancelled", "所有源已被剔除（永久错误）")
            for attempt in range(4):
                # A9 检查点：让退避重试提前放弃
                if stop.is_set():
                    raise Fail("cancelled", "已取消（已有其他分块失败，提前放弃重试）")
                wait = 2 ** attempt
                try:
                    with src["gate"]:                   # 每源独立自适应闸门（限流时收缩）
                        # 块计时起点放在闸门**获取后**：排队耗时计入块耗时会让
                        # EWMA/中位数虚高（限流时排队越久越难看），遥测失真
                        blk_t0 = time.time()
                        if not multi:
                            # 单源路径（与 v4.9 逐字节一致）：0-1 主通道，2-3 备用通道
                            blob = fetch(final, s, e, size=size, expect_etag=etag,
                                         op=(alt_op if (attempt >= 2 and alt_op is not None)
                                             else dl_op), wgen=wgen,
                                         origin_host=origin_host)
                        else:
                            # 多源：目标 URL 由认领时的源决定（重试即换源）；镜像
                            # 请求发起时按目标 host 剥凭据（18.8-F）；通道用源
                            # 自己的（v5.1：镜像继承主源通道会把国内 CDN 绕上
                            # 代理线——单源路径不受影响，仍走 dl_op/alt_op）；
                            # ETag 校验用所属源自己的（混用门放行的异 ETag 镜像
                            # 拿主源 ETag 校验必撞 etag_mismatch，v5.0 实测）
                            blob = fetch(src["url"], s, e, size=size,
                                         expect_etag=src["etag"],
                                         op=(src["alt"] if (attempt >= 2
                                            and src["alt"] is not None)
                                            else src["op"]),
                                         main_host=main_hosts, wgen=wgen,
                                         origin_host=origin_host)
                    src["gate"].reward()                # 一块成功：缓慢放回名额
                    dt_blk = time.time() - blk_t0
                    # bytes/ewma 与 block_times 并入同一临界区：跨 worker 并发
                    # 读改写会丢更新（16 连接下 bytes 少计、EWMA 混入脏值）
                    with bt_lock:
                        block_times.append(dt_blk)      # 掉队重取的中位数基准
                        src["bytes"] += len(blob)
                        # EWMA 块耗时（遥测取舍 18.5：EWMA+失败计数够用，不抄 RTT 族）
                        src["ewma"] = (dt_blk if src["ewma"] is None
                                       else 0.7 * src["ewma"] + 0.3 * dt_blk)
                    break
                except Fail:
                    raise                               # etag_mismatch 等词表错误直通，不许收敛成 network
                except urllib.error.HTTPError as he:
                    with bt_lock:
                        src["fails"] += 1              # 遥测写点与 block_times 同锁
                    if he.code in FATAL_HTTP:          # A13 永久错误不重试
                        if attempt == 3:
                            raise
                        # v5.4 P1-1：池内永久状态码（WAF 403/中途 404/410 下架）=
                        # 这个源死了，不是任务失败——原样上抛会让健康的源整场
                        # http_403 收场（单源语义原样搬进池语义的裂缝）。统一走
                        # "剔源→池空才上抛→换源"：单源/池内最后健康源死亡时剔除后
                        # 池空照样立即上抛（A13 不重试语义保持），无需拿 len(pool)==1
                        # 当"单源下载"判据——那会在并发双 403 时把"池里只剩一个
                        # 健康源"误判成单源直接 raise（套件实锤）。CPython
                        # list.remove 对无自定义 __eq__ 元素在 GIL 下原子，并发双
                        # FATAL 靠捕获 ValueError 幂等；attempt==3 守住"4 次必
                        # raise"不变量。
                        if len(pool) > 1:
                            print("\n  源 %s 被服务器永久拒绝（HTTP %d），已从源池剔除"
                                  % (src["host"], he.code))
                        try:
                            pool.remove(src)
                        except ValueError:
                            pass
                        if not pool:
                            raise
                        src = pick_source()
                        if src is None:
                            raise    # 并发剔空窗口：重抛当前 he（v5.4 复审 P2）
                        continue
                    if attempt == 3:
                        raise
                    waste_check(i)                     # v4.9：重试前查浪费熔断
                    ra = _retry_after_sec(he)
                    if he.code in (429, 503):
                        # 服务器明确说限流：先砍并发再睡。Retry-After 优先于指数
                        # 退避（但不超过 _retry_after_sec 的上限）；同乘 ±30% 抖动
                        # （与下方指数退避同款公式）——16 个 worker 同秒醒来再集体
                        # 撞一次就是雷群。ra=0 时抖动仍为 0，不影响无 Retry-After 场景。
                        # 多源下砍的是肇事源自己的份额，不动其他源
                        src["gate"].penalize("HTTP %d" % he.code)
                        wait = max(wait, ra * random.uniform(0.7, 1.3))
                    else:
                        wait *= random.uniform(0.7, 1.3)
                    if multi:
                        src = pick_source(exclude=src)  # 重试自动换源（18.4）
                        if src is None:
                            raise    # 并发剔空窗口：重抛当前 he（v5.4 复审 P2；v5.16 对齐领块与 2048 区先例）
                except Exception as exc:
                    # 注意：不能用 e 作异常名——元组解包 (i, s, e) 里 e 是块尾偏移，
                    # CPython 在 except 块退出时会 DELETE_FAST 掉异常变量，
                    # 下次循环 fetch(s, e) 会撞 UnboundLocalError，整段下载作废
                    with bt_lock:
                        src["fails"] += 1              # 遥测写点与 block_times 同锁
                    refused = (isinstance(exc, ConnectionRefusedError)
                               or isinstance(getattr(exc, "reason", None), ConnectionRefusedError))
                    if attempt == 3:
                        # 收敛到契约里的 error 词表：否则 IncompleteRead 这类内部
                        # 类名会直接泄漏进 JSON，调用方无法按词表分支
                        if refused:
                            raise Fail("network", "分块 %d 连接被拒 4 次（服务器疑似限制每 IP"
                                       "并发连接数），重跑可续传；仍失败就加 -c 8 降并发" % i)
                        raise Fail("network", "分块 %d 重试 4 次仍失败：%s: %s"
                                   % (i, type(exc).__name__, exc))
                    waste_check(i)                     # v4.9：重试前查浪费熔断
                    if refused:
                        # 探测阶段拒连=确定性失败不重试；分块阶段已有 N 条连接在跑，
                        # 新连接被拒更多意味着"并发太高"而非"服务器没了"——收缩后重试
                        src["gate"].penalize("连接被拒")
                    wait *= random.uniform(0.7, 1.3)
                    if multi:
                        src = pick_source(exclude=src)  # 重试自动换源（18.4）
                        if src is None:
                            raise Fail("network", "分块 %d 换源失败：池已被并发剔空"
                                       % i)   # v5.16：防 None 进下轮 gate 炸 internal
                # 退避等待必须可中断（v5.0 拉取队列）：v4.9 的 futs.cancel 能直接
                # 取消未启动的块，worker_loop 模型下线程已活——time.sleep 会睡满
                # 全程（1+2+4=7s），stop 置位后滞留 worker 还持着断点锁，紧随的
                # 重跑就撞 locked。stop.wait 同样睡 wait 秒，但置位即醒。
                if stop.wait(wait):
                    raise Fail("cancelled", "已取消（已有其他分块失败，提前放弃重试）")
            # ---- 18.8-A 先入账者赢：副本完成时块已入账 → 丢弃（不写盘），已收
            # 字节计入 waste（全局熔断天然兜底）----
            if is_dup:
                with ok_lock:
                    already = i in state["ok"]
                if already:
                    _add_waste(len(blob), wgen)
                    return
            # ---- stop 守卫：fetch 携回字节后、写 .part/账本前复核。置位 = 本次
            # 运行已被主循环判死（如批量模式条目失败后下一条件目已开跑）：滞留
            # worker 的旧字节若此刻落盘，会与新一轮任务的 truncate/账本状态错位，
            # 混成账本说不出口的静默混合体。字节就地丢弃（句柄尚未打开，无需
            # 清理），按既有 cancelled 语义退出。----
            if stop.is_set():
                raise Fail("cancelled", "已取消（已有其他分块失败，提前放弃重试）")
            # 先写数据、后记账本：顺序不能反，它是"账本说完成就一定写完"的唯一保证
            with open(part, "r+b") as f:
                f.seek(s)
                f.write(blob)
                # ---- 会审 P1：账本 fsync 了、数据却只在页缓存——进程强杀安全
                #（OS 会刷缓存），但断电/蓝屏后账本会声称一个磁盘上还是零洞的块。
                # 16MB 一次 fsync 的代价可接受，换"账本从不说谎"。
                f.flush()
                os.fsync(f.fileno())
            with ok_lock:
                newly = i not in state["ok"]
                state["ok"].add(i)
                write_ledger()
                if newly:
                    # 仅供显示，弱一致可接受；副本与原认领双归队时只计一次
                    state["done"] += e - s + 1
            # v5.0 进度显示从主循环移入 worker（画法见 show_progress；v5.12 提取）
            show_progress()

        # ---- v5.0 拉取队列地基（方案 18.4）：块认领收敛单入口 next_block ----
        # v4.9 是 futs 预分配（一块一 future），多源池下慢镜像会让快源 worker 干完
        # 自己那份就闲置，总速=慢源。改为固定 worker 循环空闲即认领：谁快谁多拿，
        # 速度即调度器（18.4 不做源亲和的依据）；失败块重试自动换源（work 内
        # pick_source exclude），"换源"是队列送的而不是显式切换逻辑。单源静态
        # 分块顺序与 v4.9 逐字节一致（todo 本就按块号升序，popleft 保序；
        # 142 项回归是开工门槛）。
        # inflight 值 = [首次认领时刻, 副本在途标志]——掉队重取的观测面（18.8-A）。
        queue_lock = threading.Lock()
        pending = collections.deque(todo)     # 待领取块 (i, s, e)
        inflight = {}
        block_times = []                      # 池块耗时样本（中位数基准）
        bt_lock = threading.Lock()
        dup_budget = [0]                      # 已放出副本的块大小累计（字节）
        DUP_CAP_FRAC = 0.1                    # 重复取总量封顶（文件大小的 10%，18.8-A）

        def next_block():
            """单入口。返回 (块, 是否掉队副本) 或 (None, None)=无块可领（收工）。"""
            with queue_lock:
                if stop.is_set():
                    return None, None
                if pending:
                    t = pending.popleft()
                    inflight[t[0]] = [time.time(), False]
                    return t, False
                # ---- 掉队重取 / endgame（18.8-A，BitTorrent endgame 与 Spark
                # 推测执行的同构机制）：进入收尾段（剩余未入账块 < 连接数）且
                # 某在途块耗时 > 1.5x 池块中位数（Spark 实证阈值），空闲 worker
                # 放出该块的一个副本从别的源重取；先入账者赢。每块至多一副本
                # （Hadoop 同款），重复取总量封顶 10% 文件大小。
                if not inflight:
                    return None, None
                if len(tasks) - len(state["ok"]) >= nworkers:   # 未进收尾段
                    return None, None
                with bt_lock:
                    times = sorted(block_times)
                if not times:
                    return None, None
                thr = 1.5 * times[len(times) // 2]
                now = time.time()
                cand = None
                for bi, rec in sorted(inflight.items(), key=lambda kv: kv[1][0]):
                    if rec[1] or (now - rec[0]) <= thr or bi in state["ok"]:
                        continue
                    cand = bi
                    break
                if cand is None:
                    return None, None
                ci, cs, ce = tasks[cand]
                if dup_budget[0] + (ce - cs + 1) > max(DUP_CAP_FRAC * size, ce - cs + 1):
                    return None, None               # 副本预算耗尽（单块豁免封顶）
                inflight[cand][1] = True            # 同一时刻至多一副本（副本收尾清标志后可再放）
                dup_budget[0] += ce - cs + 1
                return tasks[cand], True

        def finish_block(i, is_dup=False):
            with queue_lock:
                if is_dup:
                    # 副本收尾只清副本标志、不 pop 原块记录：pop 掉会让仍在飞的
                    # 原块从 inflight"失踪"——该块永失第二次掉队重取，linger/
                    # endgame 的在途观测也失真。记录由原 worker 收尾时自行 pop。
                    if i in inflight:
                        inflight[i][1] = False
                else:
                    inflight.pop(i, None)

        def worker_loop():
            while True:
                t, is_dup = next_block()
                if t is None:
                    # 无块可领≠立刻退休：收尾段还有在途块时留下来轮询（复审
                    # P2-3）——掉队重取需要空闲 worker 在超时窗口内发现掉队块，
                    # 全体提前退休=末块最慢时永远没人放副本。inflight 清空
                    # （最后一块完成/失败）或 stop 置位即退休，as_completed 本就
                    # 要等最后一块，滞留不增加墙钟时间。
                    with queue_lock:
                        linger = bool(inflight) and \
                            len(tasks) - len(state["ok"]) < nworkers
                    if linger and not stop.wait(0.5):
                        continue
                    return
                try:
                    work(t, is_dup)
                except Exception:
                    # ---- 复审 P1-1：副本失败不许误杀下载。endgame 副本重试
                    # 耗尽抛 Fail 会穿透主循环 stop 全体——原认领块可能本可成功。
                    # 副本的成败必须由原 worker 裁决：已入账则无事发生，原块也
                    # 失败则由它的 Fail 正常收场（副本的失败字节已计 waste）。
                    if not is_dup:
                        raise
                    print("  掉队重取副本失败（分块 %d），等待原认领结果" % t[0])
                finally:
                    finish_block(t[0], is_dup)

        def _mirror_report():
            # 遥测双半边（18.1/18.8-E）：res["mirrors"]=本次运行实时（--json），
            # mirrors.jsonl=跨会话历史（只落入池镜像；出局镜像无 bytes 信息量，
            # 出局原因在 stderr）。成功/失败都上报（失败分布更有诊断价值）。
            # 主源也进 res["mirrors"] 供 agent 看负载分布，但不写历史文件
            # （历史是"哪个镜像好"的学习面，主源不是镜像）。
            if not multi:     # v5.4 复审 P3：剔除可把池缩到 0/1，实时池长会让
                return        # 单源失败丢 mirrors 遥测——用建池固化形态判定
            stats = [{"host": p["host"], "url": p["ident"], "bytes": p["bytes"],
                      "fails": p["fails"],
                      "ewma": (round(p["ewma"], 3) if p["ewma"] is not None else None)}
                     for p in pool]
            res["mirrors"] = stats
            _mirror_log([dict(ent, ts=time.strftime("%Y-%m-%dT%H:%M:%S"))
                         for ent in stats[1:]])

        ex = cf.ThreadPoolExecutor(max(1, min(a.conns, len(todo) or 1)))
        nworkers = max(1, min(a.conns, len(todo) or 1))
        # 速度只按**本次运行**实下的字节算：续传免费继承的块不算，否则 166/167 块
        # 续传时会显示出几千 MB/s 的假速度，倒计时跟着变成 00:00。
        # 必须在 submit 前取基线：submit 后先完成的块会推高 state["done"]，
        # 续传首秒速度被免费继承的字节污染而失真
        base_done = state["done"]
        global _rx_bytes
        _rx_bytes = 0    # v5.13：新任务接收计数归零（submit 前无线程在写，安全）
        _osc = sys.stderr.isatty()   # v5.12 ⑤：真终端才发 OSC 9;4，管道零干扰
        # v5.12 ② 起步 0 进度账本：探测/预分配完成到首块写账本之间有几十秒空窗，
        # 期间 --status 只报「进度未知（账本未生成）」。worker 启动前先落一版
        # parts=[] 的账本，起步期 --status 立刻有 size/0%（hist 播种 [t0, 0]，
        # 首块完成后速度窗自然衔接）。写失败与首块写同语义：无 try，向上抛。
        if not have:
            write_ledger()
        futs = [ex.submit(worker_loop) for _ in range(nworkers)]
        try:
            # v5.12 ⑤ 进度刷新节流：cf.wait(timeout=3) 空闲 3s 无块完成也强制
            # 刷一帧（大块/慢速时「每完成一块才刷」会几十秒不动）。有完成即返回，
            # 快路径零等待。A8 语义保持：f.result() 重抛 worker 异常。
            # 注意变量名：pending 已是上方块队列（deque），此处不得遮蔽
            # （套件实锤：遮蔽后 worker set.popleft() 炸 internal）。
            fut_pending = set(futs)
            while fut_pending:
                done, fut_pending = cf.wait(fut_pending, timeout=3,
                                            return_when=cf.FIRST_COMPLETED)
                if not done:
                    show_progress()
                for f in done:
                    # ---- A8 缺这一行时主循环的 except 是死代码，异常全被吞掉 ----
                    f.result()
        except BaseException:
            # ---- A9 置位点必须在这里：with 的 __exit__ 是 shutdown(wait=True)，
            #      写在 with 外层的 except 里就已经晚了 ----
            stop.set()
            for f in futs:
                f.cancel()
            # ---- v5.0：锁必须在这里显式释放。worker 抛出的 Fail 被 future 持有，
            # f.result() 重抛时把本帧追加进异常 traceback → future→异常→本帧→futs
            # 构成引用环，lock_fh 的引用计数要等 gc 收环才归零——进程内连续两次
            # 下载（agent 嵌入/测试套件）第二跑就撞 locked，纯赌 gc 时机（v4.9 就
            # 有此环，v5.0 原块+副本双失败 future 放大了存活概率，v50_lock 实锤）。
            # 滞留 worker 仍可能在写 .part：写入的是同源同块的正确字节、账本走
            # _atomic_replace 原子替换，与 v4.9"main 返回时帧亡放锁"的窗口等价。
            _release_part_lock(lock_fh)
            raise
        finally:
            if _osc:
                sys.stderr.write("\x1b]9;4;0;0\x07")   # 清进度环（失败也清）
                sys.stderr.flush()
            ex.shutdown(wait=False)
            _mirror_report()      # 成功/失败都上报镜像遥测（waste 熔断时也有价值）
        # v5.15：真终端补换行（进度条尾不黏下文）；非 tty 行进度自带换行，无操作
        _progress_end()
        # ---- A4 完成判定用超集，不再用计数（陈旧块号会让计数法永远不相等）----
        if not need <= state["ok"]:
            raise Fail("incomplete", "有分块未完成（%d/%d），重新运行同一条命令续传。"
                       % (len(need & state["ok"]), len(need)))

    # size 到这里必为 int：流式路径已把事后得知的 got 赋回 size；0 字节文件是 0
    if os.path.getsize(part) != size:
        raise Fail("size_mismatch", "大小校验失败：本地 %d ≠ 远端 %d 字节"
                   % (os.path.getsize(part), size))

    # ---- B3 无论有没有传 --sha256，都算出并打印指纹，供事后核对 ----
    if digest is None:                             # 流式路径已经边收边算过了，不重复读盘
        digest = sha256_of(part)
    if want and digest != want:
        if os.path.exists(donef):
            _remove_quiet(donef)                  # ---- A7 账本必须一起作废 ----
        # ---- T4 不覆盖上一次的坏文件，否则排查证据就没了 ----
        bad = part + ".bad"
        n = 1
        while os.path.exists(bad):
            bad = "%s.%d" % (part + ".bad", n)
            n += 1
        try:
            _atomic_replace(part, bad)                 # 用户可能等了很久，改名保留而不是删掉
        except OSError:
            bad = part
        res["verified"] = False   # v5.11 三态：给了期望且已证伪（错误收据也如实）
        raise Fail("verify_failed", "SHA256 校验不通过。已保留为 %s 供排查；删除它后重跑即可重下" % bad)
    print("SHA256 = %s" % digest)
    if want:
        print("SHA256 校验通过")

    _atomic_replace(part, out)
    _release_part_lock(lock_fh)               # 完成清场：锁与账本都不留
    for p in (donef,):
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass
    res.update(ok=True, path=out, bytes=size, seconds=time.time() - t_start,
               sha256=digest, verified=(True if want else None))   # v5.11 三态：无期望=null
    print("完成：%s（%.2f GB，用时 %.0f 秒）" % (out, size / 1e9, time.time() - t_start))
    # ---- v5.3 B4：锁残留可见性（不改锁语义）。自己的锁文件通常已由
    # _release_part_lock close+remove 清掉（remove 失败被吞时也会计入，
    # 同样无锁语义）；这里数的是目录里的残留 .part.lock（崩溃/被杀/Fail
    # 引用计数路径留下）——真身是 OS 对文件第 1
    # 字节的字节锁，随进程生死自释，残留的 0 字节文件无任何锁含义，重跑直接
    # 接管，--clean 按 tmp 归类可清。 ----
    n_locks = 0
    try:
        n_locks = sum(1 for e in os.scandir(os.path.dirname(out) or ".")
                      if e.name.endswith(".part.lock"))
    except OSError:
        pass
    if n_locks:
        print("提示：目录残留 %d 个 .part.lock（无害：锁随进程生死，--clean 可清）" % n_locks)
    return 0


class Parser(argparse.ArgumentParser):
    """让用法错误也遵守 0/1/130 的退出码契约（argparse 默认返回 2）。"""

    def error(self, message):
        self._cli_err = message       # v5.9 P3：供 main catch 提取（exit 只打印
                                      # message 到 stderr，SystemExit 不携带）
        self.print_usage(sys.stderr)
        # v5.9 P3（zcode 两 agent 独立撞见）：此前 --json 分支在此 emit 一行
        # 收据，main 的 SystemExit catch 又 emit 一行——stdout 双 JSON 击穿
        # 契约。现在这里只写 stderr（usage + error 行），JSON 收据统一由
        # main 的 catch 单行输出。
        self.exit(1, "%s: error: %s\n" % (self.prog, message))


def emit(res, out):
    print(json.dumps(res, ensure_ascii=True), file=out, flush=True)


def journal_path():
    """下载日志：每次下载调用追加一行。测试可用 DL_JOURNAL 覆盖路径。"""
    p = os.environ.get("DL_JOURNAL")
    if p:
        return p
    # 数据文件随程序走（便携）：与 ampull.py 同目录（DL_JOURNAL 可覆盖）
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "downloads.jsonl")


def journal_append(url, res):
    """---- v3.1 下载日志：可见性不能依赖用户在场。

    写失败静默吞掉——日志永远不能影响下载本身。超过 2MB 截断保留最后 1000 行。
    """
    try:
        p = journal_path()
        d = os.path.dirname(p)
        if d:
            os.makedirs(d, exist_ok=True)
        if os.path.exists(p) and os.path.getsize(p) > (2 << 20):
            with open(p, encoding="utf-8") as f:
                lines = f.readlines()[-1000:]
            with open(p, "w", encoding="utf-8") as f:
                f.writelines(lines)
        ent = {"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "url": _redact_url(url),
               "path": res.get("path"), "ok": bool(res.get("ok")),
               "bytes": res.get("bytes"), "error": res.get("error"),
               "warning": res.get("warning")}
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(ent, ensure_ascii=True) + "\n")
    except Exception:
        pass


def _parse_duration(s):
    m = re.fullmatch(r"(\d+)([dhm])", (s or "").strip())
    if not m:
        raise Fail("bad_args", "--older-than 格式应为 数字+单位：7d / 24h / 30m（收到 %r）" % s)
    return int(m.group(1)) * {"d": 86400, "h": 3600, "m": 60}[m.group(2)]


_CLEAN_LABEL = {"tmp": "残片", "orphan_ledger": "孤儿账本", "part": "断点",
                "bad": "证据", "foreign": "外部part"}


def do_clean(a):
    """--clean 模式：扫描/清理残留。返回 (退出码, 结果dict)，JSON 由 main 统一 emit。

    分类（后缀模式为本工具独有；账本内容还能自证身份，读出 url 供展示）：
      *.part.done 且无配对 .part -> 孤儿账本；*.part.done.tmp -> 残片；
      *.part -> 断点；*.bad / *.bad.N -> 证据（T4：永不删除，只列出）。
    删除：默认只列不删；-y 删孤儿账本+残片；-y --all 连断点。两道安全闸：
      mtime 距今 < CLEAN_ACTIVE_SEC 视为可能活跃（与 C4 同值）一律不删；
      --older-than 7d|24h|30m 只删超龄项。
    非活跃断点标注"可续传"状态与已落盘进度（v5.3 B4，kept 条目带 progress_pct）。
    扫描非递归：残留永远与产物同目录（A3/A11 的 part/bad 都由 out 派生）。
    """
    # v5.7 复审 P2：与 do_serve 同款互斥——位置 URL/manifest/repo 静默被吞会让人
    # 以为下过了（--clean --manifest X 的清单参数一样不参与清理）
    if getattr(a, "urls", None) or getattr(a, "manifest", None) or getattr(a, "repo", None):
        raise Fail("usage", "--clean 与下载参数互斥（URL/manifest/repo 不会参与清理）")
    dirs = list(a.clean)
    from_journal = False
    if not dirs:
        seen = []
        try:
            with open(journal_path(), encoding="utf-8") as f:
                for line in f:
                    try:
                        m = json.loads(line)
                    except ValueError:
                        continue
                    d = os.path.dirname(m.get("path") or "")
                    if d and d not in seen:
                        seen.append(d)
        except OSError:
            pass
        dirs = seen[:50]
        from_journal = True
        if not dirs:
            print("下载日志（%s）里还没有记录，也没有指定要扫描的目录" % journal_path())
            return 0, {"ok": True, "mode": "clean", "roots": [], "counts": {},
                       "freed_bytes": 0, "deleted": [], "kept": [], "error": None}
        print("未指定目录：按下载日志扫描最近出现过的 %d 个下载目录" % len(dirs))

    older = _parse_duration(a.older_than) if a.older_than else None
    now = time.time()
    bad_dirs = [d for d in dirs if not os.path.isdir(d)]
    items = []
    for d in dirs:
        if not os.path.isdir(d):
            continue
        try:
            entries = list(os.scandir(d))
        except OSError:
            if d not in bad_dirs:
                bad_dirs.append(d)
            continue
        for ent in entries:
            if not ent.is_file():
                continue
            n = ent.name
            if n.endswith(".part.done.tmp") or n.endswith(".part.lock"):
                kind = "tmp"        # v4.6：.lock 残留无锁语义（进程死锁自释）
            elif n.endswith(".part.done"):
                if os.path.exists(ent.path[:-5]):       # [:-5] 去掉 ".done"
                    continue                            # 有配对 .part：信息记在 .part 上
                # 复审 B 对称守卫：外部 *.part.done（内容不是本工具账本 JSON）
                # 不能凭后缀就删——与 .part 的 foreign 分类同一逻辑
                own = False
                try:
                    with open(ent.path, encoding="utf-8") as f:
                        m2 = json.load(f)
                    own = isinstance(m2, dict) and ("url" in m2 or "fp" in m2)
                except (OSError, ValueError):
                    pass
                kind = "orphan_ledger" if own else "foreign"
            elif n.endswith(".bad") or re.search(r"\.bad\.\d+$", n):
                kind = "bad"
            elif n.endswith(".part"):
                # 评审 B：Firefox/浏览器部分下载同样叫 *.part，按后缀删会清掉
                # 别人正在下的东西。只有配对账本能读出本工具格式（dict 且含
                # url|fp+size）的才认作断点；否则归类 foreign，永不删除。
                own = False
                try:
                    with open(ent.path + ".done", encoding="utf-8") as f:
                        m2 = json.load(f)
                    own = isinstance(m2, dict) and ("url" in m2 or "fp" in m2) and "size" in m2
                except (OSError, ValueError):
                    pass
                kind = "part" if own else "foreign"
            else:
                continue
            try:
                st = ent.stat()
            except OSError:
                continue
            url = None
            pct = None
            if kind in ("part", "orphan_ledger"):
                lf = ent.path + ".done" if kind == "part" else ent.path
                try:
                    with open(lf, encoding="utf-8") as f:
                        m3 = json.load(f)
                    # v5.0 账本 url 字段已退出，展示改取 urls 首个（池主源）
                    url = m3.get("url") or ((m3.get("urls") or [None])[0]
                                            if isinstance(m3, dict) else None)
                    # v5.3 B4：断点状态标注——账本自带 size/chunk/parts，"有账本但
                    # 无活跃进程"的断点直接报可续传进度，agent 不用猜、不用二次解析
                    if (kind == "part" and isinstance(m3, dict)
                            and isinstance(m3.get("size"), int) and m3["size"] > 0
                            and isinstance(m3.get("chunk"), int) and m3["chunk"] > 0
                            and isinstance(m3.get("parts"), list)):
                        done_b = sum(min(m3["chunk"], m3["size"] - i * m3["chunk"])
                                     for i in m3["parts"]
                                     if isinstance(i, int) and 0 <= i * m3["chunk"] < m3["size"])
                        pct = round(min(done_b, m3["size"]) / m3["size"] * 100.0, 1)
                except (OSError, ValueError):
                    pass
            items.append((kind, ent.path, st.st_size, st.st_mtime, url, pct))

    deleted, kept, freed = [], [], 0
    counts = {"tmp": 0, "orphan_ledger": 0, "part": 0, "bad": 0, "active": 0,
              "foreign": 0}
    for kind, path, size, mtime, url, pct in items:
        counts[kind] += 1
        age = now - mtime
        active = age < CLEAN_ACTIVE_SEC
        young = older is not None and age < older
        may = ((kind in ("tmp", "orphan_ledger") and a.yes)
               or (kind == "part" and a.yes and a.all))
        if may and not active and not young:
            try:
                os.remove(path)
            except OSError as e:
                kept.append({"kind": kind, "path": path, "bytes": size,
                             "age_days": round(age / 86400.0, 1), "url": url,
                             "progress_pct": pct, "reason": "删除失败: %s" % e})
                print("  [%s] %s —— 删除失败: %s" % (_CLEAN_LABEL[kind], path, e))
                continue
            deleted.append(path)
            freed += size
            print("  [已删-%s] %s（%.1f KB）" % (_CLEAN_LABEL[kind], path, size / 1e3))
            # v5.16 P3：-y --all 删断点 .part 后同轮删配对账本，孤儿账本不再
            # 要二次 --clean 才收敛（账本 own 判定时已验明本工具格式，删除安全；
            # 删除失败只提示不回滚——数据文件已没了，账本留着必然成孤儿）
            if kind == "part":
                ledger = path + ".done"
                if os.path.exists(ledger):
                    try:
                        lb = os.path.getsize(ledger)
                        os.remove(ledger)
                        deleted.append(ledger)
                        freed += lb
                        print("  [已删-账本] %s（断点随删）" % ledger)
                    except OSError as e:
                        print("  [账本] %s —— 删除失败: %s（下次 --clean 收敛）" % (ledger, e))
            continue
        if active:
            counts["active"] += 1
            reason = "10 分钟内动过，可能活跃"
        elif young:
            reason = "未到 --older-than"
        elif kind == "foreign":
            reason = "外部 .part（无本工具账本配对，可能是浏览器下载），不删"
        elif kind == "part":
            # v5.3 B4：非活跃断点=可续传资产而非垃圾，标注状态与进度
            reason = "断点可续传：已落盘 %s，重跑原命令接上；删除需 -y --all" % (
                ("%.1f%%" % pct) if pct is not None else "比例未知")
        else:
            reason = "未加 -y"
        kept.append({"kind": kind, "path": path, "bytes": size,
                     "age_days": round(age / 86400.0, 1), "url": url,
                     "progress_pct": pct, "reason": reason})
        print("  [%s] %s（%.1f KB，%.1f 天前%s）—— %s"
              % (_CLEAN_LABEL[kind], path, size / 1e3, age / 86400.0,
                 ("，" + url[:60]) if url else "", reason))

    all_bad = bool(dirs) and len(bad_dirs) == len(dirs)
    print("扫描 %d 个目录：残片 %d、孤儿账本 %d、断点 %d、外部 %d、证据 %d（活跃跳过 %d）；"
          "已删 %d 项，释放 %.2f MB%s"
          % (len(dirs) - len(bad_dirs), counts["tmp"],
             counts["orphan_ledger"], counts["part"], counts["foreign"],
             counts["bad"], counts["active"],
             len(deleted), freed / 1e6,
             ("；日志: " + journal_path()) if not from_journal else ""))
    res_c = {"ok": not all_bad, "mode": "clean", "roots": dirs, "counts": counts,
             "freed_bytes": freed, "deleted": deleted[:200], "kept": kept[:50],
             "error": "bad_path" if all_bad else None, "bad_dirs": bad_dirs}
    return (1 if all_bad else 0), res_c


def _probe_lock_held(lp):
    """--status 的活跃判定：探测 <out>.part.lock 是否被另一进程持有。

    尝试独占锁定第 1 字节（与 _acquire_part_lock 同字节区间）：锁定失败=有主
    （下载活跃）；锁定成功=无主（残留锁文件无锁语义，与 --clean 同判），句柄
    关闭锁自释。打不开（权限/竞态已删）按无主算——查询进程不因探测失败误报
    活跃。探测是加锁尝试不是读锁，对下载进程零干扰。非 Windows 无 msvcrt：
    退化为锁文件 mtime 距今 < CLEAN_ACTIVE_SEC 视为活跃（--clean 活跃闸同启发式；
    v5.10 起 POSIX 下载进程也创建标记锁文件，该分支真实可达）。
    """
    if not os.path.exists(lp):
        return False
    if msvcrt is None:
        try:
            return (time.time() - os.path.getmtime(lp)) < CLEAN_ACTIVE_SEC
        except OSError:
            return False
    try:
        fh = open(lp, "r+b")
    except OSError:
        return False
    try:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        return False
    except OSError:
        return True
    finally:
        try:
            fh.close()
        except OSError:
            pass


def do_status(a):
    """--status 模式：只读查询目标下载状态（v5.11 起一次可给多个路径）。

    返回 (退出码, 结果dict)，JSON 由 main 统一 emit；状态人话行恒走 stderr
    （v5.11 修复：此前 --json 下人话行混入 stdout，违反"stdout 只有最后一行
    JSON"契约）。单路径收据=五态对象；多路径收据={ok, mode:"status", count,
    results:[单路径收据...]}（单路径收据=v5.10 形状外加 speed_bytes_s/eta_s
    两字段，不破老 agent）。
    """
    paths = [s.strip() for s in (a.status or [])
             if isinstance(s, str) and s.strip()]
    if not paths:
        raise Fail("usage", "--status 需要至少一个目标路径（产物名即可，.part 断点形态也认）")
    results = []
    for raw in paths:
        if _looks_url(raw):
            raise Fail("usage", "--status 收到的是 URL 不是本地路径——查的是落盘状态，给产物路径")
        results.append(_status_one(raw))
    if len(results) == 1:
        return 0, results[0]
    return 0, {"ok": True, "mode": "status", "count": len(results),
               "results": results, "error": None}


def _status_one(raw):
    """单路径状态查询（do_status 的实现核）。返回收据 dict（不含外层包装）。

    目标=产物路径，自动归一 .part/.part.done/.part.lock 后缀形态（给最终名即可）。
    全程只读不写，可与下载进程并存（账本 A2 原子写，json.load 永远拿到完整快照）：
      active    另一进程持有断点锁（下载进行中，进度来自账本）
      resumable 断点在场（.part+本工具账本、锁无主）：重跑原下载命令接上
      done      产物在场且无 .part 断点（.part 残留时 stderr 注明）
      foreign   .part 在场但无本工具账本（可能是浏览器部分下载），不判不碰
      missing   产物与断点都不存在（也是有效查询结果，退出码 0）
    进度算式与 --clean B4 同源：Σ min(chunk, size-i·chunk) / size。
    v5.11：active/resumable 另带 speed_bytes_s（账本 hist 采样窗实测速度）与
    eta_s（仅 active，正增益采样段 <2 时 null——单段外推误导）；旧账本无采样诚实 null。
    """
    t = raw
    if t.endswith(".part.lock") or t.endswith(".part.done"):
        t = t[:-10]
    elif t.endswith(".part"):
        t = t[:-5]
    if not t:
        raise Fail("usage", "--status 目标剥掉 .part 后缀后为空，请给产物路径")
    out = t
    # C-P2-1：真名含 .part 的产物（断点文件 foo.part 的产物名就叫 foo.part）。
    # 剥后缀解释有账本/锁在场时优先（说明 .part 确是断点形态）；否则原样是
    # 产物或带账本时按原样查——归一不吞真名。
    if out != raw:
        if not (os.path.exists(out + ".part.done")
                or os.path.exists(out + ".part.lock")) \
                and (os.path.isfile(raw)
                     or os.path.exists(raw + ".part.done")):
            out = raw
    if os.path.isdir(out):
        raise Fail("usage", "--status 目标是产物文件路径而非目录（目录残留扫描用 --clean）")
    part, donef, lockf = out + ".part", out + ".part.done", out + ".part.lock"
    out_exists = os.path.isfile(out)
    part_exists = os.path.exists(part)
    held = _probe_lock_held(lockf)

    # 账本只读：A2 原子写保证拿到完整快照；foreign 判据与 --clean 同款
    led = None
    if os.path.exists(donef):
        try:
            with open(donef, encoding="utf-8") as f:
                m = json.load(f)
            if isinstance(m, dict) and ("fp" in m or "url" in m) and "size" in m:
                led = m
        except (OSError, ValueError):
            pass
    db = pct = None
    if (led and isinstance(led.get("size"), int) and led["size"] > 0
            and isinstance(led.get("chunk"), int) and led["chunk"] > 0
            and isinstance(led.get("parts"), list)):
        db = sum(min(led["chunk"], led["size"] - i * led["chunk"])
                 for i in led["parts"]
                 if isinstance(i, int) and 0 <= i * led["chunk"] < led["size"])
        db = min(db, led["size"])
        pct = round(db / led["size"] * 100.0, 1)
    url = None
    if led:
        lu = led.get("urls") if isinstance(led.get("urls"), list) else None
        if (not (lu and lu[0]) and isinstance(led.get("url"), str)
                and led.get("url")):
            lu = [led["url"]]            # A-P3-6：旧账本单数 url 键兜底
        if lu and lu[0]:
            url = _redact_url(lu[0])

    size_out = None
    if held:
        state = "active"
        if led and isinstance(led.get("size"), int):
            size_out = led["size"]
    elif out_exists:
        state = "done"
        try:
            size_out = os.path.getsize(out)
            db, pct = size_out, 100.0
        except OSError:
            pass    # A/B P3：大小读不到时诚实留 null，不摆 pct=100.0+bytes=null 矛盾组合
        if part_exists:
            print("  注意：产物在场但 %s 残留（无害，--clean 可清）" % part,
                  file=sys.stderr)
    elif part_exists and led:
        state = "resumable"
        if isinstance(led.get("size"), int):
            size_out = led["size"]
    elif part_exists:
        state = "foreign"
    else:
        state = "missing"

    # v5.11 速度/ETA：账本 hist 采样窗（最近 16 次写入）。速度按**相邻正增益段**
    # 累计（ΣΔbytes/ΣΔt，只计字节增长的相邻对）——dup 零增益样本与 resume 混窗
    # 夹的停顿段（Δbytes=0）不拉低速度。旧账本无 hist、采样结构不对、窗口内无
    # 正增益段 → 诚实 null；resumable（进程已死）窗口陈旧（末次写入 >120s 前）
    # 也 null——死前速度不冒充现速（复审 A/B P2）。v5.12：正增益段 <2 时
    # eta=null（speed 照报）——单段外推误导性强（真机实锤：起步 ETA 24 分钟、
    # 实际 3.4 分钟），两个独立采样段才够看出趋势。
    sp = eta = None
    if state in ("active", "resumable") and led:
        h = led.get("hist")
        if (isinstance(h, list) and len(h) >= 2
                and all(isinstance(x, (list, tuple)) and len(x) == 2
                        and isinstance(x[0], (int, float)) and x[0] > 0
                        and isinstance(x[1], (int, float)) and x[1] >= 0
                        for x in h)
                and (state != "resumable"
                     or time.time() - h[-1][0] <= 120)):
            dt = dby = segs = 0
            for p0, p1 in zip(h, h[1:]):
                if p1[1] > p0[1]:
                    dt += p1[0] - p0[0]
                    dby += p1[1] - p0[1]
                    segs += 1
            if dt > 0 and dby > 0:
                sp = int(dby / dt)
                if (segs >= 2 and state == "active" and size_out
                        and db is not None and 0 <= db < size_out and sp > 0):
                    eta = int((size_out - db) / sp)

    abs_out = os.path.abspath(out)
    prog = ("已落盘 %.1f%%（%d / %d 字节）" % (pct, db, size_out)
            if pct is not None and db is not None and size_out else None)
    spd = ("，约 %.1f MB/s" % (sp / 1e6)) if sp else ""
    rem = ("，剩余约 %02d:%02d" % (eta // 60, eta % 60)) if eta is not None else ""
    if state == "active":
        print("状态：下载进行中（另一进程持有断点锁）—— %s%s%s%s"
              % (abs_out, ("，" + prog) if prog else "，进度未知（账本未生成）", spd, rem),
              file=sys.stderr)
    elif state == "resumable":
        print("状态：断点可续传 —— %s%s%s；重跑原下载命令接上"
              % (abs_out, ("，" + prog) if prog else "", spd), file=sys.stderr)
    elif state == "done":
        print("状态：文件已在场 —— %s%s"
              % (abs_out, ("（%d 字节）" % size_out) if size_out is not None else ""),
              file=sys.stderr)
    elif state == "foreign":
        print("状态：外部 .part（无本工具账本，可能是浏览器下载）—— %s，不判不碰" % abs_out,
              file=sys.stderr)
    else:
        print("状态：目标不存在 —— %s（产物与断点都没有）" % abs_out, file=sys.stderr)

    r = {"ok": True, "mode": "status", "path": abs_out, "state": state,
         "locked": held, "size": size_out, "downloaded_bytes": db,
         "progress_pct": pct, "speed_bytes_s": sp, "eta_s": eta,
         "url": url, "error": None}
    return r


def _looks_url(s):
    return bool(re.match(r"^[A-Za-z][A-Za-z0-9+.\-]*://", s))


def _ntfs_safe_name(name):
    """v5.14：NTFS 非法字符与结尾点/空格清洗（head() 自动取名与 name_from_url
    共用）。非法字符 <>:"|?* 替换为下划线，结尾点/空格剥离（NTFS 会静默剥，
    导致 A.MD 与 A.MD. 落成同名互相覆盖）。"""
    name = re.sub(r'[<>:"|?*]', "_", name).rstrip(" .")
    if not name:
        return "download.bin"
    return name


def name_from_url(url):
    """批量模式下从 URL 推文件名。与 head() 自动取名同规则：URL 解码 +
    Windows 保留设备名（CON/NUL/COM1…）防御；%2F 解码出的路径分隔符替换掉。"""
    name = urllib.parse.unquote(os.path.basename(urllib.parse.urlsplit(url).path.rstrip("/")))
    name = name.replace("/", "_").replace("\\", "_")
    name = _ntfs_safe_name(name)   # v5.14：收敛到共用清洗（原 v5.7 内联正则）
    if not name:
        return "download.bin"
    if re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\.|$)", name, re.I):
        name = "_" + name
    return name


def _safe_relpath(path):
    """仓库相对路径 -> 本地相对路径：挡掉 .. 穿越与 Windows 非法字符。"""
    parts = [re.sub(r'[<>:"|?*\\]', "_", p) for p in path.split("/") if p and p not in (".", "..")]
    if not parts:
        raise Fail("bad_args", "仓库文件路径异常：%r" % path)
    return os.path.join(*parts)


def read_manifest(path):
    """--manifest：每行 URL [sha256]。# 开头注释、空行忽略。UTF-8（带 BOM 也认）。

    第二列存在就必须是 64 位十六进制——写错半截指纹比不写更糟（校验必失败，
    还要白下一次）。"""
    items = []
    try:
        f = open(path, encoding="utf-8-sig")
    except OSError as e:
        raise Fail("bad_path", "清单文件打不开：%s" % e)
    with f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) > 2 or (len(parts) == 2
                                  and not re.fullmatch(r"[0-9a-fA-F]{64}", parts[1])):
                raise Fail("bad_args", "清单第 %d 行格式应为 URL [sha256]（收到 %r）"
                           % (ln, line[:80]))
            items.append((parts[0], parts[1].lower() if len(parts) == 2 else None))
    return items


def list_repo(repo, includes, a=None):
    """--repo：拉取 HF 仓库文件清单。返回 [(resolve_url, sha256, 相对路径, 大小)]。

    - API 走 HF_ENDPOINT（与官方 hf CLI 同名同义），默认 https://hf-mirror.com
      （国内可达；海外可设 https://huggingface.co）。repo 接受裸 ID 或页面链接。
    - 清单 API 与下载走同一套代理分流（v4.7 P2）：传入 a 后按 base 域名调
      _resolve_ops，hf-mirror 直连优先、huggingface.co 走代理，首选通道失败
      自动切备用。
    - LFS 文件的 oid 就是内容 sha256（已实测比对过 UD-IQ4_XS 的 10.95MB 首片），
      因此 --repo 模式天然带逐片校验；非 LFS 文件（README 等）没有，留空不校验。
    - 保留仓库子目录结构落盘（GGUF 仓库常按量化分目录，压平会重名）。
    """
    base = (os.environ.get("HF_ENDPOINT") or "https://hf-mirror.com").rstrip("/")
    # P2：清单 API 与下载同一套通道决策（此前的裸 _open 绕过了自动检测代理）
    api_op, api_alt = None, None
    if a is not None:
        api_op, api_alt = _resolve_ops(
            a, host=(urllib.parse.urlsplit(base).hostname or "").lower())
    rev = "main"
    m = re.fullmatch(r"(?:https?://[^/]+)/([^/]+/[^/]+?)(?:/(?:tree|blob)/([^/?#]+))?(?:[/?#].*)?",
                     repo)
    if m:
        repo = m.group(1)
        rev = m.group(2) or rev
    elif not re.fullmatch(r"[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+", repo):
        raise Fail("bad_args", "--repo 应为 属主/仓库名（如 unsloth/Qwen3.8-Flash-Next-GGUF）"
                   "或其页面链接（收到 %.60s）" % repo)
    entries = []
    url = "%s/api/models/%s/tree/%s?recursive=true" % (base, urllib.parse.quote(repo),
                                                       urllib.parse.quote(rev))
    for _ in range(100):
        def page():
            with _open(url, op=api_op) as r:
                return json.load(r), r.headers.get("Link") or ""
        try:
            data, link = _attempt(page, "仓库清单")
        except urllib.error.HTTPError as e:
            hint = {401: "（需要鉴权：加 --hf-token <token> 或 --header 'Authorization: Bearer <token>'）",
                    404: "（仓库不存在或已改名）"}.get(e.code, "")
            raise Fail("http_%d" % e.code, "拉取仓库文件列表失败 HTTP %d%s：%s"
                       % (e.code, hint, repo))
        except (urllib.error.URLError, OSError) as e:
            # 与 download 的 proxy_fallback 同款：首选通道不通先切备用再判死
            if api_alt is not None:
                print("  首选通道拉取清单失败（%s: %s），切换备用通道重试"
                      % (type(e).__name__, str(e)[:60]))
                api_op, api_alt = api_alt, None
                continue
            raise Fail("network", "仓库清单拉取失败（%s: %s）。检查 HF_ENDPOINT 或稍后重跑"
                       % (type(e).__name__, e))
        if not isinstance(data, list):
            raise Fail("internal", "仓库清单 API 返回了意外内容：%.80s" % str(data))
        for it in data:
            if it.get("type") != "file":
                continue
            path = it.get("path") or ""
            if not any(fnmatch.fnmatch(path, pat) for pat in includes):
                continue
            lfs = it.get("lfs") or {}
            oid = (lfs.get("oid") or "").lower()
            sha = oid if re.fullmatch(r"[0-9a-f]{64}", oid) else None
            entries.append(("%s/%s/resolve/%s/%s" % (base, repo, rev, urllib.parse.quote(path)),
                            sha, _safe_relpath(path), it.get("size") or lfs.get("size")))
        nxt = re.search(r'<([^>]+)>;\s*rel="next"', link)
        if not nxt:
            return entries
        url = nxt.group(1)
    raise Fail("internal", "仓库清单翻页超过 100 页，中止")


def _dedupe_check(sources):
    # v5.14：NTFS 大小写不敏感，A.MD 与 a.md 精确字符串判重视为不同名，实际
    # 落盘互相覆盖——按 normcase 归一判等（POSIX 恒等不受影响）；报错保留
    # 原始 rel 方便用户定位清单行。
    seen = set()
    for _, _, rel, _ in sources:
        k = os.path.normcase(rel)
        if k in seen:
            raise Fail("bad_args", "清单里有同名文件 %r，会互相覆盖；请用 --repo（保留"
                       "子目录）或分批下载" % rel)
        seen.add(k)


def _fail_code(e):
    """批量模式里把单分片的异常收敛到 error 词表（与 main 的 except 链同一映射）。"""
    if isinstance(e, Fail):
        return e.code
    if isinstance(e, urllib.error.HTTPError):
        return "http_%d" % e.code
    if isinstance(e, urllib.error.URLError):
        return "network"
    if isinstance(e, OSError):
        return "disk" if _is_disk_error(e) else "io"
    return "internal"


def _error_msg(e):
    """失败人话一行（≤200 字符），main 的 except 链与批量片级共用（v5.4 P2-10：
    批量收据 docstring 承诺 error_msg 但只有顶层有——批量恰是 agent 主用法，
    v5.2 加 error_msg 的动机场景正是它）。分支与 main 逐字对齐。"""
    if isinstance(e, Fail):
        return str(e)[:200]
    if isinstance(e, urllib.error.HTTPError):
        return ("HTTP %d %s（链接可能已过期、需要鉴权，或该地址"
                "不允许直接下载）" % (e.code, e.reason))[:200]
    if isinstance(e, urllib.error.URLError):
        return str(e.reason)[:200]
    if isinstance(e, OSError):
        if _is_disk_error(e):
            return ("磁盘写入失败（空间不足？）%s: %s" % (type(e).__name__, e))[:200]
        return ("%s: %s" % (type(e).__name__, e))[:200]
    return ("%s: %s" % (type(e).__name__, e))[:200]


def _warn(res, code):
    """v5.4 P2-9：warning 去重合并——直接赋值会让后发的 warning 吞掉先发的
    （proxy_fallback 被 html_response 覆盖），按 warning 分支的 agent 漏判。
    与批量侧"逗号连接多个"的收据契约同构。"""
    cur = res.get("warning")
    if not cur:
        res["warning"] = code
    elif code not in cur.split(","):
        res["warning"] = cur + "," + code


def _plan(a):
    """解析位置参数与 --manifest/--repo，产出单文件任务或分片批量任务。

    返回 (sources, out_dir, single)：
      single 非 None：单文件模式，out_dir 是保存路径（None=自动取名）；
      single 为 None：批量模式，sources=[(url, sha256|None, 相对路径, 大小|None)]，
                      out_dir 是目标目录（由 do_batch 负责创建）。

    位置参数识别（对旧用法完全兼容）：
      1 个 = 单文件；2 个且第 2 个不是链接 = 单文件+保存位置（旧行为）；
      其余 = 批量：末尾是本地路径则为目标目录，否则全是链接、存当前目录。
      本地路径绝不会长得像链接（scheme:// 开头），判据可靠。
    """
    pos = list(a.urls)
    if a.manifest or a.repo:
        if len(pos) > 1:
            raise Fail("usage", "--manifest/--repo 模式下位置参数最多一个（目标目录）")
        if a.sha256:
            raise Fail("bad_args", "批量模式不支持 --sha256；校验值写在清单里"
                       "（每行 URL sha256）或由 --repo 自动获取")
        out_dir = pos[0] if pos else "."
        src = []
        if a.manifest:
            src += [(u, s, name_from_url(u), None) for u, s in read_manifest(a.manifest)]
        if a.repo:
            src += list_repo(a.repo, a.include or ["*"], a)
        if not src:
            raise Fail("bad_args", "没有可下载的条目（清单为空或仓库无匹配 --include 的文件）")
        _dedupe_check(src)
        return src, out_dir, None
    if not pos:
        raise Fail("usage", "缺少下载链接（批量请用多个链接 / --manifest / --repo；"
                   "清理残留用 --clean）")
    if len(pos) == 1:
        return None, None, pos[0]
    if len(pos) == 2 and not _looks_url(pos[1]):
        return None, pos[1], pos[0]
    if a.sha256:
        raise Fail("bad_args", "多个链接时不支持 --sha256；逐片校验请用 --manifest"
                   "（每行 URL sha256）或 --repo")
    if _looks_url(pos[-1]):
        src = [(u, None, name_from_url(u), None) for u in pos]
        out_dir = "."
    else:
        src = [(u, None, name_from_url(u), None) for u in pos[:-1]]
        out_dir = pos[-1]
    _dedupe_check(src)
    return src, out_dir, None


def do_batch(sources, out_dir, a, res):
    """分片批量：逐片顺序下载（每片内部仍多连接 + 断点续传）。

    顺序而非跨文件并行：并行会把 片数 x 连接数 叠上去，正对着镜像站的限流打；
    顺序还能让磁盘问题在第一片就暴露。一个分片失败不拦后面的分片——失败的片
    留着断点，重跑同一条命令即可续传，已完成的片秒过（大小一致即跳过）。
    批量×mirror（v5.10，18.5 规划「按 host 共享池逐片双源」落地）：--mirror 规格
    解析为镜像站（host 级，URL/@文件/@auto 均可），每片候选=镜像站+片自身 path
    （query 不跨站携带——签名与源站绑定，跨站无效且外泄），逐片探测过双门后建池；
    镜像学习（mirrors.jsonl）照常逐片记。
    """
    t0 = time.time()
    dir_abs = os.path.abspath(out_dir)
    try:
        os.makedirs(dir_abs, exist_ok=True)
    except OSError as e:
        raise Fail("bad_path", "目标目录创建失败：%s（%s）" % (dir_abs, e))
    res["path"] = dir_abs
    res["files"] = []

    # 整批磁盘预检（--repo 模式大小全已知时才有意义；逐片的检查仍在 download 里）
    sized = [(rel, s) for _, _, rel, s in sources if s]
    if sized and len(sized) == len(sources):
        need = sum(s for rel, s in sized
                   if not (os.path.exists(os.path.join(dir_abs, rel))
                           and os.path.getsize(os.path.join(dir_abs, rel)) == s))
        free = shutil.disk_usage(dir_abs).free
        if need > free:
            raise Fail("disk", "磁盘空间不足：这批还需 %.2f GB，可用 %.2f GB（%s）"
                       % (need / 1e9, free / 1e9, dir_abs))
        print("共 %d 个文件、还需约 %.2f GB，逐片下载到 %s" % (len(sources), need / 1e9, dir_abs))
    else:
        print("共 %d 个文件，逐片下载到 %s" % (len(sources), dir_abs))

    # ---- v5.10 批量×mirror（18.5 规划「按 host 共享池逐片双源」落地）----
    # --mirror 规格在批量下解析为镜像站（host 级，URL/@文件/@auto 均可）；
    # 每片候选 = 镜像站 scheme+netloc + 片自身 path，query 不跨站携带。
    # 正确性仍由逐片双门兜底（大小一致+支持 Range，ETag 不一致需片级 sha 兜底）。
    mhosts = []
    mspecs = getattr(a, "mirror", None)
    if mspecs:
        seen_h = set()
        for mu in _load_mirror_urls(mspecs):
            p = urllib.parse.urlsplit(mu)
            hl = (p.hostname or "").lower()
            if not hl or hl in seen_h:
                continue
            seen_h.add(hl)
            mhosts.append((p.scheme, p.netloc.rsplit("@", 1)[-1], hl))
        if mhosts:
            print("批量镜像：解析出 %d 个镜像站，逐片替换 host 探测建池" % len(mhosts))

    if mhosts and len(sources) > 1:
        # B-P2-3：死站镜像的 20s 探测税按片数乘（实测 2 片 44.5s vs 8.4s）。批量
        # 先做一次 host 级活性预探，拒连/超时的镜像站整批跳过；单文件无乘数效应
        # 通道解析仍留主线程（v5.1：_detect_proxies 不进并发窗口）。
        # v5.14（P0）：预构主源 host 集合传入 main_host——原代码把镜像自己的
        # host 传成 main_host，_open 的「tgt not in mhs 才剥凭据」永不触发，
        # EXTRA_HEADERS（Authorization/--hf-token/X-*）原样发往每个镜像站。
        # 语义与 fetch（main_host 剥离区）、单文件镜像探测同源（v5.9 P2-1 集合判据）。
        main_hosts = {(urllib.parse.urlsplit(u).hostname or "").lower()
                      for u, _, _, _ in sources} - {""}
        items = [((sc, nl, hl), _resolve_ops(a, host=hl)[0], main_hosts)
                 for sc, nl, hl in mhosts]

        def _host_alive(item):
            (sc, nl, hl), op, mhs = item
            try:
                head(sc + "://" + nl + "/", op=op, timeout=MIRROR_TIMEOUT,
                     tries=1, main_host=mhs)
                return hl
            except urllib.error.HTTPError:
                return hl     # 有响应=通道通（根路径 403/404 很常见，站是活的）
            except Fail:
                return None   # head 把 URLError/OSError/超时打包成 network
            except Exception:
                return None

        with cf.ThreadPoolExecutor(max(1, min(len(items), 16))) as ex:
            alive_h = {h for h in ex.map(_host_alive, items) if h}
        dead = [hl for _sc, _nl, hl in mhosts if hl not in alive_h]
        if dead:
            print("批量镜像：%d 个镜像站活性预探失败（拒连/超时），整批跳过：%s"
                  % (len(dead), "、".join(dead)))
        mhosts = [(sc, nl, hl) for sc, nl, hl in mhosts if hl in alive_h]

    for n, (u, sha, rel, size) in enumerate(sources, 1):
        print("\n[%d/%d] %s%s" % (n, len(sources), rel,
                                  "（约 %.2f GB）" % (size / 1e9) if size else ""))
        rf = {"ok": False, "path": None, "bytes": 0, "seconds": 0.0,
              "sha256": None, "verified": None, "error": None, "warning": None,
              "url": _redact_url(u)}   # 评审 A：files 元素带源 URL，失败/跳过的片才能溯源（v5.4 P2-12：userinfo 脱敏）
        dest = os.path.join(dir_abs, rel)
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        sa_mirror = None
        if mhosts:
            pu = urllib.parse.urlsplit(u)
            hl = (pu.hostname or "").lower()
            cands = ["%s://%s%s" % (sc, nl, pu.path)
                     for sc, nl, mh in mhosts if mh != hl]
            sa_mirror = cands or None    # 片与镜像站同 host：无候选=该片单源
        sa = argparse.Namespace(url=u, out=dest, conns=a.conns, chunk_mb=a.chunk_mb,
                                sha256=sha, proxy=getattr(a, "proxy", None),
                                no_proxy=getattr(a, "no_proxy", False),
                                mirror=sa_mirror,
                                _batch=True)   # v5.2 复审 P1：批量禁用裸文件改名
        try:
            download(sa, rf)
        except KeyboardInterrupt:
            rf["error"] = "interrupted"
            rf["error_msg"] = "已手动中断，进度已保存，重跑同一条命令可续传"[:200]
            res["files"].append(rf)
            journal_append(u, rf)
            res["bytes"] = sum(f["bytes"] or 0 for f in res["files"])
            res["seconds"] = time.time() - t0
            raise
        except Exception as e:
            rf["error"] = _fail_code(e)
            # v5.4 P2-10：批量收据补 error_msg——docstring/schema 承诺"失败带
            # error_msg"但只有顶层单文件有，批量恰是 agent 主用法
            rf["error_msg"] = _error_msg(e)
            print("  该分片失败（%s）：%s —— 已保留断点，继续下一片" % (rf["error"], str(e)[:120]))
        res["files"].append(rf)
        journal_append(u, rf)
        if rf["error"] == "disk":
            # 磁盘满是确定性失败：剩余分片的预检也会逐一撞墙，白烧几十次 HEAD
            # 探测。跳过剩余分片（未尝试的不进 files，不给它们编造结果）；
            # 清理空间后重跑同一条命令即可续传。
            print("\n磁盘空间不足，跳过剩余 %d 个分片；清理后重跑同一条命令即可续传"
                  % (len(sources) - n))
            break

    bad = [f for f in res["files"] if not f["ok"]]
    res["ok"] = not bad
    res["bytes"] = sum(f["bytes"] or 0 for f in res["files"])
    res["seconds"] = time.time() - t0
    res["error"] = bad[0]["error"] if bad else None
    # v5.4 P2-10：顶层 error_msg 取第一个失败片的人话（批量契约与单文件对齐）
    res["error_msg"] = next((f.get("error_msg") for f in bad if f.get("error_msg")),
                            None) if bad else None
    # v5.4 P2-9：片级 warning 现在可能是逗号复合（download 内 _warn 合并产物），
    # 合并时必须拆开去重，否则 "a,b"+"b,c" 会拼出重复项
    warns = sorted({w for f in res["files"] if f["warning"]
                    for w in f["warning"].split(",")})
    # 评审 A：以前只留第一个 warning，html_response 会把 proxy_fallback 吞掉，
    # 按 warning 分支的 agent 会漏判——现在去重全保留，逗号连接
    res["warning"] = ",".join(warns) if warns else None
    # v5.11 verified 三态聚合：null=全部片都未提供期望（无从校验）；false=任一片
    # 校验不符；true=给了期望的片全部通过（未给的片不拖累结论）
    vs = [f["verified"] for f in res["files"]]
    res["verified"] = (False if any(v is False for v in vs)
                       else True if any(v is True for v in vs) else None)
    print("\n批量结束：%d/%d 成功，共 %.2f GB，用时 %.0f 秒%s"
          % (len(res["files"]) - len(bad), len(res["files"]), res["bytes"] / 1e9,
             res["seconds"],
             "" if not bad else "；失败错误码：" + ",".join(sorted(set(f["error"] for f in bad)))))
    return 0 if not bad else 1


# ════════ v4.8 --serve 回环范围服务 v0（独立进程只读账本，迅雷 VodAgent 语义） ════════
# 方案 16：主下载路径零改动，--serve 是纯新增只读分支，不碰 sweep/双开锁/账本写入。
# 语义照迅雷定案：缺口 = 挂起等待而不是 404 不断连，播放器按标准 HTTP 顺序读，
# 完全感知不到"还在下载"这回事。

SERVE_POLL = 0.25          # 缺口挂起的账本轮询间隔（秒）
SERVE_GIVEUP = 300.0       # 账本 mtime 无变化且无字节流出连续超过该秒数即断开
                           # （下载已死/停止推进时的诚实行为；播放器超时自会重连）
SERVE_NOLEDGER = 60.0      # .part 在场但账本缺席的等待上限（刚开始下载/流式路径产物）
SERVE_READ = 128 << 10     # 单次磁盘读上限（照迅雷 128KB，绝不整块入内存）


class _ServeTarget:
    """一个下载器产物的只读快照。refresh() 重判状态机（方案 16.4）：

      final        最终文件在场且 .part 不在 -> 直接流式读最终文件（spans=None 全文连续）
      partial      .part + 账本都在 -> parts 块索引展开合并为连续完整区间列表
      waiting      .part 在、账本不在 -> 全文件视为缺口，等账本（SERVE_NOLEDGER 上限；
                   流式路径的产物没有账本，等满即断开，v0 不服务）
      gone         都不在 -> 404
      etag_changed 账本 etag 与首个快照不同（双存在且不等，照 _norm_etag 归一化）
                   -> 诚实断开（继续流就是混合体）

    快照读取不加锁：账本是 tmp+fsync+replace 的原子 JSON，json.load 拿到的永远是
    任一时刻的完整状态。已知不一致窗口（记档不修，方案 16.4）：重跑下载会 A3
    truncate .part，在途连接可能遇到短读——按缺口挂起重试即可，不会流出错误数据
    （同一 etag 下重下的字节与原来相同）。
    """

    def __init__(self, out):
        self.final = out
        self.part = out + ".part"
        self.donef = out + ".part.done"
        self.etag = None          # 首个有效账本快照的 etag（refresh 判定专用）
        # 复审 P3：多线程共享本对象，快照必须单属性原子替换——mode/size/spans/mtime
        # 分散赋值会留下混合态。读端经 property 解包，永远看到自洽的一份。
        self._snap = ("gone", 0, [], 0.0)   # (mode, size, spans, 账本mtime)
        self.refresh()

    @property
    def mode(self):
        return self._snap[0]

    @property
    def size(self):
        return self._snap[1]

    @property
    def spans(self):
        return self._snap[2]

    @property
    def mtime(self):
        return self._snap[3]

    def refresh(self):
        # v5.14：isfile 而非 exists——目标是目录时 exists 成立且 getsize 对目录
        # 也成功，会误判 final 态（boot 收据 complete:true + 服务线程空转）。
        has_final = os.path.isfile(self.final)
        has_part = os.path.exists(self.part)
        if has_final and not has_part:
            try:
                size = os.path.getsize(self.final)
                self._snap = ("final", size, None, self._snap[3])
            except OSError:
                self._snap = ("gone", 0, [], self._snap[3])
            return
        if has_part:
            mt = self._snap[3]
            if os.path.exists(self.donef):
                # 复审 P1：先 getmtime 门控——账本未变就不重读。既省掉活跃流出时
                # 每循环一次的 json.load，又把"账本读取窗口撞 write_ledger 的
                # replace"从每次轮询缩小到每次账本真实变化（下载端另有重试兜底）。
                try:
                    mt2 = os.path.getmtime(self.donef)
                except OSError:
                    mt2 = None
                if mt2 is not None and mt2 == mt and self._snap[0] == "partial":
                    return
                try:
                    with open(self.donef, encoding="utf-8") as f:
                        m = json.load(f)
                except Exception:
                    m = None      # 账本原子替换的瞬间/外部文件：按 waiting 等
                if (isinstance(m, dict) and m.get("v", 1) == 1
                        and m.get("chunk") and m.get("size")):
                    # v4.9：v!=1 的账本是不认识的格式（下载端会作废重建），serve 侧
                    # 按"账本不在"处理——waiting 等满 SERVE_NOLEDGER 诚实断开
                    et = m.get("etag")
                    if self.etag is None:
                        self.etag = et
                    elif (et is not None and self.etag is not None
                            and _norm_etag(et) != _norm_etag(self.etag)):
                        self._snap = ("etag_changed",) + self._snap[1:]
                        return
                    size, chunk = int(m["size"]), int(m["chunk"])
                    spans = []
                    for i in sorted(m.get("parts") or []):
                        s = i * chunk
                        if s >= size:
                            continue
                        e = min((i + 1) * chunk, size)
                        if spans and spans[-1][1] == s:
                            spans[-1][1] = e
                        else:
                            spans.append([s, e])
                    self._snap = ("partial", size, spans, mt2 or mt)
                    return
            self._snap = ("waiting", 0, [], mt)
            return
        self._snap = ("gone", 0, [], self._snap[3])

    def cover(self, pos, end):
        """请求窗口 [pos, end) 与已完整区间的交集（最长前段）；无交集返回 None。"""
        spans = self.spans        # 单次快照读取：两次属性读之间 _snap 可能从
        if spans is None:         # partial 翻到 final（None），迭代 None 会炸连接
            return (pos, end) if pos < end else None
        for s, e in spans:
            if e <= pos:
                continue
            if s >= end:
                break
            return max(s, pos), min(e, end)
        return None


class _ServeHandler(socketserver.BaseRequestHandler):
    """手写 HTTP/1.1（零依赖，头集照迅雷响应序列化器的最小集）。

    每请求一连接（Connection: close，不做 keep-alive，方案 16.1 定案）。
    头先发、body 按"已收到的连续完整区间"流出、缺口挂起轮询——播放器按标准
    HTTP 顺序读，完全感知不到下载的存在。
    """

    _REASON = {200: "OK", 206: "Partial Content", 403: "Forbidden",
               404: "Not Found",
               405: "Method Not Allowed", 416: "Range Not Satisfiable"}

    def handle(self):
        try:
            self.request.settimeout(30)
            req = self._read_request()
            if req:
                self._dispatch(*req)
        except OSError:
            pass                  # 客户端断开/读超时：正常（播放器 seek 就是断开重来）
        except Exception:
            pass                  # 二轮复审 P3：socketserver.handle_error 会往 stderr 打
                                  # traceback，长驻进程的噪音；单请求线程的任何异常都
                                  # 只影响本连接，连接随线程结束关闭，播放器自会重连

    # ---- 请求读取：读到头为止；body 不读（GET/HEAD 无 body，多余字节随断开丢弃） ----
    def _read_request(self):
        buf = b""
        while b"\r\n\r\n" not in buf:
            if len(buf) > 65536:
                return None
            try:
                d = self.request.recv(4096)
            except OSError:
                return None
            if not d:
                return None
            buf += d
        lines = buf.split(b"\r\n\r\n", 1)[0].decode("latin-1", "replace").split("\r\n")
        p = lines[0].split(" ")
        if len(p) < 3:
            return None
        rng = None
        host = None
        for ln in lines[1:]:
            k, _, v = ln.partition(":")
            kl = k.strip().lower()
            if kl == "range":
                rng = v.strip()
            elif kl == "host":
                host = v.strip().lower()
        return p[0].upper(), rng, host

    @staticmethod
    def _parse_range(rng, total):
        """Range 头解析（v4.9 按 RFC 7233 收紧语义，修订 16.3 冻结）。

        返回：None=无 Range / "ignore"=语法非法应忽略 / "bad"=unsatisfiable 走 416 /
        (起, 止)半开区间。

        支持 bytes=s-e / s- / -n 三形态（RFC 7233；-n 是 HTML5 video 常见的尾部
        探测）。右端超出 clamp 到 total。
        - ignore（忽略头回 200 全文件）：语法无法解析、多区间、倒序（s>e）——
          RFC 7233 只把"没有任何范围与资源重叠"留给 416，语法层面的问题按无效头
          处理；多区间另享防 DoS 条款（MAY ignore）的明文背书。
        - bad（416）：起点 >= total（含 bytes=300-320 of 300）、-0（空范围）、
          后缀/前缀区间在 total=0 的空文件上。
        """
        if not rng:
            return None
        m = re.fullmatch(r"bytes\s*=\s*(\d*)\s*-\s*(\d*)", rng.strip())
        if not m or not (m.group(1) or m.group(2)):
            return "ignore"           # 语法非法/多区间（含逗号，正则不匹配）
        if m.group(1):
            s = int(m.group(1))
            if m.group(2):
                e2 = int(m.group(2))
                if e2 < s:
                    return "ignore"   # 倒序：客户端 MUST NOT 发，按无效头处理
                e = e2 + 1
            else:
                if s >= total:
                    return "bad"      # s- 形态起点超界：unsatisfiable
                e = total
        else:
            n = int(m.group(2))
            if n <= 0:
                return "bad"          # -0：空范围 = unsatisfiable
            s, e = max(0, total - n), total
        e = min(e, total)
        return "bad" if s >= e else (s, e)

    def _dispatch(self, method, rng, host=None):
        # v5.16 P3：Host 头校验（DNS rebinding 防线）。默认只绑 127.0.0.1，
        # 合法 Host 只可能是 127.0.0.1[:port] / localhost[:port] / [::1][:port]
        # / 监听地址本身。攻击页让浏览器向攻击域名发请求、DNS 再绑定到本机，
        # 带异己 Host 的请求直接 403——下载产物的读取节奏不再可被外部驱动。
        # v5.16 收尾（r6）：绑外网（0.0.0.0/::/空）时跳过校验——局域网客户端
        # 的 Host 是各自真实 IP，白名单判不了；rebinding 只威胁浏览器可达的
        # loopback 绑定，外网绑定不存在该威胁，校验只会杀合法用户。
        bind = (self.server.server_address[0] or "").lower()
        if host is not None and bind not in ("0.0.0.0", "::", ""):
            if host.startswith("["):
                h = host[1:host.index("]")]   # [::1] / [::1]:8080
            elif host.count(":") == 1:
                h = host.partition(":")[0]    # 127.0.0.1[:port] / localhost[:port]
            else:
                h = host                      # 无端口裸主机名 / 裸 IPv6
            h = h.lower()
            if h not in ("127.0.0.1", "localhost", "::1", bind):
                # 人话提示：curl/浏览器用户能直接看懂为什么被拒（审查备注：
                # 403 静默无提示）。Content-Length 按 body 实长计。
                msg = ("Host 头不匹配本服务（DNS rebinding 防线），"
                       "请用 127.0.0.1[:port] 或监听地址访问\n").encode("utf-8")
                self._respond(403, [("Content-Type", "text/plain; charset=utf-8"),
                                    ("Content-Length", str(len(msg))),
                                    ("Connection", "close")])
                self.request.sendall(msg)
                return
        if method not in ("GET", "HEAD"):
            self._respond(405, [("Allow", "GET, HEAD"),          # 复审 P3：RFC 7231 MUST
                                ("Content-Length", "0"), ("Connection", "close")])
            return
        t = self.server.target
        t.refresh()
        if t.mode == "gone":
            self._respond(404, [("Content-Length", "0"), ("Connection", "close")])
            return
        if t.mode == "etag_changed":
            return                # 内容被换：诚实断开（继续流就是混合体）
        if t.mode == "waiting":
            # .part 在、账本不在：total 未知发不出合法头，先等账本到账再发头。
            # 流式路径的产物没有账本，等满 SERVE_NOLEDGER 断开，v0 不服务。
            dl = time.time() + SERVE_NOLEDGER
            while t.mode == "waiting" and time.time() < dl:
                if self._peer_gone(SERVE_POLL):    # v4.9：等账本期间客户端断了就走
                    return
                t.refresh()
            if t.mode == "gone":
                self._respond(404, [("Content-Length", "0"), ("Connection", "close")])
                return
            if t.mode in ("waiting", "etag_changed"):
                return
        total = t.size
        span = self._parse_range(rng, total)
        if span == "bad":
            self._respond(416, [("Content-Range", "bytes */%d" % total),
                                ("Accept-Ranges", "bytes"),
                                ("Content-Length", "0"), ("Connection", "close")])
            return
        if span == "ignore":
            span = None             # RFC 7233：语法非法/不支持形态 → 忽略头回 200 全文件
        start, end = (0, total) if span is None else span
        hdrs = [("Accept-Ranges", "bytes"),
                ("Content-Type", "application/octet-stream"),
                ("Content-Length", str(end - start)),
                ("Server", "Ampull/" + UA.rsplit("/", 1)[-1]),
                ("Connection", "close")]
        if span is not None:
            hdrs.insert(0, ("Content-Range", "bytes %d-%d/%d" % (start, end - 1, total)))
        self._respond(200 if span is None else 206, hdrs)
        if method != "HEAD":
            self._stream(t, start, end)

    def _peer_gone(self, timeout):
        """挂起等待的断连探测（v4.9 P0）：select 等 timeout 秒；可读则 peek。

        FIN（recv 返回空）或 RST（OSError）=客户端已死，调用方立即收线程。
        有数据可读（异常客户端在请求外发字节）不算断开，继续挂起交给 giveup。
        """
        try:
            r, _, _ = select.select([self.request], [], [], timeout)
        except (OSError, ValueError):
            return True             # socket 已失效：当断开处理
        if not r:
            return False
        try:
            return self.request.recv(1, socket.MSG_PEEK) == b""
        except OSError:
            return True             # RST

    def _respond(self, code, hdrs):
        out = ["HTTP/1.1 %d %s" % (code, self._REASON.get(code, "OK"))]
        out += ["%s: %s" % kv for kv in hdrs]
        self.request.sendall(("\r\n".join(out) + "\r\n\r\n").encode("latin-1"))

    def _stream(self, t, start, end):
        """body 流出：请求窗口与完整区间求交集逐段流出，缺口挂起轮询账本，补齐续流。

        放弃规则（方案 16.3）：账本 mtime 无变化且无字节流出连续超过 SERVE_GIVEUP
        秒即断开——下载已死/停止推进时的诚实行为，只防线程堆积；有任何进展即重置。
        """
        pos = start
        last = time.time()        # 上次进展时刻（流出字节或账本 mtime 变化）
        last_mtime = t.mtime
        f = None
        f_open = 0.0              # 当前句柄的打开时刻（持续流出时每秒让出，二轮复审 P2）
        final_short = 0           # final 态连续短读计数（二轮复审 P3）
        try:
            while pos < end:
                t.refresh()
                if t.mode in ("etag_changed", "gone"):
                    return        # 内容被换或文件被删：断开（诚实）
                if t.mode == "final":
                    last = time.time()   # 完成态没有缺口等待问题
                span = t.cover(pos, end)
                if span:
                    s, e = span
                    src = t.final if t.mode == "final" else t.part
                    try:
                        if f is None or getattr(f, "name", None) != src:
                            if f:
                                f.close()
                            f = open(src, "rb")
                            f_open = time.time()
                        f.seek(s)
                        n = e - s
                        sending = False      # OSError 分流：True=sendall（客户端断开）
                        while n > 0:
                            d = f.read(min(SERVE_READ, n))
                            if not d:
                                # 复审 P2：truncate 竞态的短读必须有节流，否则
                                # refresh+cover+read 空转热自旋（下载推进使 mtime
                                # 一直变，giveup 永不触发，单核烧数小时）。
                                # v4.9 复审：select 等价于 sleep 且顺带接住客户端
                                # FIN——断开即收线程，不留 4Hz 滞留连接
                                if self._peer_gone(SERVE_POLL):
                                    return
                                if t.mode == "final":
                                    # 二轮复审 P3：final 态文件不再增长，短读=实际
                                    # 内容比账本声称的短（外部截断/损坏），自愈等
                                    # 不到；完成态 giveup 被 last 重置永不触发，
                                    # 必须自己数。约 20 次 x 250ms = 5 秒诚实断开。
                                    final_short += 1
                                    if final_short >= 20:
                                        return
                                break   # truncate 竞态/文件未长齐：当缺口挂起重试
                            sending = True
                            self.request.sendall(d)
                            sending = False
                            pos += len(d)
                            n -= len(d)
                            last = time.time()
                            final_short = 0
                            if time.time() - f_open > 1.0:
                                # 二轮复审 P2：持续流出也要每秒真正 close 一次——
                                # 慢客户端回压使 sendall 以秒计，只有句柄真让出，
                                # 完成瞬间的 rename 才撞得上空闲窗口（挂起释放只
                                # 覆盖"缺口等待"场景，盖不住全速流出）
                                f.close()
                                f = None
                                break   # 回外层：span 重算后按需重开
                    except OSError:
                        if sending:
                            # sendall 失败=客户端已断（RST）：立即断开收线程，
                            # 否则 active 下载使 mtime 一直变，giveup 永不触发，
                            # 死连接线程会以轮询频率滞留到下载结束
                            return
                        # 磁盘抖动/杀毒扫描占用（open/seek/read）：当缺口挂起。
                        # v4.9 复审：select 替代 sleep——等待期间接住客户端 FIN
                        if f:               # 句柄状态不明：关闭，下次 span 重开
                            f.close()
                            f = None
                        if self._peer_gone(SERVE_POLL):
                            return
                else:
                    if f:
                        # 挂起期间主动释放 .part 只读句柄：下载完成瞬间的
                        # rename（_atomic_replace 重试窗口 10 秒）不该被"正挂着
                        # 等缺口"的连接白占（复审 P1 加强）
                        f.close()
                        f = None
                    if self._peer_gone(SERVE_POLL):
                        # v4.9 P0：挂起分支的断连探测。播放器 seek=断开重连，
                        # 不探测时死连接线程会以轮询频率滞留到缺口补齐
                        # （大文件=数小时）；select 接到 FIN/RST 立即收线程
                        return
                if t.mode == "partial" and t.mtime != last_mtime:
                    last_mtime, last = t.mtime, time.time()
                if pos < end and time.time() - last > SERVE_GIVEUP:
                    return              # 下载已停止推进：断开，播放器自会重连
        finally:
            if f:
                f.close()


def do_serve(a, res, real_out):
    """--serve 模式：回环 Range 服务（方案 16）。返回退出码，进程前台长驻。

    --json 语义特例（方案 16.2）：启动即输出一行 JSON 且此后不再输出第二行
    （Ctrl+C 退出码 130 也不发退出 JSON），区别于下载模式"stdout 只有最后一行
    JSON"。启动 JSON 必须走 real_out——main 的 B1 改道会把 sys.stdout 指到
    stderr，显式传真 stdout 才能让 agent 解析到。
    """
    if a.urls or a.manifest or a.repo or a.clean is not None:
        raise Fail("usage", "--serve 与下载/清理参数互斥（URL/--manifest/--repo/--clean）")
    if not (0 <= a.port <= 65535):
        # v5.7 复审 P2：端口越界时 socket 抛 OverflowError 不是 OSError，会落
        # internal 污染闭合词表——参数校验前置（不付网费），先于目标存在性检查
        raise Fail("usage", "--port 需在 0~65535（收到 %d）" % a.port)
    p = os.path.abspath(os.path.expanduser(a.serve))
    out = (p[:-len(".part.done")] if p.endswith(".part.done")
           else p[:-len(".part")] if p.endswith(".part") else p)
    # v5.14（P0）：目标是目录时 os.path.exists 对 out 成立、getsize 也成功，
    # _ServeTarget 会误判 final 态（boot 收据 complete:true，open(目录) 抛
    # OSError 被当磁盘抖动挂起，giveup 永不触发，线程 4Hz 空转）。前置拦截。
    if os.path.isdir(out):
        raise Fail("bad_path", "--serve 目标是目录而非下载产物：%s" % a.serve)
    t = _ServeTarget(out)
    if t.mode == "gone":
        raise Fail("bad_path", "目标不存在：%s（无 .part 也无最终文件）" % a.serve)

    class Srv(socketserver.ThreadingTCPServer):
        daemon_threads = True
        allow_reuse_address = True
    try:
        srv = Srv((a.host, a.port), _ServeHandler)
    except OSError as e:
        raise Fail("io", "监听失败 %s:%d：%s" % (a.host, a.port, e))
    srv.target = t
    port = srv.server_address[1]
    url = "http://%s:%d/" % (a.host, port)
    boot = {"ok": True, "mode": "serve", "path": out, "port": port, "url": url,
            "size": t.size, "complete": t.mode == "final",
            "warning": None if t.mode == "final" else "partial_content"}
    emit(boot, real_out)
    res["_booted"] = True       # 内部约定：启动 JSON 已发出，main 的 finally 不再 emit
    print("回环服务已启动 %s（Ctrl+C 停止）" % url)
    if t.mode != "final":
        print("下载进行中：未完成区间挂起等待；重跑下载会重置 .part 内容，"
              "在途客户端读到的数据以当时的账本为准")
    try:
        srv.serve_forever(poll_interval=0.5)
        return 0
    except KeyboardInterrupt:
        print("\n服务已停止")
        return 130
    finally:
        srv.server_close()


def _build_commands(Parser):
    """子命令参数表（v5.20）：反射自各子解析器，与主参数表同构。

    手写子命令表必然漂移（主表的教训：docstring 曾漏掉已上线的 cancelled），
    这里直接问解析器要 actions，单一事实来源。
    """
    out = {}
    try:
        from ampull_hls import cli as _sub_cli
    except Exception:
        return out
    for _name in ("hls", "tools"):
        try:
            _p = _sub_cli.build_parser(_name, Parser)
        except Exception:
            continue
        _items = []
        for _x in _p._actions:
            if _x.dest == argparse.SUPPRESS or _x.help == argparse.SUPPRESS:
                continue
            _items.append({
                "name": _x.dest,
                "flags": list(_x.option_strings),
                "type": getattr(_x.type, "__name__", None) if callable(_x.type) else _x.type,
                "nargs": _x.nargs,
                "default": None if _x.default is argparse.SUPPRESS else _x.default,
                "choices": list(_x.choices) if _x.choices else None,
                "help": " ".join((_x.help or "").split()),
            })
        out[_name] = {
            "prog": _p.prog,
            "description": " ".join((_p.description or "").split()),
            "arguments": _items,
        }
    return out


def build_schema(ap):
    """--schema：从运行中的 argparse 导出机器可读契约（单一事实来源）。

    静态接口卡必与代码漂移——docstring 的 error 词表就曾漏过已上线的 cancelled。
    参数表反射运行中的 parser，词表引用 ERROR_CODES 常量；agent 跑一次拿到全部
    调用契约（几百 token），无需读源码。"""
    args = []
    for x in ap._actions:
        # v5.7 复审 P3：意图是过滤被抑制参数， SUPPRESS 也可能出现在 help 上
        # （dest 检查原样保留，补 help 检查防 "==SUPPRESS==" 字面泄入契约）
        if x.dest == argparse.SUPPRESS or x.help == argparse.SUPPRESS:
            continue
        args.append({
            "name": x.dest,
            "flags": list(x.option_strings),
            "type": getattr(x.type, "__name__", None) if callable(x.type) else x.type,
            "nargs": x.nargs,
            "default": None if x.default is argparse.SUPPRESS else x.default,
            "help": " ".join((x.help or "").split()),
        })
    return {
        "tool": "AmPull",
        "version": UA.rsplit("/", 1)[-1],
        "json_fields": {
            "ok": "bool，是否成功",
            "path": "落盘绝对路径（批量=目标目录）",
            "bytes": "字节数",
            "seconds": "耗时秒",
            "sha256": "实际内容指纹（未计算时 null）",
            "verified": "指纹是否与期望一致（null=未核对：未提供期望或未走到校验；"
                        "true=一致；false=不符，见 verify_failed）。批量顶层=聚合："
                        "任一片 false 即 false，全片 null 则 null；成功与否仍以 ok "
                        "为先决",
            "error": "失败码，取值见 error_codes；null=成功",
            "error_msg": "仅失败时出现：错误原因人话一行（≤200 字符），兜底码"
                         "（internal 等）的排查线索",
            "warning": "html_response | proxy_fallback | gh_branch_fallback | "
                       "partial_content（--serve 启动 JSON）| "
                       "ffmpeg_missing | mux_skipped | playlist_refreshed | "
                       "duration_mismatch | workers_clamped | resume_forced | "
                       "quality_fallback（以上 7 个属 hls 子命令）| 逗号连接的多个；null=无",
            "files": "仅批量模式：每分片一个对象（同上字段+url 源链接；磁盘满跳过的分片不在其中）",
            "mirrors": "仅 --mirror 且有镜像入池时出现：多源池遥测 "
                       "[{host,url,bytes,fails,ewma}]，"
                       "含主源（host=主源），按源统计本次运行实下字节/失败数/块耗时 EWMA 秒；"
                       "批量模式出现在 files[] 每片的对象里",
            "clean": "仅 --clean 模式：收据形状不同——{ok, mode:\"clean\", roots, "
                     "counts{tmp,orphan_ledger,part,foreign,bad,active}, freed_bytes, "
                     "deleted[](最多200), kept[](最多50：{kind,path,bytes,age_days,url,"
                     "progress_pct,reason}), bad_dirs, error}；八字段下载收据（path/bytes/"
                     "sha256/…）不适用，--clean 参数报错时回退八字段外壳",
            "status": "仅 --status 模式：单路径收据形状——{ok, mode:\"status\", path, "
                      "state(active|resumable|done|foreign|missing), locked, size, "
                      "downloaded_bytes, progress_pct, speed_bytes_s(账本最近16次写入"
                      "窗口实测速度，仅 active/resumable；null=账本未生成/旧账本无采样/"
                      "窗口内无正增益段/断点窗口陈旧>120s), eta_s(仅 "
                      "active，按当前速度折算；正增益采样段<2时=null——单段外推"
                      "误导), url(脱敏), error}；多路径查询收据="
                      "{ok, mode:\"status\", count, results:[单路径收据...], error}；"
                      "state=missing/foreign 也是有效查询结果（ok=true，退出码 0）；"
                      "多路径中任一路径非法（URL/目录）→ 整查 usage 失败（响亮暴露"
                      "调用方 bug，不做逐项降级）；八字段下载收据不适用，--status "
                      "参数报错时回退八字段外壳",
            "hls": "仅 hls 子命令：{variant_url, quality, segments_total, "
                   "segments_done, segments_failed, segments_skipped_resume, "
                   "encrypted, key_count, muxed, container(mp4|ts), "
                   "duration_playlist, duration_media, parts_dir, ffmpeg_source, "
                   "install_command}；分片级明细不进收据（924 行逐片会刷爆管道），"
                   "排障用 --keep-parts 后看 parts 目录",
            "tools": "仅 tools 子命令：{mode:\"tools\", action(list|path|install|remove), "
                     "tool, version, path, url, sha256, bytes, smoke_test, "
                     "source(path|local|fresh), dry_run, plan, tools[]（list 动作："
                     "{tool, supported_platforms, installed[], in_use, source, "
                     "tools_root}）, removed[]/would_remove[]（remove 动作）}",
        },
        "commands": _build_commands(Parser),
        "error_codes": list(ERROR_CODES),
        "error_dynamic": "http_XXX（XXX=HTTP 状态码，如 http_403）",
        "exit_codes": {"0": "成功", "1": "失败", "130": "用户中断"},
        "arguments": args,
        "notes": [
            "URL 一律双引号（含 & 的链接不加引号：PowerShell 前台报错却丢后台，cmd 截断参数）",
            "flag 配对（v5.16 起 usage 阶段强制，错配直接 usage 拒绝不静默忽略）："
            "--include 必须配 --repo（整仓通配过滤只服务仓库模式）；-y/--all/"
            "--older-than 必须配 --clean（删除类 flag 只服务清理模式）；"
            "--mirror 只服务下载路径；--as 只用于单 URL 下载",
            "URL 与目标目录可与任意 flag 穿插混排；批量模式末尾本地路径=目标目录（不存在会创建）",
            "失败或中断后重跑同一条命令即可续传（已完成分片秒过）；重跑时不要改 -c/--chunk-mb，"
            "改了分块边界就对不上旧断点，会从头重下",
            "同名落盘保护（v5.2）：目标已存在且无本工具断点痕迹时，若下载日志里"
            "没有同一 URL 的成功记录（journal 被清或文件非本工具产出），自动改名"
            "另存 base(N).ext 而不是覆盖/误判完成；--as 指定文件名可避免撞名",
            "--repo 不给 --include 会整仓全下（几百 GB），先看清打印的清单再放行",
            "私有 HF 仓库/gated 模型加 --hf-token 或 --header \"Authorization: Bearer <token>\"",
            "进度与提示全走 stderr；--json 下 stdout 只有最后一行 JSON；分块下载 "
            "stderr 进度条空闲 3s 强制刷一帧（v5.12，不再随块静默数分钟）；真终端"
            "（isatty）随帧发 OSC 9;4 进度环（Windows Terminal/VS Code 1.97+ 的"
            "标签页进度环+任务栏进度，结束/失败即清），agent 管道 isatty=False "
            "恒不发；v5.15 起 stderr 接管道/重定向时进度自动切整行输出（5 秒一帧"
            "节流，首帧与 100% 必出），2> 日志可实时 tail 跟踪；"
            "v5.17 起管道/重定向（非 tty）的 stdout/stderr 字节恒 UTF-8"
            "（不随 PYTHONUTF8/PYTHONIOENCODING 漂移，捕获层按 UTF-8 解码；"
            "真终端跟随控制台代码页不变）；"
            "完成通知靠推送不靠轮询：下载命令一律前台非阻塞跑（--json，v5.18 约定）——"
            "宿主卡片实时流式进度给用户看，预计超 10 分钟也一样，平台 10 分钟超时"
            "强转后台时进程不死、退出推送照达；agent 启动后即返回做别的事，任务"
            "退出通知即完成信号（正常结束与进程暴毙都会退出），读收据核对 ok/error/warning "
            "再回报；--status <产物路径> 只在用户问进度或明显超时无推送时单查一次"
            "（<1s，只读可与下载并存，五态 active/resumable/done/foreign/missing，"
            "resumable=进程死了重跑原命令续传；v5.11 起支持一次多路径，收据 "
            "{count, results}）；任何 sleep 轮询循环/阻塞式等待"
            "一律禁止（前台阻塞会卡死会话）",
            "--status 状态语义：active=另一进程下载中（锁探测）；resumable=断点可续传"
            "（重跑原命令接上）；done=产物在场；foreign=.part 无本工具账本（可能是"
            "浏览器下载）不判不碰；missing=目标不存在。进度来自账本（Σ已收块），"
            ".part 文件大小是预分配不反映进度；速度来自账本最近 16 次写入采样"
            "（相邻正增益段累计；null=旧账本无采样/窗口无正增益段/断点窗口陈旧"
            ">120s，resumable 报的是断点前实测）；eta 按当前速度折算，正增益采样"
            "段 <2 时=null（单段外推误导，真机实锤起步 ETA 24 分钟实际 3.4 分钟）；"
            "状态人话行恒走 stderr（--json "
            "下 stdout 只有最后一行 JSON）",
            "--serve 模式 JSON 特例：启动即输出一行（ok/mode/path/port/url/size/"
            "complete/warning），进程保持前台，Ctrl+C 退出码 130，退出不再输出"
            "第二行 JSON；未完成区间挂起等待而非 404，可边下边用；下载停止推进"
            "超 300 秒连接会被服务端断开（重连即可续流）；Range 按 RFC 7233——"
            "非法/多区间/倒序回 200 全文件，416 只用于起点超界",
            "HLS（v5.20）：`ampull.py hls <m3u8链接> <输出目录>`——VOD + 明文 TS + "
            "AES-128，分片级并行 + 断点续传 + 可选 mp4 封装；fMP4 / EXT-X-BYTERANGE / "
            "多 KEY 轮换 / DRM / 直播一律明确拒绝（不出错片）。缺 ffmpeg 时落 .ts 并打 "
            "warning=ffmpeg_missing，可用 `ampull.py tools install ffmpeg --yes` 自举"
            "（默认 dry-run，需用户同意；只写本地 tools 目录，不改 PATH）",
            "子命令（hls/tools）必须写在第一个位置参数：`ampull.py hls \"<url>\" <目录> --json`；"
            "子命令参数表见 schema.commands；它们不与主参数（--manifest/--repo/--serve/"
            "--clean/--status）混用，混用会报 unrecognized arguments",
            "--mirror 多源池：候选并行探测（每候选单请求 20s 超时，通道失败切"
            "备用一次）后按双门筛选入池——"
            "大小与主源一致+支持 Range；ETag 与主源不一致时必须有 --sha256 兜底"
            "否则该镜像出局（镜像内容可能不同，混用风险由终检兜底）。镜像请求"
            "自动剥离 Authorization/Cookie（凭据只发主源）。--mirror @文件 每行"
            "一个 URL（# 注释）适合整仓复用；--mirror @auto 读运行历史作候选"
            "（默认关；历史是候选不是信任清单，照常过双门）。运行历史落盘 "
            "与 ampull.py 同目录的 mirrors.jsonl 供 agent 跨会话"
            "学习镜像质量；镜像请求（探测与分块）按镜像自己的域名独立分流，"
            "主源走代理时国内镜像仍直连；批量模式同样支持（--mirror 解析为镜像站"
            " host，逐片替换路径建池，query 不跨站携带）",
            "GitHub 归档 404 自动分支回退（v5.18）：github.com 的 archive 链接"
            "（archive/refs/heads|tags/<ref>.zip 及 legacy 短形态）与 codeload "
            "直链撞 404 时，自动查仓库默认分支（api.github.com）并尝试常见分支名"
            "（master/main/develop）+ codeload 直链形态，命中即改用新 URL 继续"
            "下载（warning=gh_branch_fallback，stderr 打印改用后的链接）；"
            "gh-proxy 等包装前缀保留不动；全败仍报 http_404。仅 404 触发，"
            "403/401（鉴权/防盗链）不回退",
        ],
    }


def _parse_sub(sub, json_mode):
    """解析 hls / tools 子命令（独立 Parser；主 Parser 不参与）。

    v5.20：因为主解析用 parse_intermixed_args()（与 subparsers 不兼容），
    子命令走自己的解析器。返回的 namespace 需补齐主流程后面会无条件读取的
    属性（urls/serve/clean/status/... 等），否则 AttributeError。
    """
    try:
        from ampull_hls import cli as _sub_cli
    except ImportError as _e:
        # 单文件形态（只拷了 ampull.py）：给干净收据，不要 traceback + 零 JSON
        _msg = "子命令需要 ampull_hls/ 子包与 ampull.py 同目录（当前缺失：%s）" % _e
        print(_msg, file=sys.stderr)
        if json_mode:
            emit({"ok": False, "path": None, "bytes": 0, "seconds": 0.0,
                  "sha256": None, "verified": None, "error": "bad_args",
                  "error_msg": _msg, "warning": None}, sys.stdout)
        raise SystemExit(1)
    sap = _sub_cli.build_parser(sub, Parser, json_mode=json_mode)
    # help 必须在 parse 之前拦：位置参数是必填项，argparse 会先报缺参再轮到 help
    if any(x in ("-h", "--help") for x in sys.argv[1:]):
        sap.print_help(sys.stderr)
        if json_mode:
            emit({"ok": False, "path": None, "bytes": 0, "seconds": 0.0,
                  "sha256": None, "verified": None, "error": "help",
                  "error_msg": "子命令帮助已打印到 stderr", "warning": None},
                 sys.stdout)
            raise SystemExit(1)
        raise SystemExit(0)
    try:
        a = sap.parse_intermixed_args()
    except SystemExit as e:
        if e.code in (0, None):
            raise
        if json_mode:
            emit({"ok": False, "path": None, "bytes": 0, "seconds": 0.0,
                  "sha256": None, "verified": None, "error": "usage",
                  "error_msg": getattr(sap, "_cli_err", None)
                  or "参数解析失败：未知 flag 或非法值（详见 stderr）",
                  "warning": None}, sys.stdout)
            raise SystemExit(1)
        raise
    if getattr(a, "help", False):
        # 与主命令同契约：JSON 模式下 help 走 stderr + 单行收据（error=help），
        # 退出码 1；非 JSON 模式正常打印 help、退出码 0
        sap.print_help(sys.stderr)
        if json_mode:
            emit({"ok": False, "path": None, "bytes": 0, "seconds": 0.0,
                  "sha256": None, "verified": None, "error": "help",
                  "error_msg": "子命令帮助已打印到 stderr", "warning": None},
                 sys.stdout)
            raise SystemExit(1)
        raise SystemExit(0)
    # 主流程后续（互斥校验链 / finally 记账）会无条件读这些属性
    for _k, _v in (("urls", []), ("manifest", None), ("repo", None),
                   ("serve", None), ("clean", None), ("status", None),
                   ("mirror", None), ("as_name", None), ("include", None),
                   ("yes", False), ("all", False), ("older_than", None),
                   ("sha256", None), ("help", False), ("header", None),
                   ("hf_token", None)):
        if not hasattr(a, _k):
            setattr(a, _k, _v)
    return a


def main():
    JSON_MODE = "--json" in sys.argv

    # ---- 输出编码契约（v5.17 治本）：管道/重定向恒 UTF-8，真终端跟随控制台。
    #      旧 C1 只钉 errors 不动 encoding，输出字节随 PYTHONUTF8/PYTHONIOENCODING
    #      漂移——同一台机器，注入环境变量的宿主吐 UTF-8、干净环境吐 GBK，按系统
    #      代码页（936）解码的捕获层见谁错谁（v5.17 前实测：同行中文两种字节）。
    #      字节层钉死 UTF-8（agent 生态事实标准）+ 契约写进 --schema notes，生产/
    #      消费两张皮才合得上。TTY 不动 encoding——GBK 控制台按 ACP 解码自洽，
    #      改 utf-8 反而人眼乱码（旧 C1 教训对 TTY 仍然成立）。JSON 收据
    #      ensure_ascii 恒纯 ASCII，任何解码下不受影响 ----
    for _s in (sys.stdout, sys.stderr):
        try:
            if _s.isatty():
                _s.reconfigure(errors="replace")
            else:
                _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    # ---- v5.20 子命令分流（hls / tools）：必须在主 Parser 构建前剥掉 token。
    #      不能用 add_subparsers：主解析走 parse_intermixed_args()，CPython 对
    #      nargs=PARSER 直接 TypeError（Lib/argparse.py parse_known_intermixed_args）。
    #      改成"前置剥离 + 独立子解析器"后，Fail 捕获 / emit / journal / 退出码
    #      全部沿用主流程，子命令也不污染全局 --schema 的参数表。
    _SUB = (sys.argv[1] if len(sys.argv) > 1
            and sys.argv[1] in ("hls", "tools") else None)
    if _SUB:
        sys.argv.pop(1)
    elif len(sys.argv) > 2 and sys.argv[1].startswith("-") \
            and sys.argv[2] in ("hls", "tools"):
        # 子命令写在了 --json 之类的 flag 后面：明确报错，不要静默退化成批量
        # 下载（agent 会看到 files:[{url:"hls"},...] 这种莫名其妙的收据）
        _m = "子命令必须写在第一个位置参数：ampull.py hls <m3u8链接> <输出目录>"
        print(_m, file=sys.stderr)
        if JSON_MODE:
            emit({"ok": False, "path": None, "bytes": 0, "seconds": 0.0,
                  "sha256": None, "verified": None, "error": "usage",
                  "error_msg": _m, "warning": None}, sys.stdout)
        return 1

    # ---- 会审 P2 allow_abbrev=False：argparse 默认认前缀缩写（--js → --json），
    # 但 JSON_MODE 是字面量扫描——缩写会全程无 JSON 输出，agent 解析必失败
    ap = Parser(allow_abbrev=False,
                description="AmPull（Agent Multi-link Downloader，agent 多链接下载器）：16 连接并发 + 断点续传 + 分片批量（多链接 / --manifest / --repo）",
                formatter_class=argparse.RawDescriptionHelpFormatter,
                epilog="中断后重新运行同一条命令即可续传；批量模式下已完成的分片会直接跳过。",
                add_help=not JSON_MODE)
    if JSON_MODE:
        ap.add_argument("-h", "--help", action="store_true", help="显示帮助")
    ap.add_argument("urls", nargs="*", metavar="链接",
                    help="下载链接（--clean 模式可省略）。给多个链接=分片批量，"
                         "此时末尾的本地路径是目标目录（不存在会创建）")
    ap.add_argument("-c", "--conns", type=int, default=16, help="连接数（默认 16，1~64）")
    ap.add_argument("--sha256", help="可选：下载后校验的 SHA256 指纹（批量模式不支持，"
                                     "逐片校验用 --manifest 或 --repo）")
    ap.add_argument("--manifest", metavar="清单",
                    help="批量：清单文件，每行 URL [sha256]，# 是注释")
    ap.add_argument("--repo", metavar="仓库",
                    help="批量：HuggingFace 仓库名或页面链接，自动列出文件逐片下载"
                         "（API 走 HF_ENDPOINT，默认 hf-mirror.com；LFS 文件自动带 sha256 校验）")
    ap.add_argument("--include", action="append", metavar="通配",
                    help="--repo 的路径过滤，可多次（如 --include \"UD-IQ4_XS/*.gguf\"；"
                         "不给=全部文件，当心整仓几百 GB）")
    ap.add_argument("--proxy", metavar="URL",
                    help="强制走指定 HTTP 代理（如 http://127.0.0.1:7890）；"
                         "默认自动检测（r38 多源候选依次尝试：扫描口[双跳探针"
                         "验证境外连通性，当日复用零重扫]> 环境变量 > 系统代理"
                         "注册表 > 直连）")
    ap.add_argument("--reset-proxy", action="store_true",
                    help="清除代理扫描当日标记（scan_ok/fail/hints），下次"
                         "需要代理时强制重新扫描——换代理端口/换客户端后用")
    ap.add_argument("--no-proxy", action="store_true",
                    help="禁用一切代理（含环境变量与自动检测），强制直连")
    ap.add_argument("--header", action="append", metavar="K:V",
                    help="附加 HTTP 请求头（可多次），如 --header \"Authorization: Bearer xxx\""
                         " 或 --header \"Referer: https://example.com/page\"；"
                         "User-Agent 可覆盖内置值；Range/Host/Content-Length/Connection/"
                         "Accept-Encoding 是工具保留头，传入直接报错；"
                         "凭据不跨 host：请求目标（含 302 落点、镜像站）host 与原始"
                         " URL host 不同时按白名单剥离，只保留 Accept/"
                         "Accept-Language/Referer/User-Agent/Authorization/Range，"
                         "Cookie 与 X-Api-Key 等一律不发往异己 host")
    ap.add_argument("--as", dest="as_name", metavar="名字",
                    help="指定落盘文件名（仅单 URL 下载；配合目录输出路径或省略"
                         "输出路径）。不指定时按 Content-Disposition/URL 自动取名")
    ap.add_argument("--hf-token", metavar="TOKEN",
                    help="HuggingFace 访问令牌（糖）：等价 --header \"Authorization: Bearer TOKEN\"，"
                         "--repo 模式的清单 API 同样携带")
    ap.add_argument("--chunk-mb", type=int, default=CHUNK >> 20, help="分块大小 MB（默认 16，1~512）")
    ap.add_argument("--mirror", action="append", metavar="URL|@文件|@auto",
                    help="镜像候选（可多次）：URL 直接给；@文件 每行一个 URL（# 注释），"
                         "适合 --repo 一次写整仓复用；@auto 读本工具的运行历史"
                         " 与 ampull.py 同目录的 mirrors.jsonl（默认关）。"
                         "候选并行探测后按双门筛选：大小一致+支持 Range 才入池，"
                         "ETag 与主源不一致时需 --sha256 兜底；凭据只发主源，"
                         "镜像请求自动剥离 Authorization/Cookie；镜像请求按镜像"
                         "自己的域名走代理分流（主源走代理时国内镜像仍直连）；"
                         "批量模式下规格解析为镜像站 host，逐片替换路径建池")
    ap.add_argument("--serve", metavar="路径",
                    help="回环 Range 服务：对本工具的下载产物（.part 断点或最终文件，"
                         "给最终名自动识别）提供 HTTP 文件服务，未完成区间挂起等待，"
                         "播放器/校验工具可边下边用；进程前台长驻，Ctrl+C 停止")
    ap.add_argument("--port", type=int, default=0,
                    help="--serve 的监听端口（默认 0=临时端口）")
    ap.add_argument("--host", default="127.0.0.1",
                    help="--serve 的监听地址（默认 127.0.0.1，改绑外网需显式指定）")
    ap.add_argument("--json", action="store_true", help="stdout 只输出一行 JSON 结果")
    ap.add_argument("--schema", action="store_true",
                    help="输出机器可读契约 JSON（参数表/JSON 字段/error 词表/避坑要点）"
                         "后退出；agent 用它拿调用契约，无需读源码")
    ap.add_argument("--clean", nargs="*", metavar="目录",
                    help="扫描/清理下载残留（不给目录则按下载日志记录的位置）。默认只列不删")
    ap.add_argument("-y", "--yes", action="store_true",
                    help="--clean 时真正删除（孤儿账本与残片；断点还需 --all）")
    ap.add_argument("--all", action="store_true", help="--clean -y 时连断点 .part 一起删")
    ap.add_argument("--older-than", metavar="时长",
                    help="--clean 只删超过该年龄的（如 7d / 24h / 30m）")
    ap.add_argument("--status", nargs="+", metavar="路径",
                    help="查询下载目标状态（只读）：给产物路径（.part/.part.done/"
                         ".part.lock 形态自动归一），返回 state=active|resumable|"
                         "done|foreign|missing 与进度（active/resumable 含 "
                         "speed_bytes_s 实测速度，active 另含 eta_s（正增益采样"
                         "段<2 时=null））；可一次给"
                         "多个路径（收据 {count, results:[...]}）；可与下载进程"
                         "并存；state=missing 也是有效结果（退出码 0）")
    # ---- 评审 A 实锤：nargs="*" 被 flag 打断后，argparse 不再吞后续位置参数——
    #      "u1 u2 --header X 目录"（目录放最后的 Unix 惯例）会报 unrecognized。
    #      parse_intermixed_args 专为 * 与 flag 混排设计，URL/目录与 flag 可任意穿插 ----
    if _SUB:
        a = _parse_sub(_SUB, JSON_MODE)
    elif JSON_MODE:
        # v5.7 复审 P1：argparse 自身错误（未知 flag/--conns abc）抛 SystemExit(2)，
        # 击穿 exit_codes={0,1,130} 契约且 stdout 零 JSON。转标准 usage 收据，
        # 退出码归 1；argparse 的错误说明已自带打到 stderr。--help 的 exit 0 放行。
        try:
            a = ap.parse_intermixed_args()
        except SystemExit as e:
            if e.code in (0, None):
                raise
            # v5.9 P3：唯一 JSON 收据出口（Parser.error 已不再自打）。argparse
            # 的 message 存在 parser 实例 _cli_err 上，优先透传；其余路径回落
            # 静态文案。
            msg = getattr(ap, "_cli_err", None) \
                or "参数解析失败：未知 flag 或非法值（详见 stderr）"
            print(json.dumps({"ok": False, "path": None, "bytes": 0, "seconds": 0.0,
                              "sha256": None, "verified": None, "error": "usage",
                              "error_msg": msg,
                              "warning": None}, ensure_ascii=True))
            raise SystemExit(1)
    else:
        a = ap.parse_intermixed_args()

    # ---- v4.4 --schema：机器可读契约。放在 JSON_MODE 改道 stdout 之前，输出走
    #      真实 stdout；与 --json 同给时 --schema 优先 ----
    if getattr(a, "schema", False):
        print(json.dumps(build_schema(ap), ensure_ascii=True))
        return 0

    # ---- r38 --reset-proxy：清代理扫描当日标记（换代理端口/换客户端后用）----
    if getattr(a, "reset_proxy", False):
        st = _state_load()
        for _k in ("scan_ok_date", "scan_fail_date", "scan_proxy",
                   "socks_hint", "http_hint"):
            st.pop(_k, None)
        _state_save(st)
        print("  代理扫描当日标记已清——下次需要代理时将重新扫描")

    res = {"ok": False, "path": None, "bytes": 0, "seconds": 0.0,
           "sha256": None, "verified": None, "error": None, "warning": None}
    clean_res = None
    status_res = None
    real_out = sys.stdout
    code = 1

    # ---- B1 一行改道：既有 print 全部自动走 stderr，stdout 留给唯一那行 JSON ----
    if JSON_MODE:
        sys.stdout = sys.stderr
    try:
        # ---- v4.5 鉴权注入：放 try 内，_parse_headers 的 Fail 才能收敛进词表 ----
        EXTRA_HEADERS.update(_parse_headers(a.header))
        if a.hf_token:
            # v5.9 P2（zcode 审查）：--hf-token 直写 EXTRA_HEADERS 绕过
            # _parse_headers 的全部校验——token 混入 \r\n 即头注入。鉴权值
            # 实践中必为 ASCII 可打印字符，从严校验。
            if not a.hf_token.isascii() or any(ord(c) < 32 or ord(c) == 127
                                               for c in a.hf_token):
                raise Fail("bad_args", "--hf-token 含控制字符或非 ASCII 字符")
            if "Authorization" in EXTRA_HEADERS:
                print("--hf-token 覆盖了 --header 里的 Authorization（--hf-token 优先）")
            EXTRA_HEADERS["Authorization"] = "Bearer " + a.hf_token
        # ---- v5.16 P3：错误模式下被静默忽略的 flag 显式拦截（工具自己的
        #      哲学：静默丢弃=坑 agent）。仿 --as/--status 先例，usage 不付网费 ----
        _clash = []
        if a.include and not a.repo:
            _clash.append("--include 需要 --repo（整仓通配过滤只服务仓库模式）")
        for fl, v in (("-y/--yes", a.yes), ("--all", a.all),
                      ("--older-than", a.older_than)):
            # v5.20：子命令（tools install/remove 的 --yes）不消费 --clean
            if v and a.clean is None and not _SUB:
                _clash.append(fl + " 需要 --clean（删除类 flag 只服务清理模式）")
        if _clash:
            raise Fail("usage", "；".join(_clash))
        # ---- v5.0 --mirror 模式约束：行为必须可见，不支持的模式直接报 usage ----
        # v5.10：manifest/repo×mirror 转正（批量每片各自建池），拦截只剩
        # serve/clean——均不付网费，无需 _plan 前置
        if getattr(a, "mirror", None) and (a.serve or a.clean is not None):
            raise Fail("usage", "--mirror 只服务下载路径；--serve/--clean 不消费镜像参数")
        # ---- v5.2 --as 模式约束（dogfood 反馈③）：只服务单 URL 下载 ----
        # manifest/repo 前置（P2-5 教训：_plan 会发网络请求，usage 不该付网费）
        if getattr(a, "as_name", None) and (a.serve or a.clean is not None
                                            or a.manifest or a.repo):
            raise Fail("usage", "--as 只用于单文件下载；--serve/--clean/"
                                "--manifest/--repo 不消费 --as")
        # ---- v5.10 --status 模式约束：只读查询，不消费任何下载/清理参数 ----
        if getattr(a, "status", None):
            clash = (["下载链接"] if a.urls else []) + \
                    [fl for fl, v in (("--manifest", a.manifest), ("--repo", a.repo),
                                      ("--serve", a.serve), ("--clean", a.clean is not None),
                                      ("--mirror", getattr(a, "mirror", None)),
                                      ("--as", getattr(a, "as_name", None)),
                                      ("--sha256", a.sha256)) if v]
            if clash:
                raise Fail("usage", "--status 是只读查询，与 %s 互斥" % "、".join(clash))
        if _SUB:
            # ---- v5.20：hls / tools 走各自实现；收据仍由本函数的 finally emit ----
            try:
                from ampull_hls import cli as _sub_cli
            except ImportError as _e:
                raise Fail("bad_args",
                           "hls/tools 子命令需要 ampull_hls/ 与 ampull.py 同目录：%s"
                           % _e)
            code = _sub_cli.run(_SUB, a, res, {
                "Fail": Fail,
                "Parser": Parser,
                "extra_headers": EXTRA_HEADERS,
                "resolve_ops": _resolve_ops,
                "user_agent": UA,
                # 默认 opener 必须带 _SafeRedirect：跨 host 302 时剥离凭据
                "default_opener": _DEFAULT_OPENER,
                "json_mode": JSON_MODE,
            })
        elif JSON_MODE and getattr(a, "help", False):
            ap.print_help(sys.stderr)
            res["error"] = "help"
            code = 1
        elif getattr(a, "status", None):
            # ---- v5.10 --status：只读查询，优先级 --schema > --status > --serve > --clean > 下载 ----
            code, status_res = do_status(a)
        elif a.serve:
            # ---- v4.8 --serve：优先级 --schema > --serve > --clean > 下载 ----
            code = do_serve(a, res, real_out)
        elif a.clean is not None:
            code, clean_res = do_clean(a)
        else:
            # B-P2-2：--mirror 规格本地校验前置（@文件读失败/裸规格早报 bad_args），
            # 不付主源 HEAD/仓库清单的网费，也不留 makedirs 空目录
            if getattr(a, "mirror", None):
                _load_mirror_urls(a.mirror)
            sources, out_dir, single = _plan(a)
            if single:
                a.url, a.out = single, out_dir
                # 复审 P2：显式文件路径×--as 在 download() 之前拦（单 URL 的 _plan
                # 只做本地解析不付网费；此前放 main 前置区会撞 a.out 未推导）
                if (getattr(a, "as_name", None) and a.out
                        and a.out[-1] not in "\\/" and not os.path.isdir(a.out)):
                    raise Fail("usage", "--as 与显式输出文件路径二选一：--as 配合目录，"
                                        "或省略输出路径（默认当前目录）")
                code = download(a, res)
            else:
                # ---- v5.2：多位置参数批量同样不支持 --as（清单/repo 已在 _plan
                # 前拦）。落盘名来自 URL，--as 没有单一指派对象，静默忽略会坑 ----
                if getattr(a, "as_name", None):
                    raise Fail("usage", "批量模式（多 URL）不支持 --as——每个文件"
                               "的落盘名各不相同，请逐片单独下载")
                code = do_batch(sources, out_dir, a, res)
    except Fail as e:
        res["error"] = e.code
        # ---- v5.2 dogfood 反馈①：兜底码对 agent 是死胡同，人话截断一行随收据走
        #      （v5.4 P2-10：六分支人话抽成 _error_msg，与批量片级共用单源）----
        res["error_msg"] = _error_msg(e)
        print("\n下载失败：%s" % e)
        code = 1
    except KeyboardInterrupt:
        res["error"] = "interrupted"
        # v5.4 复审 P3：批量 KI 时片级已带 error_msg，顶层对齐（P2-10 契约）
        res["error_msg"] = "已手动中断，进度已保存，重跑同一条命令可续传"[:200]
        print("\n已手动中断，进度已保存，重新运行同一条命令即可续传。")
        code = 130
    except urllib.error.HTTPError as e:
        res["error"] = "http_%d" % e.code
        res["error_msg"] = _error_msg(e)
        print("\n下载失败：服务器拒绝（HTTP %d %s）。链接可能已过期、需要鉴权"
              "（私有资源可加 --hf-token 或 --header \"Authorization: Bearer <token>\"），"
              "或该地址不允许直接下载" % (e.code, e.reason))
        code = 1
    except urllib.error.URLError as e:
        res["error"] = "network"
        res["error_msg"] = _error_msg(e)
        print("\n下载失败：网络错误 %s（请检查网络、代理或证书）" % e.reason)
        code = 1
    except OSError as e:
        # 本地 I/O（写账本、truncate、改名等）也要收敛成词表里的码，
        # 否则 PermissionError 这类类名会直接泄漏进 JSON。
        # 磁盘写满要单独报 disk，别混进笼统的 io。
        if _is_disk_error(e):
            res["error"] = "disk"
            res["error_msg"] = _error_msg(e)
            print("\n下载失败：磁盘写入失败（空间不足？）%s" % e)
        else:
            res["error"] = "io"
            res["error_msg"] = _error_msg(e)
            print("\n下载失败：本地文件操作出错 %s: %s（检查磁盘空间、权限，"
                  "或是否有别的程序占用同名文件）" % (type(e).__name__, e))
        code = 1
    except BaseException as e:
        # ---- 会审 P1 词表闭合：兜底码固定为 internal，类名只进 stderr 人话里。
        # UnicodeEncodeError / ValueError 这种类名会让按词表分支的 agent 落空。
        res["error"] = "internal"
        res["error_msg"] = _error_msg(e)
        print("\n下载失败：%s: %s" % (type(e).__name__, e))
        code = 1
    finally:
        # ---- B1 emit 必须在 finally 里无条件执行一次：漏掉任何一条退出路径，
        #      agent 拿到的是「退出码 0 + 无输出」，比没有 --json 更坏 ----
        sys.stdout = real_out
        try:
            sys.stderr.flush()
        except Exception:
            pass
        if JSON_MODE:
            # ---- --serve 启动 JSON 已在 do_serve 里发过（方案 16.2 特例：启动后
            #      不再有第二行 JSON，Ctrl+C 也不发退出 JSON），其余模式无条件 emit ----
            if not res.get("_booted"):
                emit(status_res if status_res is not None
                     else (clean_res if clean_res is not None else res), real_out)
        # ---- v3.1 下载日志：--clean 与 --json -h 不记；其余每次调用都记一笔
        #      （批量模式没有 a.url，逐片的记录在 do_batch 里已各记一行）----
        if a.clean is None and getattr(a, "url", None):
            journal_append(a.url, res)
    return code


if __name__ == "__main__":
    _code = main()
    # ---- 会审 P1 进程滞留：失败/中断后线程池里可能还有 worker 卡在最长 120s 的
    # socket 读上，非守护线程让进程吊着不退——JSON 早已写出，agent 却拿不到返回
    # 码，只能干等外层超时（三个评审独立命中同一条）。输出已全部 flush，直接硬退。
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(_code)
