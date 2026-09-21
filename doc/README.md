# 宿舍电费看板 · 文档

南方学院（`nfuedu.zftcloud.com`）电控缴费系统的余额查询工具。仓库里有两个独立可用的部分：

| 目录 | 是什么 | 文档 |
|---|---|---|
| `web/` | **在线查询平台**：部署到公网，各人扫码绑定后**只看得到自己那一间** | [线上服务.md](线上服务.md) |
| `monitor.py` | **本地 CLI**：跑在自己电脑上，单人使用，顺便把数据上报给看板 | [本地工具.md](本地工具.md) |

## 代码结构

```
订阅电费/
├── monitor.py                 本地 CLI（单文件，只用标准库 + 本机 Edge）
├── config.json                本地 CLI 的配置
├── data/                      本地数据：cookies / 历史 / 上次状态
├── doc/                       本目录
└── web/                       在线服务（部署这一个目录）
    ├── app.py                 进程入口，只负责启动
    ├── config.json            运行配置（管理员口令等）
    ├── templates/index.html   页面骨架
    ├── static/app.css         样式
    ├── static/app.js          前端逻辑
    └── dorm/                  服务端实现
        ├── conf.py            路径、站点常量、默认配置
        ├── store.py           配置读写、凭据加解密、用户库
        ├── school.py          与学校站点的 HTTP 交互
        ├── browser.py         无头浏览器 + 手写 CDP（只用于扫码）
        ├── login.py           扫码登录会话的生命周期
        └── api.py             HTTP 接口层与看板读模型
```

两个部分都**零第三方依赖**：只用 Python 标准库，加上本机 / 容器里已有的浏览器。

## 快速开始

本地单人用：

```bash
python monitor.py login     # 弹出 Edge，手机支付宝扫码
python monitor.py check     # 查一次，写历史
python monitor.py report    # 重新生成看板 dashboard.html
```

部署在线服务：

```bash
cd web
PORT=3000 python3 app.py
```

## 那个站为什么这么难搞

它没有账号密码登录：入口 `/index/pay` 会强制 302 到支付宝 OAuth
（`app_id=2018082461109526`，`scope=auth_user,auth_base,auth_ecard`）。
数据接口 `POST /index/getReserveAM` 认的是「支付宝绑定过的 PHP 会话」，
房间列表则由服务端在登录后注入页面。

更麻烦的是支付宝的登录态是**会话级 cookie**，浏览器进程一退出就丢。
所以无论哪种用法，都绕不开「用真实浏览器完成一次扫码」这一步 ——
区别只在于之后能不能纯 HTTP 重放（能，见 [线上服务.md](线上服务.md)）。
