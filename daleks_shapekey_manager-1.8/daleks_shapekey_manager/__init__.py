# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2025 Dalek (https://dalek.coffee)

"""
Dalek's Shapekey Manager - Blender 5.0 Add-on  v1.7

Shape keys whose names match ===ANYTHING=== are treated as category
dividers. They are displayed as section headers, never selected, never
previewed, and never modified in any way.

All other keys behave as before: filter, sort, step, arrow-key nav, etc.
A "Category" dropdown appears automatically when at least one category
divider is detected, letting you filter the list to a single category.
"""

bl_info = {
    "name": "Dalek's Shapekey Manager",
    "author": "Generated for Blender 5.0",
    "version": (1, 8, 4),
    "blender": (5, 0, 0),
    "location": "Properties > Object Data > Shape Keys > Dalek's Shapekey Manager",
    "description": "Preview, filter, manage and audit large numbers of shape keys",
    "category": "Mesh",
}

# Duplicate of bl_info['version'] as a module-level constant so runtime
# code (e.g. the debug dump) can reference the version without touching
# bl_info. Blender 4.2+ extensions may omit bl_info from the module
# namespace at import time, which would raise NameError on access.
_ADDON_VERSION = (1, 8, 4)


def _addon_version_tuple():
    return _ADDON_VERSION

import time
import bpy
from bpy.props import (
    StringProperty,
    FloatProperty,
    BoolProperty,
    IntProperty,
    EnumProperty,
)
from bpy.types import Panel, Operator, PropertyGroup


# -----------------------------------------
#  Addon preferences & divider pattern config
# -----------------------------------------

_DEFAULT_PATTERNS = [
    {'token': '===', 'level': 'top'},
    {'token': '---', 'level': 'sub'},
]


def _pattern_changed(self, context):
    # Invalidate patterns cache and derived-data cache when a divider
    # pattern is edited inline via the Configuration sub-panel.
    _bump_prefs_version()


class SKP_DividerPattern(bpy.types.PropertyGroup):
    token: StringProperty(
        name="Token",
        description="Surrounding token that marks a category divider (e.g. === wraps ===VRC===)",
        default="",
        update=_pattern_changed,
    )
    level: EnumProperty(
        name="Level",
        description="Hierarchy level this token represents",
        items=[
            ('top', "Top", "Top-level category header"),
            ('sub', "Sub", "Sub-level category header"),
        ],
        default='sub',
        update=_pattern_changed,
    )


class SKP_AddonPreferences(bpy.types.AddonPreferences):
    bl_idname = __name__

    divider_patterns: bpy.props.CollectionProperty(type=SKP_DividerPattern)
    divider_patterns_index: bpy.props.IntProperty(default=0, min=0)

    delete_cooldown: FloatProperty(
        name="Delete Cooldown (s)",
        description="Seconds the confirm button is locked after opening the delete dialog",
        default=5.0,
        min=0.0,
        max=30.0,
        step=50,
    )

    def draw(self, context):
        pass  # drawn via the Configuration sub-panel in the main panel


# -----------------------------------------
#  Derived-data cache
# -----------------------------------------
#
# Walking the shape_keys list and scanning divider tokens is the hot path for
# this panel. With a few hundred shape keys and the naive design the draw
# method was re-walking the list 4-7 times per redraw, and the category
# EnumProperty callback was doing O(n * categories) counting work on every
# draw. Both of those are invoked on every keystroke in the filter field and
# on every tick of the preview slider.
#
# Instead we compute everything derived from the shape_keys list once, in
# _rebuild_cache, and cache it in _MAP_CACHE keyed on
#   (shape_keys.as_pointer(), len(key_blocks), _prefs_version).
# - pointer change  -> active object / mesh changed
# - length change   -> key added or removed by us or Blender
# - prefs version   -> divider patterns edited in the Configuration sub-panel
# All three checks are constant-time, so a cache hit costs only a dict
# comparison. Renames without a length change are an accepted staleness
# edge case - they only affect divider/label mapping and correct themselves
# the next time the structure changes.

_PATTERNS_CACHE = {'value': None, 'version': -1}
_PREFS_VERSION = 0

# Blender's EnumProperty items callback hands back raw pointers to the
# strings inside each tuple - it does NOT keep its own copies. If Python
# GCs the previous list before Blender reads it the dropdown renders
# freed memory, which looks like category labels/counts drifting or
# "blending" together after many redraws/deletions. Hold a strong
# reference here so the strings outlive Blender's access.
_ENUM_ITEMS_HOLD: list = []

_MAP_CACHE = {
    'sk_ptr': None,
    'n_blocks': 0,
    'name_sig': None,          # hash of key names - detects renames without length change
    'prefs_version': -1,
    'full_info': None,
    'category_tree': None,
    'categories': None,
    'top_labels': None,
    'sub_labels': None,
    'real_key_entries': None,  # list[(index, name)] excluding dividers, in original order
    # Counts are keyed separately per level: when a label like "Eyeline" is
    # used as BOTH a ===top=== divider and a ---sub--- divider elsewhere,
    # a single dict collapsed both into one entry and the visible counts
    # drifted away from what Delete Category would actually remove.
    'top_member_counts': None,  # {top label -> real-key count in that top}
    'top_delete_counts': None,  # {top label -> total entries a top-delete removes}
    'sub_member_counts': None,  # {sub label -> real-key count directly under that sub}
    'sub_delete_counts': None,  # {sub label -> total entries a sub-delete removes}
    'has_categories': False,
    'total_real': 0,           # real keys excluding Basis (matches default Shown count)
    'has_basis': False,
}


def _compute_name_signature(blocks):
    """Cheap fingerprint of the current key_blocks order + names.

    Length alone misses renames and reorders (both leave n_blocks and the
    shape_keys datablock pointer unchanged). This fingerprint catches
    those, at the cost of one tuple(hash(name)...) per cache lookup -
    microseconds even for thousands of keys, orders of magnitude less
    than a full rebuild."""
    return hash(tuple(kb.name for kb in blocks))


def _bump_prefs_version():
    global _PREFS_VERSION
    _PREFS_VERSION += 1


def _get_patterns():
    """Cached list of (token, level) pairs from preferences."""
    if (_PATTERNS_CACHE['value'] is not None
            and _PATTERNS_CACHE['version'] == _PREFS_VERSION):
        return _PATTERNS_CACHE['value']
    addon = bpy.context.preferences.addons.get(__name__)
    patterns = None
    if addon:
        patterns = [(p.token.strip(), p.level)
                    for p in addon.preferences.divider_patterns
                    if p.token.strip()]
        if not patterns:
            patterns = None
    if patterns is None:
        patterns = [(p['token'], p['level']) for p in _DEFAULT_PATTERNS]
    _PATTERNS_CACHE['value'] = patterns
    _PATTERNS_CACHE['version'] = _PREFS_VERSION
    return patterns


# -----------------------------------------
#  Category helpers
# -----------------------------------------

def is_category_divider(name: str) -> bool:
    for token, _level in _get_patterns():
        tlen = len(token)
        if (len(name) > tlen * 2
                and name.startswith(token)
                and name.endswith(token)):
            return True
    return False


def divider_kind(name: str):
    """Return ('top', label) or ('sub', label) or (None, name)."""
    for token, level in _get_patterns():
        tlen = len(token)
        if (len(name) > tlen * 2
                and name.startswith(token)
                and name.endswith(token)):
            return level, name[tlen:-tlen]
    return None, name


def category_label(name: str) -> str:
    _, label = divider_kind(name)
    return label


