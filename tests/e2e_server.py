"""Isolated API server for browser acceptance tests; never invokes a live model."""
from pathlib import Path
from types import SimpleNamespace
import tempfile
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import uvicorn
from code_review_agent.api import create_app
from code_review_agent.config import Settings
from code_review_agent.review_service import ReviewService


class FakeLLM:
    def __init__(self, settings):
        pass

    def complete(self, messages, **kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='## 摘要\n本次分析完成，依据来自实际 AST 检查。', tool_calls=[]))])


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='review-e2e-') as data:
        service = ReviewService(Settings(api_key='test', base_url='https://example.invalid', model='fake'), Path(data), FakeLLM)
        uvicorn.run(create_app(service), host='127.0.0.1', port=8001)
