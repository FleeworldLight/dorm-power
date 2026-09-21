#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""配置文件读写、会话凭据加解密、用户库。"""

import base64
import hashlib
import hmac
import json
import os
import shutil
import struct
import threading
import time
from .conf import KEY_PATH, PROFILE_DIR, SELECT_FIELDS, USERS_PATH
from .school import site_cookies


_lock = threading.Lock()


def _load_json(path, default):
    if not path.exists():
        return default
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d if isinstance(d, type(default)) else default
    except Exception:
        return default


def _save_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_users():
    db = _load_json(USERS_PATH, {"users": {}, "updated": None})
    db.setdefault("users", {})
    return db


def save_users(db):
    # 兜底：真实姓名/学号只允许待在加密块里，任何路径都别往明文塞
    for r in db.get("users", {}).values():
        r.pop("cust_name", None)
        r.pop("cust_no", None)
    _save_json(USERS_PATH, db)


def clean_report(raw):
    """本地上报（monitor.py / 取数脚本）的字段白名单 + 区间校验。"""
    if not isinstance(raw, dict):
        raise ValueError("body 必须是 JSON 对象")
    room_id = str(raw.get("room_id") or "").strip()
    if not room_id.isdigit() or not (1 <= len(room_id) <= 12):
        raise ValueError("room_id 必须是 1-12 位数字")
    try:
        remain = float(raw.get("remain"))
    except (TypeError, ValueError):
        raise ValueError("remain 不是数字")
    if not (0 <= remain <= 1_000_000):
        raise ValueError("remain 超出合理范围")

    def text(v, limit=24):
        return str(v or "").strip().replace("\n", " ").replace("\r", " ")[:limit]

    return {
        "room_id": room_id, "remain": round(remain, 2),
        "unit": "元" if str(raw.get("unit")) == "元" else "度",
        "area": text(raw.get("area")), "building": text(raw.get("building")),
        "floor": text(raw.get("floor")), "room": text(raw.get("room")),
        "who": text(raw.get("who")) or "匿名",
        "ts": text(raw.get("ts"), 32) or time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def get_key():
    """32 字节随机密钥，首次启动生成，权限 0600。

    诚实说明：密钥与密文在同一台机器上，所以它能防的是「密文被单独导走 / 日志泄露 /
    有人直接翻数据文件」，防不住整机失陷。配合 30 天自动过期与删除按钮使用。
    """
    if KEY_PATH.exists():
        k = KEY_PATH.read_bytes()
        if len(k) == 32:
            return k
    KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    k = os.urandom(32)
    KEY_PATH.write_bytes(k)
    try:
        os.chmod(KEY_PATH, 0o600)
    except Exception:
        pass
    return k


def _keystream(key, nonce, n):
    out, ctr = b"", 0
    while len(out) < n:
        out += hashlib.blake2b(nonce + struct.pack(">Q", ctr), key=key, digest_size=64).digest()
        ctr += 1
    return out[:n]


def enc_bytes(key, plain):
    """blake2b 密钥流异或 + HMAC-SHA256 认证（加密后再 MAC）。"""
    nonce = os.urandom(16)
    ct = bytes(a ^ b for a, b in zip(plain, _keystream(key, nonce, len(plain))))
    mac = hmac.new(key, nonce + ct, hashlib.sha256).digest()
    return base64.b64encode(nonce + ct + mac).decode("ascii")


def dec_bytes(key, token):
    raw = base64.b64decode(token)
    if len(raw) < 16 + 32:
        raise ValueError("密文长度异常")
    nonce, ct, mac = raw[:16], raw[16:-32], raw[-32:]
    want = hmac.new(key, nonce + ct, hashlib.sha256).digest()
    if not hmac.compare_digest(mac, want):
        raise ValueError("密文校验失败")
    return bytes(a ^ b for a, b in zip(ct, _keystream(key, nonce, len(ct))))


def rand_token(n=32):
    return base64.urlsafe_b64encode(os.urandom(n)).decode("ascii").rstrip("=")


def roll_point(old, remain, unit, now=None):
    """把上一天留下的临时读数落档进 history，返回 (history, 当天这笔临时值)。

    历史按「天」记：当天反复刷新只更新 pend，跨天才把上一天最后一次读数记成历史点。
    """
    now = now or time.strftime("%Y-%m-%d %H:%M:%S")
    today = now[:10]
    hist = list(old.get("history") or [])
    pend = old.get("pend") or {}
    if pend.get("ts") and str(pend.get("day") or pend["ts"][:10]) != today:
        hist.append({"ts": pend["ts"], "remain": pend["remain"],
                     "unit": pend.get("unit", unit)})
    return hist, {"day": today, "ts": now, "remain": remain, "unit": unit}


def upsert_user(cfg, key, utoken, nickname, room, remain, unit, pending, verified=True):
    """写入一条读数（历史规则见 roll_point）。"""
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    db = load_users()
    old = db["users"].get(utoken) or {}

    hist, pend = roll_point(old, remain, unit, now)
    keep = int(cfg.get("history_keep") or 500)
    if len(hist) > keep:
        del hist[:-keep]

    rec = {
        "utoken": utoken,
        "nickname": nickname,
        "room_id": room.get("roomId"), "area": room.get("areaName", ""),
        "building": room.get("architectureName", ""), "floor": room.get("floorName", ""),
        "room": room.get("roomName", ""),
        "remain": remain, "unit": unit,
        "ts": now, "created": old.get("created") or now,
        "pending": bool(pending),
        "verified": bool(verified),          # 这次读数是否来自"验证过的登录态"
        "pend": pend,
        "history": hist,
        "sealed": enc_bytes(key, json.dumps({
            # 落盘这一刻强制只留学校域的 cookie，不依赖调用方先过滤。
            "cookies": sck(cfg, site_cookies(room.get("_cookies") or [])),
            "room": {k: room.get(k, "") for k in SELECT_FIELDS},
        }, ensure_ascii=False).encode("utf-8")),
    }
    db["users"][utoken] = rec
    db["updated"] = now
    save_users(db)
    return rec


def sck(cfg, cookies):
    return [{"name": c.get("name"), "value": c.get("value"), "domain": c.get("domain"),
             "path": c.get("path"), "secure": c.get("secure"), "httpOnly": c.get("httpOnly"),
             "sameSite": c.get("sameSite"), "expires": c.get("expires")}
            for c in cookies if c.get("name")]


def open_user(cfg, utoken):
    db = load_users()
    rec = db["users"].get(utoken)
    if not rec:
        return None, None
    try:
        payload = json.loads(dec_bytes(get_key(), rec["sealed"]).decode("utf-8"))
    except Exception:
        return rec, None
    return rec, payload


def touch_user(cfg, utoken, patch):
    db = load_users()
    if utoken in db["users"]:
        db["users"][utoken].update(patch)
        db["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        save_users(db)


_last_purge = [0.0]


def purge_expired(cfg):
    now0 = time.time()
    if now0 - _last_purge[0] < 600:      # 每个请求都扫一遍文件太浪费，10 分钟一次
        return []
    _last_purge[0] = now0
    ttl = float(cfg.get("session_ttl_days") or 30) * 86400
    report_ttl = float(cfg.get("report_ttl_days") or 7) * 86400
    now = time.time()
    db = load_users()
    dead = []
    for ut, r in db["users"].items():
        try:
            t = time.mktime(time.strptime(r.get("ts") or "", "%Y-%m-%d %H:%M:%S"))
        except Exception:
            t = now
        # 本机脚本上报的房间：超过 report_ttl_days 没更新就当是废弃了（换宿舍/毕业）
        limit = report_ttl if r.get("source") == "report" else ttl
        if now - t > limit:
            dead.append(ut)
    for ut in dead:
        db["users"].pop(ut, None)
    if dead:
        save_users(db)
    # 清掉过期的浏览器 profile
    if PROFILE_DIR.exists():
        for p in PROFILE_DIR.iterdir():
            try:
                if now - p.stat().st_mtime > 3600:
                    shutil.rmtree(p, ignore_errors=True)
            except Exception:
                pass
    return dead