def _rebuild_cache(obj):
    """Recompute all derived views of the shape_keys list in a single pass."""
    shape_keys = obj.data.shape_keys
    blocks = shape_keys.key_blocks
    # Pre-extract pattern tuples with precomputed token length to avoid
    # len(token) on every key, and stop doing per-key attribute access.
    patterns = [(token, level, len(token)) for token, level in _get_patterns()]

    full_info = {}
    category_tree = []
    categories = []
    top_labels = set()
    sub_labels = set()
    real_key_entries = []
    top_member_counts = {}
    top_delete_counts = {}
    sub_member_counts = {}
    sub_delete_counts = {}
    seen_tree = set()
    seen_cat = set()

    current_top = ''
    current_sub = ''

    for i, kb in enumerate(blocks):
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
            current_top = label
            current_sub = ''
            full_info[name] = {'kind': 'top', 'parent': None,
                               'sub': None, 'label': label}
            tkey = ('top', label)
            if tkey not in seen_tree:
                seen_tree.add(tkey)
                category_tree.append({'label': label, 'kind': 'top', 'parent': None})
            if label not in seen_cat:
                seen_cat.add(label)
                categories.append(label)
            top_labels.add(label)
            # The top divider itself contributes +1 to its own delete count
            top_delete_counts[label] = top_delete_counts.get(label, 0) + 1
            top_member_counts.setdefault(label, 0)
        elif kind == 'sub':
            current_sub = label
            parent = current_top or None
            full_info[name] = {'kind': 'sub', 'parent': parent,
                               'sub': label, 'label': label}
            tkey = ('sub', label)
            if tkey not in seen_tree:
                seen_tree.add(tkey)
                category_tree.append({'label': label, 'kind': 'sub', 'parent': parent})
            if label not in seen_cat:
                seen_cat.add(label)
                categories.append(label)
            sub_labels.add(label)
            # Sub divider itself counts once for its own deletion, and once
            # toward the enclosing top's deletion (top-delete removes all
            # sub dividers nested under it too).
            sub_delete_counts[label] = sub_delete_counts.get(label, 0) + 1
            if parent:
                top_delete_counts[parent] = top_delete_counts.get(parent, 0) + 1
            sub_member_counts.setdefault(label, 0)
        else:
            parent = current_top or None
            sub = current_sub or None
            full_info[name] = {'kind': 'key', 'parent': parent,
                               'sub': sub, 'label': name}
            real_key_entries.append((i, name))
            if sub:
                sub_member_counts[sub] = sub_member_counts.get(sub, 0) + 1
                sub_delete_counts[sub] = sub_delete_counts.get(sub, 0) + 1
            if parent:
                top_member_counts[parent] = top_member_counts.get(parent, 0) + 1
                top_delete_counts[parent] = top_delete_counts.get(parent, 0) + 1

    # Basis is in real_key_entries (so "Show Basis" can surface it) but the
    # default view hides it. Report total_real as the count the user will
    # see by default; the panel adds +1 when Show Basis is toggled on.
    has_basis = any(name == "Basis" for _, name in real_key_entries)
    total_real = len(real_key_entries) - (1 if has_basis else 0)

    _MAP_CACHE.update({
        'sk_ptr': shape_keys.as_pointer(),
        'n_blocks': len(blocks),
        'name_sig': _compute_name_signature(blocks),
        'prefs_version': _PREFS_VERSION,
        'full_info': full_info,
        'category_tree': category_tree,
        'categories': categories,
        'top_labels': top_labels,
        'sub_labels': sub_labels,
        'real_key_entries': real_key_entries,
        'top_member_counts': top_member_counts,
        'top_delete_counts': top_delete_counts,
        'sub_member_counts': sub_member_counts,
        'sub_delete_counts': sub_delete_counts,
        'has_categories': bool(categories),
        'total_real': total_real,
        'has_basis': has_basis,
    })
    return _MAP_CACHE


def _get_cache(obj):
    """Return the derived-data cache for obj, rebuilding if stale. None if no shape keys."""
    if not obj or not obj.data or not obj.data.shape_keys:
        return None
    shape_keys = obj.data.shape_keys
    sk_ptr = shape_keys.as_pointer()
    blocks = shape_keys.key_blocks
    n = len(blocks)
    if (_MAP_CACHE['sk_ptr'] == sk_ptr
            and _MAP_CACHE['n_blocks'] == n
            and _MAP_CACHE['prefs_version'] == _PREFS_VERSION
            and _MAP_CACHE['full_info'] is not None
            and _MAP_CACHE['name_sig'] == _compute_name_signature(blocks)):
        return _MAP_CACHE
    return _rebuild_cache(obj)


def build_category_enum(self, context):
    """EnumProperty items callback - fires on every panel redraw.

    The returned list is stashed in _ENUM_ITEMS_HOLD so Python keeps the
    strings alive for as long as Blender is referencing their pointers.
    Without this the dropdown renders freed memory and the counts appear
    to drift the more the user interacts with the panel."""
    global _ENUM_ITEMS_HOLD
    obj = context.active_object if context else None
    cache = _get_cache(obj)
    if cache is None:
        items = [('ALL', "All Categories", "Show all shape keys")]
        _ENUM_ITEMS_HOLD = items
        return items

    props = context.scene.skp_props if context and hasattr(context.scene, 'skp_props') else None
    show_basis = bool(props.show_basis) if props else False
    total = cache['total_real'] + (1 if show_basis and cache.get('has_basis') else 0)

    items = [('ALL', f"All Categories ({total})", "Show all shape keys")]
    # Identifier format: "<kind>\x1f<label>" so a label that exists at both
    # top AND sub produces two distinct enum entries instead of colliding
    # on the same identifier. _decode_category_filter / get_filtered_keys
    # read the kind prefix to filter on the correct level.
    top_mem = cache['top_member_counts']
    sub_mem = cache['sub_member_counts']
    for entry in cache['category_tree']:
        label = entry['label']
        if entry['kind'] == 'top':
            count = top_mem.get(label, 0)
            ident = f"top\x1f{label}"
            items.append((ident, f"{label} ({count})",
                          f"Top-level category: {label}"))
        else:
            count = sub_mem.get(label, 0)
            ident = f"sub\x1f{label}"
            parent = entry.get('parent') or ''
            desc = (f"Sub-category '{label}' under '{parent}'" if parent
                    else f"Sub-category: {label}")
            items.append((ident, f"  {label} ({count})", desc))
    _ENUM_ITEMS_HOLD = items
    return items


def _decode_category_filter(cat_filter, cache):
    """Split an encoded category_filter value into (kind, label).

    Accepts both the new "<kind>\x1f<label>" form and the legacy plain-
    label form (saved into scene state before this release). For the
    legacy form, fall back to top_labels membership to pick a kind."""
    if not cat_filter or cat_filter == 'ALL':
        return None, cat_filter
    if '\x1f' in cat_filter:
        kind, _, label = cat_filter.partition('\x1f')
        if kind in ('top', 'sub') and label:
            return kind, label
    # Legacy plain label - classify by which set it lives in.
    if cache is not None:
        if cat_filter in (cache.get('top_labels') or set()):
            return 'top', cat_filter
        if cat_filter in (cache.get('sub_labels') or set()):
            return 'sub', cat_filter
    return 'top', cat_filter


# -----------------------------------------
#  Properties
# -----------------------------------------

def _update_preview_value(self, context):
    """Called instantly whenever the preview_value slider moves.
    Writes the new value directly to the active shape key - no operator needed."""
    obj = context.active_object
    if not obj or not obj.data or not obj.data.shape_keys:
        return
    blocks = obj.data.shape_keys.key_blocks
    idx = obj.active_shape_key_index
    if idx < 0 or idx >= len(blocks):
        return
    kb = blocks[idx]
    # Never touch a category divider
    if is_category_divider(kb.name):
        return
    kb.value = self.preview_value


class SKP_Properties(PropertyGroup):

    search_filter: StringProperty(
        name="Filter",
        description="Filter shape keys by name (case-insensitive)",
        default="",
        options={'TEXTEDIT_UPDATE'},
    )

    category_filter: EnumProperty(
        name="Category",
        description="Show only shape keys belonging to this category",
        items=build_category_enum,
    )

    preview_value: FloatProperty(
        name="Preview Value",
        description="Value applied live to the active shape key as you drag",
        default=1.0,
        min=0.0,
        max=1.0,
        step=10,
        update=_update_preview_value,
    )

    auto_reset: BoolProperty(
        name="Auto Reset Others",
        description="Reset all other shape keys to 0 when previewing one",
        default=True,
    )

    auto_preview: BoolProperty(
        name="Auto Preview on Select",
        description="Automatically preview a shape key when selected in the list",
        default=True,
    )

    page_size: IntProperty(
        name="Per Page",
        description="Number of shape keys to display per page",
        default=100,
        min=5,
        max=100,
        step=5,
    )

    current_page: IntProperty(
        name="Current Page",
        default=0,
        min=0,
    )

    sort_mode: EnumProperty(
        name="Sort",
        items=[
            ('NONE',    "Default",    "Original shape key order"),
            ('AZ',      "A to Z",     "Alphabetical ascending"),
            ('ZA',      "Z to A",     "Alphabetical descending"),
            ('VALUE',   "By Value",   "Sort by current value (highest first)"),
            ('NONZERO', "Non-zero",   "Show non-zero keys first"),
        ],
        default='NONE',
    )

    show_basis: BoolProperty(
        name="Show Basis",
        description="Include the Basis key in the list",
        default=False,
    )

    highlight_nonzero: BoolProperty(
        name="Highlight Active",
        description="Mark shape keys that currently have a value > 0",
        default=True,
    )

    # Cursor position within the filtered list for step navigation.
    # -1 = unset, initialised from the active shape key on first use.
    step_index: IntProperty(
        name="Step Index",
        default=-1,
        min=-1,
    )


