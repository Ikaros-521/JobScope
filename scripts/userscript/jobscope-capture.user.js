// ==UserScript==
// @name         JobScope 岗位捕获
// @namespace    jobscope.local
// @version      1.1.0
// @description  在任意浏览器的 Boss 直聘页面上，把当前页看到的岗位一键推送到本地 JobScope 捕获篮。浏览时照常用自己的定位与筛选，看到的即可捕获。
// @match        https://www.zhipin.com/*
// @grant        GM_xmlhttpRequest
// @connect      127.0.0.1
// @connect      localhost
// ==/UserScript==

(function () {
  'use strict'
  // 端口与 start.ps1 默认生产实例一致；对 8766 测试实例验收时改成 8766。
  const JOBSCOPE_PORT = 8765
  const API = `http://127.0.0.1:${JOBSCOPE_PORT}/api/import/basket/remote`

  // 与后端 boss_import.CARDS_JS 同一套抽取逻辑：锚点定位卡片容器，类名启发式取字段。
  // 排除右侧 sider-recommend 推送栏（平台推荐不是用户收藏/搜索结果）；
  // 后端会重建 id 与 URL 并清洗文本，这里抽到什么送什么。
  function collectCards() {
    const clsOf = (el) => { const c = el && el.className; return typeof c === 'string' ? c : (c && c.baseVal) || '' }
    const pick = (root, rx) => {
      for (const el of root.querySelectorAll('[class]')) {
        if (rx.test(clsOf(el))) return (el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 160)
      }
      return ''
    }
    const one = (root, sel) => {
      const el = root.querySelector(sel)
      return el ? (el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 160) : ''
    }
    const grab = (root) => ({
      title: one(root, '[class*="job-name-text"]') || pick(root, /(^|[\s-])(job-name|job-title)/),
      salary: pick(root, /salary/),
      company: one(root, '.company-info b') || one(root, '[class*="company-name"]') || pick(root, /company-name|company-info/),
      area: pick(root, /job-area|job-city|job-location|(^|[\s-])location/).replace(/[\[\]【】]/g, ''),
    })
    const cards = []
    const seen = new Set()
    for (const a of document.querySelectorAll('a[href*="/job_detail/"]')) {
      const match = (a.getAttribute('href') || '').match(/\/job_detail\/([A-Za-z0-9_-]+)\.html/)
      if (!match || seen.has(match[1])) continue
      if (a.closest('[class*="sider-recommend"]')) continue
      seen.add(match[1])
      let card = a.closest('li') || a
      for (let i = 0; i < 3; i++) {
        const parent = card.parentElement
        if (!parent) break
        card = parent
        if (/job-card|job-box|search-job|job-list/i.test(clsOf(card)) || card.tagName === 'LI') break
      }
      let fields = grab(card)
      if (!fields.salary && !fields.title && card.parentElement) fields = grab(card.parentElement)
      const tags = [...card.querySelectorAll('[class*="tag"]')]
        .map(e => (e.textContent || '').replace(/\s+/g, ' ').trim())
        .filter(t => t && t.length <= 20).slice(0, 8)
      cards.push({
        id: match[1],
        link_text: (a.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 160),
        text: (card.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 300),
        tags, ...fields,
      })
    }
    return cards
  }

  function notify(text, ok) {
    const tip = document.createElement('div')
    tip.textContent = text
    tip.style.cssText = `position:fixed;right:64px;bottom:74px;z-index:2147483647;max-width:320px;padding:10px 14px;` +
      `border-radius:8px;font-size:13px;line-height:1.6;color:#fff;background:${ok ? '#173e35' : '#a03030'};` +
      `box-shadow:0 8px 24px #0003;font-family:system-ui,sans-serif;`
    document.body.appendChild(tip)
    setTimeout(() => tip.remove(), 4000)
  }

  function push() {
    const cards = collectCards()
    if (!cards.length) { notify('本页没有解析到岗位卡片；请在岗位列表/公司职位页再试。', false); return }
    GM_xmlhttpRequest({
      method: 'POST',
      url: API,
      headers: { 'Content-Type': 'application/json' },
      data: JSON.stringify({ page_url: location.href, items: cards }),
      timeout: 8000,
      onload: (response) => {
        try {
          const data = JSON.parse(response.responseText)
          if (response.status === 200) notify(data.message || '已推送到 JobScope 捕获篮。', true)
          else notify(data.detail || `推送失败（HTTP ${response.status}）`, false)
        } catch (e) { notify(`JobScope 返回了无法读取的响应（HTTP ${response.status}）`, false) }
      },
      onerror: () => notify(`连不上 JobScope（127.0.0.1:${JOBSCOPE_PORT}）；请确认本地服务已启动，且脚本 @connect 已允许。`, false),
      ontimeout: () => notify('JobScope 响应超时，请稍后重试。', false),
    })
  }

  function mount() {
    if (document.getElementById('jobscope-capture-btn')) return
    const button = document.createElement('button')
    button.id = 'jobscope-capture-btn'
    button.type = 'button'
    button.textContent = '→ JobScope'
    button.title = '把本页岗位推送到本地 JobScope 捕获篮'
    button.style.cssText = `position:fixed;right:18px;bottom:64px;z-index:2147483647;padding:8px 12px;border:none;` +
      `border-radius:999px;background:#173e35;color:#fff;font-size:12px;cursor:pointer;` +
      `box-shadow:0 6px 18px #0003;font-family:system-ui,sans-serif;`
    button.addEventListener('click', event => { event.preventDefault(); push() })
    document.body.appendChild(button)
  }

  // Boss 是 SPA：路由切换不重载页面，轮询补挂保证按钮常在。
  setInterval(mount, 3000)
  mount()
})()
