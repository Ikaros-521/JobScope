"""Bounded, fixed-origin public-source collectors. No credentials in results."""
from __future__ import annotations

import html
import json
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx

DEFAULT_DATA = Path(__file__).resolve().parents[2] / '.data'
MAX_BYTES = 12 * 1024 * 1024
CACHE_SECONDS = 6 * 60 * 60
_cache_lock = threading.Lock()
# 逐个回验过的城市码下限；数据文件缺失时采集也不能把这些城市退化成“未映射”。
VERIFIED_BOSS_CITY_CODES = {'北京': '101010100', '上海': '101020100', '广州': '101280100', '深圳': '101280600', '杭州': '101210100', '成都': '101270100', '武汉': '101200100', '南京': '101190100', '苏州': '101190400', '西安': '101110100', '全国': '100010000'}
# 平台自身的全量字典由 scripts/harvest_city_dictionary.py 抓回，落盘前必须逐条复现上面的下限。
CITY_FILE = Path(__file__).resolve().parent / 'data' / 'cities.json'
CITY_DICTIONARY = json.loads(CITY_FILE.read_text(encoding='utf-8')) if CITY_FILE.is_file() else {}
CITY_CODES = dict(VERIFIED_BOSS_CITY_CODES, **((CITY_DICTIONARY.get('boss') or {}).get('cities') or {}))
BOSS_HOT_CITIES = (CITY_DICTIONARY.get('boss') or {}).get('hot') or list(CITY_CODES)
# 拼音取自 BOSS 城市字典（城市名是同一套中文写法，其他来源共用）。
CITY_PINYIN = CITY_DICTIONARY.get('pinyin') or {}
# 各平台城市码体系互不通用，以下取值均按该来源实际返回的工作城市逐个回验得到。
ZHAOPIN_CITY_CODES = {'北京': '530', '上海': '538', '广州': '763', '深圳': '765', '杭州': '653', '成都': '801', '武汉': '736', '南京': '635', '苏州': '639', '西安': '854'}
JOB51_CITY_CODES = {'北京': '010000', '上海': '020000', '广州': '030200', '深圳': '040000', '南京': '070200', '苏州': '070300', '杭州': '080200', '成都': '090200', '武汉': '180200', '西安': '200200', '全国': '000000'}
LIEPIN_CITY_CODES = {'北京': '010', '上海': '020', '广州': '050020', '深圳': '050090', '杭州': '070020', '成都': '280020', '武汉': '170020', '南京': '060020', '苏州': '060080', '西安': '270020'}


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


def _safe_url(value, hosts):
    parsed = urlparse(str(value or ''))
    allowed = (hosts,) if isinstance(hosts, str) else tuple(hosts)
    return str(value) if parsed.scheme == 'https' and parsed.hostname in allowed else ''


