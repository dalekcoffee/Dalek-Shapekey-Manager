# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2025 Dalek (https://dalek.coffee)

"""Debug diagnostics for Dalek's Shapekey Manager.

This module owns the developer-facing debug machinery: an independent
live walk of the shape_keys list, a side-by-side comparison of cached vs
live counts, three operators for dumping/rebuilding/verifying the cache,
and the Debug sub-panel that surfaces all of it in the UI.

Several visible counters in the main panel can disagree when something
is wrong: the category dropdown's "(N)" suffix, the panel header's
Shown, the Delete Category "(N)" button, and the row count. These all
come from different paths: dropdown+delete use member_counts/delete_counts
computed during a single walk; Shown/row count come from
get_filtered_keys which re-applies the filter via full_info lookups.
When those disagree, the cause is usually one of:
  - duplicate shape-key names (full_info is a dict keyed by name, so
    later keys overwrite earlier ones, but member_counts increments
    for every occurrence)
  - cache staleness we failed to invalidate
  - divider detection differing between the two code paths
The debug panel below shows a LIVE independent walk alongside the
cached numbers so the user can spot the discrepancy and we can
diagnose from the dump.
"""

import bpy
from bpy.types import Operator, Panel

from . import (
    _MAP_CACHE,
    _addon_version_tuple,
    _decode_category_filter,
    _get_cache,
    _get_patterns,
    _get_prefs_version,
    _rebuild_cache,
    get_filtered_keys,
)


def _compute_debug_metrics(obj):
    """Independent live walk of the shape_keys list.

    Deliberately does NOT read from _MAP_CACHE - the whole point is to
    have a reference implementation to compare the cache against.
    Returns None when there are no shape keys on obj."""
    if not obj or not obj.data or not obj.data.shape_keys:
        return None
    blocks = obj.data.shape_keys.key_blocks
    patterns = [(token, level, len(token)) for token, level in _get_patterns()]

    n_blocks = len(blocks)
    dividers_top = 0
    dividers_sub = 0
    real_keys = 0
    basis_count = 0
    name_counts = {}
    per_top_members = {}
    per_sub_members = {}
    per_top_delete = {}
    per_sub_delete = {}
    orphan_subs = []           # sub dividers encountered with no current top
    sub_to_top = {}            # {sub_label: parent_top_label} as seen live
    sub_multiple_parents = {}  # {sub_label: set(parent_tops)} when conflict
    all_top_labels = []
    all_sub_labels = []

    for kb in blocks:
        nm = kb.name
        name_counts[nm] = name_counts.get(nm, 0) + 1

    current_top = ''
    current_sub = ''
    for kb in blocks:
        name = kb.name
        nlen = len(name)
        kind = None
        label = name
        for token, lvl, tlen in patterns:
            if nlen > tlen * 2 and name.startswith(token) and name.endswith(token):
                kind = lvl
                label = name[tlen:-tlen]
                break

        if kind == 'top':
            dividers_top += 1
            current_top = label
            current_sub = ''
            all_top_labels.append(label)
            per_top_members.setdefault(label, 0)
            per_top_delete[label] = per_top_delete.get(label, 0) + 1
        elif kind == 'sub':
            dividers_sub += 1
            current_sub = label
            all_sub_labels.append(label)
            per_sub_members.setdefault(label, 0)
            per_sub_delete[label] = per_sub_delete.get(label, 0) + 1
            if current_top:
                per_top_delete[current_top] = per_top_delete.get(current_top, 0) + 1
                existing = sub_to_top.get(label)
                if existing is None:
                    sub_to_top[label] = current_top
                elif existing != current_top:
                    sub_multiple_parents.setdefault(label, {existing}).add(current_top)
            else:
                orphan_subs.append(label)
        else:
            real_keys += 1
            if name == 'Basis':
                basis_count += 1
            if current_top:
                per_top_members[current_top] = per_top_members.get(current_top, 0) + 1
                per_top_delete[current_top] = per_top_delete.get(current_top, 0) + 1
            if current_sub:
                per_sub_members[current_sub] = per_sub_members.get(current_sub, 0) + 1
                per_sub_delete[current_sub] = per_sub_delete.get(current_sub, 0) + 1

    duplicates = {nm: c for nm, c in name_counts.items() if c > 1}
    top_name_collisions = [t for t in all_top_labels if all_top_labels.count(t) > 1]
    sub_name_collisions = [s for s in all_sub_labels if all_sub_labels.count(s) > 1]
    cross_level_collisions = sorted(set(all_top_labels) & set(all_sub_labels))

    return {
        'n_blocks': n_blocks,
        'dividers_top': dividers_top,
        'dividers_sub': dividers_sub,
        'real_keys': real_keys,
        'basis_count': basis_count,
        'duplicates': duplicates,
        'per_top_members': per_top_members,
        'per_sub_members': per_sub_members,
        'per_top_delete': per_top_delete,
        'per_sub_delete': per_sub_delete,
        'orphan_subs': orphan_subs,
        'sub_to_top': sub_to_top,
        'sub_multiple_parents': {k: sorted(v) for k, v in sub_multiple_parents.items()},
        'top_name_duplicates': sorted(set(top_name_collisions)),
        'sub_name_duplicates': sorted(set(sub_name_collisions)),
        'cross_level_collisions': cross_level_collisions,
    }


