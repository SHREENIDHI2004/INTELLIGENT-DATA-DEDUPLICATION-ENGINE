"""Fuzzy matching - same logic as data_profiler_pro deduplication_app."""
import re
import unicodedata
from typing import List
import pandas as pd
from rapidfuzz import fuzz
import difflib
import jellyfish
from metaphone import doublemetaphone
from .dedup_models import DuplicateGroup


class FuzzyMatcher:
    def __init__(self, algorithm: str = 'rapidfuzz', threshold: float = 85.0):
        self.algorithm = algorithm
        self.threshold = threshold
        self.supported_algorithms = {
            'rapidfuzz': self._rapidfuzz_match,
            'levenshtein': self._rapidfuzz_match,
            'difflib': self._difflib_match,
            'jaro_winkler': self._jaro_winkler_match,
            'metaphone': self._metaphone_match,
            'combined': self._combined_match
        }

    def _normalize_text(self, text: str) -> str:
        if pd.isna(text):
            return ""
        text = str(text).lower().strip()
        text = re.sub(r'[^\w\s]', '', text)
        text = ' '.join(text.split())
        text = ''.join(c for c in unicodedata.normalize('NFD', text)
                       if unicodedata.category(c) != 'Mn')
        return text

    def _rapidfuzz_match(self, str1: str, str2: str) -> float:
        return fuzz.ratio(str1, str2)

    def _difflib_match(self, str1: str, str2: str) -> float:
        return difflib.SequenceMatcher(None, str1, str2).ratio() * 100

    def _jaro_winkler_match(self, str1: str, str2: str) -> float:
        return jellyfish.jaro_winkler_similarity(str1, str2) * 100

    def _metaphone_match(self, str1: str, str2: str) -> float:
        meta1 = doublemetaphone(str1)
        meta2 = doublemetaphone(str2)
        if meta1[0] == meta2[0]:
            return 100.0
        elif meta1[0] == meta2[1] or meta1[1] == meta2[0]:
            return 85.0
        elif meta1[1] == meta2[1] and meta1[1]:
            return 70.0
        return 0.0

    def _combined_match(self, str1: str, str2: str) -> float:
        scores = [
            self._rapidfuzz_match(str1, str2) * 0.4,
            self._jaro_winkler_match(str1, str2) * 0.4,
            self._metaphone_match(str1, str2) * 0.2
        ]
        return sum(scores)

    def find_duplicate_groups(self, series: pd.Series) -> List[DuplicateGroup]:
        values = series.dropna().astype(str).tolist()
        indices = series.dropna().index.tolist()

        if len(values) < 2:
            return []

        normalized_map = {i: self._normalize_text(v) for i, v in zip(indices, values)}
        processed = set()
        groups = []
        group_id = 0

        for i, (idx1, val1) in enumerate(zip(indices, values)):
            if idx1 in processed:
                continue

            group_indices = [idx1]
            group_values = [val1]
            normalized1 = normalized_map[idx1]

            for idx2, val2 in zip(indices[i+1:], values[i+1:]):
                if idx2 in processed:
                    continue
                normalized2 = normalized_map[idx2]
                score = self.supported_algorithms[self.algorithm](normalized1, normalized2)
                if score >= self.threshold:
                    group_indices.append(idx2)
                    group_values.append(val2)
                    processed.add(idx2)

            if len(group_indices) > 1:
                processed.add(idx1)
                group_id += 1
                groups.append(DuplicateGroup(
                    group_id=group_id,
                    indices=group_indices,
                    values=[{series.name: v} for v in group_values],
                    match_type='fuzzy',
                    similarity_score=self.supported_algorithms[self.algorithm](
                        self._normalize_text(group_values[0]),
                        self._normalize_text(group_values[1])
                    ) if len(group_values) > 1 else 100.0,
                    key_columns=[series.name],
                    representative_value=group_values[0]
                ))

        return groups