# -----------------------------------------
#  Helpers
# -----------------------------------------

def get_filtered_keys(obj, props):
    """
    Return a filtered + sorted list of (original_index, shape_key) tuples.
    Category divider entries are NEVER included - they only affect the
    category mapping used by the category_filter.
    """
    cache = _get_cache(obj)
    if cache is None:
        return []

    blocks = obj.data.shape_keys.key_blocks
    show_basis = props.show_basis

    # Carry the cached name alongside the live kb so the category filter's
    # full_info lookup uses the same string that seeded full_info. Looking
    # up by kb.name would orphan any key renamed in a window where the
    # cache is technically still valid (same sk_ptr, same length).
    # real_key_entries is pre-filtered to exclude dividers, so no per-key
    # divider test here.
    if show_basis:
        entries = [(i, name, blocks[i]) for i, name in cache['real_key_entries']]
    else:
        entries = [(i, name, blocks[i]) for i, name in cache['real_key_entries']
                   if name != "Basis"]

    # Category filter (only when categories exist)
    cat_filter = props.category_filter
    if cat_filter and cat_filter != 'ALL':
        full_info = cache['full_info']
        kind, label = _decode_category_filter(cat_filter, cache)
        parent_key = 'parent' if kind == 'top' else 'sub'
        entries = [(i, n, kb) for i, n, kb in entries
                   if full_info.get(n, {}).get(parent_key) == label]

    # Text search - filter on the LIVE name so users filter on what they see
    query = props.search_filter.strip().lower()
    if query:
        entries = [(i, n, kb) for i, n, kb in entries if query in kb.name.lower()]

    keys = [(i, kb) for i, _n, kb in entries]

    # Sort (only meaningful when sort != NONE; categories not preserved in other modes)
    mode = props.sort_mode
    if mode == 'AZ':
        keys.sort(key=lambda x: x[1].name.lower())
    elif mode == 'ZA':
        keys.sort(key=lambda x: x[1].name.lower(), reverse=True)
    elif mode == 'VALUE':
        keys.sort(key=lambda x: x[1].value, reverse=True)
    elif mode == 'NONZERO':
        keys.sort(key=lambda x: (0 if x[1].value > 0 else 1, x[0]))

    return keys


def total_pages(filtered, page_size):
    if not filtered:
        return 1
    return max(1, (len(filtered) + page_size - 1) // page_size)


def _resolve_step_index_readonly(obj, props, filtered):
    """Read-only version safe to call from draw(). Never writes to props."""
    if not filtered:
        return 0
    if props.step_index < 0:
        active_name = obj.data.shape_keys.key_blocks[obj.active_shape_key_index].name
        for fi, (_, kb) in enumerate(filtered):
            if kb.name == active_name:
                return fi
        return 0
    return max(0, min(len(filtered) - 1, props.step_index))


def _resolve_step_index(obj, props, filtered):
    """Operator version - may write to props to initialise step_index."""
    if not filtered:
        return 0
    if props.step_index < 0:
        active_name = obj.data.shape_keys.key_blocks[obj.active_shape_key_index].name
        for fi, (_, kb) in enumerate(filtered):
            if kb.name == active_name:
                props.step_index = fi
                return fi
        props.step_index = 0
    return max(0, min(len(filtered) - 1, props.step_index))


def _apply_step(context, delta):
    obj = context.active_object
    props = context.scene.skp_props

    if not obj or not obj.data or not obj.data.shape_keys:
        return {'CANCELLED'}

    filtered = get_filtered_keys(obj, props)
    if not filtered:
        return {'CANCELLED'}

    current = _resolve_step_index(obj, props, filtered)
    new_fi = max(0, min(len(filtered) - 1, current + delta))
    props.step_index = new_fi

    orig_idx, kb = filtered[new_fi]
    obj.active_shape_key_index = orig_idx

    if props.auto_preview:
        bpy.ops.skp.preview_key(key_name=kb.name)

    props.current_page = new_fi // props.page_size
    return {'FINISHED'}


# -----------------------------------------
#  Operators
# -----------------------------------------

class SKP_OT_PreviewKey(Operator):
    """Set this shape key to the preview value; optionally reset others.
    Hold Shift when clicking to extend: preview this key without resetting others
    (overrides Auto Reset for this click).
    Category divider keys are never passed to this operator."""
    bl_idname = "skp.preview_key"
    bl_label = "Preview Shape Key"
    bl_options = {'REGISTER', 'UNDO'}

    key_name: StringProperty()
    extend: BoolProperty(
        name="Extend",
        description="If true, do not reset other keys (shift-click behaviour)",
        default=False,
        options={'SKIP_SAVE'},
    )

    def invoke(self, context, event):
        self.extend = event.shift
        return self.execute(context)

    def execute(self, context):
        obj = context.active_object
        props = context.scene.skp_props

        if not obj or not obj.data or not obj.data.shape_keys:
            self.report({'WARNING'}, "No shape keys on active object.")
            return {'CANCELLED'}

        # Safety guard: never touch a category divider
        if is_category_divider(self.key_name):
            self.report({'WARNING'}, "Cannot preview a category divider.")
            return {'CANCELLED'}

        blocks = obj.data.shape_keys.key_blocks

        if props.auto_reset and not self.extend:
            for kb in blocks:
                if kb.name != "Basis" and not is_category_divider(kb.name):
                    kb.value = 0.0

        idx = blocks.find(self.key_name)
        if idx >= 0:
            blocks[idx].value = props.preview_value
            obj.active_shape_key_index = idx
        else:
            self.report({'WARNING'}, f"Shape key '{self.key_name}' not found.")
            return {'CANCELLED'}

        return {'FINISHED'}


class SKP_OT_ResetAll(Operator):
    """Reset all shape keys (except Basis and category dividers) to 0."""
    bl_idname = "skp.reset_all"
    bl_label = "Reset All Shape Keys"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = context.active_object
        if not obj or not obj.data or not obj.data.shape_keys:
            return {'CANCELLED'}
        for kb in obj.data.shape_keys.key_blocks:
            if kb.name != "Basis" and not is_category_divider(kb.name):
                kb.value = 0.0
        self.report({'INFO'}, "All shape keys reset to 0.")
        return {'FINISHED'}




class SKP_OT_ApplyKey(Operator):
    """Apply this shape key at its current value and remove it, baking its effect into the mesh.
    Category dividers are never passed to this operator."""
    bl_idname = "skp.apply_key"
    bl_label = "Apply Shape Key"
    bl_options = {'REGISTER', 'UNDO'}

    key_name: StringProperty()

    def execute(self, context):
        obj = context.active_object
        if not obj or not obj.data or not obj.data.shape_keys:
            self.report({'WARNING'}, "No shape keys on active object.")
            return {'CANCELLED'}

        if is_category_divider(self.key_name):
            self.report({'WARNING'}, "Cannot apply a category divider.")
            return {'CANCELLED'}

        blocks = obj.data.shape_keys.key_blocks
        idx = blocks.find(self.key_name)
        if idx < 0:
            self.report({'WARNING'}, f"Shape key '{self.key_name}' not found.")
            return {'CANCELLED'}

        # Set as active then use Blender's built-in apply
        obj.active_shape_key_index = idx
        bpy.ops.object.shape_key_remove(all=False, apply_mix=True)
        self.report({'INFO'}, f"Applied shape key: {self.key_name}")
        return {'FINISHED'}

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)


class SKP_OT_DeleteKey(Operator):
    """Delete this shape key without applying it.
    Category dividers are never passed to this operator."""
    bl_idname = "skp.delete_key"
    bl_label = "Delete Shape Key"
    bl_options = {'REGISTER', 'UNDO'}

    key_name: StringProperty()

    def execute(self, context):
        obj = context.active_object
        if not obj or not obj.data or not obj.data.shape_keys:
            self.report({'WARNING'}, "No shape keys on active object.")
            return {'CANCELLED'}

        if is_category_divider(self.key_name):
            self.report({'WARNING'}, "Cannot delete a category divider through this panel.")
            return {'CANCELLED'}

        blocks = obj.data.shape_keys.key_blocks
        idx = blocks.find(self.key_name)
        if idx < 0:
            self.report({'WARNING'}, f"Shape key '{self.key_name}' not found.")
            return {'CANCELLED'}

        obj.active_shape_key_index = idx
        bpy.ops.object.shape_key_remove(all=False, apply_mix=False)
        self.report({'INFO'}, f"Deleted shape key: {self.key_name}")
        return {'FINISHED'}

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)


