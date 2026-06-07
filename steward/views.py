import io
import json
import logging
import os
import uuid
import pandas as pd
from django.conf import settings
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.http import HttpResponse
from django.contrib import messages
from django.db.models import Q, IntegerField
from django.db.models.functions import Cast

from .models import Record
from .data_utils import find_exact_duplicates
from .profiler_engine import DataProfilerEngine
from .fuzzy_matching import FuzzyMatcher

logger = logging.getLogger(__name__)

# ----- Session keys for Load Data / Find Duplicates (in-memory workflow) -----
SESSION_DF_PATH = 'steward_df_path'
SESSION_FILENAME = 'steward_filename'
SESSION_EXACT_DUPS = 'steward_exact_duplicates'
SESSION_FUZZY_DUPS = 'steward_fuzzy_duplicates'
SESSION_COMBINED_DUPS = 'steward_combined_duplicates'
SESSION_AI_DUPS = 'steward_ai_duplicates'
SESSION_PREDICTIVE_DUPS = 'steward_predictive_duplicates'
SESSION_EXACT_COLUMNS = 'steward_exact_columns'


def _get_df_path(request):
    return request.session.get(SESSION_DF_PATH)


def _set_df_session(request, df_path, filename):
    request.session[SESSION_DF_PATH] = df_path
    request.session[SESSION_FILENAME] = filename
    request.session[SESSION_EXACT_DUPS] = None
    request.session[SESSION_FUZZY_DUPS] = None
    request.session[SESSION_COMBINED_DUPS] = None
    request.session[SESSION_AI_DUPS] = None
    request.session[SESSION_PREDICTIVE_DUPS] = None


def _load_df(request):
    path = _get_df_path(request)
    if not path or not os.path.isfile(path):
        return None
    try:
        return pd.read_pickle(path)
    except Exception:
        return None


def _save_df(request, df):
    path = _get_df_path(request)
    if not path:
        path = os.path.join(settings.STEWARD_UPLOAD_TEMP, f"{uuid.uuid4().hex}.pkl")
        request.session[SESSION_DF_PATH] = path
    df.to_pickle(path)
    return path


# ----- Dashboard (Data Profiler Pro style) -----
def index(request):
    stats = {
        'total_records': Record.objects.filter(is_active=True).count(),
        'pending_duplicates': Record.objects.filter(is_active=True).exclude(
            fuzzy_deduplication_candidates__isnull=True
        ).exclude(fuzzy_deduplication_candidates='').count(),
        'merged_records': Record.objects.filter(is_merged=True).count(),
    }
    df = _load_df(request)
    stats['loaded_rows'] = len(df) if df is not None else 0
    stats['loaded_filename'] = request.session.get(SESSION_FILENAME) or ''
    return render(request, 'steward/index.html', {'stats': stats})


