"""Minimal profiling engine for Exact + Fuzzy deduplication (same logic as data_profiler_pro)."""
from typing import List, Optional
import pandas as pd
from .dedup_models import DuplicateGroup
from .data_utils import find_exact_duplicates
from .fuzzy_matching import FuzzyMatcher


class DataProfilerEngine:
    def __init__(self, df: pd.DataFrame, filename: str = "data"):
        self.df = df.copy()
        self.filename = filename
        self.exact_duplicates: List[DuplicateGroup] = []
        self.fuzzy_duplicates: List[DuplicateGroup] = []
        self.combined_duplicates: List[DuplicateGroup] = []
        self.predictive_duplicates: List[DuplicateGroup] = []

    def find_exact_duplicates(self, subset: Optional[List[str]] = None) -> List[DuplicateGroup]:
        self.exact_duplicates = find_exact_duplicates(self.df, subset)
        return self.exact_duplicates

    def find_fuzzy_duplicates(
        self,
        columns: List[str],
        threshold: float = 85.0,
        algorithm: str = 'rapidfuzz'
    ) -> List[DuplicateGroup]:
        matcher = FuzzyMatcher(algorithm=algorithm, threshold=threshold)
        all_groups = []
        group_id = 0

        for col in columns:
            if not pd.api.types.is_string_dtype(self.df[col]):
                continue
            col_groups = matcher.find_duplicate_groups(self.df[col])
            for group in col_groups:
                group_id += 1
                group.group_id = group_id
                group.values = [self.df.loc[idx].to_dict() for idx in group.indices]
                all_groups.append(group)

        self.fuzzy_duplicates = all_groups
        return all_groups

    def find_combined_duplicates(
        self,
        exact_columns: List[str],
        fuzzy_columns: List[str],
        threshold: float = 85.0,
        algorithm: str = 'rapidfuzz'
    ) -> List[DuplicateGroup]:
        """Find duplicates using both exact and fuzzy matching on different columns"""
        groups = []
        
        # First, group by exact columns
        if exact_columns:
            exact_groups = self.find_exact_duplicates(subset=exact_columns)
            
            # Within each exact group, check fuzzy matching on fuzzy columns
            if fuzzy_columns and exact_groups:
                matcher = FuzzyMatcher(algorithm=algorithm, threshold=threshold)
                
                for exact_group in exact_groups:
                    indices = exact_group.indices
                    if len(indices) < 2:
                        continue
                    
                    # Check fuzzy similarity within this exact group
                    for fuzzy_col in fuzzy_columns:
                        if not pd.api.types.is_string_dtype(self.df[fuzzy_col]):
                            continue
                        
                        # Get values for this column within the exact group
                        # The Series preserves original DataFrame indices
                        group_series = self.df.loc[indices, fuzzy_col]
                        col_groups = matcher.find_duplicate_groups(group_series)
                        
                        for col_group in col_groups:
                            if len(col_group.indices) > 1:
                                # col_group.indices are original DataFrame indices (preserved by Series)
                                original_indices = col_group.indices
                                groups.append(DuplicateGroup(
                                    group_id=len(groups) + 1,
                                    indices=original_indices,
                                    values=[self.df.loc[idx].to_dict() for idx in original_indices],
                                    match_type='combined',
                                    similarity_score=col_group.similarity_score,
                                    key_columns=exact_columns + [fuzzy_col],
                                    representative_value=str(self.df.loc[original_indices[0], fuzzy_col])
                                ))
        
        # Also find pure fuzzy duplicates if no exact columns specified
        if not exact_columns and fuzzy_columns:
            return self.find_fuzzy_duplicates(fuzzy_columns, threshold, algorithm)
        
        self.combined_duplicates = groups
        return groups

    def find_predictive_duplicates(
        self,
        columns: List[str],
        threshold: float = 0.5
    ) -> List[DuplicateGroup]:
        """Find duplicates using ML-based predictive matching"""
        from .predictive_matching import PredictiveMatcher
        matcher = PredictiveMatcher(columns=columns, threshold=threshold)
        self.predictive_duplicates = matcher.find_predictive_groups(self.df)
        return self.predictive_duplicates