class SKP_OT_NewKey(Operator):
    """Add a new empty shape key (from mix) after the current active key."""
    bl_idname = "skp.new_key"
    bl_label = "New Shape Key from Mix"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = context.active_object
        if not obj or not obj.data:
            self.report({'WARNING'}, "No active mesh object.")
            return {'CANCELLED'}
        bpy.ops.object.shape_key_add(from_mix=True)
        self.report({'INFO'}, "New shape key added from mix.")
        return {'FINISHED'}

class SKP_DeleteKeyItem(bpy.types.PropertyGroup):
    """One entry in the delete-preview list."""
    name: StringProperty()
    is_divider: BoolProperty(default=False)


class SKP_UL_DeletePreview(bpy.types.UIList):
    """Scrollable list of shape keys that will be deleted.
    Each row is prefixed with a 1-based index so the user can visually
    verify the total count matches the reported figure."""
    bl_idname = "SKP_UL_delete_preview"

    def draw_item(self, context, layout, data, item, icon,
                  active_data, active_propname, index=0, flt_flag=0):
        # Size the index column based on the highest number we will need to render
        total = len(context.scene.skp_delete_preview)
        num_width = max(2, len(str(total)))
        num_text = f"{index + 1:>{num_width}}."

        row = layout.row(align=True)

        # Fixed-width, right-aligned numeric prefix so all the periods line up
        num_col = row.column()
        num_col.ui_units_x = 1.2 + max(0, num_width - 2) * 0.4
        num_col.alignment = 'RIGHT'
        num_col.label(text=num_text)

        if item.is_divider:
            row.label(text=item.name, icon='OUTLINER_COLLECTION')
        else:
            row.label(text=item.name, icon='SHAPEKEY_DATA')

    def draw_filter(self, context, layout):
        # Suppress the built-in filter bar - we render our own above the list
        pass

    def filter_items(self, context, data, propname):
        items = getattr(data, propname)
        query = getattr(context.scene, 'skp_delete_filter', '').strip().lower()
        if not query:
            return [], []
        flt_flags = []
        for item in items:
            if query in item.name.lower():
                flt_flags.append(self.bitflag_filter_item)
            else:
                flt_flags.append(0)
        return flt_flags, []


class SKP_OT_DeleteCategory(Operator):
    """Delete all shape keys belonging to the currently selected category,
    including the ===CATEGORY=== divider key itself.
    Opens a timed confirmation dialog with a 5-second cooldown and scrollable key preview."""
    bl_idname = "skp.delete_category"
    bl_label = "Delete Category"
    bl_options = {'REGISTER', 'UNDO'}

    category_name: StringProperty()
    # Which level of divider to target. Empty falls back to the previous
    # "is top if label is in top_labels" heuristic, which is ambiguous when
    # a label exists at both levels - callers from this addon always set
    # this explicitly so the wrong-level deletion can't happen.
    category_kind: StringProperty(default='')
    key_count: IntProperty()
    respect_filter: BoolProperty(
        name="Respect Filter",
        description="If true, only collect keys within this category that match the active search filter",
        default=False,
        options={'SKIP_SAVE'},
    )
    show_keys_toggle: BoolProperty(
        name="Show Keys",
        default=False,
    )

    # Class-level state shared across draw/execute calls for this invocation
    _start_time: float = 0.0
    _keys_to_delete: list = []

    @staticmethod
    def _cooldown():
        addon = bpy.context.preferences.addons.get(__name__)
        if addon:
            return addon.preferences.delete_cooldown
        return 5.0

    def _collect_keys(self, context):
        obj = context.active_object
        cache = _get_cache(obj)
        if cache is None:
            return []
        cat = self.category_name
        if self.category_kind in ('top', 'sub'):
            is_top = self.category_kind == 'top'
        else:
            is_top = cat in cache['top_labels']

        result = []
        for name, d in cache['full_info'].items():
            kind = d['kind']
            if is_top:
                if ((kind == 'top' and d['label'] == cat)
                        or (kind in ('sub', 'key') and d['parent'] == cat)):
                    result.append(name)
            else:
                if ((kind == 'sub' and d['label'] == cat)
                        or (kind == 'key' and d['sub'] == cat)):
                    result.append(name)

        if self.respect_filter:
            query = context.scene.skp_props.search_filter.strip().lower()
            if query:
                # Divider names rarely match a user filter; excluding them
                # when filter is active matches the user's mental model of
                # "delete just the visible matches, leave the category intact."
                result = [n for n in result if query in n.lower()]

        return result

    def _seconds_remaining(self):
        elapsed = time.time() - SKP_OT_DeleteCategory._start_time
        return max(0.0, self._cooldown() - elapsed)

    def execute(self, context):
        if self._seconds_remaining() > 0:
            self.report({'WARNING'}, "Please wait for the cooldown before confirming.")
            return {'CANCELLED'}

        obj = context.active_object
        if not obj or not obj.data or not obj.data.shape_keys:
            return {'CANCELLED'}

        to_delete = SKP_OT_DeleteCategory._keys_to_delete
        if not to_delete:
            self.report({'WARNING'}, f"No keys found for category '{self.category_name}'.")
            return {'CANCELLED'}

        # Delete by name each iteration - re-resolve index fresh each time
        # because the live blocks list shrinks as keys are removed.
        for name in to_delete:
            blocks = obj.data.shape_keys.key_blocks
            idx = blocks.find(name)
            if idx < 0:
                continue
            obj.active_shape_key_index = idx
            bpy.ops.object.shape_key_remove(all=False, apply_mix=False)

        self.report({'INFO'}, f"Deleted category '{self.category_name}' ({len(to_delete)} keys).")
        SKP_OT_DeleteCategory._keys_to_delete = []
        # Clear the temp collection
        context.scene.skp_delete_preview.clear()
        return {'FINISHED'}

    def invoke(self, context, event):
        SKP_OT_DeleteCategory._start_time = time.time()
        SKP_OT_DeleteCategory._keys_to_delete = self._collect_keys(context)
        self.key_count = len(SKP_OT_DeleteCategory._keys_to_delete)

        # Populate the scene-level CollectionProperty for the UIList
        col = context.scene.skp_delete_preview
        col.clear()
        for name in SKP_OT_DeleteCategory._keys_to_delete:
            item = col.add()
            item.name = name
            item.is_divider = is_category_divider(name)

        # Reset the UIList active index on the scene
        context.scene.skp_delete_preview_index = 0
        context.scene.skp_delete_filter = ""

        return context.window_manager.invoke_props_dialog(self, width=420)

    def draw(self, context):
        layout = self.layout
        remaining = self._seconds_remaining()

        # Warning header
        col = layout.column()
        col.alert = True
        if self.respect_filter:
            col.label(
                text=f"Delete filtered keys in: {self.category_name}",
                icon='ERROR',
            )
        else:
            col.label(
                text=f"Delete entire category: {self.category_name}",
                icon='ERROR',
            )
        col.alert = False

        layout.separator(factor=0.3)
        layout.label(text=f"This will permanently delete {self.key_count} shape key(s).")
        if self.respect_filter:
            query = context.scene.skp_props.search_filter.strip()
            if query:
                layout.label(text=f'Only keys matching filter "{query}" will be removed.')
            layout.label(text="The category divider will be kept. This cannot be undone easily.")
        else:
            layout.label(text="Includes the category divider. This cannot be undone easily.")

        layout.separator(factor=0.5)

        # Toggle for the scrollable key list
        toggle_icon = 'TRIA_DOWN' if self.show_keys_toggle else 'TRIA_RIGHT'
        toggle_text = (
            f"Hide keys to be deleted ({self.key_count})"
            if self.show_keys_toggle else
            f"Show keys to be deleted ({self.key_count})"
        )
        layout.prop(self, "show_keys_toggle", text=toggle_text, toggle=True, icon=toggle_icon)

        if self.show_keys_toggle:
            # Search field above the list
            row = layout.row(align=True)
            row.prop(context.scene, "skp_delete_filter", text="", icon='VIEWZOOM',
                     placeholder="Filter keys...")
            if context.scene.skp_delete_filter:
                row.operator("skp.delete_filter_clear", text="", icon='X')

            # Cap at 30 rows to avoid exceeding screen height; list is scrollable
            rows = max(5, min(30, self.key_count))
            layout.template_list(
                "SKP_UL_delete_preview", "",
                context.scene, "skp_delete_preview",
                context.scene, "skp_delete_preview_index",
                rows=rows,
            )

        layout.separator(factor=0.5)

        # Cooldown indicator
        if remaining > 0:
            secs = int(remaining) + 1
            warn_row = layout.row()
            warn_row.alert = True
            warn_row.label(text=f"OK available in {secs}s - read the warning above", icon='TIME')
        else:
            layout.label(text="Cooldown complete. Click OK to confirm.", icon='CHECKMARK')


