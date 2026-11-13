"""Read attributable full job descriptions from the user's project Chrome."""

import re
import threading
import time
from urllib.parse import urlsplit

from . import browser
from .connectors import plain
from .repository import now

batch_lock = threading.Lock()

# innerText excludes hidden anti-copy text and preserves visible line breaks.
DETAIL_SNAPSHOT = r'''return (() => {
  const text = s => document.querySelector(s)?.innerText?.trim() || '';
  const section = [...document.querySelectorAll('.job-detail-section')].find(
    e => e.querySelector('h3')?.innerText?.trim() === '职位描述');
  const description = section?.querySelector('.job-sec-text')?.innerText?.trim() || '';
  return {url: location.href, title: text('.job-banner h1'),
    description, salary: text('.job-banner .salary'),
    conditions: [...document.querySelectorAll('.job-banner .text-desc')].map(e => e.innerText.trim()).join(' · '),
    company: text('.sider-company .company-info'),
    company_profile: text('.job-detail-company .job-sec-text'),
    company_overview: text('.sider-company'),
    address: text('.job-location .location-address') || text('.job-address'),
    page_title: document.title};
})()'''

# 智联详情页的整段 JD 在页面自带的 SSR 状态里；展示类名随改版漂移，状态字段更稳。
ZHAOPIN_DETAIL_SNAPSHOT = r'''return (() => {
  const dp = ((window.__INITIAL_STATE__ || {}).jobDetail || {}).detailedPosition || {};
  return {url: location.href, title: dp.name || '', description: dp.description || '',
    raw_description: dp.jobDesc || '', salary: dp.salary || '', company: dp.companyName || '',
    working_exp: dp.workingExp || '', education: dp.education || '',
    page_title: document.title};
})()'''

# 详情读取只允许这两个已登记的主机与路径形态；其余一律拒绝，防止重放任意页面。
DETAIL_SOURCES = {
    'boss': ('www.zhipin.com', r'/job_detail/[A-Za-z0-9_-]+\.html'),
    'zhaopin': ('www.zhaopin.com', r'/jobdetail/[A-Za-z0-9]+\.htm'),
}


def valid_detail_url(job):
    rule = DETAIL_SOURCES.get(job.get('source'))
    if not rule:
        return False
    try:
        url = urlsplit(job.get('url', ''))
        return (url.scheme == 'https' and url.netloc == rule[0]
                and bool(re.fullmatch(rule[1], url.path))
                and not url.query and not url.fragment)
    except ValueError:
        return False


def _verification_block(snapshot):
    """Both sources interpose a verification or login page instead of the posting."""
    url = snapshot.get('url', '')
    page_title = str(snapshot.get('page_title', ''))
    if any(part in url for part in ('/safe/', '/verify', 'security-check')) or '验证' in page_title or 'Verification' in page_title:
        return dict(status='needs_verification', message='详情页要求人工验证，已暂停补采；请在项目Chrome完成验证后继续')
    if '/login' in url or '/passport' in url:
        return dict(status='needs_login', message='详情页要求重新登录，请在项目Chrome中完成')
    return None


def normalize_detail(job, snapshot):
    blocked = _verification_block(snapshot)
    if blocked:
        return blocked
    # 保存的岗位与快照必须同源同路径（scheme+host+path），防止任意页面冒名覆盖。
    if not valid_detail_url(job) or urlsplit(snapshot.get('url', ''))[:3] != urlsplit(job['url'])[:3]:
        return dict(status='failed', message='详情页岗位标识不一致，未覆盖原岗位')
    if job['source'] == 'zhaopin':
        return _normalize_zhaopin(job, snapshot)
    return _normalize_boss(job, snapshot)


def _normalize_boss(job, snapshot):
    description = snapshot.get('description', '').strip()
    title = snapshot.get('title', '').strip()
    if not title or len(description) < 30 or len(description) > 100000:
        return dict(status='unavailable', message='未取得完整职位描述，可能已下架或页面结构变化；保留原摘要')
    conditions = snapshot.get('conditions', '').strip()
    updated = {**job, 'title': title, 'description': '\n'.join(filter(None, [conditions, description])),
               'description_status': 'complete', 'detail_collected_at': now(),
               'detail_source_url': job['url'], 'detail_transport': 'browser_dom'}
    if snapshot.get('salary'):
        updated['salary'] = snapshot['salary']
    # The listing company is the identity scope; a detail-page name is attributed
    # as a platform statement, not silently substituted as a legal entity.
    company_text = '\n\n'.join(str(snapshot.get(k, '')).strip() for k in
                               ('company', 'company_overview', 'company_profile') if snapshot.get(k))
    evidence = None
    if company_text:
        evidence = dict(title=f"{job['company']} · 招聘平台公司自述", company=job['company'],
                        url=job['url'], kind='company_statement',
                        text='来源为招聘详情页的公司自述及平台展示，非独立核验。\n' + company_text[:100000])
    return dict(status='completed', message='完整职位描述已读取并保存，平台自述仍需核实',
                job=updated, company_evidence=evidence)


