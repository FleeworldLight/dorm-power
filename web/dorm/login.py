#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫码登录会话的生命周期。"""

import json
import shutil
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from .browser import Browser, capture_qr, find_chromium
from .conf import PROFILE_DIR, SCHOOL_BASE, UA
from .school import get_levels
from .store import rand_token


_sessions_lock = threading.Lock()


_sessions = {}          # sid -> LoginSession


_login_hits = {}        # ip -> [ts, ...]


class LoginSession:
    def __init__(self, sid, cfg):
        self.sid = sid
        self.cfg = cfg
        self.state = "starting"        # starting waiting scanned logged_in error expired
        self.error = ""
        self.cookies = []
        self.levels = []
        self.qr = ""
        self.qr_seq = 0
        self.qr_at = 0.0
        self.created = time.time()
        self.touched = time.time()
        self.br = None
        self.br_closed = False      # 浏览器已关但 cookie 保留，后续查询还要用
        self.lock = threading.Lock()

    def to_json(self, include_qr=True):
        d = {"sid": self.sid, "state": self.state, "error": self.error,
             "levels": self.levels, "qr_seq": self.qr_seq,
             "age": round(time.time() - self.created, 1)}
        if include_qr:
            d["qr"] = self.qr
        return d


def _guest_page_js():
    return """(() => {
  const v = (id) => { const e = document.getElementById(id); return (e && e.value !== undefined) ? e.value : null; };
  let info = null;
  try { info = (typeof infoDataJson !== 'undefined') ? infoDataJson : null; } catch (e) {}
  return JSON.stringify({
    url: location.href, title: document.title, ready: document.readyState,
    onSite: location.href.indexOf(%s) === 0,
    hasRoomSelect: !!document.getElementById('room'),
    hasQr: !!document.querySelector('canvas, .qrcode-body img, img[src*="qr"]'),
    info: info, custNo: v('custNo'), custName: v('custName')
  });
})()""" % json.dumps(SCHOOL_BASE)


def start_login(sid, cfg):
    s = LoginSession(sid, cfg)
    profile = PROFILE_DIR / sid
    try:
        s.br = Browser(profile, url=SCHOOL_BASE + "/index/order")
        s.br.set_viewport(430, 800, 1)
        s.br.navigate(SCHOOL_BASE + "/index/order")
        s.state = "waiting"
    except Exception as exc:
        s.state = "error"
        s.error = "启动浏览器失败: {}".format(exc)
    return s


def poll_login(s, cfg):
    """推进登录会话状态机。不做阻塞等待，每次被轮询时推进一步。"""
    s.touched = time.time()
    if s.state in ("error", "logged_in", "expired"):
        return s
    if s.br_closed:
        return s                    # 浏览器已关，会话 cookie 还在，供「换个宿舍」复用
    if s.br is None:
        s.state = "error"
        s.error = "浏览器句柄丢失"
        return s
    if time.time() - s.created > 900:
        s.state = "expired"
        s.error = "登录会话超时（15 分钟）"
        return s
    try:
        raw = s.br.evaluate(_guest_page_js(), timeout=20)
        d = json.loads(raw) if raw else {}
    except Exception as exc:
        s.error = "读取页面失败: {}".format(exc)
        return s

    if d.get("onSite") and d.get("hasRoomSelect"):
        try:
            s.cookies = s.br.get_cookies()
            s.levels = get_levels(s.cookies)
            s.qr = ""
            s.state = "logged_in"
        except Exception as exc:
            s.state = "error"
            s.error = "登录已通过，但读取层级失败: {}".format(exc)
        return s

    if s.state == "waiting":
        s.state = "scanned" if d.get("onSite") else "waiting"

    # 二维码自己会刷新，得隔几秒重截；字节没变时不重发（前端比对 qr_seq），不费流量。
    if d.get("ready") == "complete" and time.time() - s.qr_at > 8:
        try:
            png = capture_qr(s.br)
            if png:
                if png != s.qr:
                    s.qr = png
                    s.qr_seq += 1
                s.qr_at = time.time()
        except Exception:
            pass
    return s


def diag(cfg, want_qr=False):
    out = {"chromium": find_chromium(), "python": sys.version.split()[0], "school": {}}
    try:
        req = urllib.request.Request(SCHOOL_BASE + "/index/order", headers={"User-Agent": UA})
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                out["school"] = {"status": r.status, "ms": int((time.time() - t0) * 1000),
                                 "final": r.url[:120]}
        except urllib.error.HTTPError as e:
            out["school"] = {"status": e.code, "ms": int((time.time() - t0) * 1000),
                             "note": "HTTPError（302 未跟随也算通）"}
    except Exception as exc:
        out["school"] = {"error": "{}: {}".format(type(exc).__name__, exc)}

    if want_qr and out.get("chromium"):
        sid = "diag" + rand_token(4)
        br = None
        try:
            br = Browser(PROFILE_DIR / sid, url=SCHOOL_BASE + "/index/order")
            br.set_viewport(430, 800, 1)
            br.navigate(SCHOOL_BASE + "/index/order")
            time.sleep(6)
            raw = br.evaluate(_guest_page_js(), timeout=20)
            out["page"] = json.loads(raw) if raw else {}
            png = capture_qr(br)
            out["qr_png_len"] = len(png)
            out["qr_png"] = png[:200000]
        except Exception as exc:
            out["qr_error"] = "{}: {}".format(type(exc).__name__, exc)
        finally:
            if br:
                br.kill()
            shutil.rmtree(PROFILE_DIR / sid, ignore_errors=True)
    return out
