#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HTTP 接口层与看板读模型。

本模块**只提供 JSON 接口**，不渲染页面也不服务静态资源 ——
前端是独立部署的静态站（见仓库 `dist/`，可直接扔 GitHub Pages）。
两边通过 CORS 通信，白名单见 `allowed_origin`。"""

import json
import re
import shutil
import socketserver
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from .conf import MAX_BODY, PROFILE_DIR, load_cfg
from .login import _login_hits, _sessions, _sessions_lock, diag, poll_login, start_login
from .school import fetch_room_page, get_level_options, query_remain, session_is_real, site_cookies
from .store import (_lock, clean_report, get_key, load_users, open_user,
                    purge_expired, rand_token, roll_point, save_users, upsert_user)


# ---------------------------------------------------------------- CORS

# 浏览器发预检时要看的请求头。改前端如果新增了自定义头，记得在这里补上。
ALLOW_HEADERS = "Content-Type"
ALLOW_METHODS = "GET, POST, DELETE, OPTIONS"


def allowed_origin(cfg, origin):
    """把请求的 Origin 映射成回给浏览器的 Access-Control-Allow-Origin。

    这个服务现在是**纯 API**，前端可能部署在别处（例如 GitHub Pages），
    所以跨域是正常状态而不是异常。但也不能无脑回 *：回显具体来源才允许
    带凭据，也才好排查「谁在调我」。

    cors_origins 支持三种写法：
      "*"                     —— 谁都行（只在自己机器上调试时用）
      "https://a.com"         —— 精确匹配
      "https://*.github.io"   —— 通配子域（*. 只允许出现在最前面）
    留空 = 只允许同源（浏览器不发 Origin 或 Origin 与 Host 一致时不拦）。
    """
    rules = cfg.get("cors_origins")
    if isinstance(rules, str):
        rules = [rules]
    rules = [str(r).strip().rstrip("/") for r in (rules or []) if str(r).strip()]
    if not rules:
        return None
    if "*" in rules:
        return "*"
    if not origin:
        return None
    o = origin.rstrip("/")
    for r in rules:
        if r == o:
            return o
        if r.startswith("https://*.") or r.startswith("http://*."):
            scheme, _, tail = r.partition("*.")
            # 必须落在 scheme:// 之后，避免 evilgithub.io 被 *.github.io 误放行
            if o.startswith(scheme) and o[len(scheme):].endswith("." + tail):
                return o
    return None


class Handler(BaseHTTPRequestHandler):
    server_version = "dorm-power/3.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # 跨域放行：只回显白名单里的来源，不回 *（除非配置里明确写了 *）。
        ok_origin = allowed_origin(load_cfg(), self.headers.get("Origin"))
        if ok_origin:
            self.send_header("Access-Control-Allow-Origin", ok_origin)
            if ok_origin != "*":
                self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Headers", ALLOW_HEADERS)
        self.send_header("Access-Control-Allow-Methods", ALLOW_METHODS)
        self.send_header("Access-Control-Max-Age", "600")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _body_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        if n > MAX_BODY:
            raise ValueError("请求体过大")
        raw = self.rfile.read(n).decode("utf-8", "replace")
        return json.loads(raw or "{}")

    def _q(self):
        u = urllib.parse.urlparse(self.path)
        return u.path, {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}

    def do_OPTIONS(self):
        self._send(204, b"")

    # ---------------- GET
    def do_GET(self):
        path, qp = self._q()
        cfg = load_cfg()
        purge_expired(cfg)

        if path in ("/", "/index.html"):
            # 这里不提供页面了 —— 前端是独立部署的静态站（见 dist/）。
            # 回一段 JSON 说明，免得直接访问域名的人对着空白页面发懵。
            return self._json({"code": 200, "msg": "dorm-power API",
                               "data": {"frontend": "见仓库 dist/ 目录（可部署到 GitHub Pages）",
                                        "health": "/healthz", "api": "/api/summary"}})
        if path == "/healthz":
            return self._json({"code": 200, "msg": "Health Checked", "data": "Pod alive"})
        if path == "/api/diag":
            return self._json(diag(cfg, want_qr=bool(qp.get("qr"))))
        if path == "/api/summary":
            return self._json(summary(cfg, qp.get("utoken") or ""))
        if path == "/api/session/state":
            sid = qp.get("sid") or ""
            with _sessions_lock:
                s = _sessions.get(sid)
            if not s:
                return self._json({"ok": False, "error": "会话不存在或已过期"}, 404)
            try:
                seen = int(qp.get("qr_seq") or -1)
            except ValueError:
                seen = -1
            with s.lock:
                poll_login(s, cfg)
                return self._json(dict({"ok": True},
                                       **s.to_json(include_qr=(seen != s.qr_seq))))
        if path == "/api/me":
            ut = qp.get("utoken") or ""
            rec, _p = open_user(cfg, ut)
            if not rec:
                return self._json({"ok": False, "error": "没有这条记录"}, 404)
            out = {"ok": True, "room": public_user(rec, cfg)}
            # 只有拿得到 utoken 的人（记录主人）才拿得到回填用的姓名学号
            if qp.get("prefill") in ("1", "true"):
                p = _p or {}
                rm = p.get("room") or {}
                out["prefill"] = {"custName": rm.get("custName") or "",
                                  "custNo": rm.get("custNo") or ""}
            return self._json(out)
        if path == "/api/export":
            if not admin_ok(cfg, qp.get("token")):
                return self._json({"ok": False, "error": "需要管理员口令"}, 403)
            return self._json(load_users())
        return self._json({"ok": False, "error": "not found: " + path}, 404)

    # ---------------- POST
    def do_POST(self):
        path, qp = self._q()
        cfg = load_cfg()
        try:
            body = self._body_json()
        except Exception as exc:
            return self._json({"ok": False, "error": str(exc)}, 400)

        if path == "/api/report":
            try:
                rec = clean_report(body)
            except Exception as exc:
                return self._json({"ok": False, "error": str(exc)}, 400)
            # 带 client_id 的按“来源”归档（换宿舍时整条替换）；没带的按宿舍归档。
            cid = str(body.get("client_id") or "").strip()
            if not re.fullmatch(r"[0-9a-fA-F]{8,64}", cid):
                cid = ""
            ut = ("rep:" + cid) if cid else ("ext:" + rec["room_id"])
            with _lock:
                db = load_users()
                old = db["users"].get(ut) or {}
                # 本机脚本走的也是同一套"按天"规则：当天重复上报只更新当天那笔。
                hist, pend = roll_point(old, rec["remain"], rec["unit"], rec["ts"])
                keep = int(cfg.get("history_keep") or 500)
                if len(hist) > keep:
                    del hist[:-keep]
                db["users"][ut] = {
                    "utoken": ut, "nickname": rec["who"], "room_id": rec["room_id"],
                    "area": rec["area"], "building": rec["building"],
                    "floor": rec["floor"], "room": rec["room"],
                    "remain": rec["remain"], "unit": rec["unit"],
                    "ts": rec["ts"], "created": old.get("created") or rec["ts"],
                    "pending": False, "source": "report", "verified": True,
                    "pend": pend, "history": hist, "sealed": "",
                }
                db["updated"] = rec["ts"]
                save_users(db)
            return self._json({"ok": True, "room_id": rec["room_id"],
                               "remain": rec["remain"], "unit": rec["unit"],
                               "mode": "manual-report"})

        if path == "/api/session/start":
            ip = self.client_address[0]
            if not rate_ok(ip, cfg):
                return self._json({"ok": False, "error": "操作太频繁，请稍后再试"}, 429)
            with _sessions_lock:
                live = sum(1 for s in _sessions.values()
                           if s.state in ("starting", "waiting", "scanned"))
                if live >= int(cfg.get("max_login_sessions") or 3):
                    return self._json({"ok": False,
                                       "error": "同时登录的人太多，请稍等一会儿再试"}, 429)
                sid = rand_token(9)
                s = start_login(sid, cfg)
                _sessions[sid] = s
            with s.lock:
                poll_login(s, cfg)
                return self._json(dict({"ok": True}, **s.to_json()))

        if path == "/api/session/levels":
            sid = body.get("sid") or ""
            idx = int(body.get("index") or 0)
            chosen = body.get("chosen") or {}
            with _sessions_lock:
                s = _sessions.get(sid)
            if not s or s.state != "logged_in":
                return self._json({"ok": False, "error": "会话不在已登录状态"}, 400)
            try:
                lv = s.levels[idx]
                opts = get_level_options(s.cookies, lv, chosen)
                return self._json({"ok": True, "options": opts, "level": lv})
            except Exception as exc:
                return self._json({"ok": False, "error": str(exc)}, 500)

        if path == "/api/session/commit":
            return self._commit(cfg, body)

        if path == "/api/refresh":
            return self._refresh(cfg, body)

        if path == "/api/admin/drop":
            # 删掉一条记录（管理员用）：?token=<admin_token>&utoken=ext:3050
            if not admin_ok(cfg, qp.get("token")):
                return self._json({"ok": False, "error": "需要管理员口令"}, 403)
            ut = qp.get("utoken") or ""
            if not ut:
                return self._json({"ok": False, "error": "需要 utoken"}, 400)
            with _lock:
                db = load_users()
                existed = db["users"].pop(ut, None) is not None
                save_users(db)
            return self._json({"ok": True, "dropped": ut, "existed": existed})

        if path == "/api/import":
            if not admin_ok(cfg, qp.get("token")):
                return self._json({"ok": False, "error": "需要管理员口令"}, 403)
            rooms = body.get("users")
            if not isinstance(rooms, dict):
                return self._json({"ok": False, "error": "需要 {\"users\":{...}}"}, 400)
            db = load_users()
            db["users"].update(rooms)
            save_users(db)
            return self._json({"ok": True, "count": len(rooms)})

        return self._json({"ok": False, "error": "not found: " + path}, 404)

    # ---------------- DELETE
    def do_DELETE(self):
        path, qp = self._q()
        cfg = load_cfg()
        if path == "/api/me":
            ut = qp.get("utoken") or ""
            db = load_users()
            existed = db["users"].pop(ut, None) is not None
            save_users(db)
            return self._json({"ok": True, "deleted": existed})
        return self._json({"ok": False, "error": "not found: " + path}, 404)

    # ---------------- 业务
    def _commit(self, cfg, body):
        sid = body.get("sid") or ""
        with _sessions_lock:
            s = _sessions.get(sid)
        if not s or s.state != "logged_in":
            return self._json({"ok": False, "error": "登录会话已失效，请重新扫码"}, 400)

        chosen = body.get("chosen") or {}
        names = body.get("names") or {}
        room = {k: (body.get(k) or "") for k in ("custName", "custNo")}
        for k in ("areaId", "architectureId", "floor", "roomId"):
            room[k] = str(chosen.get(k) or body.get(k) or "")
        for k in ("areaName", "architectureName", "floorName", "roomName"):
            room[k] = str(names.get(k) or body.get(k) or "")

        # 带 utoken 就是「换个宿舍」：复用同一条记录（替换而非新增），姓名学号沿用上一次。
        ut_in = str(body.get("utoken") or "").strip()
        prev_rec, prev_payload = (None, None)
        if ut_in and re.fullmatch(r"[A-Za-z0-9_\-]{16,64}", ut_in):
            prev_rec, prev_payload = open_user(cfg, ut_in)
        prev_room = (prev_payload or {}).get("room") or {}
        for k in ("custName", "custNo"):
            if not room.get(k):
                room[k] = prev_room.get(k) or ""

        if not room["custName"] or not room["custNo"]:
            return self._json({"ok": False, "error": "姓名和学号必填"}, 400)
        if not room["roomId"]:
            return self._json({"ok": False, "error": "没选到宿舍"}, 400)

        try:
            remain, unit, pending = query_remain(s.cookies, room["roomId"])
        except Exception as exc:
            return self._json({"ok": False, "error": "查询失败：" + str(exc)}, 502)

        page = None
        try:
            page = fetch_room_page(s.cookies, room)
        except Exception:
            page = None
        if page:
            for k in ("areaName", "architectureName", "floorName", "roomName"):
                room[k] = room.get(k) or (page.get(k) or "")
            if page.get("roomId"):
                room["roomId"] = str(page["roomId"])

        room["_cookies"] = site_cookies(s.cookies)
        utoken = ut_in if prev_rec else rand_token(24)
        nickname = ((body.get("nickname") or "").strip()
                    or (prev_rec or {}).get("nickname") or (room["custName"][:1] + "同学"))

        # 换了房间就把旧记录清掉：不同房间的读数混进同一条历史会误导趋势图
        if prev_rec and str(prev_rec.get("room_id") or "") != str(room["roomId"]):
            with _lock:
                dbx = load_users()
                dbx["users"].pop(utoken, None)
                save_users(dbx)

        with _lock:
            rec = upsert_user(cfg, get_key(), utoken, nickname, room, remain, unit, pending)
        # 浏览器可以关掉省资源，但会话留着 —— 用户还能接着走「换个宿舍」；闲置 15 分钟由 janitor 回收。
        try:
            s.br.kill()
        except Exception:
            pass
        s.br_closed = True
        s.touched = time.time()
        return self._json({"ok": True, "utoken": utoken, "remain": remain, "unit": unit,
                           "room": public_user(rec, cfg)})

    def _refresh(self, cfg, body):
        """刷新用户自己那一间。同样不拿登录态卡它，只把结果记成 verified。"""
        ut = body.get("utoken") or ""
        rec, payload = open_user(cfg, ut)
        if not rec:
            return self._json({"ok": False, "error": "没有这条记录，请重新扫码"}, 404)
        if not payload:
            return self._json({"ok": False, "error": "本地凭据解不开，请重新扫码"}, 400)
        cookies = payload.get("cookies") or []
        room = payload.get("room") or {}
        if not cookies:
            return self._json({"ok": False, "error": "这条记录没有凭据，只能靠本机脚本上报"}, 400)
        now = time.time()
        if now - _last_refresh.get(ut, 0) < 20:
            return self._json({"ok": False, "throttled": True, "error": "刚刚刷过"})
        _last_refresh[ut] = now
        real, _why = session_is_real(cookies, room)
        try:
            remain, unit, pending = query_remain(cookies, room.get("roomId"))
        except Exception as exc:
            return self._json({"ok": False, "error": "刷新失败：" + str(exc)}, 502)
        with _lock:
            rec = upsert_user(cfg, get_key(), ut,
                              rec.get("nickname", ""), dict(room, _cookies=cookies),
                              remain, unit, pending, verified=real)
        return self._json({"ok": True, "remain": remain, "unit": unit,
                           "verified": real, "room": public_user(rec, cfg)})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self):
        # 跳过 HTTPServer.server_bind 里的 getfqdn() 反向解析：本机实测 9.2 秒，DNS 不通会卡住启动。
        socketserver.TCPServer.server_bind(self)
        self.server_name = "localhost"
        self.server_port = self.server_address[1]


def admin_ok(cfg, token):
    want = (cfg.get("admin_token") or "").strip()
    return bool(want) and token == want


def rate_ok(ip, cfg, limit=None, window=3600):
    limit = int(limit or cfg.get("login_rate_limit") or 20)
    now = time.time()
    hits = [t for t in _login_hits.get(ip, []) if now - t < window]
    if len(hits) >= limit:
        _login_hits[ip] = hits
        return False
    hits.append(now)
    _login_hits[ip] = hits
    return True


def janitor():
    """回收闲置的登录会话与浏览器进程。"""
    while True:
        time.sleep(60)
        try:
            now = time.time()
            with _sessions_lock:
                dead = [sid for sid, s in _sessions.items()
                        if now - s.touched > 900 or s.state in ("error", "expired")]
                gone = [_sessions.pop(sid) for sid in dead]
            for s in gone:
                try:
                    if s.br:
                        s.br.kill()
                except Exception:
                    pass
                shutil.rmtree(PROFILE_DIR / s.sid, ignore_errors=True)
        except Exception:
            pass


def public_user(rec, cfg):
    return {k: rec.get(k) for k in
            ("room_id", "area", "building", "floor", "room", "nickname",
             "remain", "unit", "ts", "created")}


# 打开页面就会自动刷新一次。按宿舍节流，防止有人反复刷把学校站点打爆。
_last_refresh = {}


def room_item(rec, series):
    """把一条记录 + 合并好的曲线做成看板用的一条。"""
    now = time.time()
    try:
        t = time.mktime(time.strptime(rec.get("ts") or "", "%Y-%m-%d %H:%M:%S"))
        age = max(0.0, (now - t) / 3600.0)
    except Exception:
        age = 999.0

    byday = {}
    for ts, v in sorted(series.items()):
        byday[ts[:10]] = [ts, v]                     # 一天只留最后一个点
    pts = list(byday.values())
    if len(pts) > 40:                                # 抽稀，省手机流量；保留首尾
        step = (len(pts) + 39) // 40
        pts = pts[::step] + [pts[-1]]

    return {
        "room_id": rec.get("room_id"), "remain": rec.get("remain"),
        "unit": rec.get("unit", "度"),
        "area": rec.get("area", ""), "building": rec.get("building", ""),
        "floor": rec.get("floor", ""), "room": rec.get("room", ""),
        "nickname": rec.get("nickname", ""), "ts": rec.get("ts", ""),
        "age_hours": round(age, 2), "pending": bool(rec.get("pending")),
        "source": rec.get("source", "scan"), "verified": bool(rec.get("verified", True)),
        "history": pts,
    }


def summary(cfg, utoken=""):
    """**只返回调用者自己那一间**。没有 utoken（或对不上）就给空，不给任何别人的数据。

    同一间宿舍可能因为换浏览器重扫而留下多条记录，它们的曲线在这里合并 ——
    同一块电表，读数本来就该连起来。
    """
    purge_expired(cfg)
    pub = {k: cfg[k] for k in ("site_title", "site_subtitle", "unit_price",
                               "low_threshold", "stale_hours", "source_url") if k in cfg}
    rec, _payload = open_user(cfg, utoken) if utoken else (None, None)
    if not rec:
        return {"rooms": [], "bound": False, "updated": None, "config": pub}

    rid = rec.get("room_id")
    series = {}
    for r in load_users().get("users", {}).values():
        if rid and str(r.get("room_id")) != str(rid):
            continue                                  # 只看自己这间，别人的一律跳过
        pts = list(r.get("history") or [])
        if (r.get("pend") or {}).get("ts"):
            pts.append(r["pend"])                     # 当天那笔临时读数也算进曲线
        for h in pts:
            try:
                series[str(h.get("ts", ""))] = float(h.get("remain"))
            except (TypeError, ValueError):
                continue
    return {"rooms": [room_item(rec, series)], "bound": True,
            "updated": rec.get("ts"), "config": pub}
