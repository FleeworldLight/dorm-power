#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""进程入口。部署与接口说明见 ../doc/线上服务.md。"""
import os
import threading

from dorm.api import Handler, Server, janitor
from dorm.browser import find_chromium
from dorm.conf import DATA_DIR, PROFILE_DIR
from dorm.store import get_key


def main():
    port = int(os.environ.get("PORT") or 3000)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    get_key()
    threading.Thread(target=janitor, daemon=True).start()
    srv = Server(("0.0.0.0", port), Handler)
    print("宿舍电费服务已启动  http://0.0.0.0:{}".format(port), flush=True)
    print("chromium: {}".format(find_chromium() or "未找到"), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
