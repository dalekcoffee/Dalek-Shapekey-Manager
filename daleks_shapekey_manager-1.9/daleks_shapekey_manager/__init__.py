# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2025 Dalek (https://dalek.coffee)

"""
Dalek's Shapekey Manager - Blender 5.0 Add-on

Shape keys whose names match ===ANYTHING=== are treated as category
dividers. They are displayed as section headers, never selected, never
previewed, and never modified in any way.

All other keys behave as before: filter, sort, step, arrow-key nav, etc.
A "Category" dropdown appears automatically when at least one category
divider is detected, letting you filter the list to a single category.
"""

# Single source of truth for the addon version. bl_info["version"] is
# derived from this below; blender_manifest.toml must be bumped to match
# on every release. Blender 4.2+ extensions may strip bl_info from the
# runtime module namespace, so other code paths (e.g. the debug dump)
# read _ADDON_VERSION directly.
_ADDON_VERSION = (1, 10, 5)

bl_info = {
    "name": "Dalek's Shapekey Manager",
    "author": "Dalek <https://dalek.coffee>",
    "version": _ADDON_VERSION,
    "blender": (5, 0, 0),
    "location": "Properties > Object Data > Shape Keys > Dalek's Shapekey Manager",
    "description": "Preview, filter, manage and audit large numbers of shape keys",
    "category": "Mesh",
}


def _addon_version_tuple():
    return _ADDON_VERSION

import os
import re
import time
import hashlib
import bpy
import numpy as np
from bpy.props import (
    StringProperty,
    FloatProperty,
    BoolProperty,
    IntProperty,
    EnumProperty,
    PointerProperty,
)
from bpy.types import Panel, Operator, PropertyGroup

