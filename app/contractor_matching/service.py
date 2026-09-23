from __future__ import annotations
import hashlib
from pathlib import Path
from time import perf_counter

from .data_loader import load_profiles
from .embeddings import FrozenEmbeddings
from .explainer import Explainer
from .filters import filter_contractors
from .guidance import build_guidance
from .models import SearchRequest
from .ranking import rank_contractors


class MatchingService:
    def __init__(self, data_dir: Path, cache_path: Path):
        self.profiles = load_profiles(data_dir)
        self.embeddings = FrozenEmbeddings(data_dir, self.profiles)
        self.explainer = Explainer(data_dir, cache_path)
        self.catalog_version = hashlib.sha256(b''.join((data_dir / name).read_bytes() for name in ['original.csv', 'team_synthetic.csv'])).hexdigest()

    async def search(self, request: SearchRequest) -> dict:
        started = perf_counter()
        filtered = filter_contractors(self.profiles, request)
        similarities = self.embeddings.similarities(request, filtered.survivors) if filtered.survivors else {}
        ranked = rank_contractors(filtered.survivors, request, similarities)
        batch = await self.explainer.explain(request, [entry.profile for entry in ranked]) if ranked else None
        cards = []
        for entry in ranked:
            profile = entry.profile
            cards.append({
                'id': profile.id, 'anon_name': profile.anon_name, 'category': request.category,
                'categories': sorted(profile.categories), 'city': profile.city,
                'price_from_kzt': profile.price_from_kzt, 'explanation': batch.by_id[profile.id],
                'synthetic': profile.synthetic, 'origin': profile.origin,
                'city_imputed': profile.city_imputed, 'price_imputed': profile.price_imputed,
                'available_on': request.date, 'languages': sorted(profile.languages),
                'max_hours': profile.max_hours, 'explanation_source': batch.source_by_id[profile.id],
                'explanation_evidence': batch.evidence_by_id.get(profile.id, {}),
                'warnings': batch.warnings_by_id.get(profile.id, []),
                'score': entry.score, 'score_breakdown': entry.score_breakdown,
            })
        message = filtered.message
        reason_labels = {'busy': 'заняты на дату', 'budget': 'цена выше бюджета', 'format': 'не работают с форматом', 'duration': 'не подходят по длительности', 'language': 'не указан нужный язык'}
        reasons = '; '.join(f'{label} — {getattr(filtered.stats, key)}' for key, label in reason_labels.items() if getattr(filtered.stats, key))
        count = len(filtered.survivors)
        if count >= 3:
            message = f'Показываем 3 из {count} подходящих кандидатов. На {request.date} заняты: {filtered.stats.busy}; они исключены до ранжирования.'
        elif count:
            message = f'Найдено {count} из {filtered.pool_count} в категории «{request.category}», город {request.city}. '
            message += f'Остальные исключены: {reasons}.' if reasons else 'В этой категории и городе всего столько профилей; все соответствуют условиям.'
        elif filtered.pool_count:
            message = f'В категории «{request.category}», город {request.city}, есть {filtered.pool_count} профиля, но на {request.date} ни один не подошёл: {reasons}.'
        else:
            message = f'В каталоге города {request.city} нет категории «{request.category}». Измените категорию или город.'
        stats = filtered.stats.model_dump()
        stats['total'] = filtered.stats.total()
        return {
            'outcome': filtered.outcome.value, 'message': message, 'cards': cards, 'stats': stats,
            'pool_count': filtered.pool_count, 'eligible_count': len(filtered.survivors),
            'assistant_suggestions': build_guidance(self.profiles, request, filtered),
            'diagnostics': {
                'catalog_version': self.catalog_version, 'embedding_version': self.embeddings.version,
                'embedding_model': self.embeddings.model, 'rank_mode': self.embeddings.mode,
                'rejected_by_id': filtered.rejected_by_id,
                'explanation_mode': self.explainer.mode, 'cache_hit': bool(batch and batch.cache_hit),
            },
            'elapsed_ms': round((perf_counter() - started) * 1000, 2),
        }