# ----- Load Data (Data Profiler Pro style: upload CSV/Excel for Find Duplicates + optional DB save) -----
def load_data(request):
    # Handle "Remove current file" action
    if request.method == 'POST' and request.POST.get('action') == 'remove_current':
        path = _get_df_path(request)
        try:
            if path and os.path.isfile(path):
                os.remove(path)
        except Exception:
            pass
        # Clear session-related keys
        request.session[SESSION_DF_PATH] = None
        request.session[SESSION_FILENAME] = ''
        request.session[SESSION_EXACT_DUPS] = None
        request.session[SESSION_FUZZY_DUPS] = None
        request.session[SESSION_COMBINED_DUPS] = None
        request.session[SESSION_AI_DUPS] = None
        request.session[SESSION_PREDICTIVE_DUPS] = None
        request.session[SESSION_EXACT_COLUMNS] = None
        messages.success(request, "Removed the currently loaded dataset.")
        return redirect('load_data')

    # Handle file upload
    if request.method == 'POST' and request.FILES.get('file'):
        f = request.FILES['file']
        name = f.name.lower()
        save_to_db = request.POST.get('save_to_database') == 'on'
        clear_existing = request.POST.get('clear_existing') == 'on'
        
        try:
            if name.endswith('.csv') or name.endswith('.txt') or name.endswith('.tsv'):
                df = pd.read_csv(io.BytesIO(f.read()), sep=',' if name.endswith('.csv') else '\t')
            elif name.endswith(('.xlsx', '.xls')):
                df = pd.read_excel(io.BytesIO(f.read()))
            else:
                messages.error(request, "Unsupported format. Use CSV or Excel.")
                return redirect('load_data')
        except Exception as e:
            messages.error(request, f"Error loading file: {e}")
            return redirect('load_data')

        # Save to database if requested
        if save_to_db:
            if clear_existing:
                Record.objects.all().delete()
            
            # Normalize column names (trim)
            df.columns = df.columns.str.strip()
            records_to_create = []
            
            for _, row in df.iterrows():
                # Helpers to find a column by alias or close match
                def _norm(s: str) -> str:
                    return ''.join(ch for ch in str(s).strip().lower() if ch.isalnum())

                def get_val_row_any(aliases):
                    norm_aliases = {_norm(a) for a in aliases if a}
                    # 1) Exact normalized match
                    for col in df.columns:
                        if _norm(col) in norm_aliases:
                            v = row.get(col)
                            return None if pd.isna(v) else str(v).strip() if v else None
                    # 2) Partial contains match
                    for col in df.columns:
                        ncol = _norm(col)
                        if any(a in ncol or ncol in a for a in norm_aliases):
                            v = row.get(col)
                            return None if pd.isna(v) else str(v).strip() if v else None
                    return None

                def get_val_row(c):
                    return get_val_row_any([c])

                uid_val = get_val_row_any(['UID', 'UniqueID', 'Unique_Id', 'Id', 'RecordID', 'Record_Id'])
                if not uid_val:
                    continue
                
                rec = Record(
                    uid=uid_val,
                    item_name=get_val_row_any(['Item_Name','Item Name','Name','Item','ItemDescription','Item_Description','Description','Product_Name','Part_Name','Material_Name']),
                    location=get_val_row_any(['Location','Location_Name','Site','Warehouse','Plant','Store']),
                    category=get_val_row_any(['Category','Item_Category','Item Category','Group','Class','Type']),
                    unit=get_val_row_any(['Unit','UOM','UoM','Unit_Name','Unit Name']),
                    hsn_code=get_val_row_any(['HSN_Code','HSN Code','HSN','HSNCode']),
                    source_file=get_val_row_any(['Source_File','Source File','Source']),
                    xref=get_val_row_any(['XREF','CrossRef','Cross_Ref','X-Ref','Alternate','Alt']),
                    fuzzy_deduplication_candidates=get_val_row_any(['Fuzzy_Deduplication_Candidates','Fuzzy Deduplication Candidates','Candidates','Duplicate_Candidates']),
                    is_active=True
                )
                records_to_create.append(rec)

            if records_to_create:
                before_count = Record.objects.filter(is_active=True).count()
                Record.objects.bulk_create(records_to_create, ignore_conflicts=True)
                after_count = Record.objects.filter(is_active=True).count()
                created_count = after_count - before_count
                messages.success(request, f"Loaded {len(df):,} rows. Saved {created_count} records to database. Total: {after_count}")
            else:
                messages.warning(request, "No valid records found (missing UID column).")

        # Always save to session for Find Duplicates
        path = os.path.join(settings.STEWARD_UPLOAD_TEMP, f"{uuid.uuid4().hex}.pkl")
        df.to_pickle(path)
        _set_df_session(request, path, f.name)
        
        if not save_to_db:
            messages.success(request, f"Loaded {len(df):,} rows from {f.name} for Find Duplicates.")
        
        return redirect('find_duplicates')

    df = _load_df(request)
    context = {
        'has_data': df is not None,
        'rows': len(df) if df is not None else 0,
        'columns': list(df.columns) if df is not None else [],
        'filename': request.session.get(SESSION_FILENAME) or '',
        'preview': df.head(10).to_html(classes='table') if df is not None else None,
    }
    return render(request, 'steward/load_data.html', context)


