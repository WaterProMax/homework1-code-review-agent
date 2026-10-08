from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from code_review_agent.analysis import analyze_python
from code_review_agent.api import create_app
from code_review_agent.config import Settings
from code_review_agent.memory import Finding, MemoryStore
from code_review_agent.review_service import ReviewService, ServiceError, atomic_json
from code_review_agent.tools import Toolbelt


class FakeLLM:
    def __init__(self, settings):
        pass

    def complete(self, messages, **kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='## 摘要\n分析完成。', tool_calls=[]))])


@pytest.fixture
def service(tmp_path):
    settings = Settings(api_key='test-secret', base_url='https://example.invalid', model='fake', session_dir=tmp_path / 'sessions')
    instance = ReviewService(settings, tmp_path / 'data', FakeLLM)
    yield instance
    instance.close()


def setup_project(service, tmp_path, source='def append(x, values=[]):\n    values.append(x)\n    return values\n'):
    root = tmp_path / 'project'
    root.mkdir(exist_ok=True)
    (root / 'app.py').write_text(source, encoding='utf-8')
    project = service.load_project(str(root))
    session = service.create_session(project['id'])
    return root, project, session


def finish(service, job):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = service.job(job['id'])
        if result['status'] in {'completed', 'partial', 'failed', 'interrupted'}:
            return result
        time.sleep(0.01)
    raise AssertionError('job did not finish')


def reviewed(service, tmp_path, source=None):
    root, project, session = setup_project(service, tmp_path, **({'source': source} if source else {}))
    job = finish(service, service.submit(session['id'], 'review', ['app.py']))
    assert job['status'] == 'completed', job
    return root, project, session, service.run(job['result']['run_id'])


@pytest.mark.parametrize(('source', 'codes'), [
    ('def f(*, x=[]):\n    return x\n', {'mutable-default'}),
    ('from __future__ import annotations\nx = 1\n', set()),
    ('import math\n\nx=2\n', {'unused-import'}),
    ('x = 1 // 0\ny = 2 % 0\n', {'div-zero'}),
    ('# open("fake")\nx = "open(fake)"\n', set()),
    ('with open("x") as f:\n    data=f.read()\n', set()),
    ('f=open("x")\nf.close()\n', set()),
    ('f=open("x")\ng=open("y")\ng.close()\n', {'open-without-with'}),
    ('try:\n    x=1\nexcept Exception as error:\n    pass\n', {'swallowed-exception'}),
    ('import math\ndef f():\n    return math.sqrt(4)\n', set()),
    ('import math\n__all__=["math"]\n', set()),
])
def test_static_rules(source, codes):
    assert {i['code'] for i in analyze_python(source)['issues']} == codes


def test_precise_import_line():
    result = analyze_python('\nx=1\nimport math\n')
    assert result['issues'][0]['line'] == 3


def test_directory_scope_limits(service, tmp_path):
    root, project, session = setup_project(service, tmp_path)
    (root / '.venv').mkdir()
    (root / '.venv' / 'hidden.py').write_text('x=1/0')
    (root / 'large.py').write_text('x' * 80_001)
    outside = tmp_path / 'outside.py'
    outside.write_text('x=1')
    (root / 'escape.py').symlink_to(outside)
    files = service.files(project['id'])
    assert '.venv/hidden.py' not in [f['path'] for f in files]
    assert not next(f for f in files if f['path'] == 'large.py')['available']
    assert not next(f for f in files if f['path'] == 'escape.py')['available']
    for path in ['../outside.py', 'escape.py', '.venv/hidden.py', '/etc/passwd']:
        with pytest.raises(ServiceError):
            service.source(project['id'], path)
    with pytest.raises(ServiceError):
        service.submit(session['id'], 'review', [])


def test_multiple_occurrences_keep_distinct_ids(service, tmp_path):
    root, project, session, run = reviewed(service, tmp_path, 'def f(x=[], y=[]):\n    return x,y\n')
    assert len(run['findings']) == 2
    assert len({f['finding_id'] for f in run['findings']}) == 2
    again = finish(service, service.submit(session['id'], 'review', ['app.py']))
    repeat = service.run(again['result']['run_id'])
    assert [f['finding_id'] for f in run['findings']] == [f['finding_id'] for f in repeat['findings']]


