# -*- coding: utf-8 -*-
"""hls_mux —— 拼接与封装（设计 §7.5）

有 ffmpeg：TS 直接字节拼接 → `-c copy` 封 mp4（不重编码，秒级）。
无 ffmpeg：保留 .ts，返回 warning=ffmpeg_missing（不失败，零依赖底线）。
"""

import os
import re
import shutil
import subprocess
import sys
import time

from .parse import HlsError

_ENV_KEY = "AMPULL_FFMPEG"


def program_dir():
    """程序目录 = ampull.py 所在目录（= ampull_hls 的上一级）。

    刻意不用 %LOCALAPPDATA% / ~ 这类绝对路径：AmPull 是"整包拷走就能用"的
    便携工具，依赖也应当跟着程序走（v5.20 用户裁决 2026-10-26）。
    """
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def tools_dir(tool="ffmpeg"):
    """依赖落盘根：<程序目录>/tools/<tool>（相对程序、可整包搬迁）。"""
    return os.path.join(program_dir(), "tools", tool)


def _local_tools_dirs():
    """本地 tools 目录（设计 §8.2）：不污染 PATH、不需要管理员、不写用户家目录。"""
    return [tools_dir("ffmpeg")]


def _ver_key(name):
    """版本目录名自然排序：'8.1' < '10.0'（纯字符串排序会反过来）。"""
    parts = re.split(r"[.\-_]", name)
    return tuple(int(p) if p.isdigit() else p for p in parts)


def _scan_tools_dir(root):
    """在 <tools>/ffmpeg/<version>/bin/ffmpeg(.exe) 里找最新的可用版本。"""
    if not os.path.isdir(root):
        return None
    exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    candidates = []
    for name in sorted(os.listdir(root), key=_ver_key, reverse=True):
        p = os.path.join(root, name, "bin", exe)
        if os.path.isfile(p):
            candidates.append(p)
    return candidates[0] if candidates else None


def _env_ffmpeg_dirs():
    """AMPULL_FFMPEG_DIRS：让用户直接指别处（别的程序的 ffmpeg），`;`/`:` 分隔。"""
    raw = os.environ.get("AMPULL_FFMPEG_DIRS") or ""
    out = []
    for part in raw.replace(";", os.pathsep).split(os.pathsep):
        part = part.strip().strip('"')
        if part and os.path.isdir(part):
            out.append(part)
    return out


def _candidates_in(dirs):
    """在给定目录里找 ffmpeg：目录本身 + 其 bin/ 子目录。"""
    exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    out = []
    for d in dirs:
        for cand in (os.path.join(d, exe), os.path.join(d, "bin", exe)):
            if os.path.isfile(cand):
                out.append(cand)
    return out


# 发现扫描的根：只扫"程序装在哪"的三处 + Python 包自带的 imageio-ffmpeg
_DISCOVERY_MAX_DEPTH = 4      # 够到 App/tools/ffmpeg/bin/ffmpeg.exe 这种四层布局
_DISCOVERY_DEADLINE = 3.0


def _discovery_roots(platform=None, program_parent=None):
    """发现扫描根（v2.5）：

    1. **兄弟程序目录**——程序所在目录的上一级（如 Desktop），深度 ≤3。
       实测价值：桌面 `ALrec/tools/ffmpeg/bin/ffmpeg.exe`（217MB，9.0.2 可用）
       就在这层，直接借用即可，不必再拷一份 200MB。
    2. Windows 安装根：ProgramFiles / ProgramFiles(x86) / LOCALAPPDATA\\Programs。
    3. macOS：/opt/homebrew/bin、/usr/local/bin、/Applications、~/Applications
       （mac 上 ffmpeg 一般来自 brew，装完就在 PATH，但 GUI 启动的进程 PATH 可能不全，
        所以这里显式列出来做兜底）。
    4. Python 的 imageio-ffmpeg 包目录（自带静态二进制）。

    只返回路径字符串，是否真实存在交给扫描时判断（便于单测）。
    """
    platform = platform or sys.platform
    roots = []
    parent = program_parent
    if parent is None:
        parent = os.path.dirname(program_dir())
    if parent:
        roots.append(parent)
    if platform.startswith("win"):
        for key in ("ProgramFiles", "ProgramFiles(x86)"):
            v = os.environ.get(key)
            if v:
                roots.append(v)
        la = os.environ.get("LOCALAPPDATA")
        if la:
            roots.append(os.path.join(la, "Programs"))
    else:
        for p in ("/opt/homebrew/bin", "/usr/local/bin",
                  "/Applications", os.path.expanduser("~/Applications")):
            roots.append(p)
    try:
        import imageio_ffmpeg  # noqa: F401
        roots.append(os.path.dirname(imageio_ffmpeg.__file__))
    except Exception:
        pass
    return roots


