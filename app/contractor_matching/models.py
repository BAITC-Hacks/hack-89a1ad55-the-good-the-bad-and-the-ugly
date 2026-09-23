from typing import List, Optional, Set, Literal
from pydantic import BaseModel, Field, field_validator
from enum import Enum

class SearchRequest(BaseModel):
    """Модель входящего запроса от пользователя (из UI)"""
    city: Literal['Алматы', 'Астана', 'Зарубежье']
    date: str  # Ожидается формат YYYY-MM-DD
    event_format: str
    category: str
    budget: float = Field(gt=0, description="Бюджет должен быть больше нуля")
    duration: Optional[int] = None
    language: Optional[str] = None

class ContractorProfile(BaseModel):
    """Модель профиля подрядчика из датасета"""
    id: str
    anon_name: str
    categories: List[str]
    city: str
    price_from_kzt: float
    event_formats: List[str]
    languages: List[str]
    max_hours: Optional[int] = None
    busy_dates: Set[str] = Field(default_factory=set)
    description: str
    synthetic: bool = False
    city_imputed: bool = False
    price_imputed: bool = False

    # Валидаторы для очистки грязных данных из хакатон-датасета
    @field_validator('categories', 'event_formats', 'languages', mode='before')
    @classmethod
    def split_pipe_strings(cls, v):
        if isinstance(v, str):
            return [x.strip() for x in v.split('|') if x.strip()]
        return v

    @field_validator('busy_dates', mode='before')
    @classmethod
    def parse_busy_dates(cls, v):
        if isinstance(v, str):
            return {x.strip() for x in v.split('|') if x.strip()}
        if isinstance(v, list):
            return set(v)
        return set()

    @field_validator('max_hours', mode='before')
    @classmethod
    def parse_max_hours(cls, v):
        # Спасаем флористов и декораторов от отсева
        if v in ["не применимо", "", "null", None]:
            return None
        try:
            return int(v)
        except (ValueError, TypeError):
            return None

class SearchOutcome(str, Enum):
    SUCCESS = "SUCCESS"
    NO_CATEGORY_IN_CITY = "NO_CATEGORY_IN_CITY"
    ALL_FILTERED_OUT = "ALL_FILTERED_OUT"

class RejectionStats(BaseModel):
    """Счетчики причин отсева для честной диагностики"""
    busy: int = 0
    budget: int = 0
    format: int = 0
    duration: int = 0
    language: int = 0

class ContractorCard(BaseModel):
    """Финальная карточка для выдачи на фронтенд"""
    id: str
    anon_name: str
    category: str
    city: str
    price_from_kzt: float
    explanation: str
    is_synthetic: bool

class SearchResponse(BaseModel):
    """Финальный ответ бэкенда"""
    status: SearchOutcome
    message: Optional[str] = None
    cards: List[ContractorCard] = Field(default_factory=list)
    stats: RejectionStats = Field(default_factory=RejectionStats)