def test_preview_recheck_and_immutable_history(service, tmp_path):
    root, project, session, original = reviewed(service, tmp_path)
    finding = original['findings'][0]
    preview_job = finish(service, service.submit(session['id'], 'fix', run_id=original['id'], finding_id=finding['finding_id']))
    assert preview_job['status'] == 'completed', preview_job
    preview = preview_job['result']['preview']
    assert 'values=None' in preview['after']
    assert 'if values is None:' in preview['after']
    assert (root / 'app.py').read_text() == preview['before']  # preview never writes
    (root / 'app.py').write_text(preview['after'])
    recheck = finish(service, service.submit(session['id'], 'recheck', run_id=original['id']))
    latest = service.run(recheck['result']['run_id'])
    assert latest['findings'][0]['status'] == 'resolved'
    assert latest['findings'][0]['finding_id'] == finding['finding_id']
    assert service.run(original['id'])['findings'][0]['status'] == 'open'
    assert service.run(original['id'])['sources']['app.py']['source'] == preview['before']
    assert 'resolved' in service.report(latest['id'])
    stale = finish(service, service.submit(session['id'], 'fix', run_id=original['id'], finding_id=finding['finding_id']))
    assert stale['status'] == 'failed'
    assert '已变化' in stale['error']


def test_line_shift_stays_same_issue(service, tmp_path):
    root, project, session, original = reviewed(service, tmp_path)
    (root / 'app.py').write_text('# comment\n\n' + (root / 'app.py').read_text())
    job = finish(service, service.submit(session['id'], 'recheck', run_id=original['id']))
    latest = service.run(job['result']['run_id'])
    assert latest['findings'][0]['finding_id'] == original['findings'][0]['finding_id']
    assert latest['findings'][0]['verification'] == 'still_present'
    assert latest['findings'][0]['line'] == 3


def test_missing_file_does_not_close_issue(service, tmp_path):
    root, project, session, original = reviewed(service, tmp_path)
    (root / 'app.py').unlink()
    job = finish(service, service.submit(session['id'], 'recheck', run_id=original['id']))
    latest = service.run(job['result']['run_id'])
    assert latest['status'] == 'partial'
    assert latest['findings'][0]['verification'] == 'inconclusive'
    assert latest['findings'][0]['status'] != 'resolved'


def test_partial_failure_retains_other_file(service, tmp_path):
    root, project, session = setup_project(service, tmp_path)
    (root / 'bad.py').write_bytes(b'\xff')
    job = finish(service, service.submit(session['id'], 'review', ['app.py', 'bad.py']))
    run = service.run(job['result']['run_id'])
    assert run['status'] == 'partial'
    assert run['findings']
    assert 'bad.py' in run['errors']


def test_model_failure_retains_static_evidence(tmp_path):
    class BrokenLLM(FakeLLM):
        def complete(self, messages, **kwargs):
            from code_review_agent.llm import LLMError
            raise LLMError('simulated timeout')
    settings = Settings(api_key='test', base_url='https://example.invalid', model='fake')
    service = ReviewService(settings, tmp_path / 'data', BrokenLLM)
    try:
        root, project, session = setup_project(service, tmp_path)
        job = finish(service, service.submit(session['id'], 'review', ['app.py']))
        run = service.run(job['result']['run_id'])
        assert run['status'] == 'partial'
        assert run['findings'][0]['code'] == 'mutable-default'
    finally:
        service.close()