class SKP_OT_DeleteFilterClear(Operator):
    """Clear the delete preview search filter."""
    bl_idname = "skp.delete_filter_clear"
    bl_label = "Clear Filter"

    def execute(self, context):
        context.scene.skp_delete_filter = ""
        return {'FINISHED'}


def _is_key_empty(obj, kb):
    """Return True if every vertex in this shape key matches the reference key (Basis)."""
    ref = obj.data.shape_keys.reference_key
    if kb.name == ref.name:
        return False
    for v_sk, v_ref in zip(kb.data, ref.data):
        if (v_sk.co - v_ref.co).length_squared > 1e-10:
            return False
    return True


class SKP_OT_DeleteEmptyKeys(Operator):
    """Delete all shape keys that have no vertex displacement from the Basis.
    Opens the same timed confirmation dialog as Delete Category."""
    bl_idname = "skp.delete_empty_keys"
    bl_label = "Delete Empty Blendshapes"
    bl_options = {'REGISTER', 'UNDO'}

    key_count: IntProperty()
    show_keys_toggle: BoolProperty(name="Show Keys", default=False)

    _start_time: float = 0.0
    _keys_to_delete: list = []

    @staticmethod
    def _cooldown():
        addon = bpy.context.preferences.addons.get(__name__)
        if addon:
            return addon.preferences.delete_cooldown
        return 5.0

    def _collect_empty_keys(self, context):
        obj = context.active_object
        cache = _get_cache(obj)
        if cache is None:
            return []
        blocks = obj.data.shape_keys.key_blocks
        # real_key_entries already excludes dividers
        return [name for i, name in cache['real_key_entries']
                if _is_key_empty(obj, blocks[i])]

    def _seconds_remaining(self):
        elapsed = time.time() - SKP_OT_DeleteEmptyKeys._start_time
        return max(0.0, self._cooldown() - elapsed)

    def execute(self, context):
        if self._seconds_remaining() > 0:
            self.report({'WARNING'}, "Please wait for the cooldown before confirming.")
            return {'CANCELLED'}

        obj = context.active_object
        if not obj or not obj.data or not obj.data.shape_keys:
            return {'CANCELLED'}

        to_delete = SKP_OT_DeleteEmptyKeys._keys_to_delete
        if not to_delete:
            self.report({'INFO'}, "No empty shape keys found.")
            return {'CANCELLED'}

        for name in to_delete:
            blocks = obj.data.shape_keys.key_blocks
            idx = blocks.find(name)
            if idx < 0:
                continue
            obj.active_shape_key_index = idx
            bpy.ops.object.shape_key_remove(all=False, apply_mix=False)

        self.report({'INFO'}, f"Deleted {len(to_delete)} empty shape key(s).")
        SKP_OT_DeleteEmptyKeys._keys_to_delete = []
        context.scene.skp_delete_preview.clear()
        return {'FINISHED'}

    def invoke(self, context, event):
        SKP_OT_DeleteEmptyKeys._start_time = time.time()
        SKP_OT_DeleteEmptyKeys._keys_to_delete = self._collect_empty_keys(context)
        self.key_count = len(SKP_OT_DeleteEmptyKeys._keys_to_delete)

        if self.key_count == 0:
            self.report({'INFO'}, "No empty shape keys found.")
            return {'CANCELLED'}

        col = context.scene.skp_delete_preview
        col.clear()
        for name in SKP_OT_DeleteEmptyKeys._keys_to_delete:
            item = col.add()
            item.name = name
            item.is_divider = False

        context.scene.skp_delete_preview_index = 0
        context.scene.skp_delete_filter = ""
        return context.window_manager.invoke_props_dialog(self, width=420)

    def draw(self, context):
        layout = self.layout
        remaining = self._seconds_remaining()

        col = layout.column()
        col.alert = True
        col.label(text=f"Delete {self.key_count} empty shape key(s)", icon='ERROR')
        col.alert = False

        layout.separator(factor=0.3)
        layout.label(text="These keys have no vertex displacement from Basis.")
        layout.label(text="They will be permanently removed. This cannot be undone easily.")

        layout.separator(factor=0.5)

        toggle_icon = 'TRIA_DOWN' if self.show_keys_toggle else 'TRIA_RIGHT'
        toggle_text = (
            f"Hide keys to be deleted ({self.key_count})"
            if self.show_keys_toggle else
            f"Show keys to be deleted ({self.key_count})"
        )
        layout.prop(self, "show_keys_toggle", text=toggle_text, toggle=True, icon=toggle_icon)

        if self.show_keys_toggle:
            row = layout.row(align=True)
            row.prop(context.scene, "skp_delete_filter", text="", icon='VIEWZOOM',
                     placeholder="Filter keys...")
            if context.scene.skp_delete_filter:
                row.operator("skp.delete_filter_clear", text="", icon='X')

            rows = max(5, min(30, self.key_count))
            layout.template_list(
                "SKP_UL_delete_preview", "",
                context.scene, "skp_delete_preview",
                context.scene, "skp_delete_preview_index",
                rows=rows,
            )

        layout.separator(factor=0.5)

        if remaining > 0:
            secs = int(remaining) + 1
            warn_row = layout.row()
            warn_row.alert = True
            warn_row.label(text=f"OK available in {secs}s - read the warning above", icon='TIME')
        else:
            layout.label(text="Cooldown complete. Click OK to confirm.", icon='CHECKMARK')


class SKP_OT_DeleteFiltered(Operator):
    """Delete every shape key currently matching the text filter
    (across all categories in the active category scope).
    Opens the same timed confirmation dialog as Delete Category."""
    bl_idname = "skp.delete_filtered"
    bl_label = "Delete Filtered Keys"
    bl_options = {'REGISTER', 'UNDO'}

    key_count: IntProperty()
    show_keys_toggle: BoolProperty(name="Show Keys", default=False)

    _start_time: float = 0.0
    _keys_to_delete: list = []

    @staticmethod
    def _cooldown():
        addon = bpy.context.preferences.addons.get(__name__)
        if addon:
            return addon.preferences.delete_cooldown
        return 5.0

    def _collect_filtered_keys(self, context):
        obj = context.active_object
        props = context.scene.skp_props
        # get_filtered_keys already excludes dividers
        return [kb.name for _i, kb in get_filtered_keys(obj, props)]

    def _seconds_remaining(self):
        elapsed = time.time() - SKP_OT_DeleteFiltered._start_time
        return max(0.0, self._cooldown() - elapsed)

    def execute(self, context):
        if self._seconds_remaining() > 0:
            self.report({'WARNING'}, "Please wait for the cooldown before confirming.")
            return {'CANCELLED'}

        obj = context.active_object
        if not obj or not obj.data or not obj.data.shape_keys:
            return {'CANCELLED'}

        to_delete = SKP_OT_DeleteFiltered._keys_to_delete
        if not to_delete:
            self.report({'INFO'}, "No keys matching the current filter.")
            return {'CANCELLED'}

        for name in to_delete:
            blocks = obj.data.shape_keys.key_blocks
            idx = blocks.find(name)
            if idx < 0:
                continue
            obj.active_shape_key_index = idx
            bpy.ops.object.shape_key_remove(all=False, apply_mix=False)

        self.report({'INFO'}, f"Deleted {len(to_delete)} filtered shape key(s).")
        SKP_OT_DeleteFiltered._keys_to_delete = []
        context.scene.skp_delete_preview.clear()
        return {'FINISHED'}

    def invoke(self, context, event):
        SKP_OT_DeleteFiltered._start_time = time.time()
        SKP_OT_DeleteFiltered._keys_to_delete = self._collect_filtered_keys(context)
        self.key_count = len(SKP_OT_DeleteFiltered._keys_to_delete)

        if self.key_count == 0:
            self.report({'INFO'}, "No keys matching the current filter.")
            return {'CANCELLED'}

        col = context.scene.skp_delete_preview
        col.clear()
        for name in SKP_OT_DeleteFiltered._keys_to_delete:
            item = col.add()
            item.name = name
            item.is_divider = False

        context.scene.skp_delete_preview_index = 0
        context.scene.skp_delete_filter = ""
        return context.window_manager.invoke_props_dialog(self, width=420)

    def draw(self, context):
        layout = self.layout
        remaining = self._seconds_remaining()
        props = context.scene.skp_props

        col = layout.column()
        col.alert = True
        col.label(text=f"Delete {self.key_count} filtered shape key(s)", icon='ERROR')
        col.alert = False

        layout.separator(factor=0.3)
        query = props.search_filter.strip()
        if query:
            layout.label(text=f'Matching filter: "{query}"')
        active_cat = props.category_filter
        if active_cat and active_cat != 'ALL':
            layout.label(text=f"Within category: {active_cat}")
        layout.label(text="These keys will be permanently removed. This cannot be undone easily.")

        layout.separator(factor=0.5)

        toggle_icon = 'TRIA_DOWN' if self.show_keys_toggle else 'TRIA_RIGHT'
        toggle_text = (
            f"Hide keys to be deleted ({self.key_count})"
            if self.show_keys_toggle else
            f"Show keys to be deleted ({self.key_count})"
        )
        layout.prop(self, "show_keys_toggle", text=toggle_text, toggle=True, icon=toggle_icon)

        if self.show_keys_toggle:
            row = layout.row(align=True)
            row.prop(context.scene, "skp_delete_filter", text="", icon='VIEWZOOM',
                     placeholder="Filter keys...")
            if context.scene.skp_delete_filter:
                row.operator("skp.delete_filter_clear", text="", icon='X')

            rows = max(5, min(30, self.key_count))
            layout.template_list(
                "SKP_UL_delete_preview", "",
                context.scene, "skp_delete_preview",
                context.scene, "skp_delete_preview_index",
                rows=rows,
            )

        layout.separator(factor=0.5)

        if remaining > 0:
            secs = int(remaining) + 1
            warn_row = layout.row()
            warn_row.alert = True
            warn_row.label(text=f"OK available in {secs}s - read the warning above", icon='TIME')
        else:
            layout.label(text="Cooldown complete. Click OK to confirm.", icon='CHECKMARK')