def _debug_diff_rows(cache, metrics):
    """Yield per-category diagnostic rows comparing cache vs live walk.

    Each row: (label, kind, parent, cached_mem, live_mem, cached_del,
    live_del, mem_ok, del_ok)."""
    for entry in cache['category_tree']:
        label = entry['label']
        kind = entry['kind']
        parent = entry.get('parent') or ''
        if kind == 'top':
            cached_mem = cache['top_member_counts'].get(label, 0)
            cached_del = cache['top_delete_counts'].get(label, 0)
            live_mem = metrics['per_top_members'].get(label, 0)
            live_del = metrics['per_top_delete'].get(label, 0)
        else:
            cached_mem = cache['sub_member_counts'].get(label, 0)
            cached_del = cache['sub_delete_counts'].get(label, 0)
            live_mem = metrics['per_sub_members'].get(label, 0)
            live_del = metrics['per_sub_delete'].get(label, 0)
        yield (label, kind, parent, cached_mem, live_mem, cached_del, live_del,
               cached_mem == live_mem, cached_del == live_del)


class SKP_OT_DebugDump(Operator):
    """Print the full diagnostic dump to stdout.
    Run with Blender launched from a terminal (or with the System Console
    visible on Windows) so you can copy the output back."""
    bl_idname = "skp.debug_dump"
    bl_label = "Dump Debug to Console"

    def execute(self, context):
        obj = context.active_object
        cache = _get_cache(obj)
        metrics = _compute_debug_metrics(obj)
        if cache is None or metrics is None:
            self.report({'WARNING'}, "No shape keys on active object.")
            return {'CANCELLED'}

        props = context.scene.skp_props
        filtered = get_filtered_keys(obj, props)

        # Console-safe print for this function: Blender's stdout on Windows is
        # often cp1252, so a CJK / emoji shape-key name would raise
        # UnicodeEncodeError and abort the dump mid-stream. Shadow print() so
        # unencodable characters degrade to '?' instead of crashing.
        import sys as _sys
        import builtins as _builtins
        _enc = getattr(_sys.stdout, "encoding", None) or "utf-8"

        def print(*args, **kwargs):
            safe = [str(a).encode(_enc, "replace").decode(_enc) for a in args]
            _builtins.print(*safe, **kwargs)

        sep = "=" * 78
        sub = "-" * 78
        print()
        print(sep)
        print("  Dalek's Shapekey Manager - Debug Dump")
        print(sep)
        print(f"  Object           : {obj.name}")
        print(f"  Shape keys block : {obj.data.shape_keys.name}")
        # bl_info is not reliably available in Blender 4.2+ extension runtime
        # (the extension loader may strip it). Pull from the manifest instead.
        print(f"  addon version    : {_addon_version_tuple()}")
        print()
        print("CACHE STATE")
        print(sub)
        print(f"  sk_ptr            : {cache['sk_ptr']}")
        print(f"  n_blocks          : {cache['n_blocks']}")
        print(f"  name_sig          : {cache['name_sig']}")
        print(f"  prefs_version     : {cache['prefs_version']}  (global {_get_prefs_version()})")
        print(f"  has_basis         : {cache['has_basis']}")
        print(f"  total_real (excl Basis): {cache['total_real']}")
        print(f"  has_categories    : {cache['has_categories']}")
        print(f"  categories        : {len(cache['categories'])}")
        print(f"  top_labels        : {len(cache['top_labels'])}")
        print(f"  category_tree     : {len(cache['category_tree'])}")
        print(f"  real_key_entries  : {len(cache['real_key_entries'])}")
        print(f"  full_info entries : {len(cache['full_info'])}")
        print()
        print("LIVE WALK (independent, just now)")
        print(sub)
        print(f"  n_blocks          : {metrics['n_blocks']}")
        print(f"  dividers top/sub  : {metrics['dividers_top']} / {metrics['dividers_sub']}")
        print(f"  real keys         : {metrics['real_keys']}")
        print(f"  Basis occurrences : {metrics['basis_count']}")
        if metrics['duplicates']:
            print(f"  !! DUPLICATE NAMES ({len(metrics['duplicates'])}): {metrics['duplicates']}")
        if metrics['top_name_duplicates']:
            print(f"  !! top-divider label reused: {metrics['top_name_duplicates']}")
        if metrics['sub_name_duplicates']:
            print(f"  !! sub-divider label reused: {metrics['sub_name_duplicates']}")
        if metrics['cross_level_collisions']:
            print(f"  !! label used as BOTH top and sub: {metrics['cross_level_collisions']}")
        if metrics['orphan_subs']:
            print(f"  !! sub dividers with no enclosing top: {metrics['orphan_subs']}")
        if metrics['sub_multiple_parents']:
            print(f"  !! sub divider label nested under multiple tops: {metrics['sub_multiple_parents']}")
        print()
        print("PATTERNS")
        print(sub)
        for tok, lvl in _get_patterns():
            print(f"  '{tok}'  -> {lvl}")
        print()
        print("PER-CATEGORY (cache vs live)")
        print(sub)
        print(f"  {'label':<30} {'kind':<4} {'parent':<18} {'mem_c':>6} {'mem_l':>6} {'del_c':>6} {'del_l':>6}  diff")
        mismatches = 0
        for (label, kind, parent, cmem, lmem, cdel, ldel,
             mem_ok, del_ok) in _debug_diff_rows(cache, metrics):
            diff = ''
            if not mem_ok:
                diff += f' MEM({cmem}!={lmem})'
                mismatches += 1
            if not del_ok:
                diff += f' DEL({cdel}!={ldel})'
                mismatches += 1
            print(f"  {label[:30]:<30} {kind:<4} {parent[:18]:<18} "
                  f"{cmem:>6} {lmem:>6} {cdel:>6} {ldel:>6}  {diff}")
        print()
        print("CURRENT UI STATE")
        print(sub)
        print(f"  category_filter   : {props.category_filter!r}")
        print(f"  search_filter     : {props.search_filter!r}")
        print(f"  sort_mode         : {props.sort_mode}")
        print(f"  show_basis        : {props.show_basis}")
        print(f"  current_page      : {props.current_page}")
        print(f"  page_size         : {props.page_size}")
        print(f"  filtered length   : {len(filtered)}")
        cat_raw = props.category_filter
        if cat_raw and cat_raw != 'ALL':
            _k, cat = _decode_category_filter(cat_raw, cache)
            is_top = _k == 'top'
            full_info = cache['full_info']
            parent_key = 'parent' if is_top else 'sub'
            recount_all = sum(1 for _, n in cache['real_key_entries']
                              if full_info.get(n, {}).get(parent_key) == cat)
            recount_no_basis = sum(1 for _, n in cache['real_key_entries']
                                   if n != 'Basis'
                                   and full_info.get(n, {}).get(parent_key) == cat)
            recount_by_kb = 0
            for kb in obj.data.shape_keys.key_blocks:
                info = full_info.get(kb.name)
                if info and info.get(parent_key) == cat and kb.name != 'Basis':
                    recount_by_kb += 1
            print(f"  classified        : {'TOP' if is_top else 'SUB'}")
            if is_top:
                print(f"  cached member     : {cache['top_member_counts'].get(cat, 0)}")
                print(f"  cached delete     : {cache['top_delete_counts'].get(cat, 0)}")
                print(f"  live member (top) : {metrics['per_top_members'].get(cat, 0)}")
                print(f"  live delete (top) : {metrics['per_top_delete'].get(cat, 0)}")
            else:
                print(f"  cached member     : {cache['sub_member_counts'].get(cat, 0)}")
                print(f"  cached delete     : {cache['sub_delete_counts'].get(cat, 0)}")
                print(f"  live member (sub) : {metrics['per_sub_members'].get(cat, 0)}")
                print(f"  live delete (sub) : {metrics['per_sub_delete'].get(cat, 0)}")
            print(f"  recount (all)     : {recount_all}")
            print(f"  recount (!Basis)  : {recount_no_basis}")
            print(f"  recount (kb-live) : {recount_by_kb}")
            # If member_count > recount, dig into which cached entries go missing on lookup
            cached_names_in_cat = []
            for i, nm in cache['real_key_entries']:
                info = full_info.get(nm, {})
                if info.get(parent_key) == cat and nm != 'Basis':
                    cached_names_in_cat.append(nm)
            print(f"  real_key_entries matching (!Basis): {len(cached_names_in_cat)}")
            # Report duplicates inside this category specifically
            name_occurrences_in_cat = {}
            live_current_top = ''
            live_current_sub = ''
            for kb in obj.data.shape_keys.key_blocks:
                nm = kb.name
                nlen = len(nm)
                kkind = None
                klabel = nm
                for token, lvl, tlen in [(t, l, len(t)) for t, l in _get_patterns()]:
                    if nlen > tlen * 2 and nm.startswith(token) and nm.endswith(token):
                        kkind = lvl
                        klabel = nm[tlen:-tlen]
                        break
                if kkind == 'top':
                    live_current_top = klabel
                    live_current_sub = ''
                elif kkind == 'sub':
                    live_current_sub = klabel
                else:
                    in_cat = (
                        (is_top and live_current_top == cat)
                        or (not is_top and live_current_sub == cat)
                    )
                    if in_cat and nm != 'Basis':
                        name_occurrences_in_cat[nm] = name_occurrences_in_cat.get(nm, 0) + 1
            dup_in_cat = {n: c for n, c in name_occurrences_in_cat.items() if c > 1}
            if dup_in_cat:
                print(f"  duplicates within category: {dup_in_cat}")
            # Live raw count by walking blocks
            live_in_cat = sum(name_occurrences_in_cat.values())
            print(f"  live raw count in cat (!Basis) : {live_in_cat}")

        print(sep)
        print(f"  TOTAL MISMATCHES: {mismatches}")
        print(sep)
        print()
        self.report({'INFO'}, f"Debug dumped. Mismatches: {mismatches}.")
        return {'FINISHED'}


