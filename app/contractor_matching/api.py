"""Same-origin FastAPI app and static product UI."""
from __future__ import annotations
import asyncio
from contextlib import asynccontextmanager
import logging
from collections import OrderedDict
from pathlib import Path
from time import monotonic

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .models import CITIES, CATEGORIES, EVENT_FORMATS, LANGUAGES, DATE_MIN, DATE_MAX, SearchRequest, SearchResponse
from .presets import PRESETS, COMPARISON
from .service import MatchingService
from .config import load_project_env, project_env_path
from .inquiries import InquiryService, InquiryRequest, InquiryReceipt, InquiryError, InquiryUnavailable, InquiryConflict, InquiryNotFound
from .inquiry_delivery import configured_inquiry_transport

ROOT = Path(__file__).resolve().parents[2]
LOGGER = logging.getLogger('hackalem')


def create_app(data_dir: Path | None = None, cache_path: Path | None = None, inquiry_db_path: Path | None = None) -> FastAPI:
    load_project_env(ROOT)
    data_dir = data_dir or project_env_path('DATA_DIR', 'data', ROOT)
    cache_path = cache_path or project_env_path('CACHE_PATH', '.cache/explanations.sqlite3', ROOT)
    inquiry_db_path = inquiry_db_path or project_env_path('INQUIRY_DB_PATH', '.cache/inquiries.sqlite3', ROOT)
    inquiry_limits: OrderedDict[str, list[float]] = OrderedDict()

    @asynccontextmanager
    async def lifespan(app):
        app.state.service = MatchingService(data_dir, cache_path)
        app.state.inquiries = InquiryService(inquiry_db_path, app.state.service.profiles, configured_inquiry_transport())
        yield

    app = FastAPI(title='событие. · подбор подрядчиков', version='1.0.0', lifespan=lifespan, docs_url=None, redoc_url=None)

    @app.middleware('http')
    async def headers(request: Request, call_next):
        if request.url.path == '/api/inquiries' and request.method == 'POST':
            origin = request.headers.get('origin')
            if origin and origin != f'{request.url.scheme}://{request.url.netloc}':
                return JSONResponse(status_code=403, content={'message': 'Отправляйте обращение со страницы сервиса.'})
            if request.headers.get('content-type', '').split(';')[0].strip() != 'application/json':
                return JSONResponse(status_code=415, content={'message': 'Ожидается JSON.'})
            try:
                size = int(request.headers.get('content-length', '0'))
            except ValueError:
                size = -1
            if size < 0 or size > 16384:
                return JSONResponse(status_code=413, content={'message': 'Обращение слишком большое.'})
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 16384:
                    return JSONResponse(status_code=413, content={'message': 'Обращение слишком большое.'})
            # BaseHTTPMiddleware reuses Request's cached body for downstream parsing.
            request._body = bytes(body)
            now = monotonic()
            client_id = request.client.host if request.client else 'unknown'
            recent = [stamp for stamp in inquiry_limits.pop(client_id, []) if now - stamp < 60]
            inquiry_limits[client_id] = recent
            if len(recent) >= 10:
                return JSONResponse(status_code=429, headers={'Retry-After': '60'}, content={'message': 'Слишком много попыток. Повторите через минуту.'})
            recent.append(now)
            while len(inquiry_limits) > 2000:
                inquiry_limits.popitem(last=False)
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
        if request.url.path == '/api/inquiries':
            return JSONResponse(status_code=422, content={'message': 'Проверьте имя, email/телефон/Telegram для ответа, согласие и условия события.'})
        return JSONResponse(status_code=422, content={
            'message': 'Проверьте параметры: дату, категорию, положительный бюджет и целую длительность.',
            'errors': [{'field': '.'.join(str(x) for x in item['loc'][1:]), 'message': item['msg']} for item in error.errors()],
        })

    @app.get('/health/ready')
    async def readiness(request: Request):
        service = request.app.state.service
        return {'status': 'ready', 'product': 'hackalem-contractors', 'profiles': len(service.profiles), 'rank_mode': service.embeddings.mode,
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
            'runtime': {'rank_mode': service.embeddings.mode, 'embedding_model': service.embeddings.model, 'explanation_mode': service.explainer.mode,
                        'explanation_model': service.explainer.providers[0].config.model if service.explainer.providers else None},
            'inquiries': {'enabled': request.app.state.inquiries.delivery_configured,
                          'channel_label': getattr(request.app.state.inquiries.transport, 'channel_label', '')},
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

    @app.post('/api/inquiries', response_model=InquiryReceipt)
    async def submit_inquiry(request: Request, query: InquiryRequest):
        try:
            return await request.app.state.inquiries.submit(query, request.headers.get('idempotency-key', ''))
        except InquiryUnavailable as error:
            return JSONResponse(status_code=503, content={'message': str(error)})
        except InquiryConflict as error:
            return JSONResponse(status_code=409, content={'message': str(error)})
        except InquiryError as error:
            return JSONResponse(status_code=422, content={'message': str(error)})
        except Exception:
            LOGGER.error('Inquiry unavailable; no submitted data logged')
            return JSONResponse(status_code=503, content={'message': 'Не удалось подтвердить сохранение обращения. Повторите с теми же данными.'})

    @app.get('/api/inquiries/{inquiry_id}', response_model=InquiryReceipt)
    async def inquiry_receipt(request: Request, inquiry_id: str):
        authorization = request.headers.get('authorization', '')
        token = authorization[7:] if authorization.startswith('Bearer ') else ''
        try:
            return request.app.state.inquiries.get_receipt(inquiry_id, token)
        except InquiryNotFound:
            return JSONResponse(status_code=404, content={'message': 'Заявка не найдена.'})

    app.mount('/static', StaticFiles(directory=ROOT / 'app/static', check_dir=False), name='static')
    app.mount('/', StaticFiles(directory=ROOT / 'app/static', html=True, check_dir=False), name='ui')
    return app


app = create_app()