class SKP_OT_PageNext(Operator):
    bl_idname = "skp.page_next"
    bl_label = "Next Page"

    def execute(self, context):
        props = context.scene.skp_props
        obj = context.active_object
        filtered = get_filtered_keys(obj, props)
        max_page = total_pages(filtered, props.page_size) - 1
        props.current_page = min(props.current_page + 1, max_page)
        return {'FINISHED'}


class SKP_OT_PagePrev(Operator):
    bl_idname = "skp.page_prev"
    bl_label = "Previous Page"

    def execute(self, context):
        props = context.scene.skp_props
        props.current_page = max(props.current_page - 1, 0)
        return {'FINISHED'}


class SKP_OT_PageFirst(Operator):
    bl_idname = "skp.page_first"
    bl_label = "First Page"

    def execute(self, context):
        context.scene.skp_props.current_page = 0
        return {'FINISHED'}


class SKP_OT_PageLast(Operator):
    bl_idname = "skp.page_last"
    bl_label = "Last Page"

    def execute(self, context):
        props = context.scene.skp_props
        obj = context.active_object
        filtered = get_filtered_keys(obj, props)
        props.current_page = total_pages(filtered, props.page_size) - 1
        return {'FINISHED'}


class SKP_OT_CopyKeyName(Operator):
    """Copy the shape key name to the clipboard."""
    bl_idname = "skp.copy_key_name"
    bl_label = "Copy Name"

    key_name: StringProperty()

    def execute(self, context):
        context.window_manager.clipboard = self.key_name
        self.report({'INFO'}, f"Copied: {self.key_name}")
        return {'FINISHED'}


class SKP_OT_SelectAndPreview(Operator):
    """Click to select a shape key. Click again on the active key to deselect
    (zeroes its value and returns focus to Basis).
    Hold Shift and click to enable multiple keys at once without resetting the
    others (toggles this key only; shift-clicking a non-zero key zeros it).
    Category dividers are never passed to this operator."""
    bl_idname = "skp.select_and_preview"
    bl_label = "Select Key"

    key_name: StringProperty()
    extend: BoolProperty(
        name="Extend",
        description="If true, toggle this key without resetting others (shift-click)",
        default=False,
        options={'SKIP_SAVE'},
    )

    def invoke(self, context, event):
        self.extend = event.shift
        return self.execute(context)

    def execute(self, context):
        obj = context.active_object
        props = context.scene.skp_props

        if not obj or not obj.data or not obj.data.shape_keys:
            return {'CANCELLED'}

        # Safety guard
        if is_category_divider(self.key_name):
            return {'CANCELLED'}

        blocks = obj.data.shape_keys.key_blocks
        target_idx = blocks.find(self.key_name)
        if target_idx < 0:
            return {'CANCELLED'}

        current_idx = obj.active_shape_key_index
        already_active = (current_idx == target_idx)
        kb = blocks[target_idx]

        if self.extend:
            # Shift-click: toggle this key only, leaving every other key untouched.
            # Focus moves to this key so the preview slider + step cursor target it.
            obj.active_shape_key_index = target_idx
            filtered = get_filtered_keys(obj, props)
            for fi, (_, k) in enumerate(filtered):
                if k.name == self.key_name:
                    props.step_index = fi
                    break

            if kb.value > 0.0:
                kb.value = 0.0
            else:
                kb.value = props.preview_value
            return {'FINISHED'}

        if already_active:
            # Deselect: zero this key's value and move active back to Basis
            kb.value = 0.0
            basis_idx = blocks.find('Basis')
            obj.active_shape_key_index = basis_idx if basis_idx >= 0 else 0
            props.step_index = -1
        else:
            # Select normally
            obj.active_shape_key_index = target_idx
            filtered = get_filtered_keys(obj, props)
            for fi, (_, k) in enumerate(filtered):
                if k.name == self.key_name:
                    props.step_index = fi
                    break

            if props.auto_preview:
                bpy.ops.skp.preview_key(key_name=self.key_name)

        return {'FINISHED'}


class SKP_OT_StepNext(Operator):
    """Move to the next shape key in the filtered list"""
    bl_idname = "skp.step_next"
    bl_label = "Next Shape Key"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        return _apply_step(context, +1)


class SKP_OT_StepPrev(Operator):
    """Move to the previous shape key in the filtered list"""
    bl_idname = "skp.step_prev"
    bl_label = "Previous Shape Key"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        return _apply_step(context, -1)


class SKP_OT_ArrowKeyModal(Operator):
    """
    Toggle Up/Down arrow key navigation for shape keys.
    Click once to activate - arrow keys step through the filtered list.
    Click again or press ESC to deactivate.
    """
    bl_idname = "skp.arrow_key_modal"
    bl_label = "Toggle Arrow Key Navigation"

    _running = False

    def modal(self, context, event):
        obj = context.active_object
        if not obj or not obj.data or not obj.data.shape_keys:
            self._finish(context)
            return {'CANCELLED'}

        if event.type == 'DOWN_ARROW' and event.value == 'PRESS':
            _apply_step(context, +1)
            self._redraw(context)
            return {'RUNNING_MODAL'}

        if event.type == 'UP_ARROW' and event.value == 'PRESS':
            _apply_step(context, -1)
            self._redraw(context)
            return {'RUNNING_MODAL'}

        if event.type == 'ESC' and event.value == 'PRESS':
            self._finish(context)
            return {'CANCELLED'}

        return {'PASS_THROUGH'}

    def invoke(self, context, event):
        if SKP_OT_ArrowKeyModal._running:
            self._finish(context)
            return {'CANCELLED'}

        context.window_manager.modal_handler_add(self)
        SKP_OT_ArrowKeyModal._running = True
        self.report({'INFO'}, "Arrow nav ON - Up/Down to step through shape keys, ESC to stop")
        self._redraw(context)
        return {'RUNNING_MODAL'}

    def _finish(self, context):
        SKP_OT_ArrowKeyModal._running = False
        self.report({'INFO'}, "Arrow key navigation OFF")
        self._redraw(context)

    @staticmethod
    def _redraw(context):
        for area in context.screen.areas:
            if area.type == 'PROPERTIES':
                area.tag_redraw()


# -----------------------------------------
#  Panel
# -----------------------------------------