def loads_payload(value):
    """Accept dict, JSON text, JSONP-wrapped text or bytes; reject anything else."""
    if isinstance(value, dict):
        return value
    if isinstance(value, bytes):
        value = value.decode('utf-8', 'replace')
    if not isinstance(value, str) or len(value) > MAX_BYTES:
        return None
    for candidate in (value, re.sub(r'^\s*[A-Za-z0-9_$]+\(\s*|\s*\)\s*;?\s*$', '', value.strip())):
        try:
            parsed = json.loads(candidate)
        except (ValueError, UnicodeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _published(value):
    """Normalise the timestamp formats these sources actually return."""
    text = str(value or '').strip()
    if not text:
        return None
    if re.fullmatch(r'\d{14}', text):
        text = f'{text[0:4]}-{text[4:6]}-{text[6:8]} {text[8:10]}:{text[10:12]}:{text[12:14]}'
    text = text.replace(' ', 'T', 1) if ' ' in text and 'T' not in text else text
    try:
        return datetime.fromisoformat(text[:19]).replace(tzinfo=timezone.utc).isoformat()
    except ValueError:
        return None


def _city_kept(location, city):
    """来源结果会掺入外地“相关推荐”，按用户点名的城市复核后才保存。"""
    return not city or city in str(location or '')


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


def map_boss(data, limit=20, city=''):
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
        job = {'external_id': identifier, 'title': plain(item.get('jobName')), 'company': plain(item.get('brandName')), 'location': plain(item.get('cityName')), 'salary': plain(item.get('salaryDesc')), 'url': f'https://www.zhipin.com/job_detail/{identifier}.html', 'source': 'boss', 'description': description, 'description_status': 'listing', 'evidence_ids': [], 'tags': tags}
        if not job['title'] or not job['company'] or not _city_kept(job['location'], city):
            continue
        jobs.append(job)
        evidence.append(_job_evidence(job, '公司字段为招聘平台品牌名，未核验法定主体；这里只采集列表摘要。'))
        if len(jobs) >= limit:
            break
    return result(message=f'已读取 BOSS 搜索列表 {len(jobs)} 条；完整职责及法定主体尚未核验。', jobs=jobs, evidence=evidence)


def _boss(client, query, location, limit):
    if location and location not in CITY_CODES and not re.fullmatch(r'\d{9}', location):
        return result('unsupported', f'当前城市暂未映射；可用{city_hint("boss")}或留空。')
    data = _json(client, 'GET', 'https://www.zhipin.com/wapi/zpgeek/search/joblist.json', params={'query': query, 'city': CITY_CODES.get(location, location or '100010000'), 'page': 1, 'pageSize': min(limit, 30)}, headers={'Referer': 'https://www.zhipin.com/web/geek/job'})
    return map_boss(data, limit)


def _push(job, note, jobs, evidence, seen, limit, city=''):
    identifier = str(job.get('external_id') or '')
    if not re.fullmatch(r'[A-Za-z0-9_-]{4,60}', identifier) or identifier in seen:
        return False
    if not job['title'] or not job['company'] or not job['url'] or not _city_kept(job['location'], city):
        return False
    seen.add(identifier)
    jobs.append(job)
    evidence.append(_job_evidence(job, note))
    return len(jobs) >= limit


def _zhaopin_detail_url(item):
    """翻页接口返回 http://jobs.zhaopin.com/<编号>.htm；编号与平台链接一致时，
    归一成详情通路登记的 www 详情形态，否则拒绝。"""
    number = str(item.get('number') or '')
    if not re.fullmatch(r'[A-Za-z0-9]{8,40}', number):
        return ''
    path = urlparse(str(item.get('positionUrl') or '')).path
    if not path.endswith(f'/{number}.htm'):
        return ''
    return f'https://www.zhaopin.com/jobdetail/{number}.htm'


def map_zhaopin(state, limit=20, city=''):
    """智联岗位随搜索页 HTML 一起返回；纯 HTTP 直连会被来源指纹校验拦下。"""
    if not isinstance(state, dict):
        return result('failed', '智联页面数据不是可解析的对象；可能是验证页或页面结构变化。')
    items = state.get('positionList')
    if not isinstance(items, list):
        return result('failed', '智联响应缺少岗位列表；页面结构可能已变化。')
    jobs, evidence, seen = [], [], set()
    for item in items:
        if not isinstance(item, dict):
            continue
        tags, description = [], []
        for key in ('jobSkillTags', 'skillLabel'):
            for tag in item.get(key) or []:
                if isinstance(tag, dict):
                    value = plain(tag.get('name') or tag.get('value'))
                    if value and value not in tags:
                        tags.append(value)
        for label, key in (('经验', 'workingExp'), ('学历', 'education'), ('行业', 'industryName')):
            value = plain(item.get(key))
            if value:
                description.append(f'{label}：{value}')
        # 搜索页自带的 jobDescription 是来源截断的预览（实测约百字即断句），不能当完整正文标注。
        duties = plain(item.get('jobDescription'))[:100000]
        job = {'external_id': str(item.get('number') or ''), 'title': plain(item.get('name')),
               'company': plain(item.get('companyName')),
               'location': ' '.join(x for x in (plain(item.get('workCity')), plain(item.get('cityDistrict'))) if x),
               'salary': plain(item.get('salary60')),
               'url': _safe_url(item.get('positionUrl'), 'www.zhaopin.com') or _zhaopin_detail_url(item),
               'source': 'zhaopin',
               'description': '\n'.join(description + (['', duties, '以上为搜索页摘要，来源已截断；完整岗位职责尚未读取。']
                                                        if duties else ['、'.join(tags), '仅搜索列表字段；完整岗位职责尚未读取。'])),
               'evidence_ids': [], 'tags': tags[:30], 'published_at': _published(item.get('publishTime'))}
        job['description_status'] = 'listing'
        if _push(job, '岗位摘要为智联搜索页截断预览，完整职责待详情页补读；公司为平台展示名称，法定主体未核验。',
                 jobs, evidence, seen, limit, city):
            break
    return result(message=f'已读取智联招聘搜索页岗位 {len(jobs)} 条；摘要可能被来源截断，完整职责由详情页补读，法定主体尚未核验。', jobs=jobs, evidence=evidence)


def map_job51(payload, limit=20, city=''):
    items = (((payload or {}).get('resultbody') or {}).get('job') or {}).get('items')
    if not isinstance(items, list):
        return result('failed', '前程无忧响应缺少岗位列表；可能是验证页或接口结构变化。')
    if str(payload.get('status')) != '1':
        return result('failed', f"前程无忧未返回成功响应（{plain(payload.get('message'))[:60] or '原因未知'}）；没有保存岗位数据。")
    jobs, evidence, seen = [], [], set()
    for item in items:
        if not isinstance(item, dict):
            continue
        duties = plain(item.get('jobDescribe'))[:100000]
        description = [f'{label}：{plain(item.get(key))}' for label, key in
                       (('经验', 'workYearString'), ('学历', 'degreeString'), ('类型', 'termStr')) if item.get(key)]
        tags = [plain(x) for x in item.get('jobTags') or [] if isinstance(x, str) and x][:30]
        if item.get('isPromotion'):
            tags = ['推广'] + tags
        job = {'external_id': str(item.get('jobId') or ''), 'title': plain(item.get('jobName')),
               'company': plain(item.get('fullCompanyName')) or plain(item.get('companyName')),
               'location': plain(item.get('jobAreaString')), 'salary': plain(item.get('provideSalaryString')),
               'url': _safe_url(item.get('jobHref'), ('jobs.51job.com', 'we.51job.com')), 'source': '51job',
               'description': '\n'.join(description + (['', duties] if duties else ['、'.join(tags), '仅搜索列表字段；完整岗位职责尚未读取。'])),
               'evidence_ids': [], 'tags': tags, 'published_at': _published(item.get('issueDateString'))}
        job['description_status'] = 'complete' if duties else 'listing'
        if duties:
            job['detail_transport'] = 'search_api'
        if _push(job, '岗位正文取自前程无忧检索接口字段；优先保存平台返回的完整公司名，法定主体仍需核验。'
                 if duties else '只采集检索列表字段，完整职责尚未读取；公司为平台展示名称，未核验法定主体。',
                 jobs, evidence, seen, limit, city):
            break
    return result(message=f'已读取前程无忧检索结果 {len(jobs)} 条；正文完整性按条标注，法定主体尚未核验。', jobs=jobs, evidence=evidence)


LIEPIN_API = 'https://api-c.liepin.com/api/com.liepin.searchfront4c.pc-search-job'
LIEPIN_HEADERS = {'Content-Type': 'application/json;charset=UTF-8', 'X-Client-Type': 'web',
                  'X-Fscp-Version': '1.1', 'X-Fscp-Std-Info': '{"client_id": "40108"}',
                  'X-Requested-With': 'XMLHttpRequest'}


def map_liepin(payload, limit=20, city=''):
    data = (payload or {}).get('data')
    items = ((data or {}).get('data') or {}).get('jobCardList') if isinstance(data, dict) else None
    if not isinstance(items, list):
        return result('failed', '猎聘响应缺少岗位列表；接口结构可能已变化。')
    if str(payload.get('flag')) not in ('1', 'True'):
        return result('failed', f'猎聘未返回成功响应（{plain(payload.get("msg"))[:60] or "原因未知"}）；没有保存岗位数据。')
    jobs, evidence, seen = [], [], set()
    for card in items:
        if not isinstance(card, dict):
            continue
        job_data, company = card.get('job') or {}, card.get('comp') or {}
        if not isinstance(job_data, dict) or not isinstance(company, dict):
            continue
        tags = [plain(x) for x in job_data.get('labels') or [] if isinstance(x, str) and x]
        for label, key in (('经验', 'requireWorkYears'), ('学历', 'requireEduLevel')):
            if job_data.get(key):
                tags.append(plain(job_data[key]))
        if job_data.get('topJob') or job_data.get('advViewFlag'):
            tags = ['推广'] + tags
        fields = '、'.join(filter(None, [plain(job_data.get('salary')), plain(job_data.get('dq')), '、'.join(tags[:30])]))
        job = {'external_id': str(job_data.get('jobId') or ''), 'title': plain(job_data.get('title')),
               'company': plain(company.get('compName')), 'location': plain(job_data.get('dq')),
               'salary': plain(job_data.get('salary')),
               'url': _safe_url(job_data.get('link'), 'www.liepin.com'), 'source': 'liepin',
               'description': '\n'.join([plain(job_data.get('title')), fields, '仅搜索列表字段；完整岗位职责尚未读取。']),
               'description_status': 'listing',
               'evidence_ids': [], 'tags': tags[:30], 'published_at': _published(job_data.get('refreshTime'))}
        if _push(job, '只采集猎聘检索列表字段，完整职责尚未读取；公司为平台展示名称，未核验法定主体。',
                 jobs, evidence, seen, limit, city):
            break
    return result(message=f'已读取猎聘检索列表 {len(jobs)} 条；完整职责与法定主体尚未核验。', jobs=jobs, evidence=evidence)


def _liepin(client, query, location, limit):
    code = LIEPIN_CITY_CODES.get(location or '全国', '' if not location else None)
    if code is None:
        return result('unsupported', f'当前城市暂未映射；可用{city_hint("liepin")}或留空。')
    jobs, evidence, page, failure = [], [], 0, None
    while len(jobs) < limit and page < 3:
        body = {'data': {'mainSearchPcConditionForm': {
            'city': code, 'dq': code, 'pubTime': '', 'currentPage': page,
            'pageSize': min(limit, 40), 'key': query, 'suggestTag': '', 'workYearCode': '',
            'compId': '', 'compName': '', 'compTag': '', 'industry': '', 'salaryCode': '',
            'jobKind': '', 'compScale': '', 'compKind': '', 'compStage': '', 'eduLevel': '',
            'salaryLow': '', 'salaryHigh': ''},
            'passThroughForm': {'scene': 'init' if not page else 'page', 'skId': '', 'fkId': '',
                                'ckId': '', 'suggest': None}}}
        headers = dict(LIEPIN_HEADERS, Referer='https://www.liepin.com/zhaopin/',
                       **{'X-Fscp-Trace-Id': str(uuid.uuid4())})
        payload = _json(client, 'POST', LIEPIN_API, json=body, headers=headers)
        output = map_liepin(payload, limit - len(jobs), location)
        if output['status'] != 'completed':
            failure = output
            break
        known = {item['external_id'] for item in jobs}
        fresh = [(item, ev) for item, ev in zip(output['jobs'], output['evidence'])
                 if item['external_id'] not in known]
        jobs += [item for item, _ in fresh]
        evidence += [ev for _, ev in fresh]
        pagination = ((payload.get('data') or {}).get('pagination') or {})
        if not fresh or not pagination.get('hasNext'):
            break
        page += 1
    note = f'{"全国" if not location else location} · '
    if not jobs:
        if failure:
            return failure
        return result('completed', f'{note}猎聘本次没有返回符合条件的岗位；可能是关键词过窄或结果已下架。')
    return result(message=f'{note}已读取猎聘检索列表 {len(jobs)} 条；完整职责与法定主体尚未核验。',
                  jobs=jobs, evidence=evidence)


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


# 来源登记表：来源页、采集路由、浏览器通路及前端下拉共用同一份事实，平台名不再各处硬编码。
SOURCES = {
    'boss': dict(name='BOSS 直聘', kind='jobs', transport='http_api', status='needs_verification',
                 codes=CITY_CODES, hot=BOSS_HOT_CITIES,
                 description='国内岗位接口；未登录直调可能触发验证，可尝试专属浏览器会话。'),
    'zhaopin': dict(name='智联招聘', kind='jobs', transport='browser_page', status='needs_verification',
                   codes=ZHAOPIN_CITY_CODES,
                   description='岗位随搜索页返回；本机直连会被来源风控拦下，需项目浏览器会话。'),
    '51job': dict(name='前程无忧', kind='jobs', transport='browser_page', status='needs_verification',
                  codes=JOB51_CITY_CODES,
                  description='检索接口需浏览器通过脚本校验；本机直连只得到验证页。'),
    'liepin': dict(name='猎聘', kind='jobs', transport='http_api', status='available',
                   codes=LIEPIN_CITY_CODES,
                   description='公开检索接口，无需登录即返回岗位列表；列表不含完整职责。'),
    'remotive': dict(name='Remotive', kind='jobs', transport='http_api', status='available',
                     description='公开远程岗位接口，主要为海外岗位；不是国内招聘数据源。'),
    'cninfo': dict(name='巨潮资讯', kind='evidence', transport='http_api', status='available',
                   description='公司公告检索，只保存已读取的公告元信息，不代表已解析财报正文。'),
    'user_import': dict(name='手动导入', kind='manual', transport='manual', status='available',
                        description='用户提供的岗位或公司资料，保留用户材料来源标记。'),
}
BROWSER_SOURCES = tuple(source for source, item in SOURCES.items() if item['transport'] == 'browser_page')
HTTP_SOURCES = tuple(source for source, item in SOURCES.items() if item['transport'] == 'http_api')


def city_hint(source):
    """未映射提示列热门城市与总数；BOSS 有 374 个城市，全列会把任务说明撑到读不完。"""
    item = SOURCES[source]
    names = list(item.get('hot') or item['codes'])[:10]
    return '、'.join(names) + (f' 等 {len(item["codes"])} 个城市' if len(item['codes']) > len(names) else '')


def collect(source, query, location='', limit=20, *, client=None, cache_dir=None):
    if source in BROWSER_SOURCES:
        return result('unsupported', f'{SOURCES[source]["name"]}岗位由项目浏览器会话采集，请使用浏览器采集入口。')
    if source not in HTTP_SOURCES:
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
        if source == 'liepin':
            return _liepin(client, query, location, limit)
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