class SKP_OT_DebugRebuild(Operator):
    """Force rebuild of the derived-data cache (for debugging)."""
    bl_idname = "skp.debug_rebuild"
    bl_label = "Rebuild Cache"

    def execute(self, context):
        obj = context.active_object
        if not obj or not obj.data or not obj.data.shape_keys:
            self.report({'WARNING'}, "No shape keys on active object.")
            return {'CANCELLED'}
        _rebuild_cache(obj)
        self.report({'INFO'}, "Cache rebuilt.")
        return {'FINISHED'}


class SKP_OT_DebugVerify(Operator):
    """Compare cached counts to a fresh independent walk and report mismatches."""
    bl_idname = "skp.debug_verify"
    bl_label = "Verify Counts"

    def execute(self, context):
        obj = context.active_object
        cache = _get_cache(obj)
        metrics = _compute_debug_metrics(obj)
        if cache is None or metrics is None:
            self.report({'WARNING'}, "No shape keys on active object.")
            return {'CANCELLED'}

        mismatches = []
        for (label, kind, parent, cmem, lmem, cdel, ldel,
             mem_ok, del_ok) in _debug_diff_rows(cache, metrics):
            if not (mem_ok and del_ok):
                mismatches.append((label, kind, cmem, lmem, cdel, ldel))

        if mismatches:
            print("[SKP] Count mismatches:")
            for m in mismatches:
                print(f"  {m[1]:<4} {m[0]:<30} member c={m[2]} l={m[3]}  delete c={m[4]} l={m[5]}")
            self.report({'WARNING'}, f"{len(mismatches)} mismatch(es). See console.")
        elif metrics['duplicates']:
            self.report({'WARNING'},
                        f"Counts OK but {len(metrics['duplicates'])} duplicate key names detected.")
        else:
            self.report({'INFO'}, "All counts verified.")
        return {'FINISHED'}


