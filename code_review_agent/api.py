"""Loopback-only HTTP transport for the local review workbench."""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from code_review_agent.config import load_settings
from code_review_agent.review_service import ReviewService, ServiceError


class ProjectInput(BaseModel):
    path: str = Field(min_length=1, max_length=4096)


class SessionInput(BaseModel):
    project_id: str
    new: bool = False


class JobInput(BaseModel):
    kind: Literal['review', 'recheck', 'chat', 'fix'] = 'review'
    paths: list[str] = Field(default_factory=list, max_length=20)
    instruction: str = Field(default='', max_length=4000)
    run_id: str = ''
    finding_id: str = ''


def create_app(service: ReviewService | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.service = service or ReviewService(load_settings(require_api_key=False))
        yield
        app.state.service.close()

    app = FastAPI(title='代码审查工作台', lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['localhost', '127.0.0.1', '[::1]'] + (['testserver'] if service else []))

    @app.middleware('http')
    async def local_origin(request: Request, call_next):
        origin = request.headers.get('origin')
        if origin:
            parsed = urlsplit(origin)
            if parsed.scheme != 'http' or parsed.hostname not in {'localhost', '127.0.0.1', '::1'} or parsed.port not in {request.url.port or 80, 5173}:
                return JSONResponse({'detail': '仅允许本机工作台访问'}, status_code=403)
        return await call_next(request)

    @app.exception_handler(ServiceError)
    async def handle_error(request: Request, exc: ServiceError):
        return JSONResponse({'detail': str(exc)}, status_code=exc.status)

    def svc(request: Request) -> ReviewService:
        return request.app.state.service

    @app.get('/api/health')
    def health(request: Request):
        settings = svc(request).settings
        return {'ok': True, 'model_configured': bool(settings.api_key), 'model': settings.model, 'max_files': 20, 'max_total_bytes': 1_000_000, 'demo_path': str(Path(__file__).resolve().parents[1] / 'examples' / 'demo_project')}

    @app.post('/api/projects')
    def project(body: ProjectInput, request: Request):
        return svc(request).load_project(body.path)

    @app.get('/api/projects/{project_id}/files')
    def files(project_id: str, request: Request):
        return svc(request).files(project_id)

    @app.get('/api/projects/{project_id}/source')
    def source(project_id: str, path: str, request: Request):
        return svc(request).source(project_id, path)

    @app.post('/api/sessions')
    def session(body: SessionInput, request: Request):
        return svc(request).create_session(body.project_id, body.new)

    @app.get('/api/sessions/{session_id}')
    def get_session(session_id: str, request: Request):
        return svc(request).session(session_id)

    @app.post('/api/sessions/{session_id}/jobs', status_code=202)
    def submit(session_id: str, body: JobInput, request: Request):
        return svc(request).submit(session_id, **body.model_dump())

    @app.get('/api/jobs/{job_id}')
    def job(job_id: str, request: Request, after_seq: int = Query(default=0, ge=0)):
        return svc(request).job(job_id, after_seq)

    @app.get('/api/runs/{run_id}')
    def run(run_id: str, request: Request):
        return svc(request).run(run_id)

    @app.get('/api/runs/{run_id}/report')
    def report(run_id: str, request: Request, format: Literal['md', 'json'] = 'md'):
        service = svc(request)
        if format == 'json':
            return JSONResponse(service.run(run_id), headers={'Content-Disposition': f'attachment; filename="review-{run_id[:8]}.json"'})
        return Response(service.report(run_id), media_type='text/markdown', headers={'Content-Disposition': f'attachment; filename="review-{run_id[:8]}.md"'})

    frontend = Path(__file__).resolve().parents[1] / 'frontend' / 'dist'
    if frontend.is_dir():
        app.mount('/', StaticFiles(directory=frontend, html=True), name='workbench')
    else:
        @app.get('/')
        def missing_frontend():
            return JSONResponse({'detail': '请先在 frontend 运行 npm ci 和 npm run build；开发时访问 Vite。'}, status_code=503)
    return app