# ----- Find Duplicates: Exact + Fuzzy (same logic as data_profiler_pro) -----
def find_duplicates(request):
    df = _load_df(request)
    if df is None:
        messages.info(request, "Please load data first in Load Data.")
        return redirect('load_data')

    # POST: actions (find exact, find fuzzy, remove exact, merge fuzzy group)
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'find_exact':
            cols = request.POST.getlist('exact_columns')
            subset = cols if cols else None
            request.session[SESSION_EXACT_COLUMNS] = cols
            engine = DataProfilerEngine(df)
            exact = engine.find_exact_duplicates(subset=subset)
            request.session[SESSION_EXACT_DUPS] = [
                {'group_id': g.group_id, 'indices': g.indices, 'key_columns': g.key_columns}
                for g in exact
            ]
            messages.success(request, f"Found {len(exact)} exact duplicate groups.")
            return redirect('/find-duplicates/?tab=exact')

        if action == 'find_fuzzy':
            cols = request.POST.getlist('fuzzy_columns')
            if not cols:
                messages.warning(request, "Select at least one text column.")
                return redirect('/find-duplicates/?tab=fuzzy')
            threshold = float(request.POST.get('threshold', 85))
            algorithm = request.POST.get('algorithm', 'rapidfuzz')
            engine = DataProfilerEngine(df)
            fuzzy = engine.find_fuzzy_duplicates(columns=cols, threshold=threshold, algorithm=algorithm)
            request.session[SESSION_FUZZY_DUPS] = [
                {'group_id': g.group_id, 'indices': g.indices, 'similarity_score': g.similarity_score}
                for g in fuzzy
            ]
            messages.success(request, f"Found {len(fuzzy)} fuzzy duplicate groups.")
            return redirect('/find-duplicates/?tab=fuzzy')

        if action == 'remove_exact':
            cols = request.POST.getlist('exact_columns') or request.session.get(SESSION_EXACT_COLUMNS)
            subset = cols if cols else None
            df = df.drop_duplicates(subset=subset, keep='first')
            _save_df(request, df)
            request.session[SESSION_EXACT_DUPS] = None
            messages.success(request, "Exact duplicates removed.")
            return redirect('/find-duplicates/?tab=exact')

        if action == 'find_combined':
            exact_cols = request.POST.getlist('combined_exact_columns')
            fuzzy_cols = request.POST.getlist('combined_fuzzy_columns')
            if not exact_cols and not fuzzy_cols:
                messages.warning(request, "Select at least one column type.")
                return redirect('/find-duplicates/?tab=combined')
            threshold = float(request.POST.get('combined_threshold', 85))
            algorithm = request.POST.get('combined_algorithm', 'rapidfuzz')
            engine = DataProfilerEngine(df)
            combined = engine.find_combined_duplicates(
                exact_columns=exact_cols,
                fuzzy_columns=fuzzy_cols,
                threshold=threshold,
                algorithm=algorithm
            )
            request.session[SESSION_COMBINED_DUPS] = [
                {'group_id': g.group_id, 'indices': g.indices, 'similarity_score': g.similarity_score, 'key_columns': g.key_columns}
                for g in combined
            ]
            messages.success(request, f"Found {len(combined)} combined duplicate groups.")
            return redirect('/find-duplicates/?tab=combined')

        if action == 'merge_fuzzy':
            indices_str = request.POST.get('merge_fuzzy_indices')
            if indices_str:
                indices = [int(x) for x in indices_str.split(',') if x.strip()]
                drop_indices = indices[1:]
                df = df.drop(drop_indices)
                _save_df(request, df)
                request.session[SESSION_FUZZY_DUPS] = None  # clear so user can re-run Find Fuzzy
                messages.success(request, "Fuzzy group merged (kept first).")
            return redirect('/find-duplicates/?tab=fuzzy')

        if action == 'merge_combined':
            indices_str = request.POST.get('merge_combined_indices')
            if indices_str:
                indices = [int(x) for x in indices_str.split(',') if x.strip()]
                drop_indices = indices[1:]
                df = df.drop(drop_indices)
                _save_df(request, df)
                request.session[SESSION_COMBINED_DUPS] = None  # clear so user can re-run Find Combined
                messages.success(request, "Combined group merged (kept first).")
            return redirect('/find-duplicates/?tab=combined')

        if action == 'find_ai':
            engine = DataProfilerEngine(df)
            
            # Helper to find columns by alias (case-insensitive)
            def find_cols(aliases):
                found = []
                norm_aliases = [a.lower().replace("_", "").replace(" ", "") for a in aliases]
                for col in df.columns:
                    ncol = col.lower().replace("_", "").replace(" ", "")
                    if ncol in norm_aliases:
                        found.append(col)
                return found

            # Smart multi-column discovery
            discovery_cols = find_cols(['Item_Name', 'ItemName', 'Name', 'Product', 'Material', 'Location', 'Site', 'Warehouse', 'Plant', 'Category', 'Group'])
            if not discovery_cols:
                discovery_cols = [df.columns[0]] # Fallback

            ai_groups = engine.find_combined_duplicates(
                exact_columns=[], # Discovery mode
                fuzzy_columns=discovery_cols,
                threshold=75.0, # Lower threshold for discovery
                algorithm='combined'
            )
            request.session[SESSION_AI_DUPS] = [
                {'group_id': g.group_id, 'indices': g.indices, 'similarity_score': g.similarity_score, 'key_columns': g.key_columns}
                for g in ai_groups
            ]
            messages.success(request, f"AI Discovery found {len(ai_groups)} potential duplicate groups using {', '.join(discovery_cols)}.")
            return redirect('/find-duplicates/?tab=ai')

        if action == 'find_predictive':
            cols = request.POST.getlist('predictive_columns')
            if not cols:
                messages.warning(request, "Select columns for prediction.")
                return redirect('/find-duplicates/?tab=predictive')
            threshold = float(request.POST.get('predictive_threshold', 0.5))
            engine = DataProfilerEngine(df)
            predictive_groups = engine.find_predictive_duplicates(columns=cols, threshold=threshold)
            request.session[SESSION_PREDICTIVE_DUPS] = [
                {'group_id': g.group_id, 'indices': g.indices, 'similarity_score': g.similarity_score}
                for g in predictive_groups
            ]
            messages.success(request, f"Predictive model found {len(predictive_groups)} potential duplicate groups.")
            return redirect('/find-duplicates/?tab=predictive')

        if action == 'merge_predictive':
            indices_str = request.POST.get('merge_predictive_indices')
            if indices_str:
                indices = [int(x) for x in indices_str.split(',') if x.strip()]
                drop_indices = indices[1:]
                df = df.drop(drop_indices)
                _save_df(request, df)
                request.session[SESSION_PREDICTIVE_DUPS] = None
                messages.success(request, "Predictive group merged.")
            return redirect('/find-duplicates/?tab=predictive')

        if action == 'merge_ai':
            indices_str = request.POST.get('merge_ai_indices')
            if indices_str:
                indices = [int(x) for x in indices_str.split(',') if x.strip()]
                drop_indices = indices[1:]
                df = df.drop(drop_indices)
                _save_df(request, df)
                request.session[SESSION_AI_DUPS] = None
                messages.success(request, "AI-detected group merged.")
            return redirect('/find-duplicates/?tab=ai')

        if action == 'sync_to_db':
            tab = request.POST.get('tab', 'fuzzy')
            session_keys = {
                'exact': SESSION_EXACT_DUPS,
                'fuzzy': SESSION_FUZZY_DUPS,
                'combined': SESSION_COMBINED_DUPS,
                'ai': SESSION_AI_DUPS,
                'predictive': SESSION_PREDICTIVE_DUPS
            }
            groups = request.session.get(session_keys.get(tab)) or []
            
            if not groups:
                messages.warning(request, f"No findings found in {tab} tab to sync.")
                return redirect(f'/find-duplicates/?tab={tab}')
            
            updated_count = 0
            for g in groups:
                indices = g.get('indices', [])
                if len(indices) < 2:
                    continue
                
                # Get UIDs from DataFrame
                try:
                    uids = [str(df.loc[idx].get('UID') or df.loc[idx].get('uid')) for idx in indices]
                    uids = [u for u in uids if u and u != 'nan']
                    if len(uids) < 2:
                        continue
                    
                    main_uid = uids[0]
                    other_uids = uids[1:]
                    
                    # Update the main record in DB
                    Record.objects.filter(uid=main_uid).update(
                        fuzzy_deduplication_candidates=",".join(other_uids),
                        is_reviewed=False
                    )
                    updated_count += 1
                except Exception as e:
                    logger.error(f"Error syncing group {g.get('group_id')}: {e}")
                    continue
            
            messages.success(request, f"Successfully pushed {updated_count} duplicate groups from {tab} Match to the Records List for review.")
            return redirect('record_list')

    active_tab = request.GET.get('tab', 'exact')

    engine = DataProfilerEngine(df)
    exact_groups = request.session.get(SESSION_EXACT_DUPS) or []
    fuzzy_groups = request.session.get(SESSION_FUZZY_DUPS) or []
    combined_groups = request.session.get(SESSION_COMBINED_DUPS) or []
    ai_groups = request.session.get(SESSION_AI_DUPS) or []
    predictive_groups = request.session.get(SESSION_PREDICTIVE_DUPS) or []

    # Build group previews for template
    exact_previews = []
    for g in exact_groups[:50]:
        group_df = df.loc[g['indices']]
        exact_previews.append({'group_id': g['group_id'], 'indices': g['indices'], 'preview_html': group_df.to_html(classes='table table-sm')})

    fuzzy_previews = []
    for g in fuzzy_groups:
        group_df = df.loc[g['indices']]
        fuzzy_previews.append({
            'group_id': g['group_id'],
            'indices': g['indices'],
            'similarity_score': g.get('similarity_score'),
            'preview_html': group_df.to_html(classes='table table-sm')
        })

    combined_previews = []
    for g in combined_groups:
        group_df = df.loc[g['indices']]
        combined_previews.append({
            'group_id': g['group_id'],
            'indices': g['indices'],
            'similarity_score': g.get('similarity_score'),
            'key_columns': g.get('key_columns', []),
            'preview_html': group_df.to_html(classes='table table-sm')
        })

    ai_previews = []
    for g in ai_groups:
        group_df = df.loc[g['indices']]
        ai_previews.append({
            'group_id': g['group_id'],
            'indices': g['indices'],
            'similarity_score': g.get('similarity_score'),
            'preview_html': group_df.to_html(classes='table table-sm')
        })

    predictive_previews = []
    for g in predictive_groups:
        group_df = df.loc[g['indices']]
        predictive_previews.append({
            'group_id': g['group_id'],
            'indices': g['indices'],
            'similarity_score': g.get('similarity_score'),
            'preview_html': group_df.to_html(classes='table table-sm')
        })

    columns_list = list(df.columns)
    
    # Exact Match selection
    exact_sel = request.session.get(SESSION_EXACT_COLUMNS)
    if exact_sel is None:
        exact_sel = columns_list if len(columns_list) < 10 else []
    exact_column_objects = [{'name': c, 'checked': c in exact_sel} for c in columns_list]

    # All columns for fuzzy match
    string_column_objects = [{'name': c, 'checked': False} for c in columns_list]
    combined_column_objects = [{'name': c, 'checked': False} for c in columns_list]
    predictive_column_objects = [{'name': c, 'checked': False} for c in columns_list]

    context = {
        'rows': len(df),
        'columns': columns_list,
        'exact_column_objects': exact_column_objects,
        'string_column_objects': string_column_objects,
        'combined_column_objects': combined_column_objects,
        'predictive_column_objects': predictive_column_objects,
        'exact_groups': exact_previews,
        'fuzzy_groups': fuzzy_previews,
        'combined_groups': combined_previews,
        'ai_groups': ai_previews,
        'predictive_groups': predictive_previews,
        'exact_columns_used': request.session.get(SESSION_EXACT_COLUMNS) or [],
        'active_tab': active_tab,
    }
    return render(request, 'steward/find_duplicates.html', context)


