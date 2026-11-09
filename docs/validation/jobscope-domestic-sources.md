# JobScope 国内三平台采集实际验证

日期：2026-11-09（Asia/Shanghai）。本文只记录当前机器上实际发生过的请求与返回，不承诺长期访问成功率。智联招聘、前程无忧的数据来自用户在本机项目 Chrome 中的已登录会话；未绕过任何登录、验证码或签名校验，未把 Cookie 或认证响应写入结果与记录。

## 已实现的三条通路

| 来源 | 实际请求 | 实际结果 | 可交付边界 |
| --- | --- | --- | --- |
| 猎聘 | POST `https://api-c.liepin.com/api/com.liepin.searchfront4c.pc-search-job`，仅 6 个约定头（`X-Fscp-Version: 1.1`、`X-Fscp-Std-Info: {"client_id": "40108"}`、`X-Fscp-Trace-Id: <uuid>`、`X-Client-Type: web`、`X-Requested-With`、`Content-Type`），无 Cookie | HTTP 200，`flag=1`，`data.data.jobCardList` 42 条真实岗位；`dq=010` 时 40/40 为北京，另含 1–2 条外地“相关推荐” | 普通 HTTP 采集已实现；列表字段无岗位职责，岗位标记为“仅列表摘要”，补全前不评估适配；置顶与广告位标记“推广” |
| 智联招聘 | 浏览器会话内打开 `https://www.zhaopin.com/jobs/?kw=Python&jl=530`，读取页面自带 `__INITIAL_STATE__.positionList` | 会话内 20 条岗位，含 `jobDescription` 完整职责、`number` 岗位编号、`positionUrl` 详情链接；本机直连同一地址返回 2113 字节 “Security Verification” 页；用浏览器 UA 的 `curl` 可得 302195 字节真实页面 | 只在项目浏览器会话取页面数据，不重放来源请求；本机直连被来源指纹校验拦下，因此不作为可用通路 |
| 前程无忧 | 浏览器会话内同源 GET `https://we.51job.com/api/job/search-pc?keyword=…&searchType=2&sortType=0&jobArea=…&pageNum=1&pageSize=…` | 会话内 HTTP 200、213871 字节 JSON，`status:"1"`、`resultbody.job.items` 30 条，含 `jobDescribe`；本机直连返回 92975 字节阿里云 WAF 脚本挑战页 | 只在已过脚本校验的浏览器会话内同源取数；保存前按用户点名的城市复核 `jobAreaString` |

## 城市码逐个回验

城市码不是按经验填写，而是向该来源真实请求一次、再看返回岗位的工作城市：

- 智联 `jl`：北京 530、上海 538、广州 763、深圳 765、杭州 653、成都 801、武汉 736、南京 635、苏州 639、西安 854。每码返回 20 条，`workCity` 全部为对应城市；`jl` 留空返回全国岗位（出现南昌、忻州等）。
- 前程无忧 `jobArea`：北京 010000、上海 020000、广州 030200、深圳 040000、南京 070200、苏州 070300、杭州 080200、成都 090200、武汉 180200、西安 200200。省级码（如 030000、080000）会混返回其他城市，`030000`、`050000`、`100000`、`210000` 返回 0 条，因此采用回验过的城市级码并再按城市名复核。
- 猎聘 `dq`：北京 010、上海 020、广州 050020、深圳 050090、杭州 070020、成都 280020、武汉 170020、南京 060020、苏州 060080、西安 270020；留空为全国。未映射城市返回“暂不支持”，不会静默改用其他城市。

## 走不通的入口（保留以免重复踩）

- `https://www.zhaopin.com/jobsearch/?keyword=Python&jl=530` → 302 回首页；搜索结果页实际是 `/jobs/?kw=&jl=`。
- `https://fe-api.zhaopin.com/c/i/sou` → HTTP 404；`https://searchapi.zhaopin.com/api/unify/searchUni` → HTTP 400/4005，未登录不返回职位列表。
- `https://search.51job.com/query/api/getJobList` → 直连 302 到 `www.51job.com/in/missing.php`；`https://search.51job.com/list/…99,Python,2,1.html` → 200 但长度 0。
- `https://api.liepin.com/astute/lx/compcollection/c/p/getJobListBySearch` → 直接构造 POST 得到 405 或“参数错误”，未能独立复现页面调用；页面自身在同源会话内可正常取得数据。
- 搜索页 HTML 本身不含岗位数据（前程无忧、猎聘为客户端渲染），因此不能靠抓取 HTML 兜底。

## 尚未验证

- BOSS 浏览器通路本次未复测：该 Chrome 配置目录未登录 BOSS，采集如实返回“需要验证”。
- 猎聘完整岗位职责读取未实现，详情页正文选择器尚未实测；不做未验证的承诺。
- 翻页深度、更多关键词与长期可用性未测；来源改版或风控加严后可能失效，届时应表现为明确的失败状态而不是空结果。

## 回归

```powershell
cd E:\Github_Pro\ResumeLoom\jobscope
.venv\Scripts\python.exe -m pytest backend/tests -q   # 220 passed
cd frontend
npm run build                                          # tsc -b + vite build 通过
```
