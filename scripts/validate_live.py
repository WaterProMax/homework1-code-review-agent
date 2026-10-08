"""Live acceptance check in an isolated copy; never modifies demonstration sources."""
from __future__ import annotations

import json
import shutil
import tempfile
import time
from dataclasses import replace
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from code_review_agent.config import load_settings
from code_review_agent.review_service import ReviewService


def wait(service, job):
    previous = -1
    while True:
        result = service.job(job['id'])
        if result['completed_files'] != previous:
            print(f'{job["kind"]}: {result["completed_files"]}/{result["total_files"]} files', flush=True)
            previous = result['completed_files']
        if result['status'] in {'completed', 'partial', 'failed', 'interrupted'}:
            if result['status'] not in {'completed', 'partial'}:
                if result.get('result') and result['result'].get('run_id'):
                    run = service.run(result['result']['run_id'])
                    print(json.dumps(run['errors'], ensure_ascii=False), flush=True)
                raise RuntimeError(f'Acceptance task did not complete: {result["status"]}; {result.get("error") or "inspect partial results"}')
            return result
        time.sleep(0.5)


if __name__ == '__main__':
    base = Path(__file__).resolve().parents[1]
    settings = replace(load_settings(), request_timeout=30, max_retries=2)
    with tempfile.TemporaryDirectory(prefix='review-live-') as temporary:
        root = Path(temporary) / 'project'
        shutil.copytree(base / 'examples' / 'demo_project', root)
        service = ReviewService(settings, Path(temporary) / 'data')
        try:
            project = service.load_project(str(root))
            session = service.create_session(project['id'])
            job = wait(service, service.submit(session['id'], 'review', [f['path'] for f in project['files']]))
            run = service.run(job['result']['run_id'])
            assert len(run['sources']) == 3 and run['findings']
            if run['status'] == 'partial':
                print(json.dumps(run['errors'], ensure_ascii=False), flush=True)
            finding = next(f for f in run['findings'] if f['code'] == 'mutable-default')
            chat = wait(service, service.submit(session['id'], 'chat', run_id=run['id'], finding_id=finding['finding_id'], instruction='解释这个默认参数问题的原因和影响，给出简短修复建议。'))
            assert chat['result']['reply']
            fix = wait(service, service.submit(session['id'], 'fix', run_id=run['id'], finding_id=finding['finding_id']))
            preview = fix['result']['preview']
            assert (root / finding['path']).read_text() == preview['before']
            (root / finding['path']).write_text(preview['after'])
            recheck = wait(service, service.submit(session['id'], 'recheck', run_id=run['id'], finding_id=finding['finding_id']))
            latest = service.run(recheck['result']['run_id'])
            assert any(f['finding_id'] == finding['finding_id'] and f['status'] == 'resolved' for f in latest['findings'])
            assert service.run(run['id'])['findings'] == run['findings']
            output = base / '.code_review_agent' / 'validation'
            output.mkdir(parents=True, exist_ok=True)
            (output / 'live-report.md').write_text(service.report(latest['id']))
            result = {'model':settings.model,'files':len(run['sources']),'findings':len(run['findings']),
                      'review_status':run['status'],'review_errors':run['errors'],'chat_completed':chat['status'] == 'completed','preview_did_not_write':True,
                      'recheck_status':latest['status'],'original_report_immutable':True, 'review_seconds':run['elapsed_seconds']}
            (output / 'live-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
            print(json.dumps(result, ensure_ascii=False), flush=True)
        finally:
            service.close()
