#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""宿舍电费监控（本地 CLI）。用法与背景见 doc/本地工具.md。"""

import argparse
import base64
import csv
import http.server
import json
import os
import re
import socket
import socketserver
import struct
import subprocess
import sys
import time
import urllib.request
import urllib.parse
import webbrowser
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
DATA_DIR = ROOT / "data"
HISTORY_PATH = DATA_DIR / "history.csv"
COOKIE_PATH = DATA_DIR / "cookies.json"
ROOM_PATH = DATA_DIR / "room.json"
LAST_STATE_PATH = DATA_DIR / "last_state.json"
DASHBOARD_PATH = ROOT / "dashboard.html"

# 站点结构（实测）：
#   /index/order   选择页：区域→楼栋→楼层→房间 四级联动，选完点「下一步」
#   /index/pay     选完 POST 到这里，服务端注入 var infoDataJson（含 roomId）并展示余额
#   /index/getLevels    POST，返回四级联动的层级定义
#   /index/getReserveAM POST {roomId}，返回 {respCode, data:{remainPower, remainName, resultInfo}}
# 注意：/index/pay 上的 infoDataJson 来自那次 POST 的选择结果，不是会话里记住的，
# 所以每次查询都要把选择字段一起带上。
SELECT_FIELDS = ("custName", "custNo", "areaId", "architectureId", "floor", "roomId",
                 "areaName", "architectureName", "floorName", "roomName")

DEFAULT_CONFIG = {
    "base_url": "http://nfuedu.zftcloud.com/electricity_system",
    "edge_path": "",
    "profile_dir": ".edge-profile",
    "unit_price": 0.6259,
    "low_threshold": 20.0,
    "browser_timeout_s": 90,
    "login_timeout_s": 420,
    "history_keep": 2000,
    "report_url": "",
    "display_name": "",
    "client_id": "",
}

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

CSV_HEADERS = [
    "time", "remain", "unit", "yuan", "room_id",
    "area", "building", "floor", "room", "cust_no", "cust_name", "note",
]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36 Edg/153.0.0.0")

EVAL_JS = """(() => {
  const txt = (id) => {
    const e = document.getElementById(id);
    if (!e) return null;
    if (e.options && e.selectedIndex >= 0) return e.options[e.selectedIndex].text.trim();
    return (e.innerText || e.textContent || '').trim();
  };
  const val = (id) => { const e = document.getElementById(id); return (e && e.value !== undefined) ? e.value : null; };
  let info = null;
  try { info = (typeof infoDataJson !== 'undefined') ? infoDataJson : null; } catch (e) { info = null; }
  return JSON.stringify({
    title: document.title,
    url: location.href,
    readyState: document.readyState,
    reserve: txt('reserveAM'),
    unit: txt('unit'),
    tips: txt('tips'),
    info: info,
    form: {
      custName: val('custName'), custNo: val('custNo'),
      areaId: val('area'), architectureId: val('architecture'),
      floor: val('floor'), roomId: val('room'),
      areaName: txt('area'), architectureName: txt('architecture'),
      floorName: txt('floor'), roomName: txt('room')
    }
  });
})()"""


def _js_quote(obj):
    return json.dumps(obj, ensure_ascii=False)


def build_browser_query_js(cfg, room):
    """在页面里（同源）先 POST /index/pay 拿 HTML，再 POST getReserveAM 拿余额，一并返回。"""
    return """(async () => {
  const BASE = %s;
  const FIELDS = %s;
  const out = {stamp: Date.now(), title: document.title, url: location.href,
               went_alipay: /alipay/i.test(location.href),
               pay_status: null, api_status: null, html: '', api: null, note: ''};
  if (location.href.indexOf(BASE) !== 0) {
    out.note = '不在目标站点上，当前地址 ' + location.href;
    return JSON.stringify(out);
  }
  const post = (path, data) => fetch(BASE + path, {
    method: 'POST', credentials: 'include', redirect: 'manual',
    headers: {'Content-Type': 'application/x-www-form-urlencoded',
              'X-Requested-With': 'XMLHttpRequest'},
    body: new URLSearchParams(data).toString()
  });
  try {
    const r1 = await post('/index/pay', FIELDS);
    out.pay_status = r1.status;
    if (r1.type === 'opaqueredirect' || r1.status === 0) {
      out.note = 'pay 被重定向，会话已失效';
    } else {
      out.html = await r1.text();
    }
  } catch (e) { out.note = 'pay 请求异常: ' + e; }

  if (FIELDS.roomId) {
    try {
      const r2 = await post('/index/getReserveAM', {roomId: FIELDS.roomId});
      out.api_status = r2.status;
      if (r2.status === 200) {
        const t = await r2.text();
        try { out.api = JSON.parse(t); } catch (e) { out.note += ' | api 非 JSON: ' + t.slice(0, 120); }
      }
    } catch (e) { out.note += ' | getReserveAM 异常: ' + e; }
  }
  return JSON.stringify(out);
})()""" % (_js_quote(url_base(cfg)),
           _js_quote({k: room.get(k, "") for k in SELECT_FIELDS}))


def _page_settled(br, timeout):
    """等页面真正加载完（导航后第一拍 evaluate 往往还在 about:blank）。"""
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        try:
            raw = br.evaluate(
                "JSON.stringify({u: location.href, r: document.readyState, t: document.title})",
                timeout=15)
            last = json.loads(raw) if raw else {}
        except Exception:
            last = {}
        u = last.get("u") or ""
        if u and u != "about:blank" and last.get("r") == "complete":
            return last
        time.sleep(0.8)
    return last


# ---------------------------------------------------------------- 基础工具

def setup_stdout():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


def log(tag, msg):
    print("[{:^4}] {}".format(tag, msg), flush=True)


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception as exc:
            log("warn", "config.json 解析失败，用默认值：{}".format(exc))
    if not cfg.get("edge_path") or not Path(cfg["edge_path"]).exists():
        found = find_edge()
        if found:
            cfg["edge_path"] = found
    if not cfg.get("client_id"):
        # 稳定标识：看板靠它把"同一台机器的上报"归成一条，换宿舍时替换而不是新增卡片
        cfg["client_id"] = os.urandom(8).hex()
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg


def find_edge():
    for p in EDGE_CANDIDATES:
        if Path(p).exists():
            return p
    for base in (r"C:\Program Files (x86)\Microsoft\Edge\Application",
                 r"C:\Program Files\Microsoft\Edge\Application"):
        b = Path(base)
        if b.is_dir():
            for child in sorted(b.iterdir(), reverse=True):
                exe = child / "msedge.exe"
                if exe.exists():
                    return str(exe)
    return ""


def profile_path(cfg):
    p = Path(cfg["profile_dir"])
    return p if p.is_absolute() else ROOT / p


def url_base(cfg):
    return (cfg.get("base_url") or DEFAULT_CONFIG["base_url"]).rstrip("/")


def url_order(cfg):
    return url_base(cfg) + "/index/order"


def url_pay(cfg):
    return url_base(cfg) + "/index/pay"


def url_reserve(cfg):
    return url_base(cfg) + "/index/getReserveAM"


# ---------------------------------------------------------------- 极简 WebSocket 客户端

