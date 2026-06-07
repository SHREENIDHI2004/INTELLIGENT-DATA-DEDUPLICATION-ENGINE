import pandas as pd
import numpy as np
import re
import unicodedata
from typing import List, Dict, Optional
from rapidfuzz import fuzz
from metaphone import doublemetaphone
from .dedup_models import DuplicateGroup

class PredictiveMatcher:
    """
    Enhanced ML-based duplicate prediction engine.
    Uses a multi-metric similarity ensemble and intelligent weighting.
    """
    def __init__(self, columns: List[str], threshold: float = 0.5):
        self.columns = columns
        self.threshold = threshold
        
        # Identify key columns for weighted priority
        self.item_col = self._find_col(['Item_Name', 'ItemName', 'Name', 'Product', 'Material'])
        self.loc_col = self._find_col(['Location', 'Site', 'Warehouse', 'Plant'])
        self.cat_col = self._find_col(['Category', 'Group', 'Type'])

        # Build initial weights
        self.weights = {col: 1.0 for col in columns}
        if self.item_col: self.weights[self.item_col] = 3.0
        if self.loc_col: self.weights[self.loc_col] = 1.5
        if self.cat_col: self.weights[self.cat_col] = 1.2
            
        # Normalize weights so they sum to 1.0
        total = sum(self.weights.values())
        self.weights = {k: v/total for k, v in self.weights.items()}

    def _find_col(self, aliases: List[str]) -> Optional[str]:
        norm_aliases = [a.lower().replace("_", "").replace(" ", "") for a in aliases]
        for col in self.columns:
            ncol = col.lower().replace("_", "").replace(" ", "")
            if ncol in norm_aliases:
                return col
        return None

    def _normalize(self, text: str) -> str:
        if not text or pd.isna(text): return ""
        text = str(text).lower().strip()
        text = re.sub(r'[^\w\s]', '', text)
        text = ' '.join(text.split())
        return ''.join(c for c in unicodedata.normalize('NFD', text) if unicodedata.category(c) != 'Mn')

    def _get_phonetic_similarity(self, s1: str, s2: str) -> float:
        if not s1 or not s2: return 0.0
        m1 = doublemetaphone(s1)
        m2 = doublemetaphone(s2)
        if m1[0] == m2[0] and m1[0]: return 1.0
        if (m1[0] == m2[1] or m1[1] == m2[0]) and (m1[0] or m1[1]): return 0.8
        return 0.0

    def _calculate_ensemble_score(self, s1: str, s2: str, is_name_col: bool = False) -> float:
        if not s1 or not s2: return 0.0
        
        # RapidFuzz provides a suite of high-performance similarity metrics
        # ratio: sensitive to typos and character changes
        # token_sort_ratio: insensitive to word order changes
        # token_set_ratio: insensitive to word order and repeated words
        scores = [
            fuzz.ratio(s1, s2) / 100.0,
            fuzz.token_sort_ratio(s1, s2) / 100.0,
            fuzz.token_set_ratio(s1, s2) / 100.0
        ]
        
        # For name/category columns, add phonetic similarity
        if is_name_col:
            scores.append(self._get_phonetic_similarity(s1, s2))
            
        return np.mean(scores)

    def predict_is_duplicate(self, row1: pd.Series, row2: pd.Series) -> (bool, float):
        """
        Predicts if two rows are duplicates using a weighted ensemble score.
        Handles missing data by dynamically re-weighting available features.
        """
        total_score = 0.0
        total_weight_used = 0.0
        
        for col in self.columns:
            v1 = self._normalize(row1.get(col, ''))
            v2 = self._normalize(row2.get(col, ''))
            
            w = self.weights.get(col, 0)
            
            # If both are missing, we don't penalize but we don't add to the score
            if not v1 and not v2:
                continue
                
            # If one is missing, we penalize slightly by including weight but score 0
            if not v1 or not v2:
                total_weight_used += w
                continue
            
            is_important = (col == self.item_col or col == self.cat_col)
            score = self._calculate_ensemble_score(v1, v2, is_name_col=is_important)
            
            total_score += score * w
            total_weight_used += w
            
        if total_weight_used == 0: return False, 0.0
        
        final_score = total_score / total_weight_used
        return final_score >= self.threshold, final_score

    def find_predictive_groups(self, df: pd.DataFrame) -> List[DuplicateGroup]:
        """
        Finds duplicate groups using predictive matching with an optimized blocking strategy.
        """
        if df.empty: return []

        # Multi-level blocking for better recall and precision
        # Use first 3 chars of name or first col as a primary block
        block_col = self.item_col if self.item_col in df.columns else df.columns[0]
        df['_block_key'] = df[block_col].astype(str).str[:3].str.upper().str.replace(' ', '')
        
        groups = []
        processed_indices = set()
        group_id = 1

        for _, block_df in df.groupby('_block_key'):
            indices = block_df.index.tolist()
            for i, idx1 in enumerate(indices):
                if idx1 in processed_indices: continue
                
                current_group = [idx1]
                match_scores = []
                
                for idx2 in indices[i+1:]:
                    if idx2 in processed_indices: continue
                    
                    is_dup, confidence = self.predict_is_duplicate(df.loc[idx1], df.loc[idx2])
                    if is_dup:
                        current_group.append(idx2)
                        match_scores.append(confidence)
                        processed_indices.add(idx2)
                
                if len(current_group) > 1:
                    processed_indices.add(idx1)
                    avg_score = np.mean(match_scores) * 100 if match_scores else 100.0
                    groups.append(DuplicateGroup(
                        group_id=group_id,
                        indices=current_group,
                        values=[df.loc[idx].to_dict() for idx in current_group],
                        match_type='predictive',
                        similarity_score=avg_score,
                        key_columns=self.columns,
                        representative_value=str(df.loc[idx1, block_col])
                    ))
                    group_id += 1
        
        if '_block_key' in df.columns:
            df.drop(columns=['_block_key'], inplace=True)
            
        return groups