def test_scoped_model_evidence_and_status_validation(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    target = root / 'a.py'
    target.write_text('x=1\n')
    memory = MemoryStore('scoped', tmp_path / 'memory', 4000, 4)
    tools = Toolbelt(Settings(api_key='test', base_url='', model='fake'), memory, root=root, snapshots={str(target): 'x=1\n'})
    tools.read_file(str(target))
    assert not json.loads(tools.record_finding(str(target), 2, 'bad', 'fake', 'fix', '高'))['ok']
    good = json.loads(tools.record_finding(str(target), 1, 'review', 'x=1', 'consider', '中'))
    assert good['finding']['confidence'] == 'suspected'
    assert not json.loads(tools.remember('finding', 'resolved', path=str(target), status='resolved'))['ok']
    assert not json.loads(tools.execute('read_file', '{"path":"../secret.py"}'))['ok']


def test_api_origin_and_downloads(service, tmp_path):
    root, project, session, run = reviewed(service, tmp_path)
    with TestClient(create_app(service)) as client:
        health = client.get('/api/health')
        assert health.status_code == 200
        assert 'test-secret' not in health.text
        assert client.post('/api/projects', json={'path': str(root)}, headers={'Origin': 'https://evil.example'}).status_code == 403
        assert client.get(f'/api/projects/{project["id"]}/source', params={'path':'../outside.py'}).status_code == 403
        assert client.get(f'/api/runs/{run["id"]}/report').headers['content-type'].startswith('text/markdown')
        assert client.get(f'/api/runs/{run["id"]}/report?format=json').json()['id'] == run['id']
        assert client.get('/api/runs/../../bad').status_code in {404, 405}


def test_restart_persistence_and_interruption(service, tmp_path):
    root, project, session, run = reviewed(service, tmp_path)
    service.state['jobs']['unfinished'] = {'id': 'unfinished', 'status':'running', 'events':[]}
    service._save()
    restored = ReviewService(service.settings, service.data_dir, FakeLLM)
    try:
        assert restored.job('unfinished')['status'] == 'interrupted'
        assert restored.session(session['id'])['runs'] == [run['id']]
        assert restored.run(run['id'])['sources']
    finally:
        restored.close()


def test_legacy_session_loads_new_defaults(tmp_path):
    payload = {'session_id':'legacy', 'findings':{'F001':{'finding_id':'F001','path':'a.py','severity':'高','title':'old','status':'confirmed'}}}
    atomic_json(tmp_path / 'legacy.json', payload)
    memory = MemoryStore.load('legacy', tmp_path, 4000, 4)
    assert memory.findings['F001'].code == ''
    memory.upsert_finding(Finding('', 'a.py', '高', 'other', location='2'))
    assert 'F002' in memory.findings


def test_one_of_two_defaults_fixed_does_not_swap_identity(service, tmp_path):
    root, project, session, original = reviewed(service, tmp_path, 'def f(x=[], y=[]):\n    return x,y\n')
    identities = {f['title']: f['finding_id'] for f in original['findings']}
    (root / 'app.py').write_text('def f(x=None, y=[]):\n    if x is None:\n        x=[]\n    return x,y\n')
    job = finish(service, service.submit(session['id'], 'recheck', run_id=original['id']))
    latest = service.run(job['result']['run_id'])
    by_title = {f['title']: f for f in latest['findings']}
    assert by_title['参数 x 使用了可变默认值']['status'] == 'resolved'
    assert by_title['参数 y 使用了可变默认值']['status'] == 'open'
    assert {title:f['finding_id'] for title,f in by_title.items()} == identities


def test_total_size_and_file_count_limits(service, tmp_path):
    root, project, session = setup_project(service, tmp_path)
    for index in range(21):
        (root / f'file{index}.py').write_text('#' + 'a' * 79_000)
    with pytest.raises(ServiceError, match='1–20'):
        service.submit(session['id'], 'review', [f'file{i}.py' for i in range(21)])
    with pytest.raises(ServiceError, match='1 MB'):
        service.submit(session['id'], 'review', [f'file{i}.py' for i in range(13)])


def test_large_tool_output_is_valid_json(tmp_path):
    target = tmp_path / 'big.py'
    target.write_text('# large source\n' + 'value = 1\n' * 3000)
    settings = Settings(api_key='test', base_url='', model='fake')
    memory = MemoryStore('large', tmp_path / 'memory', 7000, 6)
    tools = Toolbelt(settings, memory)
    payload = json.loads(tools.read_file(str(target)))
    assert payload['ok'] and payload['truncated']
    assert str(target) in memory.file_facts


def test_changed_scope_is_inconclusive(service, tmp_path):
    root, project, session, original = reviewed(service, tmp_path)
    (root / 'app.py').write_text('def other(x):\n    return x\n')
    job = finish(service, service.submit(session['id'], 'recheck', run_id=original['id']))
    latest = service.run(job['result']['run_id'])
    assert latest['findings'][0]['verification'] == 'inconclusive'


def test_job_lock_and_incremental_events(tmp_path):
    import threading
    started = threading.Event()
    release = threading.Event()
    class SlowLLM(FakeLLM):
        def complete(self, messages, **kwargs):
            started.set()
            release.wait(timeout=3)
            return super().complete(messages, **kwargs)
    settings = Settings(api_key='test', base_url='', model='fake')
    instance = ReviewService(settings, tmp_path / 'data', SlowLLM)
    try:
        root, project, session = setup_project(instance, tmp_path)
        job = instance.submit(session['id'], 'review', ['app.py'])
        assert started.wait(timeout=1)
        with pytest.raises(ServiceError) as error:
            instance.submit(session['id'], 'review', ['app.py'])
        assert error.value.status == 409
        events = instance.job(job['id'])['events']
        assert instance.job(job['id'], max(e['seq'] for e in events))['events'] == []
        release.set()
        assert finish(instance, job)['status'] == 'completed'
    finally:
        release.set()
        instance.close()


@pytest.mark.parametrize('case', json.loads((Path(__file__).with_name('evaluation_cases.json')).read_text(encoding='utf-8')), ids=lambda c:c['name'])
def test_labeled_evaluation_case(case):
    actual = sorted(issue['code'] for issue in analyze_python(case['source'])['issues'])
    assert actual == sorted(case['expected'])


def test_model_constructor_failure_keeps_static_results(tmp_path):
    def broken_factory(settings):
        raise RuntimeError('model transport could not initialize')
    instance = ReviewService(Settings(api_key='test', base_url='', model='fake'), tmp_path / 'data', broken_factory)
    try:
        root, project, session = setup_project(instance, tmp_path)
        job = finish(instance, instance.submit(session['id'], 'review', ['app.py']))
        run = instance.run(job['result']['run_id'])
        assert run['status'] == 'partial'
        assert run['findings'][0]['code'] == 'mutable-default'
    finally:
        instance.close()


def test_scoped_schema_excludes_direct_finding_mutations(tmp_path):
    memory = MemoryStore('schema', tmp_path / 'memory', 4000, 4)
    tools = Toolbelt(Settings(api_key='test', base_url='', model='fake'), memory, root=tmp_path)
    schema = next(t['function']['parameters'] for t in tools.openai_tools() if t['function']['name'] == 'remember')
    assert schema['properties']['kind']['enum'] == ['preference', 'note']
    assert 'status' not in schema['properties']


@pytest.mark.parametrize('mode', ['valid', 'invalid-json', 'invalid-syntax', 'wrong-fragment'])
def test_model_patch_validation(tmp_path, mode):
    class PatchLLM(FakeLLM):
        def complete(self, messages, **kwargs):
            if mode == 'invalid-json':
                content = 'not JSON'
            else:
                content = json.dumps({'before':'import math\n' if mode != 'wrong-fragment' else 'nonexistent',
                                      'after':'def broken(' if mode == 'invalid-syntax' else '',
                                      'explanation':'删除未使用导入'}, ensure_ascii=False)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=[]))])
    instance = ReviewService(Settings(api_key='test', base_url='', model='fake'), tmp_path / 'data', PatchLLM)
    try:
        root, project, session = setup_project(instance, tmp_path, 'import math\nvalue=1\n')
        review = finish(instance, instance.submit(session['id'], 'review', ['app.py']))
        run = instance.run(review['result']['run_id'])
        finding = run['findings'][0]
        job = finish(instance, instance.submit(session['id'], 'fix', run_id=run['id'], finding_id=finding['finding_id']))
        if mode == 'valid':
            assert job['status'] == 'completed'
            assert job['result']['preview']['after'] == 'value=1\n'
        else:
            assert job['status'] == 'failed'
        assert (root / 'app.py').read_text() == 'import math\nvalue=1\n'
    finally:
        instance.close()
