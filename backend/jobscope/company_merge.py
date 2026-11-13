"""公司主体归并：规则候选 + LLM 判定 + 人工兜底。

设计前提是 reliability 的老红线「错合并比漏合并更糟」，但纯靠精确名归并（`resolve_company`
只认信用代码强键或规范化名）让全称/简称/带城市前缀的写法各占一个实体、数据无法复用。
这里按三层分工放宽，同时把「错合并可逆」做进来（`Repository.split_alias`）：

- 规则（`candidates`）：子串+正则产候选对——便宜、无模型调用，但只负责「值得看」，不负责判同；
- LLM（`adjudicate`）：对候选对判 same / subsidiary / unrelated / unsure。same 且置信 ≥AUTO_MERGE_CONFIDENCE
  才自动合并；子公司、关联公司、同集团不同法人一律隔离（进待审或不并），不因为名字像就并；
- 人工：手动合并任意两家、把别名拆分独立，是最终事实源；dismissed 的候选下一轮不再重提。

合并方向（谁是主名）由 `scan` 按岗位数取多者，人工路径由用户在界面上选；本模块不猜方向。
"""
import json
import re
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .connectors import CITY_CODES
from .repository import Repository

# 自动合并的置信门槛：same 且 confidence ≥ 该值才免审。LLM 只要表现出一点犹豫（猜从属关系、
# 写法差异超出「形式不同」）就到待审列表去，人工点一下比事后拆错合并便宜。
AUTO_MERGE_CONFIDENCE = 0.8

# 公司组织形式后缀（循环剥：`（集团）有限公司` 这类叠加写法）。剥完剩下的才是「字号」。
LEGAL_SUFFIX = re.compile(r'(股份有限公司|有限责任公司|有限公司|集团有限公司|责任公司|（集团）|\(集团\)|集团)$')
# 城市前缀只认词典里的市名（排除「全国」）：`北京字节跳动科技有限公司` → `字节跳动科技`。
# 括号内城市（`阿里巴巴(中国)`）不剥——那是主体自己的行政区划后缀，剥了会把不同法人拉平。
CITY_PREFIXES = sorted((name for name in CITY_CODES if name != '全国'), key=len, reverse=True)
# 子串候选的短名门槛：字号不足 3 个字（如「阿里」）时子串关系太弱，宁漏勿错，交给人工。
MIN_SUBSTRING_CORE = 3


def _core_name(name):
    """规则匹配用的核心名：剥组织形式后缀（循环，覆盖「（集团）有限公司」叠加写法）。
    只用于产候选，实体身份仍是 normalise_name。"""
    text = Repository.normalise_name(name)
    while True:
        stripped = LEGAL_SUFFIX.sub('', text)
        if stripped == text or not stripped:
            return stripped if stripped else text
        text = stripped


def _strip_city(text):
    for city in CITY_PREFIXES:
        if text.startswith(city) and len(text) > len(city):
            return text[len(city):]
    return text


def candidates(entities):
    """对实体列表两两跑规则，产出候选对（不含判定）。返回 [{a, b, a_jobs, b_jobs, rule, note}]。

    规则刻意只有两类，都偏保守：形式等价（后缀/城市前缀差异）与保守子串（核心名 ≥3 字）。
    正则规则表以代码内列表维护，后续要开放自定义正则时在这里登记即可。
    """
    items = [dict(id=item['id'], name=item['name'], core=_strip_city(_core_name(item['name'])),
                  jobs=item.get('jobs_count', 0)) for item in entities]
    found = []
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            left, right = items[i], items[j]
            if left['core'] == right['core'] and left['name'] != right['name']:
                found.append(dict(a=left['name'], b=right['name'], a_jobs=left['jobs'], b_jobs=right['jobs'],
                                  rule='form', note='组织形式后缀或城市前缀差异，字号相同'))
            elif len(left['core']) >= MIN_SUBSTRING_CORE and len(right['core']) >= MIN_SUBSTRING_CORE \
                    and (left['core'] in right['core'] or right['core'] in left['core']) \
                    and left['core'] != right['core']:
                short, long = (left, right) if len(left['core']) <= len(right['core']) else (right, left)
                found.append(dict(a=left['name'], b=right['name'], a_jobs=left['jobs'], b_jobs=right['jobs'],
                                  rule='substring', note=f'「{short["core"]}」是「{long["core"]}」的子串（缩写写法常见）'))
    return found


SYSTEM = """你是公司主体归并判定器。输入是一批公司名对（含各自的已存岗位数），逐一判断两个名字是否指向同一个招聘主体（同一家公司）。

判定类别（verdict，四选一）：
- same：同一法人/同一招聘主体的不同写法——全称与简称、多字少字、括号地名差异、明显笔误。
- subsidiary：母子公司、总分公司、关联公司、同集团旗下不同法人——这些的数据必须隔离，绝不能并。
- unrelated：不同主体。
- unsure：仅凭名称判断不了。

只依据名称本身的写法判断，不要用常识脑补从属关系；需要猜测名称背后的股权/从属关系时，判 subsidiary 或 unsure 且置信度不要超过 0.5。置信度 confidence 取 0~1：名称差异只是「形式写法不同」（后缀、城市前后缀、缩写）时接近 1。

只输出 JSON，不要任何其他内容，格式：
{"verdicts":[{"index":0,"verdict":"same","confidence":0.95,"reason":"一句中文依据"}]}
index 是输入 pairs 数组的下标，每个下标恰好一条。"""


