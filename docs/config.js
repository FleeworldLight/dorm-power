// 后端地址。改这一行就能把前端指向别的后端，不用动其他代码。
//
//   GitHub Pages 上部署  → 填后端公网地址，例如 'https://xxx.app.workbuddy.host'
//   本地开发             → 填 'http://localhost:3000'
//   和后端同域部署        → 留空字符串，走相对路径（同源请求，不需要 CORS）
//
// 注意：填了域名之后必须在后端 config.json 的 cors_origins 里登记本站域名，
// 否则浏览器会因 CORS 拦截而拿不到数据（后端日志能看到 OPTIONS 请求被拒）。
window.API_BASE = 'https://8ad4484a314b4596abe39a61cc2e5885.app.workbuddy.host';

// 后端在 /api/summary 里会返回 site_title 等展示配置，这里只放
// 「拿不到后端时」的兜底文案。
window.FALLBACK_TITLE = '宿舍电费看板';
