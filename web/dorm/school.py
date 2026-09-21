#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""学校电控站的 HTTP 交互（选房间四级联动、查余额、会话有效性）。"""

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from .conf import SCHOOL_BASE, SELECT_FIELDS, UA


def _school_post(path, fields, cookie_hdr, timeout=25):
    body = urllib.parse.urlencode(fields, encoding="utf-8").encode()
    req = urllib.request.Request(SCHOOL_BASE + path, data=body, method="POST", headers={
        "Cookie": cookie_hdr, "User-Agent": UA,
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": SCHOOL_BASE + "/index/order",
        "Accept": "application/json, text/html, */*",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


def cookies_to_header(cookies):
    return "; ".join("{}={}".format(c["name"], c.get("value", ""))
                     for c in cookies if c.get("name"))


def site_cookies(cookies):
    return [c for c in cookies
            if "zftcloud" in (c.get("domain") or "") or "zhihuianxin" in (c.get("domain") or "")]


def get_levels(cookies):
    _, text = _school_post("/index/getLevels", {}, cookies_to_header(cookies))
    j = json.loads(text)
    if str(j.get("respCode")) != "00" or not isinstance(j.get("data"), list):
        raise RuntimeError("getLevels 返回异常: " + text[:120])
    return j["data"]


def get_level_options(cookies, level, chosen):
    _, text = _school_post("/index" + level["url"], {
        "areaId": chosen.get("areaId", ""),
        "architectureId": chosen.get("architectureId", ""),
        "floorId": chosen.get("floor", ""),
        "roomId": chosen.get("roomId", ""),
    }, cookies_to_header(cookies))
    j = json.loads(text)
    if str(j.get("respCode")) != "00":
        return []
    data = j.get("data") or []
    out = []
    for d in data:
        if isinstance(d, dict) and "key" in d:
            out.append({"key": str(d.get("key")), "value": str(d.get("value", ""))})
    return out


def query_remain(cookies, room_id):
    _, text = _school_post("/index/getReserveAM", {"roomId": room_id},
                           cookies_to_header(cookies))
    j = json.loads(text)
    if str(j.get("respCode")) != "00":
        raise RuntimeError("getReserveAM respCode={}".format(j.get("respCode")))
    data = j.get("data") or {}
    if data.get("remainPower") is None:
        raise RuntimeError("响应里没有 remainPower")
    unit = "元" if data.get("remainName") == "yuan" else "度"
    pending = str((data.get("resultInfo") or {}).get("result")) == "3"
    return float(data["remainPower"]), unit, pending


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_NOREDIR = urllib.request.build_opener(_NoRedirect)


def session_is_real(cookies, room):
    """确认"确实是以该用户身份登录着"，而不是靠接口不校验授权的漏洞拿到值。

    实测该站三处接口的鉴权不一致：
      /index/getReserveAM  —— 只要请求里带任意 PHPSESSID 就返回余额（不校验授权）
      /index/getLevels     —— 同样不校验，能枚举出全部房间
      /index/pay           —— **真正校验**：真会话返回 200 并注入 infoDataJson；
                              会话失效则 302 到支付宝 OAuth
    所以只认 /index/pay 这一个判据：过不了它，就一律不采信、不上报。
    """
    try:
        body = urllib.parse.urlencode({k: room.get(k, "") for k in SELECT_FIELDS}).encode()
        req = urllib.request.Request(SCHOOL_BASE + "/index/pay", data=body, method="POST", headers={
            "Cookie": cookies_to_header(cookies), "User-Agent": UA,
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": SCHOOL_BASE + "/index/order",
            "Accept": "text/html,application/xhtml+xml",
        })
        resp = _NOREDIR.open(req, timeout=25)
        if resp.status != 200:
            return False, "会话已失效（HTTP {}）".format(resp.status)
        html = resp.read().decode("utf-8", "replace")
        m = re.search(r"var\s+infoDataJson\s*=\s*(\{.*?\}|\[\s*\])\s*;", html, re.S)
        if m and m.group(1).startswith("{"):
            return True, ""
        return False, "会话已失效，页面已不认得登录用户"
    except urllib.error.HTTPError as exc:
        if exc.code in (301, 302, 303, 307, 308):
            return False, "会话已失效，已被重定向到登录页"
        return False, "校验会话时返回 HTTP {}".format(exc.code)
    except Exception as exc:
        return False, "校验会话失败：{}".format(type(exc).__name__)


def fetch_room_page(cookies, room):
    _, html = _school_post("/index/pay", {k: room.get(k, "") for k in SELECT_FIELDS},
                           cookies_to_header(cookies))
    m = re.search(r"var\s+infoDataJson\s*=\s*(\{.*?\}|\[\s*\])\s*;", html, re.S)
    if not m:
        return None
    try:
        info = json.loads(m.group(1))
    except Exception:
        return None
    return info if isinstance(info, dict) and info.get("roomId") else None