# Shared face-tracking target library (Resonite visemes + ARKit/UE names). Pure
# data/logic module with no bpy dependency and no back-reference to this package,
# so it is safe to import at the top (unlike the operator sub-modules spliced in
# at the bottom). It is the single source of truth used by the redundant-key
# protection, the main-list indicator, and any future face-tracking tool.
from . import face_targets


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

    # Last external .blend picked in the Transfer panel's "From .blend file"
    # mode. Lives in preferences (not the scene) so it persists across files
    # and sessions and is offered as the default next time.
    last_reference_file: StringProperty(
        name="Last Reference File",
        description="Most recent external .blend used as a Transfer Reference",
        subtype='FILE_PATH',
        default="",
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


def _get_prefs_version():
    """Live read of the global prefs version.

    Sub-modules must call this rather than `from . import _PREFS_VERSION`:
    that form binds the integer by value at import time and never sees the
    increments from _bump_prefs_version, so any displayed 'global' value
    would be permanently stale at 0."""
    return _PREFS_VERSION


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

    # Count likely face-tracking keys once here (only when the key list changes)
    # rather than re-scanning every panel redraw. is_face_target is itself
    # cached; Basis never matches so it needn't be excluded.
    face_target_count = sum(1 for _i, nm in real_key_entries
                            if face_targets.is_face_target(nm))

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
        'face_target_count': face_target_count,
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


# -----------------------------------------
#  External-file Reference (Transfer "From .blend file" mode)
# -----------------------------------------
#
# These power the Transfer panel's "From .blend file" mode, where the
# Reference mesh is pulled from an external .blend on demand instead of
# having to be present in the current scene. We scan the picked file for
# object names (cached by path+mtime) and expose them as a dropdown; the
# actual temporary append + cleanup lives in ops_sync.

# Sentinel enum id used when no file is picked / no objects are found.
_SKP_REF_NONE = '__SKP_NONE__'

# abspath -> (mtime, [object_name, ...])
_blend_object_scan_cache = {}
# Keep the last enum item tuples alive per file. Blender's EnumProperty
# items callback MUST retain references to the returned strings or they can
# be garbage-collected mid-use, corrupting the dropdown.
_blend_enum_items_cache = {}


def _scan_blend_objects(filepath):
    """Return a list of object names found in an external .blend, cached by
    (abspath, mtime). Returns [] on any error or empty path.

    Only object *names* are available from the library header without fully
    loading; type/vertex-count validation happens at append time instead."""
    if not filepath:
        return []
    try:
        abspath = bpy.path.abspath(filepath)
    except Exception:
        return []
    if not os.path.isfile(abspath):
        return []
    try:
        mtime = os.path.getmtime(abspath)
    except OSError:
        return []
    cached = _blend_object_scan_cache.get(abspath)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        with bpy.data.libraries.load(abspath, link=False) as (data_from, _data_to):
            names = list(data_from.objects)
    except Exception:
        return []
    _blend_object_scan_cache[abspath] = (mtime, names)
    return names


def _reference_file_missing(filepath):
    """True if a non-empty path points at something that isn't a real file."""
    if not filepath:
        return False
    try:
        return not os.path.isfile(bpy.path.abspath(filepath))
    except Exception:
        return True


def _match_object_name(names, target):
    """Return the entry in `names` matching target's name (exact first, then
    case-insensitive), or None if there's no match / no target."""
    if target is None:
        return None
    if target.name in names:
        return target.name
    low = target.name.lower()
    return next((n for n in names if n.lower() == low), None)


def _remember_reference_file(filepath):
    """Persist the last-picked Reference .blend into addon preferences so it
    survives across files and sessions."""
    if not filepath:
        return
    addon = bpy.context.preferences.addons.get(__name__)
    if addon is not None:
        addon.preferences.last_reference_file = filepath


def _get_remembered_reference_file():
    addon = bpy.context.preferences.addons.get(__name__)
    return addon.preferences.last_reference_file if addon is not None else ""


def _sync_reference_object_items(self, context):
    """Dynamic enum: object names inside the picked .blend file."""
    names = _scan_blend_objects(self.sync_reference_file)
    if not names:
        items = [(_SKP_REF_NONE, "(pick a .blend file)",
                  "No objects found - choose a valid .blend file above")]
    else:
        items = [(n, n, f"Use object '{n}' from the file as the Reference")
                 for n in names]
    _blend_enum_items_cache[self.sync_reference_file or ""] = items
    return items


def _sync_reference_file_update(self, context):
    """When the .blend path changes, remember it in preferences and default
    the object selection to the one whose name matches the Target mesh (the
    hybrid auto-pick), else the first object. Falls back silently if nothing
    usable is found."""
    _remember_reference_file(self.sync_reference_file)
    names = _scan_blend_objects(self.sync_reference_file)
    if not names:
        return
    chosen = _match_object_name(names, self.sync_target) or names[0]
    try:
        self.sync_reference_object = chosen
    except TypeError:
        pass


def _sync_reference_mode_update(self, context):
    """Switching reference source invalidates any running preview, so forget
    the active-preview marker. Also, entering 'From .blend file' mode with no
    file yet offers the last file remembered in preferences as the default."""
    self.sync_preview_active = ""
    if self.sync_reference_mode == 'FILE' and not self.sync_reference_file:
        last = _get_remembered_reference_file()
        if last:
            self.sync_reference_file = last  # triggers the file update cb


def _sync_target_update(self, context):
    """When the Target changes in file mode, try to re-point the Reference
    object dropdown at the same-named mesh in the external file. If there's no
    name match, leave the current selection untouched - no error."""
    if self.sync_reference_mode != 'FILE':
        return
    names = _scan_blend_objects(self.sync_reference_file)
    if not names:
        return
    match = _match_object_name(names, self.sync_target)
    if match is None:
        return
    try:
        self.sync_reference_object = match
    except TypeError:
        pass


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

    sync_reference: PointerProperty(
        type=bpy.types.Object,
        name="Reference",
        description=(
            "Read-only source mesh. For 'Delete Extras', this is the mesh "
            "that defines which keys to keep (the smaller 'keep list'). "
            "For 'Copy Missing', this is the mesh that holds the keys to "
            "copy from (the master with all blendshapes)"
        ),
        poll=lambda self, obj: obj.type == 'MESH',
    )

    sync_reference_mode: EnumProperty(
        name="Reference Source",
        description="Where the Reference mesh comes from",
        items=[
            ('SCENE', "In Scene",
             "Pick a Reference mesh that is already present in this file"),
            ('FILE', "From .blend File",
             "Pull the Reference mesh from an external .blend file on demand. "
             "It is loaded into a hidden temporary slot and never saved into "
             "your working file"),
        ],
        default='FILE',
        update=_sync_reference_mode_update,
    )

    sync_reference_file: StringProperty(
        name="Reference File",
        description="External .blend file to pull the Reference mesh from",
        subtype='FILE_PATH',
        default="",
        update=_sync_reference_file_update,
    )

    sync_reference_object: EnumProperty(
        name="Reference Mesh",
        description="Which object inside the .blend file to use as the Reference",
        items=_sync_reference_object_items,
    )

    # Name of the Reference key currently being previewed, so the per-row play
    # button can render as a stop button. Authoritative only in scene mode; in
    # file mode the live preview key on the Target is the source of truth.
    sync_preview_active: StringProperty(
        name="Active Preview Key",
        default="",
        options={'SKIP_SAVE'},
    )

    sync_target: PointerProperty(
        type=bpy.types.Object,
        name="Target",
        description=(
            "Mesh that will be modified. For 'Delete Extras', extras are "
            "removed from this mesh. For 'Copy Missing', missing keys are "
            "written into this mesh (the write target)"
        ),
        poll=lambda self, obj: obj.type == 'MESH',
        update=_sync_target_update,
    )

    sync_filter: StringProperty(
        name="Filter",
        description="Filter reference's shape keys by name (case-insensitive)",
        default="",
        options={'TEXTEDIT_UPDATE'},
    )

    sync_category_filter: EnumProperty(
        name="Category",
        description="Show only reference shape keys belonging to this category",
        items=lambda self, context: build_sync_category_enum(self, context),
    )

    sync_sort_mode: EnumProperty(
        name="Sort",
        items=[
            ('NONE',    "Default",       "Reference's original key order"),
            ('AZ',      "A to Z",        "Alphabetical ascending"),
            ('ZA',      "Z to A",        "Alphabetical descending"),
            ('MISSING', "Missing First", "Keys missing from target first"),
        ],
        default='NONE',
    )

    sync_show_only_missing: BoolProperty(
        name="Only Missing",
        description="Hide keys that already exist on the target mesh",
        default=False,
    )

    sync_skip_existing: BoolProperty(
        name="Skip Existing",
        description=(
            "When doing a bulk copy, skip keys that already exist on the "
            "target instead of overwriting them. Per-row Copy buttons "
            "always overwrite, regardless of this toggle"
        ),
        default=True,
    )

    sync_match_groups: BoolProperty(
        name="Match Groups",
        description=(
            "When copying, place each key under the same category divider "
            "(group) it has on the Reference. If the group's divider doesn't "
            "exist on the Target, you'll be asked / it can be created"
        ),
        default=True,
    )

    sync_create_groups: BoolProperty(
        name="Create Missing Groups",
        description=(
            "When 'Match Groups' is on and a key's category divider is missing "
            "on the Target, create it (as an empty divider shape key). Turn off "
            "to leave such keys ungrouped instead. Used by the bulk copy "
            "actions; the single-key copy asks each time a new group is needed"
        ),
        default=True,
    )

    sync_current_page: IntProperty(
        name="Transfer Page",
        default=0,
        min=0,
    )


# -----------------------------------------
#  Helpers
# -----------------------------------------

def _filter_sort_keys(obj, search, category_filter, sort_mode, show_basis):
    """
    Pure filter+sort core. Returns list of (original_index, shape_key) tuples.
    Category divider entries are NEVER included - they only affect the
    category mapping used by the category_filter.

    sort_mode may be any of: 'NONE', 'AZ', 'ZA', 'VALUE', 'NONZERO'. Unknown
    modes fall through to original order. Callers (sync browser) that want
    a custom sort like 'MISSING' apply it themselves after this returns.
    """
    cache = _get_cache(obj)
    if cache is None:
        return []

    blocks = obj.data.shape_keys.key_blocks

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

    if category_filter and category_filter != 'ALL':
        full_info = cache['full_info']
        kind, label = _decode_category_filter(category_filter, cache)
        parent_key = 'parent' if kind == 'top' else 'sub'
        entries = [(i, n, kb) for i, n, kb in entries
                   if full_info.get(n, {}).get(parent_key) == label]

    query = (search or "").strip().lower()
    if query:
        entries = [(i, n, kb) for i, n, kb in entries if query in kb.name.lower()]

    keys = [(i, kb) for i, _n, kb in entries]

    if sort_mode == 'AZ':
        keys.sort(key=lambda x: x[1].name.lower())
    elif sort_mode == 'ZA':
        keys.sort(key=lambda x: x[1].name.lower(), reverse=True)
    elif sort_mode == 'VALUE':
        keys.sort(key=lambda x: x[1].value, reverse=True)
    elif sort_mode == 'NONZERO':
        keys.sort(key=lambda x: (0 if x[1].value > 0 else 1, x[0]))

    return keys


def get_filtered_keys(obj, props):
    """Filter+sort the active object's shape keys for the main Manager panel."""
    return _filter_sort_keys(
        obj,
        props.search_filter,
        props.category_filter,
        props.sort_mode,
        props.show_basis,
    )


def get_sync_filtered_keys(reference, target, props):
    """Filter+sort the reference mesh's shape keys for the Transfer panel
    browser.

    Applies sync_filter, sync_category_filter, and sync_sort_mode. When
    sync_show_only_missing is true, hides keys that already exist on target
    (so the browser shows ONLY keys missing from target). Adds a 'MISSING'
    sort mode that puts missing-on-target first, original order otherwise.
    """
    base_sort = props.sync_sort_mode if props.sync_sort_mode != 'MISSING' else 'NONE'
    keys = _filter_sort_keys(
        reference,
        props.sync_filter,
        props.sync_category_filter,
        base_sort,
        show_basis=False,
    )
    if props.sync_show_only_missing or props.sync_sort_mode == 'MISSING':
        tgt_names = _shape_key_names(target, exclude_basis=False)
        if props.sync_show_only_missing:
            keys = [(i, kb) for i, kb in keys if kb.name not in tgt_names]
        if props.sync_sort_mode == 'MISSING':
            keys.sort(key=lambda x: (0 if x[1].name not in tgt_names else 1))
    return keys


def total_pages(filtered, page_size):
    if not filtered:
        return 1
    return max(1, (len(filtered) + page_size - 1) // page_size)


def _shape_key_names(obj, exclude_basis=True, exclude_dividers=False):
    """Return the set of shape-key names on obj. Empty set if no keys.

    Lives here (not in ops_transfer) because get_sync_filtered_keys above
    needs it for the 'Only Missing' filter, and PropertyGroup definitions
    can't reach across to sub-modules without circular-import gymnastics."""
    if obj is None or obj.data is None or obj.data.shape_keys is None:
        return set()
    sk = obj.data.shape_keys
    # Exclude the actual reference (rest) key by identity, not the literal name
    # "Basis" - MMD/imported rigs sometimes name the base key differently.
    basis_name = sk.reference_key.name if sk.reference_key else "Basis"
    out = set()
    for kb in sk.key_blocks:
        if exclude_basis and kb.name == basis_name:
            continue
        if exclude_dividers and is_category_divider(kb.name):
            continue
        out.add(kb.name)
    return out


def _active_key_name(obj):
    """Name of the active shape key, or None if active_shape_key_index is
    out of range. After an undo that shrinks key_blocks past the active
    index, raw subscripting throws IndexError - guarding here keeps the
    panel paintable."""
    blocks = obj.data.shape_keys.key_blocks
    idx = obj.active_shape_key_index
    if 0 <= idx < len(blocks):
        return blocks[idx].name
    return None


def _resolve_step_index_readonly(obj, props, filtered):
    """Read-only version safe to call from draw(). Never writes to props."""
    if not filtered:
        return 0
    if props.step_index < 0:
        active_name = _active_key_name(obj)
        if active_name is not None:
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
        active_name = _active_key_name(obj)
        if active_name is not None:
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

# -----------------------------------------
#  Cooldown plumbing shared by destructive dialogs
# -----------------------------------------
#
# Every destructive confirmation dialog (Delete Category, Delete Empty,
# Delete Filtered, Sync Delete Extras, Sync Copy Missing, Sync Copy
# Filtered, Preset Apply) wants the same thing: read the addon's
# delete_cooldown preference, stamp invoke() with a start time, gate
# execute() until the cooldown elapses, and paint a "OK available in Ns"
# footer in draw(). Before this mixin existed, all of that was copy-
# pasted into each operator - changing the wording or the source of the
# cooldown value meant editing seven places.
#
# `_start_time` is an instance attribute (set in each operator's invoke);
# the class-level 0.0 default below is a defensive fallback for the
# unlikely case execute is called without a prior invoke (e.g. via the
# operator search bar). With `_start_time == 0.0` the remaining time is
# very negative, max(0, ...) clamps to 0, so the cooldown is effectively
# skipped - which matches the pre-mixin behaviour.

def _get_delete_cooldown() -> float:
    """Read the configured cooldown (in seconds) from addon preferences.
    Falls back to 5.0 if the addon's preferences aren't yet registered
    (can happen during the initial register() pass)."""
    addon = bpy.context.preferences.addons.get(__name__)
    if addon:
        return addon.preferences.delete_cooldown
    return 5.0


class _CooldownMixin:
    """Mixin providing shared cooldown state + cooldown-footer rendering
    for the destructive confirmation dialogs.

    Usage in a subclass:
        def invoke(self, context, event):
            self._start_time = time.time()
            ...
            return context.window_manager.invoke_props_dialog(self, width=...)

        def draw(self, context):
            ...
            self._draw_cooldown_footer(layout)

        def execute(self, context):
            if self._seconds_remaining() > 0:
                self.report({'WARNING'}, "Please wait for the cooldown before confirming.")
                return {'CANCELLED'}
            ...
    """

    # Class-level fallback so getattr-style reads from execute-without-invoke
    # paths see a sane value. Subclasses set self._start_time in invoke().
    _start_time: float = 0.0

    @staticmethod
    def _cooldown() -> float:
        return _get_delete_cooldown()

    def _seconds_remaining(self) -> float:
        return max(0.0, self._cooldown() - (time.time() - self._start_time))

    def _draw_cooldown_footer(self, layout, *, include_warning_hint: bool = True):
        """Render the "OK available in Ns" / "Cooldown complete" footer.

        `include_warning_hint=True` adds "- read the warning above" to the
        countdown text, used by the destructive delete dialogs where the
        warning is the focal point. Copy/apply dialogs pass False because
        they lead with their own breakdown row instead of an alert."""
        remaining = self._seconds_remaining()
        if remaining > 0:
            secs = int(remaining) + 1
            row = layout.row()
            row.alert = True
            text = (
                f"OK available in {secs}s - read the warning above"
                if include_warning_hint else
                f"OK available in {secs}s"
            )
            row.label(text=text, icon='TIME')
        else:
            layout.label(text="Cooldown complete. Click OK to confirm.", icon='CHECKMARK')



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

        sk = obj.data.shape_keys
        blocks = sk.key_blocks
        basis_name = sk.reference_key.name if sk.reference_key else "Basis"

        if props.auto_reset and not self.extend:
            for kb in blocks:
                if kb.name != basis_name and not is_category_divider(kb.name):
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
        sk = obj.data.shape_keys
        basis_name = sk.reference_key.name if sk.reference_key else "Basis"
        for kb in sk.key_blocks:
            if kb.name != basis_name and not is_category_divider(kb.name):
                kb.value = 0.0
        self.report({'INFO'}, "All shape keys reset to 0.")
        return {'FINISHED'}




def _require_object_mode(obj, action_label="modify shape keys"):
    """Return (ok, msg). bpy.ops.object.shape_key_remove polls for OBJECT
    mode; calling it from Edit/Sculpt/Paint raises an opaque RuntimeError.
    We refuse rather than auto-switching because the panel acts on the
    active object and flipping the user's mode behind their back loses
    selection / unsaved sculpt state. _sync_delete_target_keys handles
    the mode switch itself because it acts on a non-active target."""
    if obj is None:
        return False, "No active object."
    if obj.mode != 'OBJECT':
        return False, f"Switch to Object Mode to {action_label}."
    return True, ""


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

        ok, msg = _require_object_mode(obj, "apply shape keys")
        if not ok:
            self.report({'WARNING'}, msg)
            return {'CANCELLED'}

        sk = obj.data.shape_keys
        blocks = sk.key_blocks
        idx = blocks.find(self.key_name)
        if idx < 0:
            self.report({'WARNING'}, f"Shape key '{self.key_name}' not found.")
            return {'CANCELLED'}

        key = blocks[idx]
        value = key.value

        # Bake this key's deformation at its CURRENT value into the mesh, then
        # remove the key. We can't use Blender's shape_key_remove(apply_mix=True)
        # for this: apply_mix only bakes when all=True, so for a single key it
        # silently removed the key WITHOUT baking - i.e. the key was effectively
        # applied at value 0 regardless of its slider (the reported bug).
        #
        # Do the delta math ourselves. The key's contribution at vertex i is
        # value * (key_co - relative_key_co). We push that delta into the Basis
        # (and the base mesh), and add the same delta to every OTHER remaining
        # key so their visual effect relative to the new Basis is unchanged.
        relkey = key.relative_key or sk.reference_key
        nverts = len(obj.data.vertices)
        kco = np.empty(nverts * 3, dtype=np.float32)
        rco = np.empty(nverts * 3, dtype=np.float32)
        key.data.foreach_get("co", kco)
        relkey.data.foreach_get("co", rco)
        delta = value * (kco - rco)

        if delta.any():
            buf = np.empty(nverts * 3, dtype=np.float32)
            for kb in blocks:
                if kb == key:
                    continue
                kb.data.foreach_get("co", buf)
                kb.data.foreach_set("co", buf + delta)
            obj.data.vertices.foreach_get("co", buf)
            obj.data.vertices.foreach_set("co", buf + delta)
            obj.data.update()

        obj.active_shape_key_index = idx
        try:
            bpy.ops.object.shape_key_remove(all=False, apply_mix=False)
        except RuntimeError as err:
            self.report({'ERROR'}, f"Apply failed: {err}")
            return {'CANCELLED'}
        self.report(
            {'INFO'},
            f"Applied shape key '{self.key_name}' at value {value:.3f}.",
        )
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

        ok, msg = _require_object_mode(obj, "delete shape keys")
        if not ok:
            self.report({'WARNING'}, msg)
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


class SKP_RedundantItem(bpy.types.PropertyGroup):
    """One row in the redundant-blendshapes review list - either a group
    header or a member key carrying a per-key delete toggle and the category
    it belongs to."""
    name: StringProperty()
    category: StringProperty()
    is_header: BoolProperty(default=False)
    group_id: IntProperty(default=0)
    group_size: IntProperty(default=0)
    # 'exact'  -> byte-identical geometry; 'split' -> full = left + right
    group_kind: StringProperty(default='exact')
    # For split members: 'full' / 'left' / 'right'. Empty for exact members.
    role: StringProperty(default='')
    # True when the name matches a Resonite viseme / ARKit face-tracking target.
    protected: BoolProperty(default=False)
    # Short reason shown next to a protected row (e.g. "Resonite viseme: AA").
    protect_reason: StringProperty(default='')
    delete: BoolProperty(
        name="Delete",
        description="Mark this key for deletion. Leave at least one key "
                    "per group unchecked to keep that shape",
        default=False,
    )


class SKP_PresetKeyItem(bpy.types.PropertyGroup):
    """One shape-key name stored in a preset. The preset records names only -
    the actual shape key data lives on the Reference mesh."""
    name: StringProperty()


class SKP_Preset(bpy.types.PropertyGroup):
    """A named set of shape-key names that describes a target-mesh
    configuration. Applying a preset uses the Reference mesh as the source
    of shape-key data and snaps the Target to exactly match the preset's
    list (add missing, delete extras)."""
    name: StringProperty(
        name="Preset Name",
        description="Identifier for this preset (shown in the preset list)",
        default="Preset",
    )
    keys: bpy.props.CollectionProperty(type=SKP_PresetKeyItem)
    source_reference: StringProperty(
        name="Captured From",
        description="Name of the mesh whose key list was captured (informational)",
        default="",
    )


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


class SKP_UL_RedundantGroups(bpy.types.UIList):
    """Grouped review list for redundant shape keys. Header rows label each
    group of identical keys; member rows carry a Delete checkbox followed by
    the key name and the category it belongs to."""
    bl_idname = "SKP_UL_redundant_groups"

    def draw_item(self, context, layout, data, item, icon,
                  active_data, active_propname, index=0, flt_flag=0):
        if item.is_header:
            row = layout.row()
            row.enabled = False
            if item.group_kind == 'split':
                row.label(text=f"{item.name}  -  full = left + right",
                          icon='MOD_MIRROR')
            elif item.group_kind == 'namesplit':
                row.label(text=f"{item.name}  -  parent + L + R by name",
                          icon='SORTALPHA')
            elif item.group_kind == 'similar':
                row.label(text=f"{item.name}  -  {item.group_size} similar keys",
                          icon='DUPLICATE')
            else:
                row.label(text=f"{item.name}  -  {item.group_size} identical keys",
                          icon='DUPLICATE')
            return

        row = layout.row(align=True)
        row.prop(item, "delete", text="")

        # Eye button: click toggles an isolate-preview of this key in the
        # viewport; the active one is shown depressed (open eye).
        is_previewing = (context.scene.skp_redundant_preview_active == item.name)
        prev_op = row.operator(
            "skp.redundant_preview", text="",
            icon='HIDE_OFF' if is_previewing else 'HIDE_ON',
            depress=is_previewing,
        )
        prev_op.key_name = item.name

        # Tag split members with their role so the relationship is legible.
        tag = {'full': "  [full]", 'left': "  [L]", 'right': "  [R]"}.get(item.role, "")
        name_col = row.row()
        name_col.alert = item.delete  # red when marked for deletion
        # Protected face-tracking keys get the yellow warning triangle in place
        # of the normal shapekey icon, so they stand out at a glance.
        name_icon = 'ERROR' if item.protected else 'SHAPEKEY_DATA'
        name_col.label(text=f"{item.name}{tag}", icon=name_icon)

        # Protected rows show why (viseme / ARKit) in a dim warning column.
        if item.protected:
            warn_col = row.row()
            warn_col.alignment = 'RIGHT'
            warn_col.alert = True
            warn_col.label(text=item.protect_reason, icon='ERROR')

        cat_col = row.row()
        cat_col.alignment = 'RIGHT'
        cat_col.enabled = False
        cat_col.label(text=item.category, icon='OUTLINER_COLLECTION')

    def draw_filter(self, context, layout):
        # No filter bar - filtering would break the group/header layout.
        pass

    def filter_items(self, context, data, propname):
        # Show every row in natural order; grouping must stay intact.
        return [], []


class SKP_OT_RedundantSelect(Operator):
    """Bulk-set the delete checkboxes in the redundant review list."""
    bl_idname = "skp.redundant_select"
    bl_label = "Set Redundant Selection"
    bl_options = {'INTERNAL'}

    mode: EnumProperty(
        items=[
            ('DEFAULT', "Recommended",
             "Exact groups: keep the first key, delete the rest. "
             "Split groups: delete the full key, keep both halves"),
            ('NONE', "Keep All", "Uncheck every key (delete nothing)"),
            ('INVERT', "Invert", "Invert every delete checkbox"),
        ],
        default='DEFAULT',
        options={'SKIP_SAVE'},
    )

    def execute(self, context):
        items = context.scene.skp_redundant_items
        if self.mode == 'NONE':
            for it in items:
                if not it.is_header:
                    it.delete = False
        elif self.mode == 'INVERT':
            for it in items:
                if not it.is_header:
                    it.delete = not it.delete
        else:  # DEFAULT - the recommended per-group selection
            # Group members so the per-group protection rule can see siblings.
            groups = {}
            order = []
            for it in items:
                if it.is_header:
                    continue
                key = (it.group_kind, it.group_id)
                if key not in groups:
                    groups[key] = []
                    order.append(key)
                groups[key].append(it)

            for key in order:
                kind, _gid = key
                members = groups[key]
                if kind in ('split', 'namesplit'):
                    # Keep both halves; drop the full (reproducible from L + R) -
                    # unless the full is a protected face-tracking key.
                    for m in members:
                        m.delete = (m.role == 'full' and not m.protected)
                elif any(m.protected for m in members):
                    # Duplicates of a tracked shape: keep the protected
                    # (correctly-named) copies, delete the redundant rest.
                    for m in members:
                        m.delete = not m.protected
                else:
                    # Exact/similar: keep the first occurrence, delete clones.
                    for pos, m in enumerate(members):
                        m.delete = pos > 0
        return {'FINISHED'}


class SKP_OT_DeleteCategory(_CooldownMixin, Operator):
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

    # Keys to delete are collected in invoke() and stored at class level so
    # draw() and execute() can read them across the dialog's lifetime.
    # _start_time lives on the mixin (instance attr, class fallback).
    _keys_to_delete: list = []

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

    def execute(self, context):
        if self._seconds_remaining() > 0:
            self.report({'WARNING'}, "Please wait for the cooldown before confirming.")
            return {'CANCELLED'}

        obj = context.active_object
        if not obj or not obj.data or not obj.data.shape_keys:
            return {'CANCELLED'}

        ok, msg = _require_object_mode(obj, "delete shape keys")
        if not ok:
            self.report({'WARNING'}, msg)
            return {'CANCELLED'}

        # Recompute from current state rather than trusting the list captured at
        # invoke: the modal dialog blocks edits in between (so this matches what
        # was shown) and it makes redo-after-undo re-derive the right keys
        # instead of finding a cleared class list.
        to_delete = self._collect_keys(context)
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
        self._start_time = time.time()
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

        self._draw_cooldown_footer(layout)


class SKP_OT_DeleteFilterClear(Operator):
    """Clear the delete preview search filter."""
    bl_idname = "skp.delete_filter_clear"
    bl_label = "Clear Filter"

    def execute(self, context):
        context.scene.skp_delete_filter = ""
        return {'FINISHED'}


def _is_key_empty(obj, kb):
    """Return True if every vertex in this shape key matches the reference
    key (Basis).

    Vectorised with numpy: each coordinate buffer is pulled in a single
    C-level foreach_get and the per-vertex squared distance is compared in
    bulk, instead of a Python loop over every vertex. For a genuinely empty
    key (the case the old early-return couldn't shortcut) this is orders of
    magnitude faster on dense meshes - the common situation for the
    'Delete Empty' scan, which tests every key on the mesh."""
    ref = obj.data.shape_keys.reference_key
    if kb.name == ref.name:
        return False
    n = len(kb.data)
    if n == 0 or n != len(ref.data):
        # Mismatched vertex counts can't be a no-op clone of Basis; an
        # empty buffer trivially matches. Either way, nothing to delete.
        return n == 0
    sk_co = np.empty(n * 3, dtype=np.float32)
    ref_co = np.empty(n * 3, dtype=np.float32)
    kb.data.foreach_get('co', sk_co)
    ref.data.foreach_get('co', ref_co)
    # float64 accumulation so the per-vertex squared distance matches the
    # old mathutils length_squared comparison against the 1e-10 threshold.
    diff = sk_co.astype(np.float64) - ref_co.astype(np.float64)
    max_sq = float((diff * diff).reshape(n, 3).sum(axis=1).max())
    return max_sq <= 1e-10


def _category_label_for(cache, name):
    """Human-readable category for a key, e.g. 'VRC / Eyes', 'VRC', or
    '(uncategorised)' when the key sits under no divider."""
    d = cache['full_info'].get(name, {})
    top = d.get('parent') or ''
    sub = d.get('sub') or ''
    if top and sub:
        return f"{top} / {sub}"
    if top:
        return top
    if sub:
        return sub
    return "(uncategorised)"


# -----------------------------------------
#  Resonite / ARKit face-tracking protection
# -----------------------------------------
#
# The face-target library (which shape-key names Resonite auto-binds for
# visemes / eye / expression tracking) lives in the shared `face_targets`
# sub-module, loaded from the bundled resonite-face-targets.json so the data is
# a single source of truth across every feature. The redundant-key tool uses
# `_face_target_info` below to flag and protect those keys; the main panel shows
# a per-row indicator; future tools can `from . import face_targets` directly
# (see that module's docstring for the full API, incl. coverage helpers).
_face_target_info = face_targets.face_target_info


def _find_redundant_groups(obj):
    """Group non-empty shape keys that share byte-identical vertex geometry.

    Two keys 'do the same exact thing' when their absolute vertex coordinate
    buffers are identical. For avatar rigs - where every key is relative to
    Basis - identical 'co' means identical deformation, so comparing absolute
    coordinates is both correct and consistent with _is_key_empty.

    Keys are bucketed by a 128-bit hash of their float32 'co' buffer, so
    detection is O(n) in keys with a single buffer held in memory at a time.
    Keys with no displacement from Basis are skipped - the dedicated 'Delete
    Empty' tool covers those, and folding every empty key into one giant
    'redundant' group would just be noise.

    Returns a list of groups (only those with >=2 members), each a list of
    (index, name) in original key order, with the groups themselves ordered
    by first appearance."""
    sk = obj.data.shape_keys
    blocks = sk.key_blocks
    ref = sk.reference_key
    cache = _get_cache(obj)
    if cache is None or ref is None:
        return []

    n = len(ref.data)
    if n == 0:
        return []

    basis = np.empty(n * 3, dtype=np.float32)
    ref.data.foreach_get('co', basis)
    basis64 = basis.astype(np.float64)

    buckets = {}
    for i, name in cache['real_key_entries']:
        kb = blocks[i]
        if kb.name == ref.name:
            continue
        if len(kb.data) != n:
            # Different vertex count can't be compared coordinate-for-coordinate
            continue

        buf = np.empty(n * 3, dtype=np.float32)
        kb.data.foreach_get('co', buf)

        # Skip empty keys (no displacement from Basis) using the same threshold
        # as _is_key_empty so the two tools agree on what 'empty' means.
        diff = buf.astype(np.float64) - basis64
        if float((diff * diff).reshape(n, 3).sum(axis=1).max()) <= 1e-10:
            continue

        digest = hashlib.blake2b(buf.tobytes(), digest_size=16).digest()
        buckets.setdefault(digest, []).append((i, name))

    groups = [m for m in buckets.values() if len(m) >= 2]
    groups.sort(key=lambda m: m[0][0])
    return groups


# Pairing more one-sided keys than this is skipped to avoid a UI stall on
# pathological meshes (every extra one-sided key multiplies the work).
_SPLIT_PAIR_LIMIT = 300000
# Note surfaced when split detection bailed early (so the UI can say so rather
# than silently showing zero splits).
_LAST_SPLIT_NOTE = {'text': ''}
# Quantisation tolerance for matching a summed pair against a full key. Left/
# right split tools often blend across the seam, so delta_L + delta_R only
# approximately equals delta_full; rounding to this grid absorbs that noise.
_SPLIT_TOL = 1e-5


def _find_split_groups(obj, exclude_names=frozenset()):
    """Find 'split' redundancy: a full shape key whose displacement equals the
    sum of a left-side key and a right-side key (delta_full = delta_L + delta_R).

    These triples are redundant because the two halves reproduce the full shape
    (drive both to 1), so one of the three keys carries no unique geometry.

    Each one-sided pair is summed and matched against the full keys by a
    quantised hash, so a smooth seam blend - where neither half equals a hard
    left/right mask - is still caught. The mirror plane is X=0, the universal
    avatar symmetry axis; meshes not symmetric across X simply yield no splits.

    exclude_names skips keys already reported elsewhere (e.g. exact-duplicate
    members) so a key is never listed in two places. Returns a list of
    (full_index, full_name, left_name, right_name), ordered by the full key's
    position."""
    sk = obj.data.shape_keys
    blocks = sk.key_blocks
    ref = sk.reference_key
    _LAST_SPLIT_NOTE['text'] = ''
    cache = _get_cache(obj)
    if cache is None or ref is None:
        return []

    n = len(ref.data)
    if n == 0:
        return []

    basis = np.empty(n * 3, dtype=np.float32)
    ref.data.foreach_get('co', basis)
    xcol = basis.astype(np.float64).reshape(n, 3)[:, 0]
    left_idx = np.where(xcol < 0.0)[0]
    right_idx = np.where(xcol >= 0.0)[0]   # seam (x==0) counts as right
    if len(left_idx) == 0 or len(right_idx) == 0:
        return []   # no X symmetry plane - can't classify halves

    def qhash(d3):
        q = np.round(d3.astype(np.float64) / _SPLIT_TOL).astype(np.int64)
        return hashlib.blake2b(q.tobytes(), digest_size=16).digest()

    lefts = []      # (index, name, delta3 float32)
    rights = []
    full_map = {}   # qhash(delta3) -> [(index, name), ...] for two-sided keys
    for i, name in cache['real_key_entries']:
        kb = blocks[i]
        if kb.name == ref.name or len(kb.data) != n or name in exclude_names:
            continue
        co = np.empty(n * 3, dtype=np.float32)
        kb.data.foreach_get('co', co)
        d3 = (co - basis).reshape(n, 3)
        lm = float((d3[left_idx] ** 2).sum())
        rm = float((d3[right_idx] ** 2).sum())
        if lm <= 1e-12 and rm <= 1e-12:
            continue  # empty
        weak = min(lm, rm)
        strong = max(lm, rm)
        # One-sided when the weaker side carries <0.01% of the motion energy;
        # this tolerates a little seam bleed while keeping true full shapes out.
        if weak <= 1e-4 * strong:
            (lefts if lm > rm else rights).append((i, name, d3.copy()))
        else:
            full_map.setdefault(qhash(d3), []).append((i, name))

    if len(lefts) * len(rights) > _SPLIT_PAIR_LIMIT:
        _LAST_SPLIT_NOTE['text'] = (
            f"Too many one-sided keys ({len(lefts)}x{len(rights)} pairs) - "
            f"split detection skipped to stay responsive.")
        return []

    found = {}   # full_index -> (full_index, full_name, left_name, right_name)
    for li, ln, ld in lefts:
        for ri, rn, rd in rights:
            s = ld + rd
            if float((s[left_idx] ** 2).sum()) <= 1e-12 or \
               float((s[right_idx] ** 2).sum()) <= 1e-12:
                continue
            hit = full_map.get(qhash(s))
            if hit:
                fi, fn = hit[0]
                if fn not in (ln, rn) and fi not in found:
                    found[fi] = (fi, fn, ln, rn)
    return sorted(found.values(), key=lambda t: t[0])


# LR suffix pairs checked in order for name-based split detection. The first
# matching pair per parent key wins so one parent never appears twice.
_LR_SUFFIX_PAIRS = [
    ('.L', '.R'),
    ('_L', '_R'),
    ('.Left', '.Right'),
    ('_Left', '_Right'),
    ('Left', 'Right'),
    ('.l', '.r'),
    ('_l', '_r'),
]

# Matches a leading alphanumeric-number prefix like "M18_", "L3_", "M100_".
# Stripping this lets the scanner match parents and children that share a base
# name but carry different sequential numbers (e.g. M18_joy2 / M19_joy2_L).
_PREFIX_RE = re.compile(r'^[A-Za-z]\d+_')


def _strip_prefix(name):
    m = _PREFIX_RE.match(name)
    return name[m.end():] if m else name


def _find_name_split_groups(obj, exclude_names=frozenset()):
    """Find LR split redundancy purely by naming convention.

    For each shape key 'foo' (or 'M18_foo'), if a left key and right key exist
    whose stripped base name matches - e.g. 'M19_foo_L' and 'M20_foo_R' -
    the parent is redundant and can be removed. Leading 'X##_' prefixes are
    stripped before comparison so mismatched sequential numbers are handled.
    No vertex comparison is done; detection is O(n) in key count.

    exclude_names skips keys already reported elsewhere. Returns a list of
    (parent_name, left_name, right_name), ordered by the parent's position."""
    sk = obj.data.shape_keys
    if not sk:
        return []
    blocks = sk.key_blocks
    ref_name = sk.reference_key.name if sk.reference_key else None

    # stripped_name -> [full_name, ...] for fast lookup ignoring the prefix.
    stripped_map = {}
    for kb in blocks:
        if kb.name == ref_name or kb.name in exclude_names:
            continue
        stripped_map.setdefault(_strip_prefix(kb.name), []).append(kb.name)

    seen_parents = set()
    seen_lr_pairs = set()   # (left_stripped, right_stripped) already claimed
    groups = []

    for kb in blocks:
        name = kb.name
        if name == ref_name or name in exclude_names or name in seen_parents:
            continue
        base = _strip_prefix(name)
        for lsuf, rsuf in _LR_SUFFIX_PAIRS:
            lkey = base + lsuf
            rkey = base + rsuf
            if (lkey, rkey) in seen_lr_pairs:
                break  # another parent already owns this pair
            lefts = [n for n in stripped_map.get(lkey, []) if n not in exclude_names]
            rights = [n for n in stripped_map.get(rkey, []) if n not in exclude_names]
            if lefts and rights:
                left_name = lefts[0]
                right_name = rights[0]
                seen_parents.add(name)
                seen_lr_pairs.add((lkey, rkey))
                groups.append((name, left_name, right_name))
                break   # first matching suffix pair wins
    return groups


# Pairwise near-duplicate clustering is O(K^2 * verts); above this approximate
# FLOP budget we fall back to fast exact hashing rather than stall the UI.
_SIMILAR_FLOP_LIMIT = 6e10
# Last similarity-scan note for the operator to surface (e.g. fallback reason).
_LAST_SIMILAR_NOTE = {'text': ''}


def _find_similar_groups(obj, threshold):
    """Cluster non-empty shape keys whose displacement fields are at least
    `threshold` (0..1) similar, where
        similarity = 1 - ||delta_A - delta_B|| / max(||delta_A||, ||delta_B||).
    1.0 means identical; 0.9 means they differ by ~10% of the larger shape's
    magnitude.

    Similarity is not transitive, so keys are grouped by connected components
    (A~B and B~C puts A, B, C together). Returns list[list[(index, name)]],
    members sorted by index and groups by first appearance, only groups with
    >=2 members.

    Heavy: O(K^2 * verts). Above _SIMILAR_FLOP_LIMIT it falls back to exact
    hashing and records a note in _LAST_SIMILAR_NOTE."""
    _LAST_SIMILAR_NOTE['text'] = ''
    sk = obj.data.shape_keys
    blocks = sk.key_blocks
    ref = sk.reference_key
    cache = _get_cache(obj)
    if cache is None or ref is None:
        return []
    n = len(ref.data)
    if n == 0:
        return []

    basis = np.empty(n * 3, dtype=np.float32)
    ref.data.foreach_get('co', basis)

    entries = []   # (index, name)
    deltas = []    # float32 displacement buffers
    for i, name in cache['real_key_entries']:
        kb = blocks[i]
        if kb.name == ref.name or len(kb.data) != n:
            continue
        co = np.empty(n * 3, dtype=np.float32)
        kb.data.foreach_get('co', co)
        d = co - basis
        d64 = d.astype(np.float64)
        if float((d64 * d64).reshape(n, 3).sum(axis=1).max()) <= 1e-10:
            continue  # empty
        entries.append((i, name))
        deltas.append(d)

    k = len(entries)
    if k < 2:
        return []

    if (k * k) * (3 * n) > _SIMILAR_FLOP_LIMIT:
        _LAST_SIMILAR_NOTE['text'] = (
            "Mesh too large for similarity scan - showing exact matches only.")
        return _find_redundant_groups(obj)

    d_mat = np.array(deltas, dtype=np.float32)          # (K, 3n)
    norms = np.sqrt((d_mat * d_mat).sum(axis=1))        # (K,)
    gram = d_mat @ d_mat.T                              # (K, K)
    nn = norms * norms
    sq = nn[:, None] + nn[None, :] - 2.0 * gram         # ||Di - Dj||^2
    np.maximum(sq, 0.0, out=sq)
    dist = np.sqrt(sq)
    denom = np.maximum(norms[:, None], norms[None, :])
    denom = np.where(denom < 1e-12, 1.0, denom)
    sim = 1.0 - dist / denom

    # Connected components over the "similar enough" adjacency (upper triangle).
    parent = list(range(k))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    ii, jj = np.where(np.triu(sim >= threshold, 1))
    for a, b in zip(ii.tolist(), jj.tolist()):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    comps = {}
    for idx in range(k):
        comps.setdefault(find(idx), []).append(entries[idx])
    groups = [m for m in comps.values() if len(m) >= 2]
    for g in groups:
        g.sort(key=lambda t: t[0])
    groups.sort(key=lambda m: m[0][0])
    return groups


def _compute_primary_groups(obj, similarity_pct):
    """Dispatch the primary (non-split) redundancy scan: fast byte-exact
    hashing at 100%, pairwise similarity clustering below it."""
    if similarity_pct >= 100:
        _LAST_SIMILAR_NOTE['text'] = ''
        return _find_redundant_groups(obj)
    return _find_similar_groups(obj, similarity_pct / 100.0)


class SKP_OT_DeleteEmptyKeys(_CooldownMixin, Operator):
    """Delete all shape keys that have no vertex displacement from the Basis.
    Opens the same timed confirmation dialog as Delete Category."""
    bl_idname = "skp.delete_empty_keys"
    bl_label = "Delete Empty Blendshapes"
    bl_options = {'REGISTER', 'UNDO'}

    key_count: IntProperty()
    show_keys_toggle: BoolProperty(name="Show Keys", default=False)

    # _start_time lives on the mixin (instance attr, class fallback).
    _keys_to_delete: list = []

    def _collect_empty_keys(self, context):
        obj = context.active_object
        cache = _get_cache(obj)
        if cache is None:
            return []
        blocks = obj.data.shape_keys.key_blocks
        # real_key_entries already excludes dividers
        return [name for i, name in cache['real_key_entries']
                if _is_key_empty(obj, blocks[i])]

    def execute(self, context):
        if self._seconds_remaining() > 0:
            self.report({'WARNING'}, "Please wait for the cooldown before confirming.")
            return {'CANCELLED'}

        obj = context.active_object
        if not obj or not obj.data or not obj.data.shape_keys:
            return {'CANCELLED'}

        ok, msg = _require_object_mode(obj, "delete shape keys")
        if not ok:
            self.report({'WARNING'}, msg)
            return {'CANCELLED'}

        # Recompute from current state (matches the shown list; makes
        # redo-after-undo work). See SKP_OT_DeleteCategory.execute.
        to_delete = self._collect_empty_keys(context)
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
        self._start_time = time.time()
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

        self._draw_cooldown_footer(layout)


# --- Redundant-review shared state -------------------------------------------
# The review dialog (SKP_OT_FindRedundant) and its helper operators (rescan,
# preview, bulk-select) all read/write scene-level controls and a class-level
# cache of the last scan, so any button drawn inside the modal popup can act on
# the same data the dialog displays.

class _RedundantState:
    primary_groups = []      # list[list[str]] - names per exact/similar group
    primary_kind = 'exact'   # 'exact' (100%) or 'similar' (<100%)
    split_groups = []        # list[(full_idx, full, left, right)]
    split_computed = False
    name_split_groups = []   # list[(parent, left, right)] - name-match only
    name_split_computed = False
    # Snapshot of every non-Basis key value at the moment the dialog opened, so
    # the user's posed expression is restored on close (the eye-preview zeroes
    # values while auditing). {name: value}.
    saved_values = {}


def _populate_redundant_items(context):
    """Rebuild the scene review list from the cached scan, honouring the
    splits toggle. User check edits are carried across rebuilds so toggling
    splits/rescanning never silently discards manual choices for keys that
    survive into the new list."""
    cache = _get_cache(context.active_object)
    col = context.scene.skp_redundant_items
    prev = {it.name: it.delete for it in col if not it.is_header}
    col.clear()

    def cat(nm):
        return _category_label_for(cache, nm) if cache else ""

    kind = _RedundantState.primary_kind
    gid = 0
    for members in _RedundantState.primary_groups:
        h = col.add()
        h.is_header = True
        h.group_kind = kind
        h.group_id = gid
        h.name = f"Group {gid + 1}"
        h.group_size = len(members)
        # If any member is a protected face-tracking key, the safe default is to
        # keep all protected copies and delete the redundant rest; otherwise
        # keep the first occurrence and delete later clones.
        protect = {nm: _face_target_info(nm) for nm in members}
        any_protected = any(p for p, _r in protect.values())
        for pos, nm in enumerate(members):
            prot, reason = protect[nm]
            it = col.add()
            it.group_kind = kind
            it.group_id = gid
            it.name = nm
            it.category = cat(nm)
            it.protected = prot
            it.protect_reason = reason
            default_del = (not prot) if any_protected else (pos > 0)
            it.delete = prev.get(nm, default_del)
        gid += 1

    if context.scene.skp_redundant_include_splits:
        for _fi, full, left, right in _RedundantState.split_groups:
            h = col.add()
            h.is_header = True
            h.group_kind = 'split'
            h.group_id = gid
            h.name = f"Split {gid + 1}"
            h.group_size = 3
            for nm, role in ((full, 'full'), (left, 'left'), (right, 'right')):
                prot, reason = _face_target_info(nm)
                it = col.add()
                it.group_kind = 'split'
                it.group_id = gid
                it.name = nm
                it.category = cat(nm)
                it.role = role
                it.protected = prot
                it.protect_reason = reason
                # Default: drop the full (reproducible from L + R), keep halves -
                # but never auto-select a protected face-tracking key.
                it.delete = prev.get(nm, role == 'full' and not prot)
            gid += 1

    if context.scene.skp_redundant_include_name_splits:
        for parent, left, right in _RedundantState.name_split_groups:
            h = col.add()
            h.is_header = True
            h.group_kind = 'namesplit'
            h.group_id = gid
            h.name = f"Name-LR {gid + 1}"
            h.group_size = 3
            for nm, role in ((parent, 'full'), (left, 'left'), (right, 'right')):
                prot, reason = _face_target_info(nm)
                it = col.add()
                it.group_kind = 'namesplit'
                it.group_id = gid
                it.name = nm
                it.category = cat(nm)
                it.role = role
                it.protected = prot
                it.protect_reason = reason
                it.delete = prev.get(nm, role == 'full' and not prot)
            gid += 1

    context.scene.skp_redundant_index = 0


def _rescan_redundant(context):
    """Run the primary scan at the current similarity threshold (and splits if
    enabled), cache it, and repopulate the review list."""
    obj = context.active_object
    sim = context.scene.skp_redundant_similarity
    primary = _compute_primary_groups(obj, sim)
    _RedundantState.primary_groups = [[nm for _i, nm in g] for g in primary]
    _RedundantState.primary_kind = 'exact' if sim >= 100 else 'similar'

    if context.scene.skp_redundant_include_splits:
        exclude = {nm for g in _RedundantState.primary_groups for nm in g}
        _RedundantState.split_groups = _find_split_groups(obj, exclude)
        _RedundantState.split_computed = True

    if context.scene.skp_redundant_include_name_splits:
        ns_exclude = {nm for g in _RedundantState.primary_groups for nm in g}
        for _fi, fn, ln, rn in _RedundantState.split_groups:
            ns_exclude.update((fn, ln, rn))
        _RedundantState.name_split_groups = _find_name_split_groups(obj, ns_exclude)
        _RedundantState.name_split_computed = True

    _populate_redundant_items(context)


def _begin_redundant_scan(context):
    """Run the redundant scan with a busy/wait cursor for feedback.

    The scan is deliberately synchronous: an invoke_props_dialog popup cannot
    repaint itself after background (timer) work in Blender - a deferred
    'scanning…' panel just sits stale until the user moves the mouse (verified:
    even a forced DRAW_WIN_SWAP won't call the popup's draw). A wait cursor plus
    progress reliably signals 'Blender is working' during the brief freeze, and
    the dialog then opens already populated with results."""
    wm = context.window_manager
    win = context.window
    if win:
        win.cursor_set('WAIT')
    try:
        wm.progress_begin(0.0, 1.0)
        wm.progress_update(0.0)
        _rescan_redundant(context)
    finally:
        wm.progress_end()
        if win:
            win.cursor_set('DEFAULT')


def _redundant_splits_update(self, context):
    """Scene-prop update callback for the 'Include L/R splits' toggle. Split
    detection is deferred until first enabled so the common scan stays fast;
    results are cached for the dialog's lifetime. A wait cursor covers the
    one-time detection freeze."""
    if context.scene.skp_redundant_include_splits and not _RedundantState.split_computed:
        win = context.window
        if win:
            win.cursor_set('WAIT')
        try:
            exclude = {nm for g in _RedundantState.primary_groups for nm in g}
            _RedundantState.split_groups = _find_split_groups(
                context.active_object, exclude)
            _RedundantState.split_computed = True
        finally:
            if win:
                win.cursor_set('DEFAULT')
    _populate_redundant_items(context)


def _redundant_name_splits_update(self, context):
    """Scene-prop update callback for the 'Find L/R parents by name' toggle.
    Name-based detection is O(n) in key count so no deferred caching is needed;
    results are cached for the dialog's lifetime the same way as geometry splits."""
    if context.scene.skp_redundant_include_name_splits and not _RedundantState.name_split_computed:
        ns_exclude = {nm for g in _RedundantState.primary_groups for nm in g}
        for _fi, fn, ln, rn in _RedundantState.split_groups:
            ns_exclude.update((fn, ln, rn))
        _RedundantState.name_split_groups = _find_name_split_groups(
            context.active_object, ns_exclude)
        _RedundantState.name_split_computed = True
    _populate_redundant_items(context)


def _redraw_view3d(context):
    for win in context.window_manager.windows:
        for area in win.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


def _reset_redundant_preview(context):
    """Zero every non-Basis, non-divider key value and clear the active-preview
    marker - the neutral baseline the eye-preview isolates a single key against."""
    obj = context.active_object
    if obj and obj.data and obj.data.shape_keys:
        sk = obj.data.shape_keys
        basis_name = sk.reference_key.name if sk.reference_key else "Basis"
        for kb in sk.key_blocks:
            if kb.name != basis_name and not is_category_divider(kb.name):
                kb.value = 0.0
    context.scene.skp_redundant_preview_active = ""
    _redraw_view3d(context)


def _snapshot_redundant_values(context):
    """Record every non-Basis, non-divider key value so the user's posed
    expression can be restored when the redundant dialog closes."""
    obj = context.active_object
    saved = {}
    if obj and obj.data and obj.data.shape_keys:
        sk = obj.data.shape_keys
        basis_name = sk.reference_key.name if sk.reference_key else "Basis"
        for kb in sk.key_blocks:
            if kb.name != basis_name and not is_category_divider(kb.name):
                saved[kb.name] = kb.value
    _RedundantState.saved_values = saved


def _restore_redundant_values(context):
    """Restore the values snapshotted at dialog open (for keys that still
    exist), clear the preview marker, and refresh the viewport. Replaces the
    blunt 'zero everything' so opening the audit doesn't wipe the user's pose."""
    obj = context.active_object
    if obj and obj.data and obj.data.shape_keys:
        blocks = obj.data.shape_keys.key_blocks
        for name, val in _RedundantState.saved_values.items():
            idx = blocks.find(name)
            if idx >= 0:
                blocks[idx].value = val
    _RedundantState.saved_values = {}
    context.scene.skp_redundant_preview_active = ""
    _redraw_view3d(context)


class SKP_OT_RedundantPreview(Operator):
    """Toggle an isolate-preview of a shape key from the redundant review
    dialog: clicking sets it to 1.0 and zeros every other key; clicking the
    same key again (or the clear button) turns the preview off. Only one key is
    ever previewed at a time, and the preview clears when the dialog closes."""
    bl_idname = "skp.redundant_preview"
    bl_label = "Preview Shape Key"
    bl_options = {'INTERNAL'}

    # Empty name = clear the preview (used by the reset button).
    key_name: StringProperty()

    def execute(self, context):
        obj = context.active_object
        if not obj or not obj.data or not obj.data.shape_keys:
            return {'CANCELLED'}
        scene = context.scene
        sk = obj.data.shape_keys
        blocks = sk.key_blocks
        basis_name = sk.reference_key.name if sk.reference_key else "Basis"

        # Always start from neutral so only one key is ever live.
        for kb in blocks:
            if kb.name != basis_name and not is_category_divider(kb.name):
                kb.value = 0.0

        # Clear request, or clicking the already-active key, turns preview off.
        if not self.key_name or scene.skp_redundant_preview_active == self.key_name:
            scene.skp_redundant_preview_active = ""
            _redraw_view3d(context)
            return {'FINISHED'}

        idx = blocks.find(self.key_name)
        if idx < 0:
            scene.skp_redundant_preview_active = ""
            _redraw_view3d(context)
            return {'CANCELLED'}
        blocks[idx].value = 1.0
        obj.active_shape_key_index = idx
        scene.skp_redundant_preview_active = self.key_name
        _redraw_view3d(context)
        return {'FINISHED'}


class SKP_OT_RedundantRescan(Operator):
    """Re-scan for redundant keys at the current similarity threshold."""
    bl_idname = "skp.redundant_rescan"
    bl_label = "Rescan"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        # Synchronous scan with a wait cursor; the popup repaints after this
        # button's own click, so results show without any extra nudge.
        _begin_redundant_scan(context)
        note = _LAST_SIMILAR_NOTE['text']
        if note:
            self.report({'WARNING'}, note)
        return {'FINISHED'}


class SKP_OT_FindRedundant(_CooldownMixin, Operator):
    """Find shape keys with identical (or, below 100% similarity, near-identical)
    geometry, grouped with the category each belongs to, then choose which to
    keep and which to delete. Optionally also detect L/R 'splits' - a full shape
    whose left and right halves exist as separate keys. Preview any key in the
    viewport with its eye button. Opens a timed confirmation dialog."""
    bl_idname = "skp.find_redundant"
    bl_label = "Find Redundant Blendshapes"
    bl_options = {'REGISTER', 'UNDO'}

    def invoke(self, context, event):
        obj = context.active_object
        if not obj or not obj.data or not obj.data.shape_keys:
            self.report({'WARNING'}, "No shape keys on active object.")
            return {'CANCELLED'}

        # Snapshot the current pose so it can be restored on close, then drop to
        # a neutral baseline the eye-preview isolates a single key against.
        _snapshot_redundant_values(context)
        _reset_redundant_preview(context)

        # Reset controls + caches to defaults for a predictable first scan.
        _RedundantState.primary_groups = []
        _RedundantState.primary_kind = 'exact'
        _RedundantState.split_groups = []
        _RedundantState.split_computed = False
        _RedundantState.name_split_groups = []
        _RedundantState.name_split_computed = False
        _LAST_SIMILAR_NOTE['text'] = ''
        context.scene.skp_redundant_similarity = 98
        context.scene.skp_redundant_include_splits = False
        context.scene.skp_redundant_include_name_splits = False

        # Scan synchronously with a wait cursor so the user sees Blender is
        # busy, then open the dialog already populated. (A popup can't repaint
        # itself after deferred work, so a standby-then-results dialog isn't
        # possible - the wait cursor is the reliable feedback.)
        _begin_redundant_scan(context)
        self._start_time = time.time()
        return context.window_manager.invoke_props_dialog(self, width=560)

    def _counts(self, context):
        to_delete = keep = groups = 0
        for it in context.scene.skp_redundant_items:
            if it.is_header:
                groups += 1
            elif it.delete:
                to_delete += 1
            else:
                keep += 1
        return to_delete, keep, groups

    def _draw_controls(self, layout, scene):
        """Similarity slider + Rescan + splits toggle (shared by results and
        empty states)."""
        box = layout.box()
        srow = box.row(align=True)
        srow.prop(scene, "skp_redundant_similarity", text="Similarity")
        srow.operator("skp.redundant_rescan", text="Rescan", icon='FILE_REFRESH')
        hint = box.row()
        hint.enabled = False
        if scene.skp_redundant_similarity >= 100:
            hint.label(text="100% = byte-identical only. Lower it, then Rescan, "
                            "to catch near-duplicates.", icon='INFO')
        else:
            hint.label(text=f"Showing keys at least {scene.skp_redundant_similarity}% "
                            f"similar (after Rescan).", icon='INFO')

        box.prop(scene, "skp_redundant_include_splits", toggle=True, icon='MOD_MIRROR')
        if scene.skp_redundant_include_splits:
            shint = box.row()
            shint.enabled = False
            shint.label(
                text=f"Splits (full = L + R): {len(_RedundantState.split_groups)} - "
                     f"default removes the full, keeps both halves",
                icon='INFO',
            )
            if _LAST_SPLIT_NOTE['text']:
                swarn = box.row()
                swarn.alert = True
                swarn.label(text=_LAST_SPLIT_NOTE['text'], icon='ERROR')

        box.prop(scene, "skp_redundant_include_name_splits", toggle=True, icon='SORTALPHA')
        if scene.skp_redundant_include_name_splits:
            nhint = box.row()
            nhint.enabled = False
            nhint.label(
                text=f"Name-LR pairs: {len(_RedundantState.name_split_groups)} found - "
                     f"default removes the parent, keeps L and R",
                icon='INFO',
            )

        if _LAST_SIMILAR_NOTE['text']:
            wrow = box.row()
            wrow.alert = True
            wrow.label(text=_LAST_SIMILAR_NOTE['text'], icon='ERROR')

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        items = scene.skp_redundant_items
        to_delete, keep, groups = self._counts(context)

        col = layout.column()
        col.alert = True
        col.label(text=f"{to_delete + keep} keys across {groups} group(s)",
                  icon='DUPLICATE')
        col.alert = False

        layout.label(text="Tick keys to delete; keep at least one per group.")
        layout.label(text="Use the eye button on a row to preview that key.")
        legend = layout.row()
        legend.enabled = False
        legend.label(
            text="Yellow-triangle keys are likely Resonite/ARKit face-tracking "
                 "targets - kept by default.",
            icon='ERROR',
        )
        caveat = layout.row()
        caveat.enabled = False
        caveat.label(
            text="Flagging is conservative; double-check before deleting any "
                 "mouth/eye/brow shape.",
        )

        self._draw_controls(layout, scene)

        # Empty-result state: keep the controls live so the user can lower the
        # threshold / enable splits and Rescan without reopening.
        if len(items) == 0:
            empty = layout.box()
            empty.label(text="No redundant blendshapes found at this similarity.",
                        icon='INFO')
            sub = empty.row()
            sub.enabled = False
            sub.label(text="Lower the similarity or enable splits, then Rescan.")
            layout.separator(factor=0.5)
            self._draw_cooldown_footer(layout)
            return

        # Bulk selection + clear preview
        row = layout.row(align=True)
        row.operator("skp.redundant_select", text="Recommended").mode = 'DEFAULT'
        row.operator("skp.redundant_select", text="Keep All").mode = 'NONE'
        row.operator("skp.redundant_select", text="Invert").mode = 'INVERT'
        # Empty key_name clears the active preview and resets all values.
        row.operator("skp.redundant_preview", text="", icon='LOOP_BACK')

        # Cap at 30 rows to avoid exceeding screen height; list is scrollable.
        rows = max(6, min(30, len(items)))
        layout.template_list(
            "SKP_UL_redundant_groups", "",
            scene, "skp_redundant_items",
            scene, "skp_redundant_index",
            rows=rows,
        )

        info = layout.row()
        info.alert = to_delete > 0
        info.label(text=f"Will delete {to_delete}  |  Keep {keep}", icon='TRASH')

        # Loud warning if any protected face-tracking key is marked for deletion.
        prot_del = sum(1 for it in items
                       if not it.is_header and it.delete and it.protected)
        if prot_del:
            warn = layout.box()
            warn.alert = True
            warn.label(
                text=f"{prot_del} protected face-tracking key(s) marked for deletion!",
                icon='ERROR',
            )
            sub = warn.row()
            sub.alert = True
            sub.label(text="These match Resonite viseme / ARKit names - "
                           "deleting may break in-game tracking.")

        layout.separator(factor=0.5)
        self._draw_cooldown_footer(layout)

    def execute(self, context):
        if self._seconds_remaining() > 0:
            self.report({'WARNING'}, "Please wait for the cooldown before confirming.")
            return {'CANCELLED'}

        obj = context.active_object
        if not obj or not obj.data or not obj.data.shape_keys:
            return {'CANCELLED'}

        ok, msg = _require_object_mode(obj, "delete shape keys")
        if not ok:
            self.report({'WARNING'}, msg)
            return {'CANCELLED'}

        to_delete = [it.name for it in context.scene.skp_redundant_items
                     if not it.is_header and it.delete]
        if not to_delete:
            self.report({'INFO'}, "No keys selected for deletion.")
            return {'CANCELLED'}

        # Delete by name, re-resolving the index each pass because the live
        # blocks list shrinks as keys are removed.
        deleted = 0
        for name in to_delete:
            blocks = obj.data.shape_keys.key_blocks
            idx = blocks.find(name)
            if idx < 0:
                continue
            obj.active_shape_key_index = idx
            bpy.ops.object.shape_key_remove(all=False, apply_mix=False)
            deleted += 1

        # Restore the pose the user had before opening the audit (deleted keys
        # are simply absent from the restore).
        _restore_redundant_values(context)
        self.report({'INFO'}, f"Deleted {deleted} redundant shape key(s).")
        context.scene.skp_redundant_items.clear()
        return {'FINISHED'}

    def cancel(self, context):
        # Dismissed (Esc / click-away): restore the pose from before the audit.
        _restore_redundant_values(context)


class SKP_OT_DeleteFiltered(_CooldownMixin, Operator):
    """Delete every shape key currently matching the text filter
    (across all categories in the active category scope).
    Opens the same timed confirmation dialog as Delete Category."""
    bl_idname = "skp.delete_filtered"
    bl_label = "Delete Filtered Keys"
    bl_options = {'REGISTER', 'UNDO'}

    key_count: IntProperty()
    show_keys_toggle: BoolProperty(name="Show Keys", default=False)

    # _start_time lives on the mixin (instance attr, class fallback).
    _keys_to_delete: list = []

    def _collect_filtered_keys(self, context):
        obj = context.active_object
        props = context.scene.skp_props
        # get_filtered_keys already excludes dividers
        return [kb.name for _i, kb in get_filtered_keys(obj, props)]

    def execute(self, context):
        if self._seconds_remaining() > 0:
            self.report({'WARNING'}, "Please wait for the cooldown before confirming.")
            return {'CANCELLED'}

        obj = context.active_object
        if not obj or not obj.data or not obj.data.shape_keys:
            return {'CANCELLED'}

        ok, msg = _require_object_mode(obj, "delete shape keys")
        if not ok:
            self.report({'WARNING'}, msg)
            return {'CANCELLED'}

        # Recompute from current state (matches the shown list; makes
        # redo-after-undo work). See SKP_OT_DeleteCategory.execute.
        to_delete = self._collect_filtered_keys(context)
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
        self._start_time = time.time()
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

        self._draw_cooldown_footer(layout)


class SKP_OT_PageNext(Operator):
    bl_idname = "skp.page_next"
    bl_label = "Next Page"

    def execute(self, context):
        props = context.scene.skp_props
        obj = context.active_object
        filtered = get_filtered_keys(obj, props)
        max_page = total_pages(filtered, props.page_size) - 1
        # Clamp the starting point first so "Next" from a stale (out-of-range)
        # current_page advances from the page the user actually sees.
        clamped = min(props.current_page, max_page)
        props.current_page = min(clamped + 1, max_page)
        return {'FINISHED'}


class SKP_OT_PagePrev(Operator):
    bl_idname = "skp.page_prev"
    bl_label = "Previous Page"

    def execute(self, context):
        props = context.scene.skp_props
        obj = context.active_object
        # If a filter shrank the list while current_page was still pointing
        # at an out-of-range page, draw() clamped the displayed page locally
        # but never wrote back. Clamp against max_page first so "Prev" from
        # the visually-shown page steps to the right place.
        filtered = get_filtered_keys(obj, props)
        max_page = total_pages(filtered, props.page_size) - 1
        clamped = min(props.current_page, max_page)
        props.current_page = max(clamped - 1, 0)
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
        # If a step raises (e.g. _apply_step → preview_key on a corrupt
        # state), Blender drops the modal handler but our _running flag
        # would stay True, leaving the toggle one-click-stale. Catch and
        # clean up so re-enabling is single-click.
        try:
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
        except Exception as err:
            self._finish(context)
            self.report({'ERROR'}, f"Arrow key navigation stopped: {err}")
            return {'CANCELLED'}

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
        was_running = SKP_OT_ArrowKeyModal._running
        SKP_OT_ArrowKeyModal._running = False
        if was_running:
            self.report({'INFO'}, "Arrow key navigation OFF")
        self._redraw(context)

    @staticmethod
    def _redraw(context):
        for area in context.screen.areas:
            if area.type == 'PROPERTIES':
                area.tag_redraw()


# -----------------------------------------
#  Mesh-to-mesh sync helpers + operators
# -----------------------------------------

# Separate hold for the sync category enum so it can't race with the main
# panel's build_category_enum. Same lifetime rationale as _ENUM_ITEMS_HOLD.
_SYNC_ENUM_ITEMS_HOLD: list = []


def build_sync_category_enum(self, context):
    """Items callback for sync_category_filter. Reads categories from the
    user-picked sync_reference, not the active object."""
    global _SYNC_ENUM_ITEMS_HOLD
    reference = None
    if context and hasattr(context.scene, 'skp_props'):
        reference = context.scene.skp_props.sync_reference
    cache = _get_cache(reference) if reference is not None else None
    if cache is None:
        items = [('ALL', "All Categories", "Show all reference shape keys")]
        _SYNC_ENUM_ITEMS_HOLD = items
        return items

    total = cache['total_real']
    items = [('ALL', f"All Categories ({total})", "Show all reference shape keys")]
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
    _SYNC_ENUM_ITEMS_HOLD = items
    return items


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

        # Face-tracking indicator summary + legend for the per-row triangle.
        # Count is precomputed in the derived-data cache (rebuilt only when the
        # key list changes), so this adds nothing to per-redraw cost.
        ft_total = cache.get('face_target_count', 0)
        if ft_total:
            ftrow = layout.row()
            ftrow.label(
                text=f"{ft_total} possible face-tracking key(s) - marked below",
                icon='ERROR',
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

                # Leading face-tracking marker: yellow triangle for keys Resonite
                # likely auto-binds (visemes / ARKit), blank otherwise so the
                # column stays aligned. Passive indicator only.
                marker = row.row()
                marker.ui_units_x = 0.9
                marker.label(
                    text="",
                    icon='ERROR' if face_targets.is_face_target(kb.name) else 'BLANK1',
                )

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

        # Bottom pager mirrors the top one so you don't have to scroll back up
        # to move between pages on a long list.
        if num_pages > 1:
            row = layout.row(align=True)
            row.operator("skp.page_first", text="", icon='REW')
            row.operator("skp.page_prev",  text="", icon='TRIA_LEFT')
            row.label(text=f"Page {current_page + 1} / {num_pages}")
            row.operator("skp.page_next",  text="", icon='TRIA_RIGHT')
            row.operator("skp.page_last",  text="", icon='FF')

        layout.separator(factor=0.5)

        # New shape key button
        layout.operator("skp.new_key", text="New Shape Key from Mix", icon='ADD')
        layout.operator("skp.delete_empty_keys", text="Delete Empty Blendshapes", icon='TRASH')
        layout.operator("skp.find_redundant", text="Find Redundant Blendshapes", icon='DUPLICATE')
        # Translate names: JP/KR/CN -> English using the built-in MMD/VRC
        # dictionary. Opens a confirmation dialog with every proposed rename.
        layout.operator("skp.translate_names",
                        text="Translate Names to English",
                        icon='WORLD_DATA')

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
#
# Sub-modules (ops_sync, ops_presets, debug, translate) are imported at
# the bottom of this file - AFTER all of __init__'s top-level symbols are
# defined - so their `from . import ...` lookups resolve cleanly. Each
# sub-module exposes a CLASSES tuple that we splice into the master
# registration order below. The order matters: dependent classes (panels)
# must come after their dependencies (operators, PropertyGroups).
from . import ops_sync, ops_presets, debug, translate  # noqa: E402  (deferred import)

CLASSES = [
    # PropertyGroups (must register before anything that PointerProperty's them)
    SKP_DividerPattern,
    SKP_AddonPreferences,
    SKP_DeleteKeyItem,
    SKP_UL_DeletePreview,
    SKP_RedundantItem,
    SKP_UL_RedundantGroups,
    SKP_PresetKeyItem,
    SKP_Preset,
    SKP_Properties,
    # Manager-panel operators
    SKP_OT_PreviewKey,
    SKP_OT_ResetAll,
    SKP_OT_ApplyKey,
    SKP_OT_DeleteKey,
    SKP_OT_NewKey,
    SKP_OT_DeleteCategory,
    SKP_OT_DeleteFilterClear,
    SKP_OT_DeleteEmptyKeys,
    SKP_OT_FindRedundant,
    SKP_OT_RedundantSelect,
    SKP_OT_RedundantPreview,
    SKP_OT_RedundantRescan,
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
    # Config sub-panel operators
    SKP_OT_AddDividerPattern,
    SKP_OT_RemoveDividerPattern,
    SKP_OT_ResetDividerPatterns,
    # Sub-module operators (defined in their own files; spliced in here so
    # registration ordering and unregistration order stay explicit).
    *debug.CLASSES[:-1],       # debug operators (panel registered later, after MainPanel)
    *ops_sync.CLASSES[:-2],    # sync operators (SyncPanel + SyncPresets registered later)
    *ops_presets.CLASSES,      # preset operators (no panels of their own; the Sync panels host them)
    *translate.CLASSES,        # translate operator (no panel; button hangs off the Manager panel)
    # Panels - order matters because sub-panels reference parents by bl_idname.
    SKP_PT_MainPanel,
    debug.CLASSES[-1],         # SKP_PT_DebugPanel (parent: SKP_PT_main)
    SKP_PT_ConfigPanel,        # parent: SKP_PT_main
    ops_sync.CLASSES[-2],      # SKP_PT_SyncPanel (top-level sibling)
    ops_sync.CLASSES[-1],      # SKP_PT_SyncPresets (parent: SKP_PT_sync)
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
    bpy.types.Scene.skp_redundant_items = bpy.props.CollectionProperty(type=SKP_RedundantItem)
    bpy.types.Scene.skp_redundant_index = bpy.props.IntProperty(default=0)
    bpy.types.Scene.skp_redundant_similarity = bpy.props.IntProperty(
        name="Similarity",
        description="Minimum geometric similarity for two keys to be grouped. "
                    "100% = byte-identical only; lower it (then Rescan) to catch "
                    "near-duplicates",
        default=98, min=75, max=100, subtype='PERCENTAGE',
    )
    bpy.types.Scene.skp_redundant_include_splits = bpy.props.BoolProperty(
        name="Include L/R splits",
        description="Also list 'split' redundancy: a full shape whose left and "
                    "right halves exist as separate keys (full = left + right)",
        default=False,
        update=_redundant_splits_update,
    )
    bpy.types.Scene.skp_redundant_include_name_splits = bpy.props.BoolProperty(
        name="Find L/R parents by name",
        description="Find keys that have matching .L and .R (or _L/_R, Left/Right, etc.) "
                    "variants by name alone - no geometry check. The parent key is "
                    "redundant since the two halves together reproduce it",
        default=False,
        update=_redundant_name_splits_update,
    )
    bpy.types.Scene.skp_redundant_preview_active = bpy.props.StringProperty(
        name="Active Preview Key",
        description="Name of the shape key currently isolate-previewed in the "
                    "redundant dialog (empty = none)",
        default="",
    )
    bpy.types.Scene.skp_presets = bpy.props.CollectionProperty(type=SKP_Preset)
    bpy.types.Scene.skp_preset_index = bpy.props.IntProperty(default=0)
    # Seed default patterns (only runs if prefs collection is empty)
    addon = bpy.context.preferences.addons.get(__name__)
    if addon:
        _ensure_default_patterns(addon.preferences)

    # Handlers that keep the Transfer "From .blend file" temp Reference out of
    # saved files (save_pre) but restore it for the live session (save_post),
    # and clean up any stray temp object on file open (load_post).
    for handler, fn in (
        (bpy.app.handlers.save_pre,   ops_sync._skp_save_pre_handler),
        (bpy.app.handlers.save_post,  ops_sync._skp_save_post_handler),
        (bpy.app.handlers.load_post,  ops_sync._skp_load_post_handler),
    ):
        if fn not in handler:
            handler.append(fn)


def unregister():
    SKP_OT_ArrowKeyModal._running = False

    # Remove our app handlers and clean up any temp Reference left behind.
    for handler, fn in (
        (bpy.app.handlers.save_pre,   ops_sync._skp_save_pre_handler),
        (bpy.app.handlers.save_post,  ops_sync._skp_save_post_handler),
        (bpy.app.handlers.load_post,  ops_sync._skp_load_post_handler),
    ):
        if fn in handler:
            handler.remove(fn)
    try:
        ops_sync._skp_purge_temp_reference()
    except Exception:
        pass

    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
    del bpy.types.Scene.skp_props
    del bpy.types.Scene.skp_delete_preview
    del bpy.types.Scene.skp_delete_preview_index
    del bpy.types.Scene.skp_delete_filter
    del bpy.types.Scene.skp_redundant_items
    del bpy.types.Scene.skp_redundant_index
    del bpy.types.Scene.skp_redundant_similarity
    del bpy.types.Scene.skp_redundant_include_splits
    del bpy.types.Scene.skp_redundant_include_name_splits
    del bpy.types.Scene.skp_redundant_preview_active
    del bpy.types.Scene.skp_presets
    del bpy.types.Scene.skp_preset_index


if __name__ == "__main__":
    register()