# ----- upload_file removed - functionality merged into load_data -----


def record_list(request):
    records = Record.objects.filter(is_active=True)
    filter_type = request.GET.get('filter', 'all')
    sort_type = request.GET.get('sort', 'uid_asc')
    
    if filter_type == 'pending':
        records = records.filter(is_reviewed=False).exclude(
            Q(fuzzy_deduplication_candidates__isnull=True) | Q(fuzzy_deduplication_candidates='')
        )
    elif filter_type == 'clean':
        records = records.filter(is_reviewed=False, is_merged=False).filter(
            Q(fuzzy_deduplication_candidates__isnull=True) | Q(fuzzy_deduplication_candidates='')
        )
    elif filter_type == 'reviewed':
        records = records.filter(is_reviewed=True, is_merged=False)
    elif filter_type == 'merged':
        records = records.filter(is_merged=True)

    q = request.GET.get('q')
    if q:
        records = records.filter(
            Q(uid__icontains=q) | Q(item_name__icontains=q) |
            Q(location__icontains=q) | Q(category__icontains=q)
        )

    # Sorting logic - Default to uid_asc now
    if sort_type == 'uid_desc':
        records = records.annotate(
            uid_int=Cast('uid', output_field=IntegerField())
        ).order_by('-uid_int')
    elif sort_type == 'id_desc':
        records = records.order_by('-id')
    else:
        # Default case: uid_asc
        records = records.annotate(
            uid_int=Cast('uid', output_field=IntegerField())
        ).order_by('uid_int')

    survivor_uids = set()
    for r in Record.objects.filter(is_active=False, merged_info__startswith="Merged into "):
        try:
            target_uid = r.merged_info.replace("Merged into ", "").strip()
            if target_uid:
                survivor_uids.add(target_uid)
        except Exception:
            pass

    # Check if user has data loaded for Find Duplicates but not in DB
    df = _load_df(request)
    has_loaded_data = df is not None
    loaded_filename = request.session.get(SESSION_FILENAME) or ''

    # Build dataset columns and values mapped by UID for dynamic Record List rendering
    dataset_columns = []
    dataset_by_uid = {}
    dataset_preview_html = None
    uid_col = None
    if df is not None:
        dataset_columns = list(df.columns)
        try:
            dataset_preview_html = df.to_html(classes='table table-sm', index=False)
        except Exception:
            dataset_preview_html = None
        def _norm(s: str) -> str:
            return ''.join(ch for ch in str(s).strip().lower() if ch.isalnum())
        uid_aliases = {_norm(x) for x in ['UID','UniqueID','Unique_Id','Id','RecordID','Record_Id']}
        uid_col = None
        for col in df.columns:
            if _norm(col) in uid_aliases:
                uid_col = col
                break
        if uid_col is None and len(df.columns):
            uid_col = df.columns[0]
        try:
            for _, row in df.iterrows():
                uid_val = row.get(uid_col) if uid_col in df.columns else None
                if uid_val is None or (isinstance(uid_val, float) and pd.isna(uid_val)):
                    continue
                uid_key = str(uid_val)
                dataset_by_uid[uid_key] = {c: (None if pd.isna(row.get(c)) else str(row.get(c))) for c in dataset_columns}
            # Limit to first 5 columns for display
            dataset_columns = dataset_columns[:5]
        except Exception:
            dataset_columns = []
            dataset_by_uid = {}

    # Evaluate queryset and prepare dynamic cells per record based on dataset columns
    records_list = list(records)

    if not dataset_columns:
        dataset_columns = ['UID', 'Item_Name', 'Location', 'Category']

    # Build alias -> attribute mapping for fallback values from DB
    def _norm(s: str) -> str:
        return ''.join(ch for ch in str(s).strip().lower() if ch.isalnum())
    alias_to_attr = {
        'uid': 'uid', 'uniqueid': 'uid', 'uniqueid': 'uid', 'recordid': 'uid', 'id': 'uid',
        'itemname': 'item_name', 'item': 'item_name', 'name': 'item_name', 'itemdescription': 'item_name', 'description': 'item_name', 'productname': 'item_name', 'partname': 'item_name', 'materialname': 'item_name',
        'location': 'location', 'locationname': 'location', 'site': 'location', 'warehouse': 'location', 'plant': 'location', 'store': 'location',
        'category': 'category', 'itemcategory': 'category', 'group': 'category', 'class': 'category', 'type': 'category',
        'unit': 'unit', 'uom': 'unit', 'unitname': 'unit',
        'hsncode': 'hsn_code', 'hsn': 'hsn_code',
        'sourcefile': 'source_file', 'source': 'source_file',
        'xref': 'xref', 'crossref': 'xref', 'crossref': 'xref', 'alt': 'xref', 'alternate': 'xref',
    }

    for rec in records_list:
        cells = []
        row_map = dataset_by_uid.get(str(rec.uid), {})
        for col in dataset_columns:
            val = row_map.get(col)
            if val is None or str(val).lower() == 'none':
                ncol = _norm(col)
                attr = alias_to_attr.get(ncol)
                if attr:
                    val = getattr(rec, attr, None)
            cells.append(val)
        setattr(rec, 'dataset_cells', cells)
        c_str = rec.fuzzy_deduplication_candidates or ''
        c_list = [c.strip() for c in str(c_str).split(',') if c.strip()]
        count = len(c_list)
        setattr(rec, 'candidates_count', count)

        display_text = f"{count} candidates"
        if count > 0:
            preview = ", ".join(c_list[:3])
            display_text += f" — {preview}"
            overflow = count - 3
            if overflow > 0:
                display_text += f" + {overflow} more"

        setattr(rec, 'candidates_display_text', display_text)
        setattr(rec, 'is_reviewable', count > 0 and not rec.is_reviewed)
        setattr(rec, 'has_candidates', count > 0)

    recordlist_colspan = len(dataset_columns) + 2  # + Candidates + Action
    return render(request, 'steward/list.html', {
        'records': records_list,
        'survivor_uids': survivor_uids,
        'total_count': len(records_list),
        'has_loaded_data': has_loaded_data,
        'loaded_filename': loaded_filename,
        'dataset_columns': dataset_columns,
        'recordlist_colspan': recordlist_colspan,
        'sort_type': sort_type,
        'uid_col': uid_col
    })