class SKP_PT_MainPanel(Panel):
    bl_label = "Dalek's Shapekey Manager"
    bl_idname = "SKP_PT_main"
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = "data"
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 1

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (
            obj is not None
            and obj.type == 'MESH'
            and obj.data.shape_keys is not None
        )

    def draw(self, context):
        layout = self.layout
        obj = context.active_object
        props = context.scene.skp_props

        # Single cache fetch covers every derived view we need below
        # (categories, full_info, tree, top_labels, member/delete counts,
        # total real-key count). poll() guarantees shape_keys exists.
        cache = _get_cache(obj)
        has_categories = cache['has_categories']
        categories = cache['categories']
        # total_real excludes Basis; add it back when the user has opted to
        # see it so Total and Shown line up with no filter applied.
        total_real = cache['total_real'] + (1 if props.show_basis and cache.get('has_basis') else 0)

        filtered = get_filtered_keys(obj, props)
        total_filtered = len(filtered)

        row = layout.row()
        row.label(
            text=f"Total: {total_real}  |  Shown: {total_filtered}"
                 + (f"  |  Categories: {len(categories)}" if has_categories else ""),
            icon='SHAPEKEY_DATA',
        )

        layout.separator(factor=0.5)

        # Preview Settings
        box = layout.box()
        box.label(text="Preview Settings", icon='PLAY')
        row = box.row(align=True)
        row.prop(props, "preview_value", slider=True)
        row = box.row(align=True)
        row.prop(props, "auto_reset", toggle=True)
        row.prop(props, "auto_preview", toggle=True)
        hint_row = box.row()
        hint_row.enabled = False
        hint_row.label(text="Tip: Shift-click to toggle a key without resetting others", icon='INFO')
        row = box.row()
        row.operator("skp.reset_all", icon='LOOP_BACK', text="Reset All to 0")

        layout.separator(factor=0.5)

        # Step Navigation
        box = layout.box()
        box.label(text="Step Navigation", icon='ANIM')

        num_filtered = len(filtered)
        if num_filtered > 0 and props.step_index >= 0:
            clamped = _resolve_step_index_readonly(obj, props, filtered)
            _, step_kb = filtered[clamped]
            step_label = f"{step_kb.name}  ({clamped + 1} / {num_filtered})"
        elif num_filtered > 0:
            step_label = f"- / {num_filtered}  (click Prev or Next to begin)"
        else:
            step_label = "No keys"

        box.label(text=step_label, icon='LAYER_ACTIVE')

        row = box.row(align=True)
        row.operator("skp.step_prev", text="Prev", icon='TRIA_UP')
        row.operator("skp.step_next", text="Next", icon='TRIA_DOWN')

        arrow_on = SKP_OT_ArrowKeyModal._running
        arrow_row = box.row()
        arrow_row.alert = arrow_on
        arrow_row.operator(
            "skp.arrow_key_modal",
            text="Arrow Keys: ON  (ESC to stop)" if arrow_on else "Enable Arrow Keys",
            icon='CHECKBOX_HLT' if arrow_on else 'CHECKBOX_DEHLT',
            depress=arrow_on,
        )

        layout.separator(factor=0.5)

        # Filter & Sort (merged with category filter when categories exist)
        box = layout.box()
        box.label(text="Filter & Sort", icon='FILTER')

        row = box.row(align=True)
        row.prop(props, "search_filter", text="", icon='VIEWZOOM')

        row = box.row(align=True)
        row.prop(props, "sort_mode", text="")
        row.prop(props, "show_basis", text="Basis", toggle=True)
        row.prop(props, "highlight_nonzero", text="Highlight", toggle=True)

        if has_categories:
            row = box.row(align=True)
            row.prop(props, "category_filter", text="", icon='OUTLINER_COLLECTION')

            # Delete-category button - disabled when "All Categories" is selected
            active_cat = props.category_filter
            is_specific_cat = active_cat and active_cat != 'ALL'
            del_cat_row = box.row(align=True)
            del_cat_row.enabled = is_specific_cat
            if is_specific_cat:
                # Count pre-computed in _rebuild_cache - no extra scan.
                kind, label = _decode_category_filter(active_cat, cache)
                if kind == 'top':
                    n_total = cache['top_delete_counts'].get(label, 0)
                else:
                    n_total = cache['sub_delete_counts'].get(label, 0)
                del_op = del_cat_row.operator(
                    "skp.delete_category",
                    text=f"Delete Category ({n_total})",
                    icon='TRASH',
                )
                del_op.category_name = label
                del_op.category_kind = kind
                del_op.key_count = n_total
                del_op.respect_filter = False
            else:
                del_cat_row.operator(
                    "skp.delete_category",
                    text="Delete Category",
                    icon='TRASH',
                )

        # Delete-Filtered button - only active when a text filter is present,
        # to prevent a misclick wiping out every visible key.
        has_query = bool(props.search_filter.strip())
        del_filt_row = box.row(align=True)
        del_filt_row.enabled = has_query
        if has_query:
            del_filt_op = del_filt_row.operator(
                "skp.delete_filtered",
                text=f"Delete Filtered ({total_filtered})",
                icon='TRASH',
            )
            del_filt_op.key_count = total_filtered
        else:
            del_filt_row.operator(
                "skp.delete_filtered",
                text="Delete Filtered",
                icon='TRASH',
            )

        # Pagination (based on the filtered key count, not display items with headers)
        num_pages = total_pages(filtered, props.page_size)
        # Clamp locally - never write to props inside draw()
        current_page = max(0, min(props.current_page, num_pages - 1))

        page_start = current_page * props.page_size
        page_end = page_start + props.page_size

        row = layout.row(align=True)
        row.operator("skp.page_first", text="", icon='REW')
        row.operator("skp.page_prev",  text="", icon='TRIA_LEFT')
        row.label(text=f"Page {current_page + 1} / {num_pages}")
        row.operator("skp.page_next",  text="", icon='TRIA_RIGHT')
        row.operator("skp.page_last",  text="", icon='FF')

        layout.separator(factor=0.3)

        # Key list - build display items (with optional category headers)
        # We paginate the filtered list first, then inject headers for that slice
        page_keys = filtered[page_start:page_end]

        if not page_keys:
            layout.label(text="No shape keys found in this category.", icon='INFO')
        else:
            col = layout.column(align=True)
            active_idx = obj.active_shape_key_index

            # Inject headers when:
            #   - ALL categories (show both top + sub headers)
            #   - A top-level category is selected (show sub headers within it)
            # Not when a sub-category is selected (single flat group, no headers needed)
            cat_filter = props.category_filter
            _vkind, _vlabel = _decode_category_filter(cat_filter, cache)
            viewing_top = _vkind == 'top' and cat_filter != 'ALL'
            viewing_all = cat_filter == 'ALL'

            inject_headers = (
                props.sort_mode == 'NONE'
                and (viewing_all or viewing_top)
                and has_categories
            )

            full_info = cache['full_info'] if inject_headers else {}
            last_shown_top = object()   # sentinels
            last_shown_sub = object()

            for list_pos, (idx, kb) in enumerate(page_keys):
                # Inject category headers when the top or sub changes
                if inject_headers:
                    d = full_info.get(kb.name, {})
                    this_top = d.get('parent') or ''
                    this_sub = d.get('sub') or ''

                    # Only show top-level header when viewing ALL categories
                    if viewing_all and this_top != last_shown_top:
                        last_shown_top = this_top
                        last_shown_sub = object()  # reset sub sentinel
                        if this_top:
                            top_row = col.row()
                            lbl = top_row.row()
                            lbl.enabled = False
                            lbl.label(text=this_top, icon='OUTLINER_COLLECTION')
                            btn = top_row.row(align=True)
                            btn.alignment = 'RIGHT'
                            top_del = btn.operator(
                                "skp.delete_category", text="", icon='TRASH',
                            )
                            top_del.category_name = this_top
                            top_del.category_kind = 'top'
                            top_del.respect_filter = True
                        elif not this_top and not this_sub:
                            top_row = col.row()
                            top_row.enabled = False
                            top_row.label(text="(uncategorised)", icon='OUTLINER_COLLECTION')

                    # Always show sub-category header when it changes
                    if this_sub and this_sub != last_shown_sub:
                        last_shown_sub = this_sub
                        sub_row = col.row()
                        lbl = sub_row.row()
                        lbl.enabled = False
                        indent = "    " if viewing_all else "  "
                        lbl.label(text=f"{indent}{this_sub}", icon='DOT')
                        btn = sub_row.row(align=True)
                        btn.alignment = 'RIGHT'
                        sub_del = btn.operator(
                            "skp.delete_category", text="", icon='TRASH',
                        )
                        sub_del.category_name = this_sub
                        sub_del.category_kind = 'sub'
                        sub_del.respect_filter = True

                is_active = (idx == active_idx)
                has_value = kb.value > 0.0

                row = col.row(align=True)

                icon = (
                    'LAYER_ACTIVE' if is_active else
                    'KEYFRAME_HLT' if (has_value and props.highlight_nonzero) else
                    'SHAPEKEY_DATA'
                )

                op = row.operator(
                    "skp.select_and_preview",
                    text=kb.name,
                    icon=icon,
                    emboss=not is_active,
                )
                op.key_name = kb.name

                val_col = row.column()
                val_col.ui_units_x = 2.5
                if has_value and props.highlight_nonzero:
                    val_col.alert = True
                val_col.label(text=f"{kb.value:.2f}")

                preview_op = row.operator("skp.preview_key", text="", icon='PLAY')
                preview_op.key_name = kb.name

                copy_op = row.operator("skp.copy_key_name", text="", icon='COPYDOWN')
                copy_op.key_name = kb.name

                apply_op = row.operator("skp.apply_key", text="", icon='CHECKMARK')
                apply_op.key_name = kb.name

                del_op = row.operator("skp.delete_key", text="", icon='X')
                del_op.key_name = kb.name

        layout.separator(factor=0.5)

        # New shape key button
        layout.operator("skp.new_key", text="New Shape Key from Mix", icon='ADD')
        layout.operator("skp.delete_empty_keys", text="Delete Empty Blendshapes", icon='TRASH')

        layout.separator(factor=0.3)
        sub = layout.row()
        sub.prop(props, "page_size", text="Per Page")

        layout.separator(factor=0.3)
        end_idx = min(page_end, total_filtered)
        layout.label(
            text=f"Showing {page_start + 1}-{end_idx} of {total_filtered}",
            icon='INFO',
        )


