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


def test_pay_value_compares_the_company_median_with_the_whole_city_sample(tmp_path):
    repo, identity = seed(tmp_path, [('北京', '20-35K', '负责服务端开发', 'listing')] * 4
                          + [('北京', '10-15K', '负责客服', 'listing')] * 4)
    # 同城样本来自全库：乙公司的北京岗位也进分母，上海的岗位不进。
    repo.save_job(dict(title='别家岗位', company='乙公司', description='负责其他', location='北京',
                       salary='30-50K', url='https://jobs.example.com/other'), 'test_jobs')
    repo.save_job(dict(title='上海岗位', company='乙公司', description='负责华东', location='上海',
                       salary='30-50K', url='https://jobs.example.com/sh'), 'test_jobs')
    value, city = job_stats.pay_value(repo.jobs(), names_of(identity))
    assert city == '北京'
    # 甲：4×27500 与 4×12500 → 中位数 20000；北京全市 9 条 → 中位数排序后第 5 个 = 27500。
    assert value['company_jobs'] == 8 and value['city_jobs'] == 9
    assert value['company_median'] == 20000.0 and value['city_median'] == 27500.0
    assert value['ratio'] == round(20000 / 27500, 3)


def test_pay_value_without_any_parsable_quote_still_reports_the_zero_sample(tmp_path):
    # 一条可解析报价都没有：统计量全零，闸门缺口据此说清「样本不够」，而不是无声无息。
    repo, identity = seed(tmp_path, [('北京', '年薪 60 万元', '负责服务端开发', 'listing')])
    value, city = job_stats.pay_value(repo.jobs(), names_of(identity))
    assert city == '北京'
    assert value == {'company_median': 0, 'city_median': 0, 'ratio': 0,
                     'company_jobs': 0, 'city_jobs': 0}


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
    first_value, first_city = job_stats.pay_value(first_repo.jobs(), names_of(first_identity))
    second_value, second_city = job_stats.pay_value(second_repo.jobs(), names_of(second_identity))
    assert job_stats.pay_note(first_value, first_city) == job_stats.pay_note(second_value, second_city)
    first_jd = job_stats.jd_value(first_repo.jobs(), names_of(first_identity))
    assert not re.search(r'\d{2}:\d{2}', job_stats.jd_note(first_jd))
    assert '不访问网络' in job_stats.jd_note(first_jd)