def review_merge(request, uid):
    main_record = get_object_or_404(Record, uid=uid)
    candidates_str = main_record.fuzzy_deduplication_candidates
    candidates = []
    if candidates_str:
        candidate_ids = [c.strip() for c in str(candidates_str).split(',') if c.strip()]
        candidates = list(Record.objects.filter(uid__in=candidate_ids, is_active=True))

    if request.method == 'POST':
        selected_merge_uids = request.POST.getlist('merge_candidate_uids')
        candidates_to_merge = [c for c in candidates if c.uid in selected_merge_uids]
        candidates_to_unlink = [c for c in candidates if c.uid not in selected_merge_uids]
        action = request.POST.get('action')

        if action == 'mark_reviewed':
            main_record.is_reviewed = True
            main_record.save()
            messages.success(request, f"Record {main_record.uid} marked as reviewed.")
            return redirect(f"{reverse('record_list')}?filter=pending")

        if action == 'merge_separately' and candidates_to_merge:
            new_main = candidates_to_merge[0]
            new_candidates = candidates_to_merge[1:]
            current_ids = [c.strip() for c in str(main_record.fuzzy_deduplication_candidates or '').split(',') if c.strip()]
            selected_ids = [c.uid for c in candidates_to_merge]
            remaining_ids = [cid for cid in current_ids if cid not in selected_ids]
            main_record.fuzzy_deduplication_candidates = ",".join(remaining_ids) if remaining_ids else None
            main_record.save()
            if new_candidates:
                new_main.fuzzy_deduplication_candidates = ",".join([c.uid for c in new_candidates])
                new_main.save()
            messages.success(request, f"Separated group {new_main.uid} created. Return to pending list.")
            return redirect(f"{reverse('record_list')}?filter=pending")

        # Confirm merge
        mergeable_fields = [f.name for f in Record._meta.fields if f.name not in [
            'id', 'uid', 'is_active', 'merged_info', 'fuzzy_deduplication_candidates', 'is_reviewed', 'ai_suggested_values'
        ]]
        for field in mergeable_fields:
            val = request.POST.get(f'win_{field}')
            if val is not None:
                setattr(main_record, field, val)
        for cand in candidates_to_merge:
            cand.is_active = False
            cand.merged_info = f"Merged into {main_record.uid}"
            cand.save()
        main_record.fuzzy_deduplication_candidates = None
        main_record.is_reviewed = True
        main_record.is_merged = True
        main_record.save()
        messages.success(request, f"Merged {len(candidates_to_merge)} records.")
        return redirect(f"{reverse('record_list')}?filter=pending")

    model_fields = [f for f in Record._meta.fields if f.name not in ['id', 'is_active', 'merged_info']]
    comparison_data = []
    for f in model_fields:
        main_val = getattr(main_record, f.name)
        candidate_data = []
        for c in candidates:
            c_val = getattr(c, f.name)
            candidate_data.append({
                'value': c_val,
                'is_match': str(c_val).strip() == str(main_val).strip() if c_val and main_val else c_val == main_val
            })
        comparison_data.append({
            'label': f.verbose_name,
            'name': f.name,
            'main_val': main_val,
            'candidate_vals': candidate_data
        })

    ai_recommendation = None
    if request.GET.get('ask_ai') and candidates:
        ai_data = get_ai_recommendation(main_record, candidates)
        main_record.ai_recommendation = ai_data.get('recommendation')
        main_record.ai_reason = ai_data.get('reason')
        main_record.ai_confidence = str(ai_data.get('confidence', ''))
        main_record.ai_suggested_values = json.dumps(ai_data.get('suggested_values', {}))
        main_record.save()
        main_record.refresh_from_db()
        ai_recommendation = ai_data
    elif main_record.ai_recommendation:
        ai_recommendation = {
            'recommendation': main_record.ai_recommendation,
            'reason': main_record.ai_reason,
            'confidence': main_record.ai_confidence
        }

    merged_records = Record.objects.filter(merged_info__contains=f"Merged into {main_record.uid}")
    merged_count = merged_records.count()
    return render(request, 'steward/review.html', {
        'main_record': main_record,
        'candidates': candidates,
        'comparison_data': comparison_data,
        'merged_count': merged_count,
        'merged_records': merged_records,
        'ai_recommendation': ai_recommendation
    })


