#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""路径、站点常量与运行配置。"""

import json
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent   # web/


DATA_DIR = Path(os.environ.get("DATA_DIR") or (ROOT / "data"))


USERS_PATH = DATA_DIR / "users.json"


KEY_PATH = DATA_DIR / "key.bin"


PROFILE_DIR = DATA_DIR / "profiles"


CONFIG_PATH = ROOT / "config.json"


SCHOOL_BASE = "http://nfuedu.zftcloud.com/electricity_system"


ALIPAY_HOST = "openauth.alipay.com"


ALLOWED_HOSTS = ("nfuedu.zftcloud.com", "alipay.com", "alipayobjects.com", "alipayimg.com")


DEFAULT_CONFIG = {
    "site_title": "宿舍电费看板",
    "site_subtitle": "南方学院 · 电控缴费系统",
    "unit_price": 0.6259,
    "low_threshold": 20.0,
    "stale_hours": 36,
    "history_keep": 500,
    "source_url": SCHOOL_BASE + "/index/order",
    "session_ttl_days": 30,
    "max_users": 200,
    "max_login_sessions": 3,
    "admin_token": "",
    # 跨域白名单。前端独立部署（如 GitHub Pages）时必须登记它的来源，
    # 否则浏览器会拦掉响应。支持精确来源与 "https://*.github.io" 这类通配。
    # 留空 = 只服务同源请求。
    "cors_origins": [],
}


UA = ("Mozilla/5.0 (Linux; Android 13; zh-CN; M2012K11AC) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36 "
      "AliApp(AP/10.5.90.8000) AlipayClient/10.5.90.8000")


SELECT_FIELDS = ("custName", "custNo", "areaId", "architectureId", "floor", "roomId",
                 "areaName", "architectureName", "floorName", "roomName")


MAX_BODY = 128 * 1024


def load_cfg():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
    return cfg
