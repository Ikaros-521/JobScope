import sys
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jobscope.main import create_app
from jobscope.reliability import job_stats, wage_baseline


GOOD_CSV = ('城市,职类,学历,月薪中位数,年份,备注\n'
            '杭州,技术研发,本科,18000,2025,2025年工资价位\n'
            '杭州市,技术研发,大专,12000,2025,\n')


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv('JOBSCOPE_LLM_URL', raising=False)
    with TestClient(create_app(tmp_path)) as client:
        yield client


def make_job(company='甲公司', city='杭州', title='后端工程师', exp='3-5年', edu='本科', salary='15-25K'):
    return dict(title=title, company=company, location=city,
                description=f'职责：开发服务\n经验：{exp}\n学历：{edu}', salary=salary)


def test_parse_validates_and_normalises():
    rows, errors = wage_baseline.parse('\ufeff' + GOOD_CSV)
    assert errors == []
    assert len(rows) == 2
    assert rows[0] == dict(city='杭州', role='技术研发', education='本科',
                           monthly_median=18000.0, year='2025', note='2025年工资价位')
    # 「杭州市」归一到字典写法 杭州
    assert rows[1]['city'] == '杭州'

    bad = ('城市,职类,学历,月薪中位数,年份,备注\n'
           '火星,技术研发,本科,100,2025,\n'
           '杭州,神秘职类,本科,100,2025,\n'
           '杭州,技术研发,博士后,100,2025,\n'
           '杭州,技术研发,本科,高薪,2025,\n'
           '杭州,技术研发,本科,0,2025,\n'
           '杭州,技术研发,本科,100,明年,\n'
           '杭州,技术研发,本科,100,\n')
    rows, errors = wage_baseline.parse(bad)
    assert rows == []
    assert len(errors) == 7  # 六类坏行 + 最后一行少一列
    assert '城市' in errors[0] and '职类' in errors[1]

    rows, errors = wage_baseline.parse('')
    assert rows == [] and len(errors) == 1
    rows, errors = wage_baseline.parse('a,b,c\n1,2,3')
    assert rows == [] and '表头' in errors[0]
    # 备注含逗号不炸列
    rows, errors = wage_baseline.parse('城市,职类,学历,月薪中位数,年份,备注\n杭州,技术研发,本科,18000,2025,含,逗号,备注')
    assert errors == [] and rows[0]['note'] == '含,逗号,备注'


def test_repository_upserts_by_identity(client):
    repo = client.app.state.repo
    repo.save_wage_baselines([dict(city='杭州', role='技术研发', education='本科', monthly_median=15000, year='2024')])
    repo.save_wage_baselines([dict(city='杭州', role='技术研发', education='本科', monthly_median=18000, year='2025')])
    repo.save_wage_baselines([dict(city='杭州', role='技术研发', education='本科', monthly_median=19000, year='2025', note='重导')])
    rows = repo.wage_baselines()
    assert len(rows) == 2  # 同键同年在原位更新，不堆行
    latest = rows[0] if rows[0]['year'] == '2025' else rows[1]
    assert latest['monthly_median'] == 19000 and latest['note'] == '重导'
    assert repo.delete_wage_baseline(latest['id']) == 1
    assert repo.delete_wage_baseline(latest['id']) == 0