from django.http import JsonResponse

def ai_agent_recommend(request, uid):
    """
    AJAX endpoint for AI agent recommendations.
    """
    try:
        main_record = get_object_or_404(Record, uid=uid)
        candidate_uids = [c.strip() for c in str(main_record.fuzzy_deduplication_candidates or '').split(',') if c.strip()]
        candidates = Record.objects.filter(uid__in=candidate_uids)
        
        if not candidates:
            return JsonResponse({'success': False, 'error': 'No candidates found for analysis.'})
            
        ai_data = get_ai_recommendation(main_record, candidates)
        
        # Save to DB for persistence
        main_record.ai_recommendation = ai_data.get('recommendation')
        main_record.ai_reason = ai_data.get('reason')
        main_record.ai_confidence = str(ai_data.get('confidence', ''))
        main_record.ai_suggested_values = json.dumps(ai_data.get('suggested_values', {}))
        main_record.save()
        
        return JsonResponse({
            'success': True,
            'recommendation': ai_data.get('recommendation'),
            'reason': ai_data.get('reason'),
            'confidence': ai_data.get('confidence'),
            'recommendations': ai_data.get('suggested_values', {})
        })
    except Exception as e:
        logger.exception("AJAX AI recommendation error")
        return JsonResponse({'success': False, 'error': str(e)})


