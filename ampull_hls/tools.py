# -*- coding: utf-8 -*-
"""hls_tools —— 依赖自举（设计 §8）

红线：
  · 只写 %LOCALAPPDATA%\\ampull\\tools\\（或 ~/.local/share/ampull/tools/）
  · 永不改 PATH、永不写系统目录、不请求提权
  · 不带 --yes 只打计划（dry-run），下载前先给 URL/size/sha256/落盘绝对路径
  · 校验不过就回滚，不留坏目录
  · 工具名必须命中 PINS 白名单（防 `tools remove ../../..` 把家目录删了）

sha256 来源：GitHub Releases API 的 asset `digest` 字段（sha256:...）。
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
import zipfile

from .parse import HlsError
from . import mux as hmux

API_LATEST = "https://api.github.com/repos/BtbN/FFmpeg-Builds/releases/latest"

PINS = {
    "ffmpeg": {
        "repo": "BtbN/FFmpeg-Builds",
        "assets": {
            # 实际资产名：ffmpeg-n8.1-latest-win64-gpl-8.1.zip（含 -latest- 段）
            "windows": r"^ffmpeg-n[\d.]+-latest-win64-gpl-[\d.]+\.zip$",
            "linux": r"^ffmpeg-n[\d.]+-latest-linux64-gpl-[\d.]+\.tar\.xz$",
        },
        "bin": "ffmpeg.exe" if os.name == "nt" else "ffmpeg",
    },
}


def _require(tool):
    """白名单：工具名必须是 PINS 里的字面量，否则路径拼接会逃出 tools 目录。"""
    if tool not in PINS:
        raise HlsError("tools_not_found",
                       "不支持的工具 %r（可选：%s）" % (tool, ", ".join(sorted(PINS))))
    return PINS[tool]


def tools_root(tool="ffmpeg"):
    """依赖落盘根 = <程序目录>/tools/<tool>（相对程序目录，便携）。

    v5.20 用户裁决：不要 `%LOCALAPPDATA%` / `~` 这类绝对位置——
    整包拷走时依赖应当跟着走。实现指向 mux.tools_dir，单一事实来源。
    """
    _require(tool)
    return hmux.tools_dir(tool)


def _inside(root, path):
    """realpath 包含性检查（防符号链接/`..` 逃逸）。"""
    root = os.path.normcase(os.path.realpath(root))
    path = os.path.normcase(os.path.realpath(path))
    return path == root or path.startswith(root + os.sep)


def installed_versions(tool="ffmpeg"):
    pin = _require(tool)
    root = tools_root(tool)
    out = []
    if not os.path.isdir(root):
        return out
    exe = pin["bin"]
    for name in sorted(os.listdir(root)):
        marker = os.path.join(root, name, "marker.json")
        exe_path = os.path.join(root, name, "bin", exe)
        if os.path.isfile(marker) and os.path.isfile(exe_path) \
                and _inside(root, exe_path):
            out.append({"version": name, "path": exe_path,
                        "root": os.path.join(root, name)})
    return out


def smoke_test(path):
    try:
        proc = subprocess.run([path, "-version"], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=30)
    except Exception as e:                                     # noqa: BLE001
        return False, "%s: %s" % (type(e).__name__, e)
    text = (proc.stdout or b"").decode("utf-8", "replace")
    first = text.splitlines()[0] if text else ""
    return (proc.returncode == 0 and "version" in first.lower()), first[:120]


def _http_json(url):
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "ampull-hls"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def pick_asset(tool="ffmpeg", fetch_json=None):
    """查最新 release，挑当前平台资产，返回 {url,name,size,sha256,version}。"""
    pin = _require(tool)
    platform = ("windows" if os.name == "nt"
                else ("linux" if sys.platform.startswith("linux") else "darwin"))
    pattern = pin["assets"].get(platform)
    if not pattern:
        hint = ("macOS 建议 `brew install ffmpeg`（装完在 /opt/homebrew/bin 或 "
                "/usr/local/bin，AmPull 会自动发现）；也可用 --ffmpeg 显式指定"
                if platform == "darwin"
                else "请自行安装 %s 后用 --ffmpeg 指定" % tool)
        raise HlsError("tools_not_found",
                       "当前平台（%s）没有官方构建：%s" % (platform, hint))
    rx = re.compile(pattern)
    data = (fetch_json or _http_json)(API_LATEST)
    for asset in data.get("assets") or []:
        name = asset.get("name") or ""
        if not rx.match(name):
            continue
        digest = (asset.get("digest") or "")
        sha = digest.split(":", 1)[1] if digest.startswith("sha256:") else None
        m = re.search(r"-n([\d.]+)-latest-", name) or re.search(r"-n([\d.]+)-", name)
        return {"url": asset["browser_download_url"], "name": name,
                "size": asset.get("size") or 0, "sha256": sha,
                "version": m.group(1) if m else "latest",
                "release": data.get("tag_name")}
    raise HlsError("tools_install_failed",
                   "最新 release 里没找到匹配 %s 的资产（%s）" % (pattern, API_LATEST))


def plan_text(tool="ffmpeg", fetch_json=None):
    a = pick_asset(tool, fetch_json=fetch_json)   # 失败直接抛，不做假成功
    return ("计划安装 %s %s\n"
            "  下载源 : %s\n"
            "  体积   : %.1f MB\n"
            "  sha256 : %s\n"
            "  落盘   : %s\n"
            "  加 --yes 才真正下载" % (
                tool, a["version"], a["url"], a["size"] / 1048576.0,
                a["sha256"] or "（发布方未提供，将只校验体积+烟测）",
                os.path.join(tools_root(tool), a["version"], "bin")))


def _safe_extract(archive, dest):
    """解压并拒绝路径穿越（zip 3.6+ 自带清洗，tar 没有——必须自己判）。"""
    if archive.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            for member in z.infolist():
                target = os.path.join(dest, member.filename)
                if not _inside(dest, target):
                    raise HlsError("tools_install_failed",
                                   "压缩包含越界路径：%s" % member.filename)
            z.extractall(dest)
        return
    with tarfile.open(archive) as t:
        for member in t.getmembers():
            target = os.path.join(dest, member.name)
            if not _inside(dest, target):
                raise HlsError("tools_install_failed",
                               "压缩包含越界路径：%s" % member.name)
            if member.issym() or member.islnk():
                raise HlsError("tools_install_failed",
                               "压缩包含链接条目，拒绝解压：%s" % member.name)
        t.extractall(dest)


def install(tool="ffmpeg", yes=False, fetch_bytes=None, fetch_json=None,
            fetch_to_file=None):
    """安装。返回收据 dict（含 source/version/path/sha256/smoke_test）。

    fetch_to_file(url, dest) -> (sha256_hex, size)：流式落盘版（推荐，
    100MB+ 不进内存）；不提供时退回 fetch_bytes 全内存。
    """
    pin = _require(tool)
    if not yes:
        return {"ok": True, "mode": "tools", "action": "install", "tool": tool,
                "dry_run": True, "plan": plan_text(tool, fetch_json=fetch_json),
                "seconds": 0.0}

    for item in installed_versions(tool):
        ok, _ = smoke_test(item["path"])
        if ok:
            return {"ok": True, "mode": "tools", "action": "install", "tool": tool,
                    "path": item["path"], "version": item["version"],
                    "source": "local", "seconds": 0.0}

    t0 = time.time()
    asset = pick_asset(tool, fetch_json=fetch_json)
    root = tools_root(tool)
    ver_dir = os.path.join(root, asset["version"])
    tmp_dir = ver_dir + ".tmp"
    if os.path.isdir(tmp_dir):
        shutil.rmtree(tmp_dir, ignore_errors=True)
    os.makedirs(tmp_dir, exist_ok=True)

    archive = os.path.join(tmp_dir, asset["name"])
    try:
        if fetch_to_file:
            got_sha, size = fetch_to_file(asset["url"], archive)
        else:
            data = fetch_bytes(asset["url"]) if fetch_bytes else None
            if not data:
                raise HlsError("tools_install_failed",
                               "下载失败（空响应）：%s" % asset["url"])
            size = len(data)
            got_sha = hashlib.sha256(data).hexdigest()
            with open(archive, "wb") as f:
                f.write(data)
        if asset["size"] and size != asset["size"]:
            raise HlsError("tools_install_failed",
                           "体积不符（期望 %d，实得 %d）" % (asset["size"], size))
        if asset["sha256"] and got_sha != asset["sha256"].lower():
            raise HlsError("tools_install_failed",
                           "sha256 不符（期望 %s，实得 %s）"
                           % (asset["sha256"], got_sha))

        _safe_extract(archive, tmp_dir)
        try:
            os.remove(archive)
        except OSError:
            pass

        exe = pin["bin"]
        found = None
        for dirpath, _dirs, files in os.walk(tmp_dir):
            if exe in files:
                found = dirpath
                break
        if not found:
            raise HlsError("tools_install_failed", "解压后找不到 %s" % exe)

        norm_bin = os.path.join(tmp_dir, "bin")
        if os.path.normcase(os.path.abspath(found)) != \
                os.path.normcase(os.path.abspath(norm_bin)):
            os.makedirs(norm_bin, exist_ok=True)
            for name in os.listdir(found):
                src = os.path.join(found, name)
                if os.path.isfile(src):
                    shutil.copy2(src, os.path.join(norm_bin, name))

        exe_path = os.path.join(norm_bin, exe)
        ok, out = smoke_test(exe_path)
        if not ok:
            raise HlsError("tools_install_failed", "烟测失败：%s" % out)

        with open(os.path.join(tmp_dir, "marker.json"), "w", encoding="utf-8") as f:
            json.dump({"version": asset["version"], "url": asset["url"],
                       "sha256": asset["sha256"], "size": size,
                       "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S")},
                      f, ensure_ascii=False, indent=1)

        if os.path.isdir(ver_dir):
            shutil.rmtree(ver_dir, ignore_errors=True)
        try:
            os.replace(tmp_dir, ver_dir)
        except OSError as e:
            raise HlsError("tools_install_failed",
                           "落位失败（文件被占用？）：%s" % e)
    finally:
        if os.path.isdir(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)

    return {"ok": True, "mode": "tools", "action": "install", "tool": tool,
            "path": os.path.join(ver_dir, "bin", pin["bin"]),
            "version": asset["version"], "url": asset["url"],
            "sha256": asset["sha256"], "bytes": size,
            "smoke_test": {"ok": True, "output": out}, "source": "fresh",
            "seconds": round(time.time() - t0, 2)}


def remove(tool="ffmpeg", yes=False):
    _require(tool)
    root = tools_root(tool)
    if not os.path.isdir(root):
        return {"ok": True, "mode": "tools", "action": "remove", "tool": tool,
                "tool_removed": [], "note": "本地 tools 目录不存在"}
    if not yes:
        return {"ok": True, "mode": "tools", "action": "remove", "tool": tool,
                "dry_run": True,
                "would_remove": [v["root"] for v in installed_versions(tool)]}
    removed = []
    for name in os.listdir(root):
        p = os.path.join(root, name)
        if not _inside(root, p):
            continue                      # 逃逸条目一律不碰
        try:
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
            else:
                os.remove(p)
            removed.append(p)
        except OSError:
            pass
    return {"ok": True, "mode": "tools", "action": "remove", "tool": tool,
            "removed": removed}
