# -*- coding: utf-8 -*-
"""test_proxy_scan.py — AmPull r38 代理扫描离线仿真套件（零外网）。

与 awsearch 的 parts/test_proxy_scan.py 同手法：本地假代理服务器矩阵
（真 socket、全回环）+ netstat 文本/state monkeypatch。覆盖四分类、
当日标记复用/退避、候选顺序、env 分支备用通道、LAN 绑定端到端。
Usage: python tests/test_proxy_scan.py -> ALL GREEN
"""
import importlib.util
import json
import os
import socket
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

spec = importlib.util.spec_from_file_location("ampull", "AmPull.py")
ampull = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ampull)

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name
          + ("  " + detail if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


class _FakeProxy(threading.Thread):
    """mode: vpn=双 CONNECT 全 200；domestic=baidu 200 / google 502；
    socks=SOCKS5 握手回 05 00（HTTP CONNECT 直接断）；dead=收到即断。"""
    daemon = True

    def __init__(self, mode, bind="127.0.0.1"):
        super().__init__(daemon=True)
        self.mode = mode
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind((bind, 0))
        self.port = self.srv.getsockname()[1]
        self.srv.listen(16)

    @staticmethod
    def _readhead(c):
        buf = b""
        while b"\r\n\r\n" not in buf and len(buf) < 8192:
            ch = c.recv(4096)
            if not ch:
                break
            buf += ch
        return buf

    def _serve_conn(self, c):
        # 命名注意：不能用 _handle——Py3.13 Thread 实例属性 _handle
        # 会覆盖方法（awsearch 套件首跑实锤）
        try:
            c.settimeout(2)
            first = c.recv(1)
            if not first:
                return
            if first == b"\x05":
                rest = b""
                while len(rest) < 2:
                    ch = c.recv(2 - len(rest))
                    if not ch:
                        return
                    rest += ch
                if self.mode == "socks":
                    c.sendall(b"\x05\x00")
                return
            head = first + self._readhead(c)
            if self.mode == "dead":
                return
            if self.mode == "socks":
                return  # 纯 socks 口对 HTTP CONNECT 直接断（真实行为）
            low = head.lower()
            if self.mode == "domestic" and b"google" in low:
                c.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                return
            if b"connect" in low:
                c.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        except Exception:
            pass
        finally:
            try:
                c.close()
            except Exception:
                pass

    def run(self):
        self.srv.settimeout(0.3)
        while True:
            try:
                c, _ = self.srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._serve_conn, args=(c,),
                             daemon=True).start()