def get_ai_recommendation(main_record, candidates):
    """
    Get recommendations for merging.
    Tries Azure OpenAI first, falls back to an enhanced Rule-Based Engine.
    """
    # First, get a baseline recommendation from the improved rule-based engine
    rule_rec = _get_rule_based_recommendation(main_record, candidates)

    try:
        from openai import AzureOpenAI
        api_key = getattr(settings, 'AZURE_OPENAI_API_KEY', None)
        endpoint = getattr(settings, 'AZURE_OPENAI_ENDPOINT', None)

        if not api_key or not endpoint:
            rule_rec['reason'] = "[Smart Engine] " + rule_rec['reason'] + " (Azure OpenAI not configured)"
            return rule_rec

        client = AzureOpenAI(
            api_key=api_key,
            api_version=getattr(settings, 'AZURE_OPENAI_API_VERSION', '2025-01-01-preview'),
            azure_endpoint=endpoint,
            timeout=20.0,  # Add a timeout
        )
        deployment = getattr(settings, 'AZURE_OPENAI_DEPLOYMENT', 'gpt-4o')

        # Create a more detailed and structured prompt
        main_info = {
            "uid": main_record.uid,
            "item_name": main_record.item_name,
            "location": main_record.location,
            "category": main_record.category
        }
        candidates_info = [
            {"uid": c.uid, "item_name": c.item_name, "location": c.location, "category": c.category}
            for c in candidates
        ]

        prompt = f"""
        You are a data stewardship expert. Your task is to analyze a main record and a list of candidate records to determine if they are duplicates.

        **Analysis Steps:**
        1.  Compare the `item_name`, `location`, and `category` fields across all records.
        2.  Look for strong similarities in names (e.g., typos, abbreviations, different word order).
        3.  Consider if records share the same location or category, which increases the likelihood of being a duplicate.
        4.  Based on your analysis, decide if the records should be merged.
        5.  If merging, suggest the best possible value for each field (`item_name`, `location`, `category`) from all available records. Choose the most complete and descriptive value.

        **Main Record:**
        {json.dumps(main_info, indent=2)}

        **Candidate Records:**
        {json.dumps(candidates_info, indent=2)}

        **Output Format:**
        You MUST respond with a valid JSON object only, with no other text before or after it. The JSON object must have the following structure:
        {{ "recommendation": "MERGE" or "KEEP_SEPARATE", "reason": "A brief, clear explanation for your decision.", "confidence": "high" or "medium" or "low", "suggested_values": {{ "item_name": "...", "location": "...", "category": "..." }} }}
        """

        response = client.chat.completions.create(
            model=deployment,
            messages=[
                {"role": "system", "content": "You are a data stewardship expert that only responds with JSON."},
                {"role": "user", "content": prompt}
            ],
            response_format={"type": "json_object"},  # Enforce JSON output
            temperature=0.2,
            max_tokens=500,
        )

        content = response.choices[0].message.content
        ai_data = json.loads(content)

        # Validate the response from the AI
        if not all(k in ai_data for k in ['recommendation', 'reason', 'confidence', 'suggested_values']):
            logger.warning("Azure OpenAI response was missing required keys. Falling back to rules.")
            rule_rec['reason'] = "[Smart Engine] " + rule_rec['reason'] + " (AI response invalid)"
            return rule_rec

        # Add AI source to the reason for clarity
        ai_data['reason'] = "[Azure OpenAI] " + ai_data.get('reason', 'No reason provided.')
        return ai_data

    except Exception as e:
        logger.error(f"Azure OpenAI call failed: {e}. Falling back to rule-based engine.")
        rule_rec['reason'] = "[Smart Engine] " + rule_rec['reason'] + f" (Azure OpenAI failed: {e})"
        return rule_rec


def _get_rule_based_recommendation(main_record, candidates):
    """
    An enhanced rule-based engine for fallback recommendations.
    It makes decisions based on data completeness and similarity.
    """
    from rapidfuzz import fuzz
    all_records = [main_record] + list(candidates)
    
    # Rule 1: Suggest best values based on completeness
    suggested_values = {}
    fields_to_check = ['item_name', 'location', 'category']
    for field in fields_to_check:
        best_value = getattr(main_record, field, '') or ''
        max_len = len(str(best_value))
        for rec in candidates:
            current_value = getattr(rec, field, '') or ''
            if len(str(current_value)) > max_len:
                max_len = len(str(current_value))
                best_value = current_value
        suggested_values[field] = best_value

    # Rule 2: Decide on MERGE/KEEP_SEPARATE based on name similarity
    main_name = str(suggested_values.get('item_name', '')).lower()
    similarities = []
    for rec in candidates:
        cand_name = str(getattr(rec, 'item_name', '')).lower()
        if main_name and cand_name:
            score = fuzz.token_set_ratio(main_name, cand_name)
            similarities.append(score)
    
    avg_similarity = sum(similarities) / len(similarities) if similarities else 0
    
    if avg_similarity > 70:
        recommendation = "MERGE"
        reason = f"Records have a high average name similarity of {avg_similarity:.0f}%."
        confidence = "high" if avg_similarity > 85 else "medium"
    elif avg_similarity > 50:
        recommendation = "MERGE"
        reason = f"Records have a moderate average name similarity of {avg_similarity:.0f}%. Review carefully."
        confidence = "low"
    else:
        recommendation = "KEEP_SEPARATE"
        reason = f"Records have a low average name similarity of {avg_similarity:.0f}%."
        confidence = "high"

    return {
        'recommendation': recommendation,
        'reason': reason,
        'confidence': confidence,
        'suggested_values': suggested_values
    }


