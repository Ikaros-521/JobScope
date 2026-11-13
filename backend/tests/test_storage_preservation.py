from jobscope.repository import Repository


def test_personal_tags_and_manual_jd_survive_refresh(tmp_path):
    repo = Repository(tmp_path / 'store.sqlite3')
    data = dict(title='开发', company='甲', url='https://example.test/job', description='平台摘要', tags=['平台'], description_status='summary')
    job = repo.save_job(data, 'platform')
    repo.update_jobs([job['id']], {'tags': ['个人'], 'starred': True})
    revised = repo.supplement_job_description(job['id'], '用户完整职责', '用户从招聘邮件补录')
    refreshed = repo.save_job({**data, 'description': '新平台原文', 'description_status': 'complete'}, 'platform')
    assert refreshed['description'] == '用户完整职责'
    assert refreshed['description_source'] == 'user'
    assert refreshed['tags'] == ['平台', '个人'] and refreshed['starred']
    assert set(revised['evidence_ids']) <= set(refreshed['evidence_ids'])
    repo.update_jobs([job['id']], {'tags': []})
    assert repo.save_job(data, 'platform')['tags'] == ['平台']
    snapshots = [repo.get_evidence(eid) for eid in refreshed['evidence_ids']]
    assert any(e['source'] == 'user_import' and e['text'] == '用户完整职责' for e in snapshots)
    assert all('用户完整职责' not in e['text'] for e in snapshots if e['source'] == 'platform')


def test_history_is_unbounded_and_branch_preserves_original(tmp_path):
    repo = Repository(tmp_path / 'store.sqlite3')
    scope = {'kind': 'global'}
    cid = repo.create_conversation(scope)
    for i in range(101):
        repo.append_messages(cid, [{'role': 'user', 'content': str(i)}, {'role': 'assistant', 'content': '答', 'citations': ['E-old']}])
    original = repo.get_conversation(cid, scope)['messages']
    assert len(original) == 202 and original[0]['content'] == '0'
    branch, prefix = repo.branch_for_edit(cid, scope, original[2]['id'])
    assert prefix == original[:2]
    assert repo.get_conversation(cid, scope)['messages'] == original
    assert repo.get_conversation(branch, scope)['messages'] == prefix