class _Verdict(BaseModel):
    model_config = ConfigDict(extra='forbid')
    index: int = Field(ge=0)
    verdict: Literal['same', 'subsidiary', 'unrelated', 'unsure']
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = Field(default='', max_length=500)


class _Verdicts(BaseModel):
    model_config = ConfigDict(extra='forbid')
    verdicts: list[_Verdict] = Field(min_length=1, max_length=200)


def _parse_verdicts(content, pair_count):
    """与 reliability/extract.py 同型的三段解析：剥围栏 → 整段 JSON → 抓首尾大括号；再后校验下标。"""
    text = re.sub(r'^```(?:json)?\s*|\s*```$', '', str(content or '').strip())
    bracket = re.search(r'\{.*\}', text, re.S)
    for candidate in [text] + ([bracket[0]] if bracket else []):
        try:
            parsed = _Verdicts.model_validate(json.loads(candidate))
        except (ValidationError, ValueError, TypeError):
            continue
        result = {}
        for item in parsed.verdicts:
            if item.index < pair_count and item.index not in result:
                result[item.index] = item
        return result if result else None
    return None


def adjudicate(pairs):
    """LLM 判定候选对。返回 (verdicts_by_index, message)：模型未配置或调用失败时 verdicts 为 None，
    调用方把候选全部转入待审而不是中断扫描——规则候选本身不依赖模型。"""
    from .settings import get_purpose_config, purpose_configured
    if not purpose_configured('extraction'):
        return None, '未配置抽取模型，候选全部转入待审（判定来源：规则）'
    payload = json.dumps([dict(a=p['a'], b=p['b'], a_jobs=p['a_jobs'], b_jobs=p['b_jobs']) for p in pairs],
                         ensure_ascii=False)
    from .model_transport import complete
    try:
        content, _usage = complete(get_purpose_config('extraction'), SYSTEM, payload,
                                   temperature=0, max_tokens=2000, timeout=60)
    except (ValueError, httpx.HTTPError) as exc:
        return None, f'模型调用失败（{exc}），候选全部转入待审（判定来源：规则）'
    verdicts = _parse_verdicts(content, len(pairs))
    if verdicts is None:
        return None, '模型未返回可解析的判定结构，候选全部转入待审（判定来源：规则）'
    return verdicts, ''


def scan(repo, task=None):
    """归并扫描全流程：规则候选 → LLM 判定 → 自动合并（same 且高置信）/ 落待审。

    task 是可选的任务记录（repo.create_task 产物），扫描结束由调用方或这里更新；
    返回汇总 dict 供 task message 与前端 toast 共用同一份文案。
    """
    entities = repo.list_merge_entities()
    pairs = candidates(entities)
    if not pairs:
        return dict(entities=len(entities), candidates=0, merged=0, proposed=0,
                    message=f'扫过 {len(entities)} 个主体，没有发现疑似同一主体的写法对。')
    verdicts, fallback = adjudicate(pairs)
    merged, proposed = [], []
    for index, pair in enumerate(pairs):
        verdict = (verdicts or {}).get(index)
        if verdict is not None and verdict.verdict == 'same' and verdict.confidence >= AUTO_MERGE_CONFIDENCE:
            primary, secondary = (pair['a'], pair['b']) if pair['a_jobs'] >= pair['b_jobs'] else (pair['b'], pair['a'])
            try:
                primary_entity = repo.find_company(primary)
                secondary_entity = repo.find_company(secondary)
                repo.merge_companies(primary_entity['id'], secondary_entity['id'])
                merged.append(f'{secondary}→{primary}')
                continue
            except ValueError:
                # 扫描期间该主体已被并发合并/删除：这一对按待审处理，不中断整轮。
                pass
        if verdict is not None:
            reason = f'llm:{verdict.verdict}({verdict.confidence}) {verdict.reason}'
        else:
            reason = f"rule:{pair['rule']} {pair['note']}"
        if repo.add_merge_proposal(pair['a'], pair['b'], reason):
            proposed.append(f"{pair['a']} × {pair['b']}")
    head = fallback + '；' if fallback else ''
    message = (f"{head}扫过 {len(entities)} 个主体、{len(pairs)} 对候选：自动合并 {len(merged)} 对"
               f"（{'、'.join(merged) or '无'}），待审 {len(proposed)} 对。")
    return dict(entities=len(entities), candidates=len(pairs), merged=len(merged),
                proposed=len(proposed), merged_pairs=merged, message=message)
