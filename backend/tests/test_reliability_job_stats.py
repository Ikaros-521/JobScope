"""job_stats 的本地统计：城市归一、同城样本、完整 JD 统计，以及「口径正文不带时刻」的幂等前提。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jobscope.reliability import job_stats
from jobscope.repository import Repository


def seed(tmp_path, jobs, company='甲公司'):
    repo = Repository(tmp_path)
    for index, (location, salary, description, status) in enumerate(jobs):
        repo.save_job(dict(title=f'岗位{index}', company=company, description=description, location=location,
                           salary=salary, url=f'https://jobs.example.com/{index}',
                           description_status=status), 'test_jobs')
    return repo, repo.resolve_company(company)


def names_of(identity):
    return {Repository.normalise_name(identity['name'])}


def test_known_city_words_normalise_and_unknown_stay_verbatim():
    # 命中词典就归一；词典外按规范化原文精确分组，不做模糊匹配、不做「周边城市并入」。
    assert job_stats.normalise_city('北京市海淀区') == '北京'
    assert job_stats.normalise_city('上海·浦东新区') == '上海'
    assert job_stats.normalise_city('广州') == '广州'
    assert job_stats.normalise_city('  火星 基地 ') == '火星基地'
    assert job_stats.normalise_city('火星基地') == job_stats.normalise_city('火星　基地')
    assert job_stats.normalise_city('') == ''


def job(title, company, location, salary, description, status='complete'):
    return dict(title=title, company=company, description=description, location=location,
                salary=salary, url='', description_status=status)


def save_jobs(repo, jobs, source='test_jobs'):
    for index, item in enumerate(jobs):
        item = dict(item)
        item.setdefault('url', f'https://jobs.example.com/{index}')
        repo.save_job(item, source)


TECH_DESC = '负责服务端开发\n经验：3-5年\n学历：本科'


def test_stratum_requires_role_experience_and_education_annotations():
    # 四重标注缺一不可：未标注的岗位和谁都不保证同层，绝不硬算「要求相当」。
    assert job_stats.job_stratum(dict(title='后端工程师', description=TECH_DESC)) == ('技术研发', '3-5年', '本科')
    assert job_stats.job_stratum(dict(title='体系审核员', description='经验：经验不限\n学历：大专')) == ('审核认证', '不限', '大专')
    # 「高级」头衔不抬级别：级别只看写出的经验要求；招聘专员归人事类，不影响层级判断。
    assert job_stats.job_stratum(dict(title='高级招聘专员', description=TECH_DESC)) == ('人事与财务', '3-5年', '本科')
    assert job_stats.job_stratum(dict(title='神秘岗位', description='负责一些事情')) is None
    assert job_stats.job_stratum(dict(title='后端工程师', description='')) is None
    # 档位归一：研究生并入硕士；「高级」头衔不抬级别，级别只看写出的经验要求。
    assert job_stats.education_bucket('学历：研究生') == '硕士'
    assert job_stats.experience_bucket('经验：5-10年') == '5-10年'
    assert job_stats.experience_bucket('经验：经验不限') == '不限'
    assert job_stats.experience_bucket('3年以上') == '3年以上'


def test_pay_value_compares_only_within_matched_strata(tmp_path):
    """分层对比：本公司「技术·本科·3-5年」的 3 条只和同城同层的对照比；混层岗位不进分母。"""
    repo, identity = seed(tmp_path, [])
    repo.save_job(dict(title='后端工程师A', company='甲公司', description=TECH_DESC, location='北京',
                       salary='15-18K', url='a1', description_status='complete'), 'test_jobs')
    repo.save_job(dict(title='算法工程师B', company='甲公司', description=TECH_DESC, location='北京',
                       salary='16-19K', url='a2', description_status='complete'), 'test_jobs')
    repo.save_job(dict(title='测试工程师C', company='甲公司', description=TECH_DESC, location='北京',
                       salary='17-20K', url='a3', description_status='complete'), 'test_jobs')
    # 同职类同层但缺标注的岗位（30-35K 的管理层）：不参与评分，绝不混进本公司样本。
    for index in range(4):
        repo.save_job(dict(title=f'神秘高管{index}', company='甲公司', description='带团队',
                           location='北京', salary='30-35K', url=f'n{index}',
                           description_status='listing'), 'test_jobs')
    # 同城同层对照 12 条，中位 25000；上海的同层岗位不进北京分母。
    for index in range(12):
        repo.save_job(dict(title=f'工程师{index}', company='乙公司', description=TECH_DESC,
                           location='北京', salary='20-30K', url=f'c{index}',
                           description_status='listing'), 'test_jobs')
    repo.save_job(dict(title='上海工程师', company='乙公司', description=TECH_DESC, location='上海',
                       salary='50-60K', url='sh', description_status='listing'), 'test_jobs')
    analysis = job_stats.pay_analysis(repo.jobs(), names_of(identity))
    assert analysis['city'] == '北京'
    # 对照池含本公司同层岗位（12 乙 + 3 甲 = 15 条）：中位仍是 25000，比值不变。
    # 合并中位数（17500/25000）只给前端图表当量纲，分数由分层比值决定。
    assert analysis['value'] == {'ratio': round(17500 / 25000, 3), 'company_jobs': 3, 'city_jobs': 15,
                                 'company_median': 17500.0, 'city_median': 25000.0}
    # 口径正文写明分层明细与排除数量，且不带时刻（证据身份含内容哈希）。
    assert '技术研发·3-5年·本科' in analysis['note']
    assert '4 条职类或经验/学历标注缺失' in analysis['note']
    assert '不访问网络' in analysis['note']
    import re
    assert not re.search(r'\d{2}:\d{2}', analysis['note'])
    # 兼容入口保持（统计量，城市）二元组。
    value, city = job_stats.pay_value(repo.jobs(), names_of(identity))
    assert city == '北京' and value == analysis['value']


def test_pay_value_with_no_comparable_stratum_reports_the_zero_sample(tmp_path):
    # 同城同层对照不足（需 ≥5 条）：分不出可比层 → 全零统计量，缺口正文写明差在哪。
    repo, identity = seed(tmp_path, [])
    for index in range(3):
        repo.save_job(dict(title=f'后端工程师{index}', company='甲公司', description=TECH_DESC,
                           location='北京', salary='20-35K', url=f'a{index}',
                           description_status='complete'), 'test_jobs')
    for index in range(1):  # 同层对照只有 1 条：池 = 甲 3 + 对照 1 = 4 条，凑不齐 ≥5 的闸门
        repo.save_job(dict(title=f'对照工程师{index}', company='乙公司', description=TECH_DESC,
                           location='北京', salary='30-50K', url=f'c{index}',
                           description_status='listing'), 'test_jobs')
    analysis = job_stats.pay_analysis(repo.jobs(), names_of(identity))
    assert analysis['city'] == '北京'
    assert analysis['value'] == {'ratio': 0, 'company_jobs': 0, 'city_jobs': 0}
    assert '样本不够' in analysis['note']


def test_pay_value_without_any_parsable_quote_still_reports_the_zero_sample(tmp_path):
    # 一条可解析报价都没有：统计量全零，闸门缺口据此说清「样本不够」，而不是无声无息。
    repo, identity = seed(tmp_path, [('北京', '年薪 60 万元', '负责服务端开发', 'listing')])
    analysis = job_stats.pay_analysis(repo.jobs(), names_of(identity))
    assert analysis['city'] == '北京'
    assert analysis['value'] == {'ratio': 0, 'company_jobs': 0, 'city_jobs': 0}


def test_pay_value_weights_multiple_strata_by_company_sample(tmp_path):
    """两个可比分层按本公司样本数加权；每层的闸门独立判定，不足的层只留说明。"""
    repo, identity = seed(tmp_path, [])
    audit_desc = '负责体系审核\n经验：经验不限\n学历：大专'
    for index in range(4):   # 技术层 4 条
        repo.save_job(dict(title=f'工程师{index}', company='甲公司', description=TECH_DESC,
                           location='杭州', salary='15-18K', url=f't{index}',
                           description_status='complete'), 'test_jobs')
    for index in range(3):   # 审核层 3 条
        repo.save_job(dict(title=f'体系审核员{index}', company='甲公司', description=audit_desc,
                           location='杭州', salary='10-15K', url=f'u{index}',
                           description_status='complete'), 'test_jobs')
    for index in range(12):  # 技术层同城对照
        repo.save_job(dict(title=f'工程师{index}', company='乙公司', description=TECH_DESC,
                           location='杭州', salary='20-30K', url=f'c{index}',
                           description_status='listing'), 'test_jobs')
    for index in range(10):  # 审核层同城对照
        repo.save_job(dict(title=f'审核员{index}', company='乙公司', description=audit_desc,
                           location='杭州', salary='10-14K', url=f'd{index}',
                           description_status='listing'), 'test_jobs')
    analysis = job_stats.pay_analysis(repo.jobs(), names_of(identity))
    # 技术：16500/25000 = 0.66（同层 16 条含本公司 4 条）；审核：12500/12000 = 1.042（同层 13 条
    # 含本公司 3 条）→ 按本公司样本数加权 (4×0.66 + 3×1.042)/7
    tech, audit = round(16500 / 25000, 3), round(12500 / 12000, 3)
    # 多层覆盖不给合并中位数：跨层合并会把审核层与技术层混在一起，图表宁缺勿误导。
    assert analysis['value'] == {'ratio': round((4 * tech + 3 * audit) / 7, 3),
                                 'company_jobs': 7, 'city_jobs': 29}
    assert '审核认证·不限·大专' in analysis['note']


def test_jd_value_counts_only_complete_descriptions(tmp_path):
    full = '岗位职责：\n负责核心服务开发，支撑日活 100 万的业务\n要求：\n3 年以上 Python 经验，熟悉 MySQL\n'
    repo, identity = seed(tmp_path, [
        ('北京', '20-35K', full, 'complete'),
        ('北京', '20-30K', full.replace('MySQL', 'PostgreSQL'), 'complete'),
        ('北京', '20-30K', '后端工程师', 'listing'),  # 只有列表摘要的岗位不参与
        ('北京', '20-30K', '   ', 'complete'),        # 空描述同样不参与
    ])
    value = job_stats.jd_value(repo.jobs(), names_of(identity))
    assert value['jobs'] == 2
    assert value['items_mean'] == 4.0          # 四个非空行 = 四条职责/要求条目
    assert value['skills_mean'] == 2.0         # Python 与 MySQL / PostgreSQL 各命中两项
    assert value['quantified_share'] == 0.25   # 只有「日活 100 万」那一行含量化结果


def test_jd_value_with_zero_complete_jobs_is_all_zero_not_none(tmp_path):
    # 有岗位但没有完整 JD：统计量全零，让闸门缺口说清「完整职责未采集」，而不是「尚无来源」。
    repo, identity = seed(tmp_path, [('北京', '20-35K', '后端工程师', 'listing')])
    assert job_stats.jd_value(repo.jobs(), names_of(identity)) == {
        'jobs': 0, 'chars_mean': 0, 'items_mean': 0, 'skills_mean': 0, 'quantified_share': 0}


def test_notes_are_stable_and_carry_no_timestamp(tmp_path):
    """口径正文写进证据，证据身份含内容哈希：带时刻会让每次点击都新增记录、input_hash 每次都变。"""
    import re
    # 用两个独立库跑同一份输入：正文必须逐字一致，且没有时刻形态的数字。
    first_repo, first_identity = seed(tmp_path / 'first', [('北京', '20-35K', '负责开发', 'complete')])
    second_repo, second_identity = seed(tmp_path / 'second', [('北京', '20-35K', '负责开发', 'complete')])
    first_pay = job_stats.pay_analysis(first_repo.jobs(), names_of(first_identity))
    second_pay = job_stats.pay_analysis(second_repo.jobs(), names_of(second_identity))
    assert first_pay['note'] == second_pay['note'] and first_pay['value'] == second_pay['value']
    assert not re.search(r'\d{2}:\d{2}', first_pay['note'])
    first_jd = job_stats.jd_value(first_repo.jobs(), names_of(first_identity))
    assert not re.search(r'\d{2}:\d{2}', job_stats.jd_note(first_jd))
    assert '不访问网络' in job_stats.jd_note(first_jd)
