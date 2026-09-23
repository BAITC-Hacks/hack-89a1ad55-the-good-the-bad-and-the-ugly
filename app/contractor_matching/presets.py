"""Verified catalog scenarios, not handpicked recommendation outputs."""
BASE = dict(city='Алматы', date='2026-10-04', event_format='корпоратив', category='Ведущий', budget=2_000_000, duration=6, language='русский')
PRESETS = [
    {'id': 'dense', 'label': 'Ведущие · Алматы', 'description': '5 свободных кандидатов из 10: покажем тройку по рейтингу.', 'request': BASE},
    {'id': 'rare', 'label': 'Декораторы · Астана', 'description': 'Пробел каталога закрыт тремя явно отмеченными профилями команды.', 'request': dict(city='Астана', date='2026-10-05', event_format='свадьба', category='Декоратор', budget=3_000_000, duration=10, language='русский')},
    {'id': 'florists', 'label': 'Редкая категория', 'description': 'Всего два флориста в Алматы. Длительность не отсеивает услуги без почасового лимита.', 'request': dict(city='Алматы', date='2026-10-04', event_format='свадьба', category='Флорист', budget=300_000, duration=10, language='русский')},
    {'id': 'absent', 'label': 'Категории нет', 'description': 'В зарубежном каталоге нет ведущих: сервис объяснит причину.', 'request': {**BASE, 'city': 'Зарубежье', 'date': '2026-10-10'}},
    {'id': 'budget', 'label': 'Все вне бюджета', 'description': 'Два флориста есть в городе, но бюджет ниже обеих стартовых цен.', 'request': dict(city='Алматы', date='2026-10-04', event_format='свадьба', category='Флорист', budget=100_000, duration=10, language='русский')},
    {'id': 'busy', 'label': 'Все заняты', 'description': 'Те же флористы заняты 1 октября: смена даты меняет доступность.', 'request': dict(city='Алматы', date='2026-10-01', event_format='свадьба', category='Флорист', budget=300_000, duration=10, language='русский')},
    {'id': 'venue', 'label': 'Банкетные залы', 'description': 'Площадки проходят тот же календарь и фильтры, что и остальные подрядчики.', 'request': dict(city='Алматы', date='2026-10-01', event_format='свадьба', category='Банкетный зал', budget=4_000_000, duration=8, language='русский')},
]
COMPARISON = {'date_a': '2026-10-04', 'date_b': '2026-10-05', 'request': BASE}
