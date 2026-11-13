import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jobscope.repository import Repository
from jobscope.research.index import ResearchIndex


def test_supplement_remains_retrievable_with_its_own_source_after_recollection(tmp_path):
    repo = Repository(tmp_path)
    listing = dict(external_id='fixture-only', title='数据分析师', company='示例甲',
                   location='西安', salary='12-18K', description='仅列表摘要', description_status='listing')
    first = repo.save_job(listing, 'liepin')
    index = ResearchIndex(repo)
    index.sync()
    manual = repo.supplement_job_description(first['id'], '负责 SQL 质量审计', '用户粘贴的招聘方邮件')
    for price in (None, '15-19K'):
        if price:
            repo.save_job({**listing, 'salary': price}, 'liepin')
        index.sync()
        with repo.connection() as c:
            current = c.execute('''SELECT d.evidence_id,d.source FROM research_docs d
                JOIN research_job_documents m ON m.evidence_id=d.evidence_id WHERE m.job_id=?''',
                (first['id'],)).fetchall()
        assert [(row['evidence_id'], row['source']) for row in current] == [
            (manual['manual_description_evidence_id'], 'user_import')]
        assert repo.get_evidence(current[0]['evidence_id'])['source_note'] == '用户粘贴的招聘方邮件'
