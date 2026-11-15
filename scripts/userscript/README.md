# JobScope 油猴脚本：任意浏览器一键推送 Boss 岗位

把日常浏览器里逛到的 Boss 岗位一键推进本地 JobScope 捕获篮——不需要项目浏览器，你的定位与筛选所见即所得。

## 安装

1. 浏览器装 [Tampermonkey](https://www.tampermonkey.net/) 扩展。
2. Tampermonkey 面板 → 「添加新脚本」→ 整体粘贴 [jobscope-capture.user.js](./jobscope-capture.user.js) 的内容 → 保存（Ctrl+S）。
3. 确认本地 JobScope 已启动（`start.ps1` 默认端口 8765）。对 8766 测试实例验收时，把脚本顶部 `JOBSCOPE_PORT` 改成 8766。
4. 打开任意 zhipin.com 页面，右下角出现绿色「→ JobScope」按钮即安装成功。

## 使用

- 在岗位搜索页 / 公司职位页点「→ JobScope」：当前页所有岗位卡进入 JobScope 的捕获篮（岗位研究页 → 「从 Boss 导入」可查看与确认入库）。
- 推送去重：同一岗位重复推送只合并字段，不会产生重复条目。
- 保密纪律：脚本只读取页面上可见的岗位卡片，只访问 `127.0.0.1` 本地服务，不发任何第三方请求。

## 工作原理与信任边界

- 抽取逻辑与后端 `boss_import.CARDS_JS` 同源：`a[href*="/job_detail/"]` 锚点上溯卡片容器，类名启发式取字段；Boss 改版时两边需要一起校准。
- 请求走 `GM_xmlhttpRequest`（油猴特权 API）+ `@connect 127.0.0.1`，**不需要** JobScope 服务端开 CORS。
- 服务端（`POST /api/import/basket/remote`）不信任客户端：external_id 与岗位 URL 一律服务端重建为 canonical 无参链接，文本字段过 `plain()` 与长度截断，`page_url` 必须是 zhipin.com 页面，否则整包拒绝。