def test_pay_analysis_baseline_fallback_and_caliber_note():
    jobs = [make_job(),
            make_job(company='乙公司', title='后端开发', exp='3-5年'),
            make_job(company='丙公司', title='Java 工程师', exp='3-5年')]
    # 无基线：同城同层只有 2 条 < 5，整层不比，value 全零。
    result = job_stats.pay_analysis(jobs, {'甲公司'})
    assert result['value']['ratio'] == 0
    assert '样本不够' in result['note']

    baseline = [dict(city='杭州', role='技术研发', education='本科', monthly_median=15000, year='2025')]
    result = job_stats.pay_analysis(jobs, {'甲公司'}, baselines=baseline)
    value = result['value']
    # 公司 1 条报价 20K（15-25K 中点）/ 基线 15K → 1.333；闸门走基线钥匙。
    assert value['ratio'] == 1.333 and value['city_baseline_layers'] == 1
    assert value['company_median'] == 20000 and value['city_median'] == 15000
    assert '官方工资基线 15000 元（2025 年·人社局指导价位导入）' in result['note']
    assert '口径不同' in result['note']
    # 其他城市的基线不掺进来；职类/学历对不上的层照样不比。
    other = [dict(city='苏州', role='技术研发', education='本科', monthly_median=14000, year='2025')]
    result = job_stats.pay_analysis(jobs, {'甲公司'}, baselines=other)
    assert result['value']['ratio'] == 0
    mismatch = [dict(city='杭州', role='技术研发', education='硕士', monthly_median=16000, year='2025')]
    result = job_stats.pay_analysis(jobs, {'甲公司'}, baselines=mismatch)
    assert result['value']['ratio'] == 0


def test_rubric_scorer_accepts_baseline_path():
    from jobscope.reliability.limits import MIN_CITY_JOBS
    from jobscope.reliability.rubric import _pay_points
    insufficient = dict(ratio=1.0, company_jobs=1, city_jobs=MIN_CITY_JOBS - 1)
    assert _pay_points(insufficient) is None
    assert _pay_points({**insufficient, 'city_baseline_layers': 1}) == 0.8


def test_api_import_list_delete_and_report_fallback(client):
    repo = client.app.state.repo
    repo.save_job(make_job(), 'user_import')

    # 表头错误如实回错，不入库。
    bad = client.post('/api/wage-baselines/import', json={'csv': 'a,b\n1,2'})
    assert bad.status_code == 200 and bad.json()['saved'] == 0 and bad.json()['errors']

    imported = client.post('/api/wage-baselines/import', json={'csv': GOOD_CSV})
    assert imported.status_code == 200
    assert imported.json() == {'saved': 2, 'errors': []}
    listed = client.get('/api/wage-baselines').json()['baselines']
    assert len(listed) == 2

    # 重算求职参考：pay 维度靠基线兜底出分（同城同层报价池只有 0 条）。
    report = client.post('/api/companies/甲公司/reliability', json={'credit_code': ''}).json()
    pay = next(item for item in report['dimensions'] if item['dimension'] == 'pay')
    assert pay['score'] is not None
    pay_finding = next(f for f in pay['findings'] if f['rule'] == 'pay_vs_city_median')
    assert '官方工资基线' in pay_finding['text'] or '官方工资基线' in report.get('summary', '')

    # 删除后重算：闸门缺口回来，pay 维度不再出分（其余维度也不足以撑总分出分）。
    assert client.delete(f"/api/wage-baselines/{listed[0]['id']}").json()['deleted'] == listed[0]['id']
    client.delete(f"/api/wage-baselines/{listed[1]['id']}")
    assert client.get('/api/wage-baselines').json()['baselines'] == []
    report = client.post('/api/companies/甲公司/reliability', json={'credit_code': ''}).json()
    pay = next(item for item in report['dimensions'] if item['dimension'] == 'pay')
    assert pay['score'] is None
    assert any('工资基线' in line for line in pay['missing'])


def test_parse_rejects_nonfinite_numbers():
    """float('inf')/'Infinity' 能过 >0 校验但入库后 JSON 序列化会打挂前端：必须在解析层闸住。"""
    bad = ('城市,职类,学历,月薪中位数,年份,备注\n'
           '杭州,技术研发,本科,inf,2025,\n'
           '杭州,技术研发,本科,Infinity,2025,\n'
           '杭州,技术研发,本科,nan,2025,\n')
    rows, errors = wage_baseline.parse(bad)
    assert rows == [] and len(errors) == 3
    assert all('有限数字' in item for item in errors)