def main():
    tmp = tempfile.mkdtemp(prefix="ampull-ps-")
    ampull._state_path = lambda: os.path.join(tmp, "ampull.state.json")
    if os.path.exists(ampull._state_path()):
        os.remove(ampull._state_path())

    # ---- T1 netstat 解析 ----
    vpn2 = _FakeProxy("vpn", bind="127.0.0.2")
    vpn2.start()
    fake_ns = ("  TCP    0.0.0.0:135       0.0.0.0:0    LISTENING    100\n"
               "  TCP    127.0.0.2:%-6d 0.0.0.0:0    LISTENING    101\n"
               "  TCP    192.168.1.5:7890 0.0.0.0:0    LISTENING    103\n"
               "  TCP    [fe80::1%%12]:139 [::]:0      LISTENING    104\n"
               ) % vpn2.port

    class _FR:
        def __init__(self, stdout):
            self.stdout = stdout

    old_sub = ampull.subprocess
    ampull.subprocess = type("S", (), {
        "run": staticmethod(lambda *a, **k: _FR(fake_ns))})
    try:
        addrs = ampull._listen_addrs()
    finally:
        ampull.subprocess = old_sub
    check("T1a lan-bind + 127/8 captured",
          ("127.0.0.2", vpn2.port) in addrs
          and ("192.168.1.5", 7890) in addrs, str(addrs))
    check("T1b <1024 and v6-global filtered",
          ("127.0.0.1", 135) not in addrs
          and not any(h.startswith("fe80") for h, _ in addrs), str(addrs))

    # ---- T2 四分类 ----
    vpn = _FakeProxy("vpn")
    domestic = _FakeProxy("domestic")
    socks = _FakeProxy("socks")
    dead = _FakeProxy("dead")
    for s in (vpn, domestic, socks, dead):
        s.start()
    vpn_url = "http://127.0.0.1:%d" % vpn.port
    dom_url = "http://127.0.0.1:%d" % domestic.port
    socks_url = "http://127.0.0.1:%d" % socks.port
    dead_url = "http://127.0.0.1:%d" % dead.port
    check("T2a classify vpn", ampull._classify_port(vpn_url) == "vpn")
    check("T2b classify domestic -> http",
          ampull._classify_port(dom_url) == "http")
    check("T2c classify socks", ampull._classify_port(socks_url) == "socks")
    check("T2d classify dead -> other",
          ampull._classify_port(dead_url) == "other")

    # ---- T3 检测管线：vpn 主选 / domestic+socks 落提示 ----
    fake_addrs = [("127.0.0.1", vpn.port), ("127.0.0.1", domestic.port),
                  ("127.0.0.1", socks.port), ("127.0.0.1", dead.port)]
    old_addrs = ampull._listen_addrs
    ampull._listen_addrs = lambda: list(fake_addrs)
    ampull._PROXY_CACHE = None
    try:
        found = ampull._detect_proxies()
    finally:
        ampull._listen_addrs = old_addrs
    check("T3a primary = vpn port (NOT domestic)",
          found == [vpn_url], "%s" % found)
    st = ampull._state_load()
    check("T3b day mark + hints recorded",
          st.get("scan_ok_date") == time.strftime("%Y-%m-%d")
          and dom_url in (st.get("http_hint") or "")
          and socks_url in (st.get("socks_hint") or "")
          and vpn_url not in (st.get("http_hint") or ""),
          json.dumps({k: st.get(k) for k in
                      ("scan_ok_date", "http_hint", "socks_hint")},
                     ensure_ascii=False))

    # ---- T4 当日复用零重扫 ----
    calls = {"n": 0}

    def _count_cls(u, timeout=1.5):
        calls["n"] += 1
        return "other"

    ampull._PROXY_CACHE = None
    old_cls = ampull._classify_port
    ampull._classify_port = _count_cls
    try:
        found2 = ampull._detect_proxies()
    finally:
        ampull._classify_port = old_cls
    check("T4 same-day reuse: zero reprobe",
          found2 == [vpn_url] and calls["n"] == 0,
          "reprobed %d times" % calls["n"])

    # ---- T5 失败当日退避 ----
    ampull._scan_reset if hasattr(ampull, "_scan_reset") else None
    ampull._PROXY_CACHE = None
    os.remove(ampull._state_path())
    ampull._listen_addrs = lambda: [("127.0.0.1", dead.port)]
    ampull._PROXY_CACHE = None
    try:
        found3 = ampull._detect_proxies()
    finally:
        ampull._listen_addrs = old_addrs
    st3 = ampull._state_load()
    check("T5 dead-only: empty + fail_date same-day",
          found3 == [] and st3.get("scan_fail_date")
          == time.strftime("%Y-%m-%d"))

    # ---- T6 env 分支：原生失败备用=扫描口 opener ----
    ampull._listen_addrs = lambda: [("127.0.0.1", vpn.port)]
    ampull._PROXY_CACHE = None
    os.remove(ampull._state_path())
    old_env = ampull._env_proxy_active
    ampull._env_proxy_active = lambda: True
    ampull._PROXY_CACHE = None
    try:
        class _A:
            url = "https://github.com/x/y/archive/main.zip"
            no_proxy = False
            proxy = None
        primary, alt = ampull._resolve_ops(_A())
        check("T6 env active: primary native (None), alt = scan opener",
              primary is None and alt is not None)
    finally:
        ampull._env_proxy_active = old_env
        ampull._listen_addrs = old_addrs

    print("-" * 40)
    if FAILS:
        print("FAILED: %d -> %s" % (len(FAILS), ", ".join(FAILS)))
        return 1
    print("ALL GREEN")
    return 0


if __name__ == "__main__":
    sys.exit(main())