def _app_bundle_candidates():
    """macOS .app 里的 ffmpeg：X.app/Contents/MacOS/ffmpeg（浅层 glob，比深扫便宜）。"""
    import glob
    out = []
    for pat in ("/Applications/*.app/Contents/MacOS/ffmpeg",
                os.path.expanduser("~/Applications/*.app/Contents/MacOS/ffmpeg")):
        try:
            out.extend(glob.glob(pat))
        except Exception:
            pass
    return out


def _discover_candidates(roots=None, max_depth=_DISCOVERY_MAX_DEPTH,
                         deadline=_DISCOVERY_DEADLINE):
    """在常见安装根下有限深度扫描别处自带的 ffmpeg（最后兜底）。

    约束（为什么不是全盘搜索）：
      · 只扫有限几个根 + 深度 ≤3 + 总时限（默认 3s）——全盘扫太慢也不礼貌；
      · 只按文件名匹配 ffmpeg[.exe]，不读内容；
      · 候选还要过 -version 烟测才会被采用（见 find_ffmpeg），坏文件不会被选中。
    """
    exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    if roots is None:
        roots = _discovery_roots()
    found = []
    if sys.platform == "darwin":
        found.extend(_app_bundle_candidates())
    t0 = time.time()
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        stack = [(root, 0)]
        while stack:
            if time.time() - t0 > deadline:
                return found
            d, depth = stack.pop()
            try:
                entries = list(os.scandir(d))
            except OSError:
                continue
            for e in entries:
                try:
                    if e.is_file() and e.name.lower() == exe.lower():
                        found.append(e.path)
                    elif e.is_dir() and depth < max_depth \
                            and not e.name.startswith((".", "$", "WindowsApps")):
                        stack.append((e.path, depth + 1))
                except OSError:
                    continue
    return found


# 共享安装位的约定布局（v2.6）：多个工具共用一份 ffmpeg 时放在这些位置，
# 即使没进 PATH（GUI 启动的进程常继承不到完整 PATH）也能被找到。
# 只做定点探测，不递归：每个盘最多 5 次 isfile。
_SHARED_REL = (
    "ffmpeg/bin/ffmpeg",
    "ffmpeg/ffmpeg",
    "tools/ffmpeg/bin/ffmpeg",
    "tools/ffmpeg/ffmpeg",
    "opt/ffmpeg/bin/ffmpeg",
)


def _fixed_drives():
    """Windows 上枚举存在的盘符（C:..H:）；其它平台返回根目录 '/'。"""
    if os.name != "nt":
        return ["/"]
    return [d + ":/" for d in "CDEFGH" if os.path.isdir(d + ":/")]


def _shared_candidates(bases=None):
    """共享安装位候选（定点探测，不递归）。"""
    exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    out = []
    for base in (bases if bases is not None else _fixed_drives()):
        for rel in _SHARED_REL:
            p = os.path.normpath(os.path.join(base, rel.replace("/", os.sep)))
            if not p.lower().endswith(exe.lower()):
                p += ".exe" if os.name == "nt" else ""
            if os.path.isfile(p):
                out.append(p)
    return out


