"""Same-origin FastAPI app and static product UI."""
from __future__ import annotations
import asyncio
from contextlib import asynccontextmanager
import logging
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .models import CITIES, CATEGORIES, EVENT_FORMATS, LANGUAGES, DATE_MIN, DATE_MAX, SearchRequest, SearchResponse
from .presets import PRESETS, COMPARISON
from .service import MatchingService

ROOT = Path(__file__).resolve().parents[2]
LOGGER = logging.getLogger('hackalem')


def create_app(data_dir: Path | None = None, cache_path: Path | None = None) -> FastAPI:
    data_dir = data_dir or Path(os.environ.get('DATA_DIR', ROOT / 'data'))
    cache_path = cache_path or Path(os.environ.get('CACHE_PATH', ROOT / '.cache/explanations.sqlite3'))

    @asynccontextmanager
    async def lifespan(app):
        app.state.service = MatchingService(data_dir, cache_path)
        yield

    app = FastAPI(title='событие. · подбор подрядчиков', version='1.0.0', lifespan=lifespan, docs_url=None, redoc_url=None)

    @app.middleware('http')
    async def headers(request: Request, call_next):
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        if request.url.path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        # Return only field names/messages; never echo arbitrary submitted input.
        return JSONResponse(status_code=422, content={
            'message': 'Проверьте параметры: дату, категорию, положительный бюджет и целую длительность.',
            'errors': [{'field': '.'.join(str(x) for x in item['loc'][1:]), 'message': item['msg']} for item in error.errors()],
        })

    @app.get('/health/ready')
    async def readiness(request: Request):
        service = request.app.state.service
        return {'status': 'ready', 'profiles': len(service.profiles), 'rank_mode': service.embeddings.mode,
                'embedding_version': service.embeddings.version, 'explanation_mode': service.explainer.mode}

    @app.get('/api/meta')
    async def metadata(request: Request):
        service = request.app.state.service
        return {
            'cities': list(CITIES), 'categories': sorted(CATEGORIES), 'event_formats': list(EVENT_FORMATS),
            'languages': list(LANGUAGES), 'date_min': str(DATE_MIN), 'date_max': str(DATE_MAX),
            'presets': PRESETS, 'comparison': COMPARISON,
            'catalog': {'total': len(service.profiles), 'original': sum(p.origin == 'source_dataset' for p in service.profiles),
                        'team_added': sum(p.origin == 'team_extension' for p in service.profiles), 'synthetic': sum(p.synthetic for p in service.profiles)},
            'runtime': {'rank_mode': service.embeddings.mode, 'embedding_model': service.embeddings.model, 'explanation_mode': service.explainer.mode},
        }

    @app.post('/api/search', response_model=SearchResponse)
    async def search(request: Request, query: SearchRequest):
        try:
            async with asyncio.timeout(8):
                return await request.app.state.service.search(query)
        except TimeoutError:
            return JSONResponse(status_code=503, content={'message': 'Подбор занял слишком много времени. Повторите запрос: календарь и каталог сохранены.'})
        except Exception:
            # Do not expose provider bodies, request headers, credentials, or stack traces to the UI.
            LOGGER.error('Search failed; check catalog/cache availability')
            return JSONResponse(status_code=503, content={'message': 'Сервис временно недоступен. Проверьте готовность каталога и повторите запрос.'})

    app.mount('/static', StaticFiles(directory=ROOT / 'app/static', check_dir=False), name='static')
    app.mount('/', StaticFiles(directory=ROOT / 'app/static', html=True, check_dir=False), name='ui')
    return app


app = create_app()
