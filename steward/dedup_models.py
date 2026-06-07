"""Dataclasses for in-memory deduplication (Exact / Fuzzy from Data Profiler Pro)."""
from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class DuplicateGroup:
    group_id: int
    indices: List[int]
    values: List[Dict]
    match_type: str
    similarity_score: Optional[float] = None
    key_columns: List[str] = None
    representative_value: Optional[str] = None

    def __post_init__(self):
        if self.key_columns is None:
            self.key_columns = []
