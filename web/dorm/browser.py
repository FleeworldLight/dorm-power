#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""无头浏览器与 CDP（手写 WebSocket），只用于扫码登录那一步。"""

import base64
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


class WebSocket:
    """仅实现 CDP 需要的部分：文本帧、掩码、ping/pong、分片。"""

    def __init__(self, url, timeout=30):
        m = re.match(r"^ws://([^/:]+):(\d+)(/.*)?$", url)
        if not m:
            raise ValueError("不支持的 ws 地址: " + url)
        host, port, path = m.group(1), int(m.group(2)), m.group(3) or "/"
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((
            "GET {} HTTP/1.1\r\nHost: {}:{}\r\nUpgrade: websocket\r\n"
            "Connection: Upgrade\r\nSec-WebSocket-Key: {}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n").format(path, host, port, key).encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("WebSocket 握手时连接被关闭")
            buf += chunk
        head, _, rest = buf.partition(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise RuntimeError("WebSocket 握手失败")
        self._buf = rest
        self._id = 0

    def _frame(self, opcode, payload):
        header = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        self.sock.sendall(bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def send_text(self, text):
        self._frame(0x1, text.encode("utf-8"))

    def _read_exact(self, n):
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("WebSocket 连接被对端关闭")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def recv_text(self):
        frags = []
        while True:
            b1, b2 = self._read_exact(2)
            fin, opcode, masked = b1 & 0x80, b1 & 0x0F, b2 & 0x80
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read_exact(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read_exact(8))[0]
            mask = self._read_exact(4) if masked else None
            payload = self._read_exact(n) if n else b""
            if mask:
                payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
            if opcode == 0x9:
                self._frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x8:
                raise RuntimeError("WebSocket 被对端关闭")
            frags.append(payload)
            if fin:
                return b"".join(frags).decode("utf-8", "replace")

    def call(self, method, params=None, timeout=60):
        self._id += 1
        mid = self._id
        self.send_text(json.dumps({"id": mid, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.sock.settimeout(max(0.5, min(deadline - time.time(), 5.0)))
            try:
                obj = json.loads(self.recv_text())
            except socket.timeout:
                continue
            if obj.get("id") == mid:
                if "error" in obj:
                    raise RuntimeError("CDP {} 报错: {}".format(method, obj["error"]))
                return obj.get("result", {})
        raise TimeoutError("CDP 调用超时: " + method)

    def close(self):
        try:
            self._frame(0x8, b"")
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


def find_chromium():
    for n in ("chromium", "chromium-browser", "google-chrome", "msedge", "chrome"):
        p = shutil.which(n)
        if p:
            return p
    for p in ("/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome"):
        if Path(p).exists():
            return p
    return ""


QR_RECT_JS = """(() => {
  const out = [];
  const sels = ['.qrcode-img img','.qrcode-body img','.qrcode-detail-img','img[src*="qr"]',
                'canvas','.qrcode-img','.qrcode-body','.qrcode-login img','img[src*="QR"]'];
  for (const s of sels) {
    document.querySelectorAll(s).forEach(e => {
      const r = e.getBoundingClientRect();
      if (r.width >= 90 && r.height >= 90 && r.width <= 600 && r.height <= 600) {
        out.push([s, r.x, r.y, r.width, r.height]);
      }
    });
  }
  out.sort((a, b) => b[3] * b[4] - a[3] * a[4]);
  return JSON.stringify(out.slice(0, 5));
})()"""


def capture_qr(br):
    """优先只截二维码那一小块（体积从 700KB 降到十几 KB），取不到就退回整屏。"""
    try:
        raw = br.evaluate(QR_RECT_JS, timeout=15)
        cands = json.loads(raw) if raw else []
    except Exception:
        cands = []
    for _sel, x, y, w, h in cands:
        pad = max(10.0, min(w, h) * 0.08)
        try:
            png = br.screenshot_clip(max(0.0, x - pad), max(0.0, y - pad),
                                     w + pad * 2, h + pad * 2, scale=3)
            if png and len(png) > 3000:
                return png
        except Exception:
            continue
    try:
        return br.screenshot()
    except Exception:
        return ""


class Browser:
    """启一个带调试端口的 chromium，连 CDP。"""

    def __init__(self, profile, url="about:blank", timeout=40):
        exe = find_chromium()
        if not exe:
            raise RuntimeError("这台机器上没有 chromium")
        profile = Path(profile)
        profile.mkdir(parents=True, exist_ok=True)
        port_file = profile / "DevToolsActivePort"
        port_file.unlink(missing_ok=True)

        cmd = [exe, "--remote-debugging-port=0",
               "--user-data-dir=" + str(profile),
               "--no-first-run", "--no-default-browser-check",
               "--disable-extensions", "--disable-background-networking",
               "--disable-dev-shm-usage", "--headless=new", "--disable-gpu",
               "--no-sandbox"]          # 容器里通常以 root 跑，必须关 sandbox
        cmd.append(url)

        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        port = None
        deadline = time.time() + timeout
        while time.time() < deadline:
            if port_file.exists():
                lines = port_file.read_text(encoding="utf-8", errors="replace").strip().splitlines()
                if lines and lines[0].strip().isdigit():
                    port = int(lines[0].strip())
                    break
            if self.proc.poll() is not None:
                raise RuntimeError("chromium 启动后立刻退出（码 {}）".format(self.proc.returncode))
            time.sleep(0.3)
        if not port:
            self.kill()
            raise RuntimeError("等不到 DevToolsActivePort")
        self.port = port
        self.ws = None
        self._connect()

    def _http_json(self, path, timeout=10):
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with op.open("http://127.0.0.1:{}{}".format(self.port, path), timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    def _connect(self, timeout=25):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                targets = self._http_json("/json/list")
            except Exception:
                time.sleep(0.4)
                continue
            pages = [t for t in targets
                     if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
            if pages:
                self.ws = WebSocket(pages[0]["webSocketDebuggerUrl"], timeout=30)
                for m in ("Page.enable", "Runtime.enable", "Network.enable"):
                    try:
                        self.ws.call(m, timeout=20)
                    except Exception:
                        pass
                return
            time.sleep(0.4)
        raise RuntimeError("连不上页面调试目标")

    def evaluate(self, expression, timeout=30):
        r = self.ws.call("Runtime.evaluate",
                         {"expression": expression, "returnByValue": True,
                          "awaitPromise": True}, timeout=timeout)
        res = r.get("result", {})
        if res.get("subtype") == "error":
            raise RuntimeError("页面脚本出错: " + str(res.get("description"))[:200])
        return res.get("value")

    def navigate(self, url, timeout=60):
        self.ws.call("Page.navigate", {"url": url}, timeout=timeout)

    def set_viewport(self, w, h, scale=2):
        try:
            self.ws.call("Emulation.setDeviceMetricsOverride",
                         {"width": w, "height": h, "deviceScaleFactor": scale,
                          "mobile": False}, timeout=20)
        except Exception:
            pass

    def screenshot(self):
        r = self.ws.call("Page.captureScreenshot", {"format": "png"}, timeout=40)
        return r.get("data") or ""

    def screenshot_clip(self, x, y, w, h, scale=2):
        r = self.ws.call("Page.captureScreenshot", {
            "format": "png",
            "clip": {"x": x, "y": y, "width": w, "height": h, "scale": scale}}, timeout=40)
        return r.get("data") or ""

    def get_cookies(self):
        return self.ws.call("Network.getAllCookies", timeout=30).get("cookies", [])

    def set_cookies(self, cookies):
        params = []
        for c in cookies:
            if not c.get("name"):
                continue
            domain = c.get("domain") or ""
            scheme = "https" if c.get("secure") else "http"
            item = {"name": c["name"], "value": c.get("value", ""),
                    "url": "{}://{}{}".format(scheme, domain.lstrip("."), c.get("path") or "/"),
                    "domain": domain, "path": c.get("path") or "/",
                    "secure": bool(c.get("secure")), "httpOnly": bool(c.get("httpOnly"))}
            if c.get("sameSite") in ("Strict", "Lax", "None"):
                item["sameSite"] = c["sameSite"]
            if isinstance(c.get("expires"), (int, float)) and c["expires"] > 0:
                item["expires"] = c["expires"]
            params.append(item)
        if params:
            self.ws.call("Network.setCookies", {"cookies": params}, timeout=30)

    def kill(self):
        if getattr(self, "ws", None):
            try:
                self.ws.close()
            except Exception:
                pass
        if getattr(self, "proc", None) and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=6)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
