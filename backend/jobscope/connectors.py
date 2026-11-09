"""Bounded, fixed-origin public-source collectors. No credentials in results."""
from __future__ import annotations

import html
import json
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx

DEFAULT_DATA = Path(__file__).resolve().parents[2] / '.data'
MAX_BYTES = 12 * 1024 * 1024
CACHE_SECONDS = 6 * 60 * 60
_cache_lock = threading.Lock()
CITY_CODES = {'北京': '101010100', '上海': '101020100', '广州': '101280100', '深圳': '101280600', '杭州': '101210100', '成都': '101270100', '武汉': '101200100', '南京': '101190100', '苏州': '101190400', '西安': '101110100', '全国': '100010000'}


def result(status='completed', message='', jobs=None, evidence=None):
    return {'status': status, 'message': message, 'jobs': jobs or [], 'evidence': evidence or []}


def plain(value):
    return html.unescape(re.sub(r'<[^>]*>', ' ', str(value or ''))).strip()


def _json(client, method, url, **kwargs):
    # No arbitrary origins or redirected requests. Stream before decoding.
    with client.stream(method, url, timeout=20, follow_redirects=False, **kwargs) as response:
        if response.status_code == 429:
            raise CollectionError('backoff', '来源限制访问频率，请稍后手动重试。')
        if response.status_code in (401,):
            raise CollectionError('needs_login', '来源要求有效登录会话。')
        if response.status_code in (403,):
            raise CollectionError('needs_verification', '来源拒绝请求或要求人工验证。')
        if response.status_code != 200:
            raise CollectionError('failed', f'来源返回 HTTP {response.status_code}，未采集有效数据。')
        chunks = []
        size = 0
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > MAX_BYTES:
                raise CollectionError('failed', '来源响应超过大小上限，已停止读取。')
            chunks.append(chunk)
        try:
            payload = json.loads(b''.join(chunks))
        except (ValueError, UnicodeError):
            raise CollectionError('failed', '来源未返回有效 JSON；可能是登录页、验证页或页面结构变化。') from None
        if not isinstance(payload, dict):
            raise CollectionError('failed', '来源响应结构不受支持。')
        return payload


class CollectionError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


def _safe_url(value, host):
    parsed = urlparse(str(value or ''))
    return str(value) if parsed.scheme == 'https' and parsed.hostname == host else ''


def _job_evidence(job, note=''):
    text = '\n'.join([job['title'], f"公司/招聘品牌：{job['company']}", f"地点：{job['location']}", f"招聘薪资：{job['salary'] or '未披露'}", note, job['description']])
    return {'title': job['title'], 'url': job['url'], 'source': job['source'], 'kind': 'job_posting', 'company': job['company'], 'text': text[:150000], 'published_at': job.get('published_at')}


def _remotive(client, query, location, limit, cache_dir):
    cache = Path(cache_dir) / 'remotive-snapshot.json'
    with _cache_lock:
        data = None
        cached = False
        try:
            if time.time() - cache.stat().st_mtime < CACHE_SECONDS:
                candidate = json.loads(cache.read_text(encoding='utf-8'))
                if isinstance(candidate, dict) and isinstance(candidate.get('jobs'), list):
                    data, cached = candidate, True
        except (OSError, ValueError):
            pass
        if data is None:
            data = _json(client, 'GET', 'https://remotive.com/api/remote-jobs')
            if not isinstance(data.get('jobs'), list):
                raise CollectionError('failed', 'Remotive 响应缺少岗位列表。')
            cache.parent.mkdir(parents=True, exist_ok=True)
            temporary = cache.with_suffix('.tmp')
            temporary.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
            temporary.replace(cache)
    jobs, evidence, seen = [], [], set()
    terms = query.casefold().split()
    for item in data['jobs']:
        if not isinstance(item, dict):
            continue
        content = plain(' '.join(str(item.get(k, '')) for k in ('title', 'company_name', 'description', 'tags'))).casefold()
        place = str(item.get('candidate_required_location') or '')
        if not all(term in content for term in terms) or (location and location.casefold() not in place.casefold()):
            continue
        url = _safe_url(item.get('url'), 'remotive.com')
        if not url or url in seen:
            continue
        seen.add(url)
        job = {'external_id': str(item.get('id') or ''), 'title': plain(item.get('title')), 'company': plain(item.get('company_name')), 'location': place, 'salary': plain(item.get('salary')), 'url': url, 'source': 'remotive', 'description': plain(item.get('description'))[:100000], 'evidence_ids': [], 'tags': [plain(t) for t in item.get('tags', []) if isinstance(t, str)][:30], 'published_at': item.get('publication_date')}
        if not job['title'] or not job['company']:
            continue
        jobs.append(job)
        evidence.append(_job_evidence(job, 'Remotive 远程岗位；发布可能延迟约24小时，非中国招聘来源。'))
        if len(jobs) >= limit:
            break
    return result(message=f"Remotive {'使用6小时缓存' if cached else '公开接口已读取并缓存6小时'}，本地筛选命中 {len(jobs)} 条。数据可能延迟24小时；原始来源 https://remotive.com/。", jobs=jobs, evidence=evidence)