def export_data(request):
    records = Record.objects.filter(is_active=True)
    survivor_uids = set()
    for r in Record.objects.filter(is_active=False, merged_info__startswith="Merged into "):
        try:
            target_uid = r.merged_info.replace("Merged into ", "").strip()
            if target_uid:
                survivor_uids.add(target_uid)
        except Exception:
            pass

    fields_map = {
        'UID': 'uid', 'Item_Name': 'item_name', 'Location': 'location', 'Category': 'category',
        'Unit': 'unit', 'HSN_Code': 'hsn_code', 'Source_File': 'source_file', 'XREF': 'xref',
        'Fuzzy_Deduplication_Candidates': 'fuzzy_deduplication_candidates',
        'AI Recommendation': 'ai_recommendation', 'AI Reason': 'ai_reason', 'AI Confidence': 'ai_confidence',
        'Candidates': 'computed_candidates_status',
    }
    data = []
    for r in records:
        row = {}
        ai_vals = json.loads(r.ai_suggested_values) if r.ai_suggested_values else {}
        for col_name, field_name in fields_map.items():
            if field_name == 'computed_candidates_status':
                if r.fuzzy_deduplication_candidates and str(r.fuzzy_deduplication_candidates).strip():
                    row[col_name] = 'pending review'
                elif r.uid in survivor_uids:
                    row[col_name] = 'merged'
                else:
                    row[col_name] = 'clean'
            else:
                row[col_name] = getattr(r, field_name)
        data.append(row)
    df = pd.DataFrame(data)
    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = 'attachment; filename=Master_List_Merged.xlsx'
    with pd.ExcelWriter(response, engine='openpyxl') as writer:
        df.to_excel(writer, index=False)
    return response


def unmerge_records(request, uid):
    if request.method == 'POST':
        main_record = get_object_or_404(Record, uid=uid)
        merged_records = Record.objects.filter(merged_info__contains=f"Merged into {uid}")
        if not merged_records.exists():
            messages.warning(request, "No merged records to restore.")
            return redirect('review_merge', uid=uid)
        restored_uids = []
        for rec in merged_records:
            rec.is_active = True
            rec.merged_info = None
            rec.save()
            restored_uids.append(rec.uid)
        current_candidates = main_record.fuzzy_deduplication_candidates or ""
        existing_cand_list = [c.strip() for c in current_candidates.split(',') if c.strip()]
        for r_uid in restored_uids:
            if r_uid not in existing_cand_list:
                existing_cand_list.append(str(r_uid))
        main_record.fuzzy_deduplication_candidates = ", ".join(existing_cand_list)
        main_record.save()
        messages.success(request, f"Unmerged {len(restored_uids)} records.")
        return redirect(f"{reverse('record_list')}?filter=pending")
    return redirect('review_merge', uid=uid)


def mark_as_reviewed(request, uid):
    if request.method == 'POST':
        rec = get_object_or_404(Record, uid=uid)
        rec.is_reviewed = True
        rec.save()
        messages.success(request, f"Record {uid} marked as reviewed/clean.")
    return redirect(f"{reverse('record_list')}?filter=pending")


def export_duplicates(request, match_type):
    df = _load_df(request)
    if df is None:
        messages.error(request, "No data loaded to export.")
        return redirect('load_data')

    session_keys = {
        'exact': SESSION_EXACT_DUPS,
        'fuzzy': SESSION_FUZZY_DUPS,
        'combined': SESSION_COMBINED_DUPS,
        'ai': SESSION_AI_DUPS,
        'predictive': SESSION_PREDICTIVE_DUPS
    }
    
    session_key = session_keys.get(match_type)
    if not session_key:
        messages.error(request, "Invalid match type for export.")
        return redirect('find_duplicates')

    groups = request.session.get(session_key) or []
    if not groups:
        messages.warning(request, f"No {match_type} duplicates to export.")
        return redirect(f'/find-duplicates/?tab={match_type}')

    # Identify the UID column more robustly
    def _norm(s): return ''.join(ch for ch in str(s).strip().lower() if ch.isalnum())
    uid_aliases = {_norm(x) for x in ['UID','UniqueID','Unique_Id','Id','RecordID','Record_Id']}
    uid_col = next((c for c in df.columns if _norm(c) in uid_aliases), None)
    if uid_col is None and len(df.columns) > 0:
        uid_col = df.columns[0]

    export_data = []
    for group in groups:
        indices = group.get('indices', [])
        if not indices:
            continue

        # First record is the main one
        main_record_index = indices[0]
        main_record = df.loc[main_record_index].to_dict()

        # Other records are candidates
        candidate_indices = indices[1:]
        candidate_uids = []
        for idx in candidate_indices:
            # Find the UID from the original DataFrame
            uid_val = df.loc[idx].get(uid_col)
            if pd.notna(uid_val):
                # Clean up the UID string (remove .0 if float)
                u_str = str(uid_val).strip()
                if u_str.endswith('.0'):
                    u_str = u_str[:-2]
                candidate_uids.append(u_str)

        main_record['Fuzzy_Deduplication_Candidates'] = ", ".join(candidate_uids)
        export_data.append(main_record)
    
    if not export_data:
        messages.warning(request, "No data to export.")
        return redirect(f'/find-duplicates/?tab={match_type}')

    export_df = pd.DataFrame(export_data)

    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename={match_type}_duplicates.csv'
    export_df.to_csv(response, index=False)
    return response