def _smoke(path):
    """-version 探活：能跑出版本号才算可用（别处自带的可能缺 DLL）。"""
    try:
        proc = subprocess.run([path, "-version"], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=15)
    except Exception:                                          # noqa: BLE001
        return False
    out = (proc.stdout or b"").lower()
    return proc.returncode == 0 and b"version" in out


def find_ffmpeg(explicit=None, discover=True):
    """搜索链（设计 §8.2 v2.4）：

    --ffmpeg → AMPULL_FFMPEG → AMPULL_FFMPEG_DIRS（含 bin/）→ 本项目 tools/
      → **共享安装位**（C:\\ffmpeg、<盘>:\\tools\\ffmpeg 等定点探测）
      → PATH（winget/choco/scoop 装的一般在这）→ 别处自带的（有限扫描 + 烟测）

    返回 (path|None, source)。
    source ∈ explicit|env|env_dir|local|shared|path|discovered|None
    """
    if explicit:
        return (explicit, "explicit") if os.path.isfile(explicit) else (None, None)
    env = os.environ.get(_ENV_KEY)
    if env and os.path.isfile(env):
        return env, "env"
    for cand in _candidates_in(_env_ffmpeg_dirs()):
        if _smoke(cand):
            return cand, "env_dir"
    for root in _local_tools_dirs():
        hit = _scan_tools_dir(root)
        if hit:
            return hit, "local"
    # 共享安装位优先于 PATH：多工具共用的那份通常是刻意维护的（版本较新），
    # 而且不依赖进程是否继承到完整 PATH（GUI 启动常见缺 PATH）
    for cand in _shared_candidates():
        if _smoke(cand):
            return cand, "shared"
    which = shutil.which("ffmpeg")
    if which:
        return which, "path"
    if discover:
        for cand in _discover_candidates():
            if _smoke(cand):
                return cand, "discovered"
    return None, None


def find_ffprobe(ffmpeg_path):
    """同目录找 ffprobe（缺了就不做时长核对，不算失败）。"""
    if not ffmpeg_path:
        return None
    d = os.path.dirname(ffmpeg_path)
    name = "ffprobe.exe" if os.name == "nt" else "ffprobe"
    p = os.path.join(d, name)
    if os.path.isfile(p):
        return p
    return shutil.which("ffprobe")


def concat_parts(part_files, ts_path):
    """按序字节拼接。每段仍校验 188 对齐（防上游改动后拼出坏片）。"""
    total = 0
    with open(ts_path, "wb") as out:
        for p in part_files:
            with open(p, "rb") as f:
                data = f.read()
            if not data or len(data) % 188 != 0 or data[0] != 0x47:
                raise HlsError("hls_seg_corrupt",
                               "拼接时发现不合格分片：%s（%d 字节）"
                               % (os.path.basename(p), len(data)))
            out.write(data)
            total += len(data)
    return total


def mux_to_mp4(ts_path, mp4_path, ffmpeg):
    """-c copy 封装。返回 (ok, err_text)。失败由调用方降级为 .ts。"""
    cmd = [ffmpeg, "-y", "-v", "error", "-i", ts_path,
           "-c", "copy", "-bsf:a", "aac_adtstoasc", mp4_path]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=1800)
    except subprocess.TimeoutExpired:
        return False, "ffmpeg 超时（1800s）"
    except OSError as e:
        return False, "无法执行 ffmpeg：%s" % e
    err = (proc.stderr or b"").decode("utf-8", "replace").strip()
    if proc.returncode != 0 or not os.path.isfile(mp4_path) \
            or os.path.getsize(mp4_path) == 0:
        return False, err[-400:] or ("ffmpeg 退出码 %d" % proc.returncode)
    return True, err[-200:]


def probe_duration(path, ffprobe):
    """读容器时长（秒）。拿不到返回 None（不影响成败）。"""
    if not ffprobe:
        return None
    try:
        proc = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
        text = (proc.stdout or b"").decode("utf-8", "replace").strip()
        return round(float(text.split()[0]), 3) if text else None
    except Exception:
        return None
