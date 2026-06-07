"""Exact duplicate detection - same logic as data_profiler_pro deduplication_app."""
from typing import List, Optional
import pandas as pd
from .dedup_models import DuplicateGroup


def find_exact_duplicates(df: pd.DataFrame, subset: Optional[List[str]] = None) -> List[DuplicateGroup]:
    """Find exact duplicate rows using vectorized hashing."""
    try:
        if subset is None:
            subset = df.columns.tolist()

        hashes = pd.util.hash_pandas_object(df[subset], index=False)
        dup_mask = hashes.duplicated(keep=False)

        if not dup_mask.any():
            return []

        dup_hashes = hashes[dup_mask]
        hash_groups = dup_hashes.groupby(dup_hashes).groups

        groups = []
        group_id = 1

        for hash_val, indices in hash_groups.items():
            idx_list = indices.tolist()
            if len(idx_list) < 2:
                continue

            first_row_idx = idx_list[0]
            try:
                rep_val = str(df.loc[first_row_idx, subset].to_dict())
            except Exception:
                rep_val = ""

            stored_indices = idx_list
            if len(idx_list) > 100:
                stored_values = [df.loc[i, subset].to_dict() for i in idx_list[:100]]
            else:
                stored_values = [df.loc[i, subset].to_dict() for i in idx_list]

            groups.append(DuplicateGroup(
                group_id=group_id,
                indices=stored_indices,
                values=stored_values,
                match_type='exact',
                similarity_score=100.0,
                key_columns=subset,
                representative_value=rep_val
            ))
            group_id += 1

        return groups

    except Exception:
        return []
