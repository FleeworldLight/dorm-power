# 宿舍电费看板 · 文档

南方学院（`nfuedu.zftcloud.com`）电控缴费系统的余额查询工具。仓库里有三个独立可用的部分：

| 目录 | 是什么 | 文档 |
|---|---|---|
| `dist/` | **前端静态站**：纯 HTML/CSS/JS，可直接部署到 GitHub Pages | [前端静态站.md](前端静态站.md) |
| `web/` | **后端 API**：部署到公网沙箱，只提供 JSON 接口 | [线上服务.md](线上服务.md) |
| `monitor.py` | **本地 CLI**：跑在自己电脑上，单人使用，顺便把数据上报给看板 | [本地工具.md](本地工具.md) |

## 代码结构

```
订阅电费/
├── monitor.py                 本地 CLI（单文件，只用标准库 + 本机 Edge）
├── config.json                本地 CLI 的配置（本机状态，不入库）
├── data/                      本地数据：cookies / 历史 / 上次状态（不入库）
├── doc/                       本目录（文档）
├── sync-docs.sh               dist/ -> docs/ 的同步脚本
├── dist/                      前端静态站（源文件，唯一真相）
│   ├── index.html             页面骨架（无模板占位符，纯静态）
│   ├── config.js              ★ 唯一的部署开关：后端地址
│   ├── app.css                样式
│   └── app.js                 前端逻辑
├── docs/                      GitHub Pages 发布目录（由脚本生成，别手改）
└── web/                       后端 API（部署这一个目录到沙箱）
    ├── app.py                 进程入口，只负责启动
    ├── config.json            运行配置（管理员口令、CORS 白名单）
    └── dorm/                  服务端实现
        ├── conf.py            路径、站点常量、默认配置
        ├── store.py           配置读写、凭据加解密、用户库
        ├── school.py          与学校站点的 HTTP 交互
        ├── browser.py         无头浏览器 + 手写 CDP（只用于扫码）
        ├── login.py           扫码登录会话的生命周期
        └── api.py             HTTP 接口层（纯 JSON）与看板读模型
```

全部**零第三方依赖**：只用 Python 标准库，加上本机 / 容器里已有的浏览器。

## 前后端为什么分开

因为**两边的部署条件不一样**：

- 扫码登录必须靠服务器上的无头浏览器截支付宝二维码，这部分**只能在沙箱跑**。
- 而 GitHub Pages 只能托管静态文件。前端做成静态站就能放上去，白拿一个 CDN。

于是前端读 `config.js` 里的 `API_BASE`，跨域调后端。改一处地址就能搬家：
本地调试填 `http://localhost:3000`，Pages 上填沙箱域名。

## 快速开始

本地单人用：

```bash
python monitor.py login     # 弹出 Edge，手机支付宝扫码
python monitor.py check     # 查一次，写历史
python monitor.py report    # 重新生成看板 dashboard.html
```

起后端：

```bash
cd web
PORT=3000 python3 app.py
```

看前端（另开一个终端）：

```bash
cd dist
python3 -m http.server 8080
# 把 config.js 里的 API_BASE 改成 http://localhost:3000
```

## 那个站为什么这么难搞

它没有账号密码登录：入口 `/index/pay` 会强制 302 到支付宝 OAuth
（`app_id=2018082461109526`，`scope=auth_user,auth_base,auth_ecard`）。
数据接口 `POST /index/getReserveAM` 认的是「支付宝绑定过的 PHP 会话」，
房间列表则由服务端在登录后注入页面。

更麻烦的是支付宝的登录态是**会话级 cookie**，浏览器进程一退出就丢。
所以无论哪种用法，都绕不开「用真实浏览器完成一次扫码」这一步 ——
区别只在于之后能不能纯 HTTP 重放（能，见 [线上服务.md](线上服务.md)）。
