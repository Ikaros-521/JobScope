"""Harvest a platform's own city dictionary into backend/jobscope/data/cities.json.

来源字典只从平台自身取，绝不手写码值；写入前必须逐条复现 connectors 里已回验的城市码，
对不上就拒绝落盘，避免把猜出来的码变成静默采错城市。
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from jobscope import connectors

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'backend' / 'jobscope' / 'data' / 'cities.json'
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'

# 每个入口都记着它实测来自哪里；换平台时先按同样标准找平台自带字典，再补这张表。
ORIGINS = {
    'boss': dict(url='https://www.zhipin.com/wapi/zpCommon/data/city.json', parse='boss'),
}


def get(url):
    response = httpx.get(url, headers={'User-Agent': UA, 'Accept': 'application/json', 'Accept-Language': 'zh-CN,zh;q=0.9'},
                         timeout=25, follow_redirects=False)
    if response.status_code != 200:
        raise SystemExit(f'来源返回 HTTP {response.status_code}，未取到城市字典。')
    try:
        return response.json()
    except ValueError:
        raise SystemExit('来源返回的不是 JSON，可能已被验证页替换。') from None


def initials_of(name, platform_pinyin):
    """首字母只在 pypinyin 的整词读法与平台拼音一致时才采信，避免多音字给出错候选。"""
    try:
        from pypinyin import Style, lazy_pinyin
    except ImportError:
        return ''
    full = ''.join(lazy_pinyin(name)).replace("'", '')
    if platform_pinyin and full != platform_pinyin:
        return ''
    return ''.join(lazy_pinyin(name, style=Style.FIRST_LETTER))


def parse_boss(payload):
    """把省→市两级展开；只取带拼音的城市层，行政区与省级码不进入采集字典。"""
    data = payload['zpData']
    cities, pinyin = {}, {}
    for province in data.get('cityList') or []:
        for city in province.get('subLevelModelList') or []:
            name, code = str(city.get('name') or ''), str(city.get('code') or '')
            if not name or not re.fullmatch(r'\d{9}', code):
                continue
            cities[name] = code
            full = str(city.get('pinyin') or '')
            if full:
                pinyin[name] = [full, initials_of(name, full)]
    hot = [str(item.get('name') or '') for item in data.get('hotCityList') or [] if item.get('name')]
    for name, code in zip(hot, [str(item.get('code') or '') for item in data.get('hotCityList') or []]):
        cities.setdefault(name, code)
    return dict(cities=cities, pinyin=pinyin, hot=hot)


def main():
    source = sys.argv[1] if len(sys.argv) > 1 else 'boss'
    origin = ORIGINS.get(source)
    if not origin:
        raise SystemExit(f'{source} 的城市字典入口尚未实测；先按 README 的回验标准找到来源自身的字典，再登记到这里。')
    harvested = parse_boss(get(origin['url'])) if origin['parse'] == 'boss' else None
    # 与已回验的下限逐条比对；用 CITY_CODES 比会拿合并结果自证，失去校验意义。
    known = getattr(connectors, {'boss': 'VERIFIED_BOSS_CITY_CODES'}.get(source, ''), {})
    mismatched = {name: (code, known[name]) for name, code in known.items() if harvested['cities'].get(name) != code}
    missing = [name for name in known if name not in harvested['cities']]
    shortened = sum(1 for _, short in harvested['pinyin'].values() if short)
    print(f'来源字典 {source}: {len(harvested["cities"])} 个城市，{len(harvested["hot"])} 个热门，'
          f'{len(harvested["pinyin"])} 个拼音（其中 {shortened} 个可按首字母命中）')
    print(f'已回验 {len(known)} 个城市：复现 {len(known) - len(mismatched) - len(missing)}，码不符 {len(mismatched)}，字典缺失 {len(missing)}')
    for name, (found, expected) in mismatched.items():
        print(f'  码不符 {name}: 来源 {found} / 已回验 {expected}')
    for name in missing:
        print(f'  来源缺失 {name}: 已回验 {known[name]}')
    if mismatched or missing:
        raise SystemExit('来源字典与已回验的城市码不一致，拒绝落盘；请人工核对码制再改。')

    store = json.loads(DATA.read_text(encoding='utf-8')) if DATA.exists() else {}
    store[source] = dict(origin=origin['url'], cities=harvested['cities'], hot=harvested['hot'])
    store.setdefault('pinyin', {}).update(harvested['pinyin'])
    store['updated_at'] = datetime.now(timezone.utc).isoformat()
    DATA.parent.mkdir(parents=True, exist_ok=True)
    temporary = DATA.with_suffix('.tmp')
    temporary.write_text(json.dumps(store, ensure_ascii=False, sort_keys=True, indent=1), encoding='utf-8')
    temporary.replace(DATA)
    print(f'已写入 {DATA.relative_to(ROOT)}：{len(store[source]["cities"])} 个城市可用于采集。')


if __name__ == '__main__':
    main()