# -----------------------------------------
#  Debug sub-panel
# -----------------------------------------

class SKP_PT_DebugPanel(Panel):
    """Diagnostic panel showing cache vs live-walk counts.

    This is intentionally verbose: when the user reports a mismatch
    between the category dropdown, the Shown counter and the Delete
    Category button, the quickest way to triage is to see every
    relevant number side-by-side."""
    bl_label = "Debug"
    bl_idname = "SKP_PT_debug"
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = "data"
    bl_parent_id = "SKP_PT_main"
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        # Inlined copy of SKP_PT_MainPanel.poll to avoid importing it
        # (which would create a forward reference at module load time).
        # Both polls must stay in sync if main panel's requirements change.
        obj = context.active_object
        return (
            obj is not None
            and obj.type == 'MESH'
            and obj.data.shape_keys is not None
        )

    def draw(self, context):
        layout = self.layout
        obj = context.active_object
        cache = _get_cache(obj)
        if cache is None:
            layout.label(text="No shape keys.", icon='INFO')
            return

        props = context.scene.skp_props
        metrics = _compute_debug_metrics(obj)
        filtered = get_filtered_keys(obj, props)

        # --- Action row ---
        row = layout.row(align=True)
        row.operator("skp.debug_dump", text="Dump Console", icon='CONSOLE')
        row.operator("skp.debug_verify", text="Verify", icon='CHECKMARK')
        row.operator("skp.debug_rebuild", text="Rebuild", icon='FILE_REFRESH')

        # --- Cache header ---
        box = layout.box()
        box.label(text="Cache vs Live", icon='INFO')
        col = box.column(align=True)
        n_match = cache['n_blocks'] == metrics['n_blocks']
        r = col.row()
        r.alert = not n_match
        r.label(text=f"n_blocks cache={cache['n_blocks']} live={metrics['n_blocks']}")
        col.label(text=f"dividers live: top={metrics['dividers_top']} sub={metrics['dividers_sub']}")
        col.label(text=f"real keys live: {metrics['real_keys']} (Basis x{metrics['basis_count']})")
        col.label(text=f"cached total_real (excl Basis): {cache['total_real']}")
        col.label(text=f"categories cached: {len(cache['categories'])}  tree: {len(cache['category_tree'])}")
        col.label(text=f"prefs_version cache={cache['prefs_version']} global={_get_prefs_version()}")

        # Red-flag section
        flag_box = None
        def _flag_row(text):
            nonlocal flag_box
            if flag_box is None:
                flag_box = layout.box()
                flag_box.label(text="Warnings", icon='ERROR')
            r = flag_box.row()
            r.alert = True
            r.label(text=text, icon='ERROR')

        if metrics['duplicates']:
            _flag_row(f"{len(metrics['duplicates'])} duplicate shape-key names")
        if metrics['top_name_duplicates']:
            _flag_row(f"top label reused: {', '.join(metrics['top_name_duplicates'][:3])}")
        if metrics['sub_name_duplicates']:
            _flag_row(f"sub label reused: {', '.join(metrics['sub_name_duplicates'][:3])}")
        if metrics['cross_level_collisions']:
            xs = metrics['cross_level_collisions']
            head = ', '.join(xs[:6])
            more = f" (+{len(xs) - 6} more)" if len(xs) > 6 else ''
            _flag_row(f"label is both top AND sub: {head}{more}")
        if metrics['orphan_subs']:
            _flag_row(f"orphan sub dividers (no top): {len(metrics['orphan_subs'])}")
        if metrics['sub_multiple_parents']:
            _flag_row(f"sub nested under multiple tops: {len(metrics['sub_multiple_parents'])}")

        # --- Focus: currently selected category ---
        cat_raw = props.category_filter
        if cat_raw and cat_raw != 'ALL':
            _fkind, cat = _decode_category_filter(cat_raw, cache)
            is_top = _fkind == 'top'
            box2 = layout.box()
            box2.label(text=f"Focus: {cat}", icon='OUTLINER_COLLECTION')
            col2 = box2.column(align=True)
            col2.label(text=f"classified as: {'TOP' if is_top else 'SUB'}")

            if is_top:
                cached_mem = cache['top_member_counts'].get(cat, 0)
                cached_del = cache['top_delete_counts'].get(cat, 0)
                live_mem = metrics['per_top_members'].get(cat, 0)
                live_del = metrics['per_top_delete'].get(cat, 0)
            else:
                cached_mem = cache['sub_member_counts'].get(cat, 0)
                cached_del = cache['sub_delete_counts'].get(cat, 0)
                live_mem = metrics['per_sub_members'].get(cat, 0)
                live_del = metrics['per_sub_delete'].get(cat, 0)

            r = col2.row()
            r.alert = cached_mem != live_mem
            r.label(text=f"member: cache={cached_mem}  live={live_mem}")
            r = col2.row()
            r.alert = cached_del != live_del
            r.label(text=f"delete: cache={cached_del}  live={live_del}")

            # get_filtered_keys recount using the same full_info lookup the UI uses
            full_info = cache['full_info']
            parent_key = 'parent' if is_top else 'sub'
            recount_all = sum(1 for _, n in cache['real_key_entries']
                              if full_info.get(n, {}).get(parent_key) == cat)
            recount_no_basis = sum(1 for _, n in cache['real_key_entries']
                                   if n != 'Basis'
                                   and full_info.get(n, {}).get(parent_key) == cat)
            # Also note when this label exists at both levels so the user
            # knows why the dropdown now has two entries for the same name.
            if cat in cache.get('top_labels', ()) and cat in cache.get('sub_labels', ()):
                r = col2.row()
                r.alert = True
                r.label(text="label exists at BOTH top AND sub", icon='INFO')
            shown_expected = recount_all if props.show_basis else recount_no_basis
            r = col2.row()
            r.alert = len(filtered) != shown_expected
            r.label(text=f"Shown: {len(filtered)}  (expected via full_info: {shown_expected})")

            # If live member > expected shown, the gap is usually a name
            # collision in full_info - surface that explicitly.
            if live_mem != shown_expected:
                basis_in_cat = 1 if (cache['has_basis']
                                     and full_info.get('Basis', {}).get(parent_key) == cat) else 0
                gap = live_mem - shown_expected - (0 if props.show_basis else basis_in_cat)
                r = col2.row()
                r.alert = True
                r.label(text=f"GAP: {gap} key(s) missing from Shown", icon='ERROR')
                r = col2.row()
                r.alert = True
                r.label(text="(likely duplicate names overwriting full_info)", icon='INFO')

        # --- Patterns in effect ---
        box3 = layout.box()
        box3.label(text="Patterns", icon='FILTER')
        for tok, lvl in _get_patterns():
            box3.label(text=f"'{tok}' -> {lvl}")


CLASSES = (
    SKP_OT_DebugDump,
    SKP_OT_DebugRebuild,
    SKP_OT_DebugVerify,
    SKP_PT_DebugPanel,
)