# -----------------------------------------
#  Config operators
# -----------------------------------------

class SKP_OT_AddDividerPattern(Operator):
    """Add a new category divider pattern."""
    bl_idname = "skp.add_divider_pattern"
    bl_label = "Add Divider Pattern"

    def execute(self, context):
        prefs = context.preferences.addons[__name__].preferences
        item = prefs.divider_patterns.add()
        item.token = "***"
        item.level = 'sub'
        prefs.divider_patterns_index = len(prefs.divider_patterns) - 1
        _bump_prefs_version()
        return {'FINISHED'}


class SKP_OT_RemoveDividerPattern(Operator):
    """Remove this divider pattern."""
    bl_idname = "skp.remove_divider_pattern"
    bl_label = "Remove Divider Pattern"

    index: IntProperty(default=0)

    def execute(self, context):
        prefs = context.preferences.addons[__name__].preferences
        if 0 <= self.index < len(prefs.divider_patterns):
            prefs.divider_patterns.remove(self.index)
            prefs.divider_patterns_index = max(0, self.index - 1)
            _bump_prefs_version()
        return {'FINISHED'}


class SKP_OT_ResetDividerPatterns(Operator):
    """Reset divider patterns to the built-in defaults (=== and ---)."""
    bl_idname = "skp.reset_divider_patterns"
    bl_label = "Reset to Defaults"

    def execute(self, context):
        prefs = context.preferences.addons[__name__].preferences
        prefs.divider_patterns.clear()
        for p in _DEFAULT_PATTERNS:
            item = prefs.divider_patterns.add()
            item.token = p['token']
            item.level = p['level']
        prefs.divider_patterns_index = 0
        _bump_prefs_version()
        self.report({'INFO'}, "Divider patterns reset to defaults.")
        return {'FINISHED'}


# -----------------------------------------
#  Debug diagnostics
# -----------------------------------------
#
# Several visible counters in this panel can disagree when something is
# wrong: the category dropdown's "(N)" suffix, the panel header's Shown,
# the Delete Category "(N)" button, and the row count. These all come
# from different paths: dropdown+delete use member_counts/delete_counts
# computed during a single walk; Shown/row count come from
# get_filtered_keys which re-applies the filter via full_info lookups.
# When those disagree, the cause is usually one of:
#   - duplicate shape-key names (full_info is a dict keyed by name, so
#     later keys overwrite earlier ones, but member_counts increments
#     for every occurrence)
#   - cache staleness we failed to invalidate
#   - divider detection differing between the two code paths
# The debug panel below shows a LIVE independent walk alongside the
# cached numbers so the user can spot the discrepancy and we can
# diagnose from the dump.

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
        print(f"  prefs_version     : {cache['prefs_version']}  (global {_PREFS_VERSION})")
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
            # Find keys whose cached membership is lost via name collision in full_info
            lost = []
            seen = set()
            for i, nm in cache['real_key_entries']:
                if nm in seen:
                    continue
                seen.add(nm)
                info = full_info.get(nm, {})
                if info.get(parent_key) != cat:
                    continue
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
        return SKP_PT_MainPanel.poll(context)

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
        col.label(text=f"prefs_version cache={cache['prefs_version']} global={_PREFS_VERSION}")

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
            # collision in full_info — surface that explicitly.
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


# -----------------------------------------
#  Configuration sub-panel
# -----------------------------------------

class SKP_PT_ConfigPanel(Panel):
    bl_label = "Configuration"
    bl_idname = "SKP_PT_config"
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = "data"
    bl_parent_id = "SKP_PT_main"
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return SKP_PT_MainPanel.poll(context)

    def draw(self, context):
        layout = self.layout
        addon = context.preferences.addons.get(__name__)
        if not addon:
            layout.label(text="Preferences unavailable.", icon='ERROR')
            return
        prefs = addon.preferences

        # --- Category divider patterns ---
        layout.label(text="Category Divider Patterns", icon='OUTLINER_COLLECTION')

        col = layout.column(align=True)
        for i, pattern in enumerate(prefs.divider_patterns):
            row = col.row(align=True)
            row.prop(pattern, "token", text="")
            row.prop(pattern, "level", text="")
            op = row.operator("skp.remove_divider_pattern", text="", icon='X')
            op.index = i

        row = layout.row(align=True)
        row.operator("skp.add_divider_pattern", text="Add Pattern", icon='ADD')
        row.operator("skp.reset_divider_patterns", text="Reset Defaults", icon='LOOP_BACK')

        layout.separator(factor=0.5)

        # --- Other settings ---
        layout.label(text="Other Settings", icon='PREFERENCES')
        layout.prop(prefs, "delete_cooldown")


# -----------------------------------------
#  Registration
# -----------------------------------------

CLASSES = [
    SKP_DividerPattern,
    SKP_AddonPreferences,
    SKP_DeleteKeyItem,
    SKP_UL_DeletePreview,
    SKP_Properties,
    SKP_OT_PreviewKey,
    SKP_OT_ResetAll,
    SKP_OT_ApplyKey,
    SKP_OT_DeleteKey,
    SKP_OT_NewKey,
    SKP_OT_DeleteCategory,
    SKP_OT_DeleteFilterClear,
    SKP_OT_DeleteEmptyKeys,
    SKP_OT_DeleteFiltered,
    SKP_OT_PageNext,
    SKP_OT_PagePrev,
    SKP_OT_PageFirst,
    SKP_OT_PageLast,
    SKP_OT_CopyKeyName,
    SKP_OT_SelectAndPreview,
    SKP_OT_StepNext,
    SKP_OT_StepPrev,
    SKP_OT_ArrowKeyModal,
    SKP_OT_AddDividerPattern,
    SKP_OT_RemoveDividerPattern,
    SKP_OT_ResetDividerPatterns,
    SKP_OT_DebugDump,
    SKP_OT_DebugRebuild,
    SKP_OT_DebugVerify,
    SKP_PT_MainPanel,
    SKP_PT_DebugPanel,
    SKP_PT_ConfigPanel,
]


def _ensure_default_patterns(prefs):
    """Populate divider patterns with defaults if the collection is empty."""
    if not prefs.divider_patterns:
        for p in _DEFAULT_PATTERNS:
            item = prefs.divider_patterns.add()
            item.token = p['token']
            item.level = p['level']


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.skp_props = bpy.props.PointerProperty(type=SKP_Properties)
    bpy.types.Scene.skp_delete_preview = bpy.props.CollectionProperty(type=SKP_DeleteKeyItem)
    bpy.types.Scene.skp_delete_preview_index = bpy.props.IntProperty(default=0)
    bpy.types.Scene.skp_delete_filter = bpy.props.StringProperty(default="")
    # Seed default patterns (only runs if prefs collection is empty)
    addon = bpy.context.preferences.addons.get(__name__)
    if addon:
        _ensure_default_patterns(addon.preferences)


def unregister():
    SKP_OT_ArrowKeyModal._running = False
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
    del bpy.types.Scene.skp_props
    del bpy.types.Scene.skp_delete_preview
    del bpy.types.Scene.skp_delete_preview_index
    del bpy.types.Scene.skp_delete_filter


if __name__ == "__main__":
    register()