def map_boss(data, limit=20):
    code = data.get('code')
    if code in (37, 35):
        return result('needs_verification', 'BOSS 返回访问验证状态；请使用项目浏览器正常登录并完成人工验证后采集。')
    if code in (7, 8, 9):
        return result('needs_login', 'BOSS 要求有效登录，请在项目浏览器中登录。')
    if code != 0:
        return result('failed', 'BOSS 未返回成功响应；没有保存岗位数据。')
    payload = data.get('zpData')
    items = payload.get('jobList') if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return result('failed', 'BOSS 响应结构变化或未包含岗位列表。')
    jobs, evidence, seen = [], [], set()
    for item in items:
        if not isinstance(item, dict):
            continue
        identifier = str(item.get('encryptJobId') or '')
        if not re.fullmatch(r'[A-Za-z0-9_-]+', identifier) or identifier in seen:
            continue
        seen.add(identifier)
        tags = [plain(x) for x in item.get('jobLabels', []) if isinstance(x, str)][:30]
        description = '\n'.join([plain(item.get('jobName')), f"经验：{plain(item.get('jobExperience'))}", f"学历：{plain(item.get('jobDegree'))}", '、'.join(tags), '仅搜索列表字段；完整岗位职责尚未读取。'])
        job = {'external_id': identifier, 'title': plain(item.get('jobName')), 'company': plain(item.get('brandName')), 'location': plain(item.get('cityName')), 'salary': plain(item.get('salaryDesc')), 'url': f'https://www.zhipin.com/job_detail/{identifier}.html', 'source': 'boss', 'description': description, 'evidence_ids': [], 'tags': tags}
        if not job['title'] or not job['company']:
            continue
        jobs.append(job)
        evidence.append(_job_evidence(job, '公司字段为招聘平台品牌名，未核验法定主体；这里只采集列表摘要。'))
        if len(jobs) >= limit:
            break
    return result(message=f'已读取 BOSS 搜索列表 {len(jobs)} 条；完整职责及法定主体尚未核验。', jobs=jobs, evidence=evidence)


def _boss(client, query, location, limit):
    if location and location not in CITY_CODES and not re.fullmatch(r'\d{9}', location):
        return result('unsupported', '当前城市暂未映射；可用北京、上海、广州、深圳、杭州、成都、武汉、南京、苏州、西安或留空。')
    data = _json(client, 'GET', 'https://www.zhipin.com/wapi/zpgeek/search/joblist.json', params={'query': query, 'city': CITY_CODES.get(location, location or '100010000'), 'page': 1, 'pageSize': min(limit, 30)}, headers={'Referer': 'https://www.zhipin.com/web/geek/job'})
    return map_boss(data, limit)


def _cninfo(client, query, limit):
    evidence, seen = [], set()
    for page in range(1, 4):
        data = _json(client, 'POST', 'https://www.cninfo.com.cn/new/hisAnnouncement/query', data={'pageNum': str(page), 'pageSize': str(min(limit, 30)), 'column': 'szse', 'tabName': 'fulltext', 'searchkey': query, 'isHLtitle': 'true', 'sortName': 'time', 'sortType': 'desc'}, headers={'Referer': 'https://www.cninfo.com.cn/'})
        items = data.get('announcements')
        if items is None and data.get('totalAnnouncement') == 0:
            break
        if not isinstance(items, list):
            raise CollectionError('failed', '巨潮响应缺少公告列表。')
        for item in items:
            if not isinstance(item, dict):
                continue
            identifier = str(item.get('announcementId') or '')
            path = str(item.get('adjunctUrl') or '')
            if not identifier or identifier in seen or not re.fullmatch(r'finalpage/[A-Za-z0-9_./-]+', path) or '..' in path:
                continue
            company = plain(item.get('secName'))
            title = plain(item.get('announcementTitle'))
            if not company or not title:
                continue
            seen.add(identifier)
            published = None
            try:
                if item.get('announcementTime'):
                    published = datetime.fromtimestamp(float(item['announcementTime']) / 1000, timezone.utc).isoformat()
            except (ValueError, TypeError, OSError, OverflowError):
                pass
            evidence.append({'title': title, 'url': f'https://static.cninfo.com.cn/{path}', 'source': 'cninfo', 'kind': 'disclosure_metadata', 'company': company, 'text': f"公告：{title}\n披露主体简称：{company}\n证券代码：{plain(item.get('secCode'))}\n公告 ID：{identifier}\n仅公告元信息，未读取或解析附件正文，不能据此判断业绩。", 'published_at': published})
            if len(evidence) >= limit:
                break
        if len(evidence) >= limit or not data.get('hasMore') or not items:
            break
    return result(message=f'巨潮公开接口取得 {len(evidence)} 条公告元信息；主体按披露简称保存，未读取附件正文。', evidence=evidence)


def collect(source, query, location='', limit=20, *, client=None, cache_dir=None):
    if source not in ('remotive', 'boss', 'cninfo'):
        return result('unsupported', '未支持的数据来源。')
    query, location = str(query).strip()[:200], str(location or '').strip()[:100]
    try:
        limit = max(1, min(int(limit), 50))
    except (ValueError, TypeError):
        return result('failed', '采集数量必须是整数。')
    if not query:
        return result('failed', '请输入搜索关键词。')
    owned = client is None
    client = client or httpx.Client(timeout=20, follow_redirects=False)
    try:
        if source == 'remotive':
            return _remotive(client, query, location, limit, cache_dir or DEFAULT_DATA / 'cache')
        if source == 'boss':
            return _boss(client, query, location, limit)
        return _cninfo(client, query, limit)
    except CollectionError as exc:
        return result(exc.status, exc.message)
    except httpx.TimeoutException:
        return result('failed', '来源请求超时，已停止；可稍后重试。')
    except (httpx.HTTPError, OSError, ValueError, TypeError):
        return result('failed', '采集请求或数据解析失败；未保存不完整结果。')
    finally:
        if owned:
            client.close()