class WebSocket:
    """只实现 CDP 需要的部分：文本帧、掩码、ping/pong、分片。纯标准库。"""

    def __init__(self, url, timeout=30):
        m = re.match(r"^ws://([^/:]+):(\d+)(/.*)?$", url)
        if not m:
            raise ValueError("不支持的 ws 地址: {}".format(url))
        host, port, path = m.group(1), int(m.group(2)), m.group(3) or "/"
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        handshake = (
            "GET {} HTTP/1.1\r\nHost: {}:{}\r\nUpgrade: websocket\r\n"
            "Connection: Upgrade\r\nSec-WebSocket-Key: {}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).format(path, host, port, key)
        self.sock.sendall(handshake.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("WebSocket 握手时连接被关闭")
            buf += chunk
        head, _, rest = buf.partition(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise RuntimeError("WebSocket 握手失败: " + head.decode("latin1", "replace"))
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
            fin = b1 & 0x80
            opcode = b1 & 0x0F
            masked = b2 & 0x80
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
            left = deadline - time.time()
            self.sock.settimeout(max(0.5, min(left, 5.0)))
            try:
                obj = json.loads(self.recv_text())
            except socket.timeout:
                continue
            if obj.get("id") == mid:
                if "error" in obj:
                    raise RuntimeError("CDP {} 报错: {}".format(method, obj["error"]))
                return obj.get("result", {})
        raise TimeoutError("CDP 调用超时: {}".format(method))

    def close(self):
        try:
            self._frame(0x8, b"")
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


# ---------------------------------------------------------------- 浏览器控制

class Browser:
    """用 --remote-debugging-port=0 启动 Edge，读 DevToolsActivePort 拿端口，连 CDP。"""

    def __init__(self, cfg, headless=True, url="about:blank"):
        edge = cfg.get("edge_path") or find_edge()
        if not edge:
            raise RuntimeError("找不到 msedge.exe，请在 config.json 里填 edge_path")
        self.profile = profile_path(cfg)
        self.profile.mkdir(parents=True, exist_ok=True)
        port_file = self.profile / "DevToolsActivePort"
        port_file.unlink(missing_ok=True)

        cmd = [edge, "--remote-debugging-port=0",
               "--user-data-dir={}".format(self.profile),
               "--no-first-run", "--no-default-browser-check",
               "--disable-extensions", "--disable-background-networking"]
        if headless:
            cmd += ["--headless=new", "--disable-gpu"]
        cmd.append(url)

        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        port = None
        deadline = time.time() + 30
        while time.time() < deadline:
            if port_file.exists():
                lines = port_file.read_text(encoding="utf-8", errors="replace").strip().splitlines()
                if lines and lines[0].strip().isdigit():
                    port = int(lines[0].strip())
                    break
            if self.proc.poll() is not None:
                raise RuntimeError("Edge 启动后立刻退出（退出码 {}）".format(self.proc.returncode))
            time.sleep(0.3)
        if not port:
            self.kill()
            raise RuntimeError("等不到 DevToolsActivePort，Edge 没能开出调试端口")
        self.port = port
        self.ws = None
        self._connect_page()

    def _http_json(self, path, timeout=10):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open("http://127.0.0.1:{}{}".format(self.port, path), timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))

    def _connect_page(self, timeout=25):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                targets = self._http_json("/json/list")
            except Exception:
                time.sleep(0.4)
                continue
            pages = [t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
            if pages:
                self.ws = WebSocket(pages[0]["webSocketDebuggerUrl"], timeout=30)
                for m in ("Page.enable", "Runtime.enable", "Network.enable"):
                    try:
                        self.ws.call(m, timeout=20)
                    except Exception:
                        pass
                return
            time.sleep(0.4)
        raise RuntimeError("连不上 Edge 的页面调试目标")

    def evaluate(self, expression, timeout=30):
        r = self.ws.call("Runtime.evaluate",
                         {"expression": expression, "returnByValue": True,
                          "awaitPromise": True}, timeout=timeout)
        res = r.get("result", {})
        if res.get("subtype") == "error":
            raise RuntimeError("页面脚本执行出错: {}".format(res.get("description")))
        return res.get("value")

    def navigate(self, url, timeout=60):
        self.ws.call("Page.navigate", {"url": url}, timeout=timeout)

    def get_cookies(self):
        return self.ws.call("Network.getAllCookies", timeout=30).get("cookies", [])

    def set_cookies(self, cookies):
        params = []
        for c in cookies:
            if not c.get("name"):
                continue
            domain = c.get("domain") or ""
            scheme = "https" if c.get("secure") else "http"
            item = {
                "name": c["name"], "value": c.get("value", ""),
                "url": "{}://{}{}".format(scheme, domain.lstrip("."), c.get("path") or "/"),
                "domain": domain, "path": c.get("path") or "/",
                "secure": bool(c.get("secure")), "httpOnly": bool(c.get("httpOnly")),
            }
            if c.get("sameSite") in ("Strict", "Lax", "None"):
                item["sameSite"] = c["sameSite"]
            exp = c.get("expires")
            if isinstance(exp, (int, float)) and exp > 0:
                item["expires"] = exp
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
                subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass


# ---------------------------------------------------------------- 页面状态采集

def _extract_infodata(doc):
    """取服务端注入的 var infoDataJson = {...}; 未登录时是空数组。"""
    if not doc:
        return None
    m = re.search(r"var\s+infoDataJson\s*=\s*", doc)
    if not m:
        return None
    i = m.end()
    while i < len(doc) and doc[i] in " \t\r\n":
        i += 1
    if i >= len(doc):
        return None
    if doc[i] == "[":
        j = doc.find("]", i)
        if j < 0:
            return None
        return {} if not doc[i + 1:j].strip() else None
    if doc[i] != "{":
        return None
    depth, in_str, esc = 0, False, False
    for k in range(i, len(doc)):
        c = doc[k]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(doc[i:k + 1])
                    except Exception:
                        return None
    return None


def _text_of(doc, elem_id):
    m = re.search(r'id="{}"[^>]*>(.*?)</\w+>'.format(re.escape(elem_id)), doc or "", re.S)
    if not m:
        return None
    return re.sub(r"<[^>]*>", "", m.group(1)).strip()


def _state_from_html(html):
    mt = re.search(r"<title>(.*?)</title>", html or "", re.S)
    title = re.sub(r"\s+", " ", mt.group(1)).strip() if mt else ""
    return {
        "title": title, "url": "", "readyState": "complete",
        "reserve": _text_of(html, "reserveAM"),
        "unit": _text_of(html, "unit"),
        "tips": _text_of(html, "tips"),
        "info": _extract_infodata(html),
    }


def apply_reserve_api(state, api):
    """把 getReserveAM 的响应合并进 state。"""
    if not isinstance(api, dict):
        return state
    if str(api.get("respCode")) != "00":
        state.setdefault("_api_note", "getReserveAM 返回 respCode={}".format(api.get("respCode")))
        return state
    data = api.get("data") or {}
    rp = data.get("remainPower")
    if rp is not None:
        state["reserve"] = str(rp)
    rn = data.get("remainName")
    if rn == "yuan":
        state["unit"] = "元"
    elif rn == "du":
        state["unit"] = "度"
    info = data.get("resultInfo") or {}
    if str(info.get("result")) == "3":
        state["tips"] = "有订单正在处理"
    return state


def _cookie_header_for(cfg, cookies):
    host = urllib.parse.urlparse(url_base(cfg)).hostname or ""
    jar = [c for c in cookies
           if c.get("name") and host.endswith((c.get("domain") or "").lstrip(".") or "\0")]
    if not jar:
        return None
    return "; ".join("{}={}".format(c["name"], c.get("value", "")) for c in jar)


def _http_post(cfg, path, fields, cookie_hdr, timeout=25):
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url_base(cfg) + path, data=body, method="POST", headers={
        "Cookie": cookie_hdr, "User-Agent": UA,
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": url_order(cfg),
        "Accept": "text/html,application/xhtml+xml,*/*",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


def query_via_http(cfg, room):
    """快路径：纯 HTTP，不开浏览器。

    路线1：只带 roomId 直接问 getReserveAM（实测未登录会 500，所以返回 00 即会话有效）。
    路线2：POST /index/pay 带上完整选择字段，取服务端注入的 infoDataJson，再问余额。
    """
    cookies = load_cookies()
    if not cookies or not room.get("roomId"):
        return None
    cookie_hdr = _cookie_header_for(cfg, cookies)
    if not cookie_hdr:
        return None

    try:
        _, text = _http_post(cfg, "/index/getReserveAM", {"roomId": room["roomId"]}, cookie_hdr)
        api = json.loads(text)
        if isinstance(api, dict) and str(api.get("respCode")) == "00" and api.get("data"):
            state = {"title": "", "url": "", "readyState": "complete",
                     "reserve": None, "unit": None, "tips": None, "info": dict(room)}
            apply_reserve_api(state, api)
            if state.get("reserve"):
                return state
    except Exception as exc:
        log("info", "HTTP 路线1 未成功（{}）".format(type(exc).__name__))

    try:
        _, html = _http_post(cfg, "/index/pay",
                             {k: room.get(k, "") for k in SELECT_FIELDS}, cookie_hdr)
        st = _state_from_html(html)
        info = st.get("info")
        if isinstance(info, dict) and info.get("roomId"):
            try:
                _, text = _http_post(cfg, "/index/getReserveAM", {"roomId": info["roomId"]}, cookie_hdr)
                apply_reserve_api(st, json.loads(text))
            except Exception:
                pass
        return st
    except Exception as exc:
        log("info", "HTTP 路线2 未成功（{}）".format(type(exc).__name__))
        return None


def query_via_browser(cfg, room):
    """慢路径：开无头 Edge，回灌 cookie，导航到 /index/order 重建会话，再在页面内发请求。"""
    br = Browser(cfg, headless=True, url=url_order(cfg))
    try:
        saved = load_cookies()
        if saved:
            try:
                br.set_cookies(saved)          # 必须先灌再导航，否则第一跳就落到支付宝登录页
            except Exception as exc:
                log("warn", "灌 cookie 失败：{}".format(exc))
        br.navigate(url_order(cfg))

        # 导航后第一拍往往还在 about:blank，必须等页面真正落定
        page = _page_settled(br, min(30, cfg["browser_timeout_s"]))
        if "alipay" in (page.get("u") or "").lower():
            return {"title": "登录 - 支付宝", "url": page.get("u") or "",
                    "readyState": "complete", "reserve": None, "unit": None,
                    "tips": None, "info": None}

        deadline = time.time() + cfg["browser_timeout_s"]
        payload, raw, empty_tries = None, None, 0
        while time.time() < deadline:
            try:
                raw = br.evaluate(build_browser_query_js(cfg, room), timeout=45)
                payload = json.loads(raw) if raw else None
            except Exception:
                payload = None
            if payload and (payload.get("api") or payload.get("html")):
                break
            empty_tries += 1
            if empty_tries >= 6 and payload and payload.get("note"):
                break          # 明显是会话失效，不用耗满超时
            time.sleep(1.0)

        if not payload:
            return None
        if payload.get("html"):
            state = _state_from_html(payload["html"])
        else:
            state = {"title": "", "url": "", "readyState": "complete",
                     "reserve": None, "unit": None, "tips": None, "info": None}
        state["url"] = payload.get("url") or ""
        if payload.get("api"):
            apply_reserve_api(state, payload["api"])
        if payload.get("went_alipay"):
            state["title"] = state.get("title") or "登录 - 支付宝"
        if payload.get("note"):
            state["_note"] = payload["note"]

        info = state.get("info")
        if isinstance(info, dict) and info.get("roomId"):
            try:
                fresh = br.get_cookies()
                if fresh:
                    save_cookies(fresh)
            except Exception:
                pass
        return state
    finally:
        br.kill()


# ---------------------------------------------------------------- 状态判读

def interpret(state):
    """绝不在没把握时返回数字，避免把模板里的 0 当成余额误报低电。"""
    out = {"status": "error", "message": "", "data": None}
    if not state:
        out["message"] = "没取到页面状态"
        return out

    title = state.get("title") or ""
    if "支付宝" in title or "alipay" in title.lower():
        out["status"] = "not_logged_in"
        out["message"] = "落在支付宝页面（{}），登录态已失效，请重新执行 login".format(title)
        return out

    info = state.get("info")
    if info is None:
        out["status"] = "error"
        out["message"] = "页面上没有 infoDataJson，站点结构可能变了"
        return out
    if not isinstance(info, dict) or not info.get("roomId"):
        out["status"] = "not_logged_in"
        out["message"] = "infoDataJson 是空的（roomId 缺失），会话没有被识别成学生"
        return out

    tips = state.get("tips") or ""
    if "订单" in tips:
        out["status"] = "pending_order"
        out["message"] = "有订单正在处理，本次读数不可信：" + tips
        return out

    raw = state.get("reserve")
    if raw is None or str(raw).strip() in ("", "未知"):
        out["status"] = "unknown"
        out["message"] = "余额显示为「{}」，未取到有效数值".format(raw)
        return out
    try:
        remain = float(str(raw).replace(",", ""))
    except ValueError:
        out["status"] = "unknown"
        out["message"] = "余额不是数字：{!r}".format(raw)
        return out

    unit_raw = state.get("unit") or "度"
    unit = "度" if "度" in unit_raw else ("元" if "元" in unit_raw else unit_raw)

    out["status"] = "ok"
    out["message"] = "读取成功"
    out["data"] = {
        "remain": remain, "unit": unit,
        "room_id": str(info.get("roomId") or ""),
        "area": info.get("areaName") or "",
        "building": info.get("architectureName") or "",
        "floor": info.get("floorName") or "",
        "room": info.get("roomName") or "",
        "cust_no": info.get("custNo") or "",
        "cust_name": info.get("custName") or "",
        "page_title": title,
    }
    return out


# ---------------------------------------------------------------- cookie 存取

COOKIE_FIELDS = ("name", "value", "domain", "path", "secure", "httpOnly",
                 "sameSite", "expires", "session")


def save_cookies(cookies):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    keep = [{k: c.get(k) for k in COOKIE_FIELDS} for c in cookies]
    COOKIE_PATH.write_text(json.dumps(keep, ensure_ascii=False, indent=2), encoding="utf-8")
    return COOKIE_PATH


def load_cookies():
    if not COOKIE_PATH.exists():
        return []
    try:
        data = json.loads(COOKIE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_room(room):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ROOM_PATH.write_text(json.dumps(room, ensure_ascii=False, indent=2), encoding="utf-8")
    return ROOM_PATH


def load_room():
    if not ROOM_PATH.exists():
        return {}
    try:
        d = json.loads(ROOM_PATH.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def display_name(cfg, d):
    """看板上的昵称。默认对真实姓名脱敏，不做明文外发。"""
    name = (cfg.get("display_name") or "").strip()
    if name:
        return name
    real = (d.get("cust_name") or "").strip()
    if not real or real in ("null", "样例"):
        return "匿名"
    return real[0] + "同学"


def _ssl_context():
    """本机出口有 TLS 拦截代理，Python 3.13+ 默认的 VERIFY_X509_STRICT 会误杀它的证书。"""
    import ssl
    ctx = ssl.create_default_context()
    try:
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    except Exception:
        pass
    return ctx


def upload_report(cfg, d):
    """把「房间号 + 剩余 + 时间」报到看板服务。只发这些，不发任何凭据。"""
    base = (cfg.get("report_url") or "").strip()
    if not base:
        return None
    payload = {
        "room_id": d.get("room_id"), "remain": d.get("remain"), "unit": d.get("unit"),
        "area": d.get("area"), "building": d.get("building"),
        "floor": d.get("floor"), "room": d.get("room"),
        "who": display_name(cfg, d),
        "client_id": cfg.get("client_id") or "",
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    kw = {}
    if base.lower().startswith("https"):
        kw["context"] = _ssl_context()
    req = urllib.request.Request(base.rstrip("/") + "/api/report", data=body,
                                 method="POST",
                                 headers={"Content-Type": "text/plain; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=25, **kw) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:
        return {"ok": False, "error": "{}: {}".format(type(exc).__name__, exc)}


def form_complete(form):
    """选择页上四项都选好、姓名学号也填了才算完整。"""
    if not isinstance(form, dict):
        return False
    need = ("custName", "custNo", "areaId", "architectureId", "floor", "roomId")
    return all(str(form.get(k) or "").strip() not in ("", "null", "请选择") for k in need)


def room_desc(room):
    if not isinstance(room, dict):
        return "?"
    s = " ".join(x for x in (room.get("areaName"), room.get("architectureName"),
                             room.get("floorName"), room.get("roomName")) if x)
    return s or str(room.get("roomId") or "?")


# ---------------------------------------------------------------- 历史

def append_history(rec):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    new_file = not HISTORY_PATH.exists()
    with HISTORY_PATH.open("a", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        if new_file:
            w.writerow(CSV_HEADERS)
        w.writerow([rec.get(k, "") for k in CSV_HEADERS])


def load_history():
    if not HISTORY_PATH.exists():
        return []
    rows = []
    try:
        with HISTORY_PATH.open("r", newline="", encoding="utf-8-sig") as fh:
            for r in csv.DictReader(fh):
                try:
                    r["remain"] = float(r["remain"])
                    r["dt"] = datetime.strptime(r["time"], "%Y-%m-%d %H:%M:%S")
                except (TypeError, ValueError):
                    continue
                rows.append(r)
    except Exception as exc:
        log("warn", "历史文件读取失败：{}".format(exc))
    rows.sort(key=lambda r: r["dt"])
    return rows


# ---------------------------------------------------------------- 看板

def _svg_chart(rows, unit, threshold):
    W, H = 720, 240
    L, R, T, B = 52, 16, 16, 34
    max_pts = 120
    if len(rows) > max_pts:
        stride = (len(rows) + max_pts - 1) // max_pts
        rows = rows[::stride] + [rows[-1]]
    if not rows:
        return ('<svg viewBox="0 0 {} {}" width="100%" role="img">'
                '<text x="{}" y="{}" font-size="13" fill="#8a8a80" '
                'text-anchor="middle">暂无历史数据</text></svg>').format(W, H, W // 2, H // 2)

    vals = [r["remain"] for r in rows]
    xs = list(range(len(rows)))
    vmin, vmax = min(vals), max(vals)
    if vmax - vmin < 1e-9:
        vmax, vmin = vmin + 1, vmin - 1
    pad = (vmax - vmin) * 0.12
    vmin -= pad
    vmax += pad

    def px(i):
        return L + (W - L - R) / 2 if len(xs) == 1 else L + (W - L - R) * i / (len(xs) - 1)

    def py(v):
        return T + (H - T - B) * (1 - (v - vmin) / (vmax - vmin))

    parts = ['<svg viewBox="0 0 {} {}" width="100%" role="img" '
             'font-family="system-ui,\'Microsoft YaHei\',sans-serif">'.format(W, H)]
    parts.append('<title>宿舍电费余额变化</title>')
    # 余额变化幅度通常很小（小数点后两位），范围窄时多给一位小数，免得刻度出现重复值
    nd = 2 if (vmax - vmin) < 5 else 1
    vfmt = "{:." + str(nd) + "f}"
    for k in range(5):
        y = T + (H - T - B) * k / 4
        v = vmax - (vmax - vmin) * k / 4
        parts.append('<line x1="{}" y1="{:.1f}" x2="{}" y2="{:.1f}" stroke="#e6e6df" '
                     'stroke-width="1"/>'.format(L, y, W - R, y))
        parts.append('<text x="{}" y="{:.1f}" font-size="11" fill="#8a8a80" text-anchor="end" '
                     'dominant-baseline="central">{}</text>'.format(L - 8, y, vfmt.format(v)))
    if threshold:
        ty = py(threshold)
        if T <= ty <= H - B:
            parts.append('<line x1="{}" y1="{:.1f}" x2="{}" y2="{:.1f}" stroke="#e24b4a" '
                         'stroke-width="1" stroke-dasharray="5 4"/>'.format(L, ty, W - R, ty))
            parts.append('<text x="{}" y="{:.1f}" font-size="11" fill="#a32d2d" '
                         'text-anchor="end">阈值 {:g} {}</text>'.format(W - R, ty - 8, threshold, unit))

    pts = " ".join("{:.1f},{:.1f}".format(px(i), py(v)) for i, v in zip(xs, vals))
    area = "{:.1f},{:.1f} {} {:.1f},{:.1f}".format(px(0), H - B, pts, px(len(xs) - 1), H - B)
    parts.append('<polygon points="{}" fill="#e6f1fb" opacity="0.75"/>'.format(area))
    parts.append('<polyline points="{}" fill="none" stroke="#185fa5" stroke-width="1.8" '
                 'stroke-linejoin="round"/>'.format(pts))
    for i, v in zip(xs, vals):
        parts.append('<circle cx="{:.1f}" cy="{:.1f}" r="{:.1f}" fill="#185fa5"/>'.format(
            px(i), py(v), 2.6 if len(rows) <= 60 else 1.5))

    label_step = max(1, len(rows) // 8)
    labelled = None
    for i in range(0, len(rows), label_step):
        day = rows[i]["dt"].strftime("%m-%d")
        if day == labelled:
            continue
        labelled = day
        parts.append('<text x="{:.1f}" y="{}" font-size="10.5" fill="#8a8a80" '
                     'text-anchor="middle">{}</text>'.format(px(i), H - B + 16, day))
    last_day = rows[-1]["dt"].strftime("%m-%d")
    if last_day != labelled:
        parts.append('<text x="{:.1f}" y="{}" font-size="10.5" fill="#8a8a80" '
                     'text-anchor="middle">{}</text>'.format(
                         px(len(rows) - 1), H - B + 16, last_day))
    parts.append('</svg>')
    return "".join(parts)


def _daily_use(rows, unit):
    consumed, hours = 0.0, 0.0
    for a, b in zip(rows, rows[1:]):
        dh = (b["dt"] - a["dt"]).total_seconds() / 3600.0
        if dh <= 0.5:
            continue
        d = b["remain"] - a["remain"]
        if d < 0:
            consumed += -d
            hours += dh
    if hours <= 0 or consumed <= 0:
        return None
    return consumed / hours * 24.0


def build_dashboard(cfg):
    rows = load_history()
    unit_price = float(cfg.get("unit_price") or 0)
    threshold = float(cfg.get("low_threshold") or 0)
    latest = rows[-1] if rows else None
    remain = latest["remain"] if latest else None
    unit = (latest.get("unit") if latest else "度") or "度"
    yuan = None
    if remain is not None:
        yuan = remain * unit_price if unit == "度" else remain
    rate = _daily_use(rows, unit)
    days_left = (remain / rate) if (rate and remain is not None and rate > 0) else None
    low = remain is not None and threshold and remain < threshold
    room_desc = " ".join(x for x in [
        (latest.get("area") if latest else ""), (latest.get("building") if latest else ""),
        (latest.get("floor") if latest else ""), (latest.get("room") if latest else "")] if x) or "未获取"

    def fmt(v, nd=2, dash="--"):
        return dash if v is None else ("{:,.%df}" % nd).format(v)

    big, big_unit = (fmt(remain), unit) if remain is not None else ("--", "")
    cards = [
        ("当前剩余", big, big_unit or "&nbsp;"),
        ("折算金额", ("¥ " + fmt(yuan)) if yuan is not None else "--",
         "单价 {:g} 元/度".format(unit_price)),
        ("预计可用", ("{:,.1f} 天".format(days_left)) if days_left else "--",
         "日均 {:,.2f} 度".format(rate) if rate else "样本不足"),
        ("记录条数", str(len(rows)), "阈值 {:g} {}".format(threshold, unit)),
    ]
    cards_html = "".join(
        '<div class="card"><div class="k">{}</div><div class="v">{}</div>'
        '<div class="s">{}</div></div>'.format(k, v, s) for k, v, s in cards)

    if low:
        banner = ('<div class="banner danger">余额 {:,.2f} {} 已低于阈值 {:g} {} —— '
                  '折算约 ¥{:.2f}，按当前速度还能用约 {}</div>').format(
            remain, unit, threshold, unit, yuan or 0,
            "{:.1f} 天".format(days_left) if days_left else "未知")
    elif remain is None:
        banner = ('<div class="banner warn">还没有成功记录过数据。先执行 '
                  '<code>python monitor.py login</code> 扫码登录，再执行 '
                  '<code>python monitor.py check</code>。</div>')
    else:
        banner = ('<div class="banner ok">余额充足（{:,.2f} {}，高于阈值 {:g} {}）</div>').format(
            remain, unit, threshold, unit)

    recent = rows[-30:][::-1]
    trs = "".join(
        "<tr><td>{}</td><td class='num'>{:,.2f}</td><td>{}</td><td class='num'>{}</td>"
        "<td>{}</td></tr>".format(
            r["dt"].strftime("%Y-%m-%d %H:%M"), r["remain"], r.get("unit", ""),
            ("¥ {:.2f}".format(r["remain"] * unit_price) if r.get("unit") == "度" else "--"),
            " ".join(x for x in (r.get("area", ""), r.get("building", ""),
                                 r.get("floor", ""), r.get("room", "")) if x) or "--")
        for r in recent) or '<tr><td colspan="5" class="empty">暂无记录</td></tr>'

    updated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    html_doc = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>宿舍电费监控</title>
<style>
  :root{{--bg:#f5f4ef;--panel:#ffffff;--line:#e6e6df;--tx:#2c2c2a;--tx2:#6b6b64;--acc:#185fa5}}
  *{{box-sizing:border-box}}
  body{{margin:0;background:var(--bg);color:var(--tx);font:14px/1.6 system-ui,-apple-system,"Microsoft YaHei",sans-serif;padding:28px 20px 48px}}
  .wrap{{max-width:860px;margin:0 auto}}
  h1{{font-size:20px;font-weight:600;margin:0 0 4px}}
  .sub{{color:var(--tx2);font-size:12.5px;margin-bottom:18px}}
  .banner{{border-radius:10px;padding:12px 16px;margin-bottom:18px;font-size:13.5px;border:1px solid}}
  .banner.danger{{background:#fcebeb;border-color:#f09595;color:#791f1f}}
  .banner.warn{{background:#faeeda;border-color:#ef9f27;color:#633806}}
  .banner.ok{{background:#e1f5ee;border-color:#5dcaa5;color:#085041}}
  .cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin-bottom:20px}}
  .card{{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 16px}}
  .card .k{{font-size:12.5px;color:var(--tx2)}}
  .card .v{{font-size:24px;font-weight:600;margin:6px 0 2px;color:var(--acc);word-break:break-all}}
  .card .s{{font-size:11.5px;color:var(--tx2)}}
  .panel{{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:20px}}
  .panel h2{{font-size:14px;font-weight:600;margin:0 0 12px}}
  table{{width:100%;border-collapse:collapse;font-size:13px}}
  th,td{{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left}}
  th{{font-weight:500;color:var(--tx2);font-size:12.5px}}
  td.num{{text-align:right;font-variant-numeric:tabular-nums}}
  td.empty{{text-align:center;color:var(--tx2);padding:22px}}
  tbody tr:last-child td{{border-bottom:none}}
  code{{background:#f1efe8;border-radius:4px;padding:1px 5px;font-size:12.5px}}
  .foot{{color:var(--tx2);font-size:11.5px;margin-top:22px;line-height:1.7}}
</style>
</head>
<body>
<div class="wrap">
  <h1>宿舍电费监控</h1>
  <div class="sub">数据源 nfuedu.zftcloud.com · 房间 {room} · 看板更新于 {updated}</div>
  {banner}
  <div class="cards">{cards}</div>
  <div class="panel">
    <h2>余额变化（共 {n} 次记录）</h2>
    {chart}
  </div>
  <div class="panel">
    <h2>最近记录</h2>
    <table>
      <thead><tr><th>时间</th><th class="num">剩余</th><th>单位</th><th class="num">折算</th><th>房间</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
  </div>
  <div class="foot">
    数据来自学校电控系统的估算值，页面提示「请以实际电表为准」。<br>
    余额变化中向上的跳变是充值，向下的斜线是消耗；红色虚线是低电阈值（只在坐标范围内时绘出）。
  </div>
</div>
</body>
</html>""".format(room=room_desc, updated=updated, banner=banner, cards=cards_html,
                    chart=_svg_chart(rows, unit, threshold), n=len(rows), rows=trs)
    DASHBOARD_PATH.write_text(html_doc, encoding="utf-8")
    return DASHBOARD_PATH


# ---------------------------------------------------------------- 命令

def cmd_login(cfg, args):
    if not (cfg.get("edge_path") or find_edge()):
        log("err", "找不到 msedge.exe")
        return 3

    print()
    log("info", "即将打开 Edge（专用配置目录，不影响你日常的 Edge）")
    print("       地址是选择页 /index/order —— 这才是正确的入口。")
    print()
    print("       请依次完成：")
    print("         1. 用手机支付宝扫码登录（出现授权页就点「同意」）")
    print("         2. 填「姓名」「学号」")
    print("         3. 依次选好 区域 → 楼栋名 → 楼层 → 房间名")
    print("       【不用点下一步，选完脚本就会自动记下来】")
    print()

    br = None
    try:
        br = Browser(cfg, headless=False, url=url_order(cfg))
        deadline = time.time() + cfg["login_timeout_s"]
        phase = "login"
        last_note = time.time()
        last_seen = {}

        while time.time() < deadline:
            try:
                raw = br.evaluate(EVAL_JS, timeout=15)
                state = json.loads(raw) if raw else {}
            except Exception:
                state = {}

            info = state.get("info")
            form = state.get("form") or {}
            seen_room = room_desc(form) if form.get("roomId") else None

            # 路径 A：直接到了余额页（服务端注入了 infoDataJson）
            if isinstance(info, dict) and info.get("roomId"):
                room = {k: (info.get(k) or "") for k in
                        ("custName", "custNo", "areaName", "architectureName",
                         "floorName", "roomName")}
                room["roomId"] = str(info.get("roomId"))
                for k in ("areaId", "architectureId", "floor"):
                    room.setdefault(k, "")
                save_room(room)
                cookies = br.get_cookies()
                save_cookies(cookies)
                log("ok", "登录成功！已保存 {} 个 cookie 到 data/cookies.json".format(len(cookies)))
                log("ok", "房间：{}（roomId={}）".format(room_desc(room), room["roomId"]))
                if not room.get("areaId"):
                    print("       注意：没抓到区域/楼栋/楼层的内部 id，但 roomId 已足够查询。")
                print()
                log("info", "现在可以执行：python monitor.py check")
                return 0

            # 路径 B：选择页上四项都选全了
            if form_complete(form):
                room = {k: (form.get(k) or "") for k in SELECT_FIELDS}
                save_room(room)
                cookies = br.get_cookies()
                save_cookies(cookies)
                log("ok", "登录成功！已保存 {} 个 cookie 到 data/cookies.json".format(len(cookies)))
                log("ok", "房间：{}（roomId={}）".format(room_desc(room), room.get("roomId")))
                print()
                log("info", "现在可以执行：python monitor.py check")
                return 0

            # 进度提示
            on_alipay = "alipay" in (state.get("url") or "").lower() or "支付宝" in (state.get("title") or "")
            if on_alipay:
                if phase != "login":
                    phase = "login"
                    last_note = 0
                if time.time() - last_note > 15:
                    last_note = time.time()
                    print("       ...等待扫码登录（还剩 {} 秒）".format(int(deadline - time.time())))
            else:
                if phase != "select":
                    phase = "select"
                    last_note = 0
                    print("       已登录。请在窗口里填姓名/学号，并选完 区域→楼栋→楼层→房间。")
                if seen_room and seen_room != last_seen.get("r"):
                    last_seen["r"] = seen_room
                    print("       当前选择：{}，还差一点就选完".format(seen_room))
                if time.time() - last_note > 20:
                    last_note = time.time()
                    print("       ...等待选择完成（还剩 {} 秒）".format(int(deadline - time.time())))
            time.sleep(2)

        log("err", "等待超时。请重试 python monitor.py login")
        return 3
    except Exception as exc:
        log("err", "启动浏览器失败：{}".format(exc))
        return 3
    finally:
        if br:
            br.kill()


def run_check(cfg, verbose=False, no_report=False):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    room = load_room()

    if not room.get("roomId"):
        msg = ("还没有记录宿舍信息（data/room.json 不存在或缺少 roomId）。"
               "请先执行 python monitor.py login 完成一次扫码并选好房间。")
        if verbose:
            log("err", msg)
        return {"status": "not_logged_in", "message": msg, "exit_code": 2}

    state, source = None, None

    try:
        state = query_via_http(cfg, room)
        if state and interpret(state)["status"] == "ok":
            source = "HTTP 直连"
    except Exception as exc:
        if verbose:
            log("info", "HTTP 路径异常：{}".format(exc))

    if not state or interpret(state)["status"] != "ok":
        if verbose and state:
            log("info", "HTTP 路径没拿到有效数据，改用浏览器")
        try:
            state2 = query_via_browser(cfg, room)
            if state2:
                state = state2
                source = "无头 Edge + cookie 注入"
        except Exception as exc:
            if verbose:
                log("info", "浏览器路径异常：{}".format(exc))

    if state is None:
        msg = "两条取数路径都没拿到页面状态"
        if verbose:
            log("err", msg)
        return {"status": "error", "message": msg, "exit_code": 3}

    try:
        LAST_STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

    parsed = interpret(state)
    status = parsed["status"]

    if status == "ok":
        d = parsed["data"]
        unit_price = float(cfg.get("unit_price") or 0)
        yuan = d["remain"] * unit_price if d["unit"] == "度" else d["remain"]
        append_history({
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "remain": d["remain"], "unit": d["unit"], "yuan": "{:.2f}".format(yuan),
            "room_id": d["room_id"], "area": d["area"], "building": d["building"],
            "floor": d["floor"], "room": d["room"],
            "cust_no": d["cust_no"], "cust_name": d["cust_name"], "note": "ok",
        })
        build_dashboard(cfg)
        up = None if no_report else upload_report(cfg, d)
        threshold = float(cfg.get("low_threshold") or 0)
        low = threshold and d["remain"] < threshold
        if verbose:
            rm = " ".join(x for x in [d["area"], d["building"], d["floor"], d["room"]] if x)
            log("ok", "剩余 {:,.2f} {}（约 ¥{:.2f}）  房间 {}   [来源: {}]".format(
                d["remain"], d["unit"], yuan, rm or d["room_id"], source))
            if low:
                log("warn", "低于阈值 {:g} {}，该充钱了".format(threshold, d["unit"]))
            print("        看板已更新：{}".format(DASHBOARD_PATH))
            if isinstance(up, dict):
                if up.get("ok"):
                    log("ok", "已上报到在线看板（昵称：{}）".format(display_name(cfg, d)))
                else:
                    log("warn", "上报在线看板失败：{}".format(up.get("error")))
        return {"status": "ok", "message": parsed["message"], "data": d,
                "yuan": round(yuan, 2), "low": bool(low), "source": source,
                "uploaded": up, "exit_code": 4 if low else 0}

    if verbose:
        log("warn" if status == "not_logged_in" else "err", parsed["message"])
        note = state.get("_note")
        if note:
            print("        细节：{}".format(note))
        if LAST_STATE_PATH.exists():
            print("        页面状态已存到：{}".format(LAST_STATE_PATH))
        if status == "not_logged_in":
            print("        修复办法：python monitor.py login")
    return {"status": status, "message": parsed["message"],
            "exit_code": 2 if status == "not_logged_in" else 3}


def append_run_log(path, result):
    """给定时任务留一行可查的痕迹。"""
    try:
        p = Path(path)
        if not p.is_absolute():
            p = ROOT / p
        p.parent.mkdir(parents=True, exist_ok=True)
        if result.get("status") == "ok":
            d = result.get("data") or {}
            detail = "{:,.2f} {}  ¥{:.2f}  roomId={}".format(
                d.get("remain", 0), d.get("unit", ""), result.get("yuan") or 0,
                d.get("room_id", ""))
            if result.get("low"):
                detail += "  [低于阈值]"
        else:
            detail = result.get("message", "")[:100]
        if isinstance(result.get("uploaded"), dict) and not result["uploaded"].get("ok"):
            detail += "  [看板上报失败]"
        with p.open("a", encoding="utf-8") as fh:
            fh.write("{}\t{}\t{}\t退出码{}\n".format(
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                result.get("status", "?"), detail, result.get("exit_code", "?")))
    except Exception:
        pass


def cmd_check(cfg, args):
    result = run_check(cfg, verbose=True, no_report=getattr(args, "no_report", False))
    log_path = getattr(args, "log", None)
    if log_path:
        append_run_log(log_path, result)
    if getattr(args, "json", False):
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return result["exit_code"]


def cmd_report(cfg, args):
    log("ok", "看板已生成：{}".format(build_dashboard(cfg)))
    return 0


def cmd_status(cfg, args):
    rows = load_history()
    ck = load_cookies()
    print("配置文件      : {}".format(CONFIG_PATH))
    print("Edge          : {}".format(cfg.get("edge_path") or "未找到"))
    print("配置目录      : {}".format(profile_path(cfg)))
    print("cookie 凭据   : {} 条{}".format(
        len(ck), " → data/cookies.json" if ck else "（没有，先跑 python monitor.py login）"))
    if ck:
        print("              其中 alipay {} 条 / 站点 {} 条".format(
            sum(1 for c in ck if "alipay" in (c.get("domain") or "")),
            sum(1 for c in ck if "zftcloud" in (c.get("domain") or ""))))
    room = load_room()
    print("宿舍信息      : {}".format(
        "{}（roomId={}）".format(room_desc(room), room.get("roomId"))
        if room.get("roomId") else "（没有，先跑 python monitor.py login 选好房间）"))
    if room.get("custName") or room.get("custNo"):
        print("               姓名 {} / 学号 {}".format(
            room.get("custName") or "?", room.get("custNo") or "?"))
    print("入口地址      : {}".format(url_order(cfg)))
    rep = (cfg.get("report_url") or "").strip()
    print("在线看板上报  : {}".format(
        "{}（昵称 {}）".format(rep, (cfg.get("display_name") or "").strip() or "姓名首字+同学")
        if rep else "未配置（在 config.json 里填 report_url 即可）"))
    print("电价 / 阈值   : {:g} 元/度 / {:g} 度".format(
        float(cfg.get("unit_price") or 0), float(cfg.get("low_threshold") or 0)))
    print("历史记录      : {} 条".format(len(rows)))
    if rows:
        print("最早 / 最新   : {} / {}".format(rows[0]["dt"].strftime("%Y-%m-%d %H:%M"),
                                          rows[-1]["dt"].strftime("%Y-%m-%d %H:%M")))
        print("最新余额      : {:,.2f} {}".format(rows[-1]["remain"], rows[-1].get("unit", "")))
        rate = _daily_use(rows, rows[-1].get("unit", "度"))
        if rate:
            print("日均用电      : {:,.2f} 度/天  →  预计可用 {:,.1f} 天".format(
                rate, rows[-1]["remain"] / rate))
    print("看板          : {} {}".format(DASHBOARD_PATH,
                                     "已存在" if DASHBOARD_PATH.exists() else "未生成"))
    return 0


def cmd_selftest(cfg, args):
    """不联网：验证 CDP 通道（启动浏览器 / 执行脚本 / 读写 cookie / cookie 是否真被发送）。"""
    import http.server
    import socketserver
    import threading

    port = 8791

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = ("<html><head><title>selftest</title></head><body>"
                    "<span id='reserveAM'>42.5</span></body></html>").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Set-Cookie", "selftest=OK123; Path=/")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    class Srv(socketserver.TCPServer):
        allow_reuse_address = True

        def handle_error(self, request, client_address):
            pass  # 关浏览器时会有连接重置，属正常，不必打印堆栈

    srv = Srv(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    ok, br = True, None
    try:
        print("1) 启动无头 Edge 并连上 CDP ...")
        br = Browser(cfg, headless=True, url="about:blank")
        log("ok", "调试端口 {}".format(br.port))

        print("2) 打开本地页面并执行脚本 ...")
        br.navigate("http://127.0.0.1:{}/".format(port))
        time.sleep(1.5)
        title = br.evaluate("document.title")
        reserve = br.evaluate("document.getElementById('reserveAM').innerText")
        log("ok", "title={!r}  reserveAM={!r}".format(title, reserve))
        ok = ok and title == "selftest" and reserve == "42.5"

        print("3) 读取 cookie ...")
        ck = br.get_cookies()
        names = [c["name"] for c in ck]
        log("ok", "共 {} 条，含 selftest: {}".format(len(ck), "selftest" in names))
        ok = ok and "selftest" in names

        print("4) 清空后重新注入 cookie ...")
        br.ws.call("Network.clearBrowserCookies", timeout=20)
        after_clear = [c["name"] for c in br.get_cookies()]
        br.set_cookies([c for c in ck if c["name"] == "selftest"])
        after_set = [c["name"] for c in br.get_cookies()]
        log("ok", "清空后 {} 条 → 注入后 {} 条".format(len(after_clear), len(after_set)))
        ok = ok and "selftest" not in after_clear and "selftest" in after_set

        print("5) 验证注入的 cookie 真的会被发出去 ...")
        br.navigate("http://127.0.0.1:{}/".format(port))
        time.sleep(1.2)
        sent = br.evaluate("document.cookie")
        log("ok", "document.cookie = {!r}".format(sent))
        ok = ok and "selftest=OK123" in (sent or "")
    except Exception as exc:
        log("err", "自检失败：{}".format(exc))
        ok = False
    finally:
        if br:
            br.kill()
        srv.shutdown()

    print()
    log("ok" if ok else "err",
        "CDP 通道自检全部通过 —— 可以执行 python monitor.py login" if ok
        else "自检未通过，把上面的输出发我看")
    return 0 if ok else 3


# ---------------------------------------------------------------- 本地网页服务
#
# 只监听 127.0.0.1，并且要求 URL 里带一个随机 token —— 因为浏览器里任何一个网页
# 都能往 http://127.0.0.1:端口 发请求，没有 token 的话就能被别的页面隔着屏幕点按钮。
# 另外校验 Host 头，防 DNS rebinding。

LOCAL_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>宿舍电费 · 本地看板</title>
<style>
  :root{--bg:#f5f4ef;--panel:#fff;--line:#e6e6df;--tx:#2c2c2a;--tx2:#6b6b64;--acc:#185fa5}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--tx);font:14px/1.6 system-ui,-apple-system,"Microsoft YaHei",sans-serif;padding:26px 18px 56px}
  .wrap{max-width:880px;margin:0 auto}
  h1{font-size:20px;font-weight:600;margin:0 0 4px}
  .sub{color:var(--tx2);font-size:12.5px;margin-bottom:18px}
  button{font:inherit;font-size:13px;padding:8px 16px;border-radius:9px;border:1px solid var(--line);background:var(--panel);cursor:pointer;color:var(--tx)}
  button:hover{border-color:var(--tx2)} button:disabled{opacity:.45;cursor:default}
  button.primary{background:var(--acc);border-color:var(--acc);color:#fff}
  button.danger{color:#a32d2d;border-color:#e6c9c9}
  .row{display:flex;flex-wrap:wrap;gap:10px;align-items:center}
  .stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin:16px 0 18px}
  .stat{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:13px 15px}
  .stat .k{font-size:12.5px;color:var(--tx2)} .stat .v{font-size:23px;font-weight:600;color:var(--acc);margin:5px 0 2px}
  .panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:18px}
  .panel h2{font-size:14px;font-weight:600;margin:0 0 10px}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{padding:7px 9px;border-bottom:1px solid var(--line);text-align:left}
  th{font-weight:500;color:var(--tx2);font-size:12.5px}
  td.num{text-align:right;font-variant-numeric:tabular-nums}
  tbody tr:last-child td{border-bottom:none}
  .note{font-size:12.5px;color:var(--tx2)}
  .hint{font-size:12.5px;color:var(--acc)}
  .warn{background:#faeeda;border:1px solid #ef9f27;color:#633806;border-radius:9px;padding:9px 12px;font-size:12.5px;margin-top:10px}
  .kv{font-size:12.5px;color:var(--tx2);line-height:1.9}
  .kv b{color:var(--tx);font-weight:500}
  .mask{position:fixed;inset:0;background:rgba(44,44,42,.35);display:flex;align-items:center;justify-content:center;z-index:9}
  .dlg{background:#fff;border-radius:14px;padding:20px 22px;max-width:360px;box-shadow:0 8px 30px rgba(0,0,0,.18)}
  .dlg h3{margin:0 0 8px;font-size:15px;font-weight:600}
  .dlg p{margin:0 0 16px;font-size:13px;color:var(--tx2)}
</style>
</head>
<body>
<div class="wrap">
  <h1>宿舍电费 · 本地看板</h1>
  <div class="sub" id="sub">加载中…</div>

  <div class="row">
    <button class="primary" id="btnCheck">立即查询</button>
    <button id="btnRelogin">换绑宿舍</button>
    <button class="danger" id="btnUnbind">取消绑定</button>
    <span class="hint" id="hint"></span>
  </div>
  <div class="warn" id="warn" style="display:none"></div>

  <div class="stats" id="stats"></div>

  <div class="panel">
    <h2>余额变化</h2>
    <div id="chart"></div>
  </div>

  <div class="panel">
    <h2>最近记录</h2>
    <table>
      <thead><tr><th>时间</th><th class="num">剩余</th><th>单位</th><th class="num">折算</th></tr></thead>
      <tbody id="rows"></tbody>
    </table>
  </div>

  <div class="panel">
    <h2>状态</h2>
    <div class="kv" id="kv"></div>
  </div>

  <div class="note">这个页面只在你这台机器上能打开。按 Ctrl+C 可停止服务。</div>
</div>

<div class="mask" id="mask" style="display:none">
  <div class="dlg">
    <h3 id="dlgTitle">确认</h3>
    <p id="dlgText"></p>
    <div class="row" style="justify-content:flex-end">
      <button id="dlgNo">取消</button>
      <button class="danger" id="dlgYes">确定</button>
    </div>
  </div>
</div>

<script>
const fmt=(n,d=2)=>(n===null||n===undefined||isNaN(n))?'--':Number(n).toLocaleString('zh-CN',{minimumFractionDigits:d,maximumFractionDigits:d});
const q=(s)=>document.querySelector(s);
let POLL=null;

async function api(path,opt){
  const r=await fetch(path,Object.assign({cache:'no-store'},opt||{}));
  try{ return await r.json(); }catch(e){ return {ok:false,error:'HTTP '+r.status}; }
}
const post=(act)=>api('api/'+act,{method:'POST',headers:{'X-Dorm':'1'}});

let S={};
async function load(){
  const d=await api('api/state');
  if(!d.ok){ q('#sub').textContent='读取状态失败：'+(d.error||''); return; }
  S=d; render();
}

function render(){
  const room=S.room;
  q('#sub').textContent = room
    ? ('当前绑定：' + (S.room_desc||('房间 '+room.roomId)))
    : '还没有绑定宿舍 —— 点「换绑宿舍」扫一次码即可';

  const L=S.latest;
  q('#stats').innerHTML=[
    ['当前剩余', L?fmt(L.remain):'--', L?L.unit:'&nbsp;'],
    ['折算金额', L&&L.yuan!==null?('¥ '+fmt(L.yuan)):'--', '单价 '+S.unit_price+' 元/度'],
    ['预计可用', S.days_left?fmt(S.days_left,1)+' 天':'--',
      S.rate?('日均 '+fmt(S.rate)+' 度'):'样本不足'],
  ].map(([k,v,s])=>'<div class="stat"><div class="k">'+k+'</div><div class="v">'+v+'</div><div class="k">'+s+'</div></div>').join('');

  q('#chart').innerHTML = S.count>1 ? S.chart
    : '<div class="note" style="padding:18px 0">只有 ' + S.count + ' 个数据点，还画不出趋势。多查几次就有了。</div>';

  q('#rows').innerHTML = (S.recent&&S.recent.length)
    ? S.recent.map(r=>'<tr><td>'+r.ts+'</td><td class="num">'+fmt(r.remain)+'</td><td>'+r.unit
        +'</td><td class="num">'+(r.yuan!==null&&r.yuan!==undefined?('¥ '+fmt(r.yuan)):'--')+'</td></tr>').join('')
    : '<tr><td colspan="4" class="note">暂无记录</td></tr>';

  q('#kv').innerHTML=[
    '登录态：<b>'+(S.logged_in?'已保存':'没有（需要扫码）')+'</b>',
    '房间号：<b>'+(room?room.roomId:'—')+'</b>　累计记录：<b>'+S.count+' 条</b>',
    '在线看板上报：<b>'+(S.report_url||'未配置')+'</b>',
    '最近一次运行：<b>'+(S.last_run||'没有记录')+'</b>',
  ].join('<br>');

  const w=q('#warn');
  if(!room){ w.style.display='block'; w.textContent='还没有绑定宿舍。点「换绑宿舍」会打开一个浏览器窗口，用手机支付宝扫码并选好房间即可。'; }
  else w.style.display='none';
}

function ask(title,text){
  return new Promise(res=>{
    q('#dlgTitle').textContent=title; q('#dlgText').textContent=text;
    q('#mask').style.display='flex';
    q('#dlgYes').onclick=()=>{ q('#mask').style.display='none'; res(true); };
    q('#dlgNo').onclick=()=>{ q('#mask').style.display='none'; res(false); };
  });
}

function busy(on,msg){ 
  ['#btnCheck','#btnRelogin','#btnUnbind'].forEach(s=>q(s).disabled=on);
  q('#hint').textContent=msg||'';
}

async function doCheck(){
  busy(true,'正在查询…');
  const d=await post('check');
  if(!d.ok){ busy(false,'查询失败：'+((d.result&&d.result.message)||d.error||'未知')); }
  else { busy(false,'查询成功'); }
  await load();
  if(!d.ok) return;
}

async function doRelogin(){
  if(S.room){
    const yes=await ask('换绑宿舍？','会先清除当前的房间绑定和登录态（历史记录保留），然后打开浏览器让你重新扫码。');
    if(!yes) return;
  }
  busy(true,'正在打开扫码窗口…');
  const d=await post('relogin');
  if(!d.ok){ busy(false,'打开失败：'+(d.error||'')); return; }
  busy(false,'请在弹出窗口里扫码；扫完这里会自动刷新');
  if(POLL) clearInterval(POLL);
  const t0=Date.now();
  POLL=setInterval(async ()=>{
    await load();
    if(S.room){ clearInterval(POLL); POLL=null; busy(false,'已绑定 '+(S.room_desc||'')); return; }
    if(Date.now()-t0>300000){ clearInterval(POLL); POLL=null; busy(false,'等太久了，没检测到绑定。可以关掉扫码窗口重试。'); }
  },3000);
}

async function doUnbind(){
  const yes=await ask('取消绑定？','会删除房间绑定和登录态（历史记录保留）。之后需要重新扫码才能继续查询。');
  if(!yes) return;
  busy(true,'正在解绑…');
  const d=await post('unbind');
  busy(false, d.ok?('已解绑（清掉 '+(d.removed||[]).join('、')+'）'):('解绑失败：'+(d.error||'')));
  await load();
}

q('#btnCheck').onclick=doCheck;
q('#btnRelogin').onclick=doRelogin;
q('#btnUnbind').onclick=doUnbind;
load();
</script>
</body>
</html>"""


def local_state(cfg):
    rows = load_history()
    room = load_room()
    unit_price = float(cfg.get("unit_price") or 0)
    threshold = float(cfg.get("low_threshold") or 0)
    latest = rows[-1] if rows else None
    unit = (latest.get("unit") if latest else "度") or "度"
    remain = latest["remain"] if latest else None
    yuan = None
    if remain is not None:
        yuan = remain * unit_price if unit == "度" else remain
    rate = _daily_use(rows, unit)
    days_left = (remain / rate) if (rate and remain is not None and rate > 0) else None

    last_run = ""
    try:
        lg = DATA_DIR / "run.log"
        if lg.exists():
            lines = lg.read_text(encoding="utf-8").strip().splitlines()
            if lines:
                last_run = lines[-1]
    except Exception:
        pass

    return {
        "room": room or None,
        "room_desc": room_desc(room) if room.get("roomId") else "",
        "logged_in": COOKIE_PATH.exists(),
        "count": len(rows),
        "latest": ({"remain": remain, "unit": unit, "yuan": yuan,
                    "ts": latest["dt"].strftime("%Y-%m-%d %H:%M")} if latest else None),
        "days_left": days_left,
        "rate": rate,
        "unit_price": unit_price,
        "low_threshold": threshold,
        "report_url": (cfg.get("report_url") or "").strip(),
        "last_run": last_run,
        "chart": _svg_chart(rows[-200:], unit, threshold) if rows else "",
        "recent": [{"ts": r["dt"].strftime("%m-%d %H:%M"), "remain": r["remain"],
                    "unit": r.get("unit", ""),
                    "yuan": (r["remain"] * unit_price if r.get("unit") == "度" else None)}
                   for r in rows[-15:][::-1]],
    }


class LocalServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, token):
        self.token = token
        super().__init__(addr, handler)

    def server_bind(self):
        # 跳过 HTTPServer.server_bind 里的 socket.getfqdn() 反向解析（本机实测要 9 秒）
        socketserver.TCPServer.server_bind(self)
        self.server_name = "localhost"
        self.server_port = self.server_address[1]


class LocalHandler(http.server.BaseHTTPRequestHandler):
    server_version = "dorm-monitor-local/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _authorized(self):
        host = self.headers.get("Host") or ""
        if not re.match(r"^(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$", host):
            return False                                    # 防 DNS rebinding
        return self.path.split("?")[0].startswith("/t/{}/".format(self.server.token))

    def do_GET(self):
        if not self._authorized():
            return self._json({"ok": False, "error": "unauthorized"}, 403)
        path = urllib.parse.urlparse(self.path).path
        cfg = load_config()
        if path == "/t/{}/".format(self.server.token):
            return self._send(200, LOCAL_PAGE, "text/html; charset=utf-8")
        if path == "/t/{}/api/state".format(self.server.token):
            return self._json({"ok": True, **local_state(cfg)})
        return self._json({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        if not self._authorized():
            return self._json({"ok": False, "error": "unauthorized"}, 403)
        if self.headers.get("X-Dorm") != "1":
            return self._json({"ok": False, "error": "缺少 X-Dorm 请求头"}, 403)
        act = urllib.parse.urlparse(self.path).path.rsplit("/", 1)[-1]
        cfg = load_config()

        if act == "check":
            try:
                res = run_check(cfg, verbose=False)
            except Exception as exc:
                return self._json({"ok": False, "error": str(exc)}, 500)
            return self._json({"ok": res.get("status") == "ok", "result": {
                "status": res.get("status"), "message": res.get("message"),
                "data": res.get("data"), "yuan": res.get("yuan"),
                "low": res.get("low"), "exit_code": res.get("exit_code")}})

        if act == "unbind":
            removed = []
            for p in (ROOM_PATH, COOKIE_PATH):
                try:
                    if p.exists():
                        p.unlink()
                        removed.append(p.name)
                except Exception:
                    pass
            return self._json({"ok": True, "removed": removed})

        if act == "relogin":
            for p in (ROOM_PATH, COOKIE_PATH):
                try:
                    p.unlink(missing_ok=True)
                except Exception:
                    pass
            try:
                subprocess.Popen([sys.executable, str(ROOT / "monitor.py"), "login"],
                                 cwd=str(ROOT), stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
            except Exception as exc:
                return self._json({"ok": False, "error": str(exc)}, 500)
            return self._json({"ok": True, "message": "已打开扫码窗口"})

        return self._json({"ok": False, "error": "not found"}, 404)


def cmd_serve(cfg, args):
    port = int(getattr(args, "port", None) or 8788)
    token = base64.urlsafe_b64encode(os.urandom(9)).decode("ascii").rstrip("=")
    try:
        srv = LocalServer(("127.0.0.1", port), LocalHandler, token)
    except OSError as exc:
        log("err", "端口 {} 起不来（{}）。换个端口试试：python monitor.py serve --port 8899".format(port, exc))
        return 3
    url = "http://127.0.0.1:{}/t/{}/".format(srv.server_address[1], token)
    print()
    log("ok", "本地看板已启动：")
    print("       {}".format(url), flush=True)
    print()
    print("       只在你这台机器上能访问（地址里的随机串是防火防盗的，别外发）。")
    print("       按 Ctrl+C 停止。")
    print()
    if not getattr(args, "no_open", False):
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print()
        log("info", "已停止")
    return 0


def main():
    setup_stdout()
    p = argparse.ArgumentParser(
        prog="monitor.py",
        description="宿舍电费监控（南方学院电控缴费系统 / Edge + CDP 方案）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
典型用法
    python monitor.py login     扫码登录一次，脚本自动保存登录凭据
    python monitor.py check     查询一次 → 追加历史 → 刷新 dashboard.html
    python monitor.py report    不改数据，只按历史重画看板
    python monitor.py status    看配置、cookie、历史一览
    python monitor.py selftest  离线自检 CDP 通道（不联网）

退出码
    0 查到数据   2 需要重新登录   3 其他异常   4 查到了但低于阈值
""")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("login", help="打开 Edge 完成一次支付宝扫码登录并保存凭据")
    pc = sub.add_parser("check", help="查询一次并记录")
    pc.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    pc.add_argument("--no-report", action="store_true", help="本次不上报到在线看板")
    pc.add_argument("--log", metavar="PATH", help="把本次结果追加到日志文件（定时任务用）")
    sub.add_parser("report", help="只重新生成看板")
    sub.add_parser("status", help="显示配置、cookie 与历史概览")
    sub.add_parser("selftest", help="离线自检 CDP 通道")
    pv = sub.add_parser("serve", help="开本地网页看板（查询 / 换绑 / 取消绑定）")
    pv.add_argument("--port", type=int, default=8788, help="本地端口，默认 8788")
    pv.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    args = p.parse_args()

    if not args.cmd:
        p.print_help()
        return 0

    cfg = load_config()
    return {"login": cmd_login, "check": cmd_check, "report": cmd_report,
            "status": cmd_status, "selftest": cmd_selftest,
            "serve": cmd_serve}[args.cmd](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