def _normalize_zhaopin(job, snapshot):
    description = str(snapshot.get('description', '') or '').strip()
    if not description and snapshot.get('raw_description'):
        description = plain(snapshot['raw_description'])
    title = str(snapshot.get('title', '') or '').strip()
    if not title or len(description) < 30 or len(description) > 100000:
        return dict(status='unavailable', message='未取得完整职位描述，可能已下架或页面结构变化；保留原摘要')
    # 与搜索页摘要保持同一头部格式，正文整段替换截断预览。
    head = [f'{label}：{plain(snapshot.get(key))}' for label, key in (('经验', 'working_exp'), ('学历', 'education'))
            if plain(snapshot.get(key))]
    updated = {**job, 'title': title, 'description': '\n'.join(head + ['', description]),
               'description_status': 'complete', 'detail_collected_at': now(),
               'detail_source_url': job['url'], 'detail_transport': 'browser_dom'}
    if plain(snapshot.get('salary')):
        updated['salary'] = plain(snapshot['salary'])
    return dict(status='completed', message='完整职位描述已读取并保存；公司为平台展示名称，法定主体未核验',
                job=updated, company_evidence=None)


def collect_detail(job, data_dir):
    if not valid_detail_url(job):
        return dict(status='unsupported', message='仅支持已保存的BOSS或智联原始岗位详情链接')
    port = browser._connection(data_dir)
    if not port:
        return dict(status='needs_login', message='请先打开项目Chrome并登录对应平台')
    if not browser._lock.acquire(blocking=False):
        return dict(status='backoff', message='项目浏览器正在采集，请稍后重试')
    tab = None
    output = None
    try:
        with browser.session_lock(data_dir) as acquired:
            if not acquired:
                return dict(status='backoff', message='其他进程正在使用项目浏览器')
            from DrissionPage import Chromium
            tab = Chromium(f'127.0.0.1:{port}').new_tab()
            try:
                tab.get(job['url'], timeout=20)
            except Exception:
                # A loaded document can still be readable after a subresource timeout.
                pass
            script = ZHAOPIN_DETAIL_SNAPSHOT if job.get('source') == 'zhaopin' else DETAIL_SNAPSHOT
            for attempt in range(6):
                snapshot = tab.run_js(script)
                output = normalize_detail(job, snapshot)
                if output['status'] != 'unavailable':
                    break
                if attempt < 5:
                    time.sleep(1)
            return output
    except Exception:
        output = dict(status='failed', message='岗位详情读取失败，原摘要和已保存正文未修改')
        return output
    finally:
        if tab is not None and output and output['status'] not in {'needs_login', 'needs_verification'}:
            try:
                tab.close()
            except Exception:
                pass
        browser._lock.release()


def save_detail(repo, output):
    saved = repo.save_job(output['job'], output['job'].get('source', 'boss'))
    if output.get('company_evidence'):
        repo.save_evidence(output['company_evidence'], output['job'].get('source', 'boss') + '_detail')
    return saved


def pending_jobs(repo, jobs=None):
    # detail_transport='page_state' 是旧版把搜索页截断摘要误标为完整的记录，也必须重读详情页。
    # jobs 参数允许调用方复用已取的全量岗位（状态端点一次全量供过滤与计数两用）。
    pool = repo.jobs() if jobs is None else jobs
    return [j for j in pool if j['source'] in DETAIL_SOURCES
            and (j.get('description_status') != 'complete' or j.get('detail_transport') == 'page_state')]


def run_batch(repo, task, limit=500, ids=None, collector=None, pause=time.sleep):
    collector = collector or collect_detail
    if ids:
        # 反查指定岗位（收藏页入口）：不限 complete 状态——反查的意义就是重读已完整的岗位看变化。
        wanted = set(ids)
        pending = [j for j in repo.jobs() if j['id'] in wanted][:limit]
        message = '正在反查指定岗位，逐条保存，可在中断后继续'
    else:
        pending = pending_jobs(repo)[:limit]
        message = '正在补全完整JD，逐条保存，可在中断后继续'
    task.update(kind='job_details', total=len(pending), processed=0, count=0, failed=0,
                status='running', message=message)
    repo.update_task(task)
    consecutive_errors = 0
    try:
        for index, job in enumerate(pending):
            task.update(current_job=job['id'], message=f"正在读取 {index + 1}/{len(pending)}：{job['title']}")
            repo.update_task(task)
            output = collector(job, data_dir=repo.data_dir)
            task['processed'] += 1
            if output['status'] == 'completed':
                save_detail(repo, output)
                task['count'] += 1
                consecutive_errors = 0
            elif output['status'] in {'needs_login', 'needs_verification', 'backoff'}:
                task.update(status=output['status'], message=f"已保存{task['count']}份完整JD。" + output['message'])
                repo.update_task(task)
                return
            else:
                task['failed'] += 1
                consecutive_errors += 1
                if consecutive_errors >= 3:
                    task.update(status='failed', message=f"连续3份详情未能读取，已暂停；已保存{task['count']}份完整JD。" + output['message'])
                    repo.update_task(task)
                    return
            repo.update_task(task)
            if index < len(pending) - 1:
                pause(2)
        task.update(status='completed', message=f"本批完成：保存{task['count']}份完整JD，{task['failed']}份未读取；再次执行会跳过已完成岗位")
    except Exception:
        task.update(status='failed', message=f"补采中断，已保存{task['count']}份完整JD，可继续未完成岗位")
    repo.update_task(task)
