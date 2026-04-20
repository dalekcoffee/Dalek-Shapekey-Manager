# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2025 Dalek (https://dalek.coffee)

"""
Dalek's Shapekey Manager - Blender 5.0 Add-on  v1.5.1

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
    "version": (1, 5, 1),
    "blender": (5, 0, 0),
    "location": "Properties > Object Data > Shape Keys > Dalek's Shapekey Manager",
    "description": "Preview, filter, manage and audit large numbers of shape keys",
    "category": "Mesh",
}

import re
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


class SKP_DividerPattern(bpy.types.PropertyGroup):
    token: StringProperty(
        name="Token",
        description="Surrounding token that marks a category divider (e.g. === wraps ===VRC===)",
        default="",
    )
    level: EnumProperty(
        name="Level",
        description="Hierarchy level this token represents",
        items=[
            ('top', "Top", "Top-level category header"),
            ('sub', "Sub", "Sub-level category header"),
        ],
        default='sub',
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


def _get_patterns():
    """Return list of (token, level) from preferences, falling back to defaults."""
    addon = bpy.context.preferences.addons.get(__name__)
    if addon:
        patterns = [(p.token.strip(), p.level)
                    for p in addon.preferences.divider_patterns
                    if p.token.strip()]
        if patterns:
            return patterns
    return [(p['token'], p['level']) for p in _DEFAULT_PATTERNS]


# -----------------------------------------
#  Category helpers
# -----------------------------------------

def is_category_divider(name: str) -> bool:
    for token, _ in _get_patterns():
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
    """Extract inner label from either divider syntax, or return name unchanged."""
    _, label = divider_kind(name)
    return label


def _build_full_map(obj):
    """
    Single-pass walk that returns:
      info_map  : { kb.name -> {'kind': 'top'|'sub'|'key',
                                'parent': str|None,   # label of enclosing top-level
                                'sub':    str|None,   # label of enclosing sub-category
                                'label':  str } }
    Parent detection rule:
      Two consecutive dividers (no real keys between them) means the first is a
      parent header and the second starts the first child beneath it.
    """
    if not obj or not obj.data or not obj.data.shape_keys:
        return {}

    blocks = list(obj.data.shape_keys.key_blocks)
    info = {}

    current_top = ''
    current_sub = ''
    prev_was_divider = False

    for kb in blocks:
        kind, label = divider_kind(kb.name)

        if kind == 'top':
            current_top = label
            current_sub = ''
            info[kb.name] = {'kind': 'top', 'parent': None, 'sub': None, 'label': label}
            prev_was_divider = True

        elif kind == 'sub':
            if prev_was_divider and current_sub == '':
                # First sub right after another divider - this IS the first child group
                pass
            current_sub = label
            info[kb.name] = {'kind': 'sub', 'parent': current_top or None, 'sub': label, 'label': label}
            prev_was_divider = True

        else:
            # Real shape key
            info[kb.name] = {
                'kind': 'key',
                'parent': current_top or None,
                'sub':    current_sub or None,
                'label':  kb.name,
            }
            prev_was_divider = False

    return info


def assign_categories(obj) -> dict:
    """
    Return { key_name -> category_string } where category_string is the
    most specific divider label above this key (sub beats top).
    Divider entries themselves are excluded.
    Keys before any divider map to ''.
    """
    info = _build_full_map(obj)
    return {
        name: (d['sub'] or d['parent'] or '')
        for name, d in info.items()
        if d['kind'] == 'key'
    }


def get_categories(obj) -> list:
    """
    Return an ordered list of unique category labels (both top and sub)
    in the order they first appear. Returns [] if none exist.
    """
    if not obj or not obj.data or not obj.data.shape_keys:
        return []
    seen = []
    for kb in obj.data.shape_keys.key_blocks:
        if is_category_divider(kb.name):
            label = category_label(kb.name)
            if label not in seen:
                seen.append(label)
    return seen


def get_category_tree(obj) -> list:
    """
    Return a structured list for building the enum / headers:
      [ {'label': str, 'kind': 'top'|'sub', 'parent': str|None}, ... ]
    in original order, deduplicated.
    """
    if not obj or not obj.data or not obj.data.shape_keys:
        return []
    info = _build_full_map(obj)
    seen = set()
    result = []
    for kb in obj.data.shape_keys.key_blocks:
        d = info.get(kb.name)
        if d and d['kind'] in ('top', 'sub'):
            key = (d['kind'], d['label'])
            if key not in seen:
                seen.add(key)
                result.append({'label': d['label'], 'kind': d['kind'], 'parent': d['parent']})
    return result


def build_category_enum(self, context):
    """
    Dynamic EnumProperty items callback.
    ALL first, then top-level categories, then sub-categories indented under their parent.
    """
    items = [('ALL', "All Categories", "Show all shape keys")]
    obj = context.active_object if context else None
    tree = get_category_tree(obj)
    for entry in tree:
        label = entry['label']
        if entry['kind'] == 'top':
            items.append((label, label, f"Top-level category: {label}"))
        else:
            indent = f"  {label}"
            items.append((label, indent, f"Sub-category: {label}"))
    return items


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
    if not obj or not obj.data or not obj.data.shape_keys:
        return []

    # Build category map once
    cat_map = assign_categories(obj)

    keys = []
    for i, kb in enumerate(obj.data.shape_keys.key_blocks):
        # Skip category dividers entirely
        if is_category_divider(kb.name):
            continue
        # Skip Basis unless requested
        if kb.name == "Basis" and not props.show_basis:
            continue
        keys.append((i, kb))

    # Category filter (only when categories exist)
    cat_filter = props.category_filter
    if cat_filter and cat_filter != 'ALL':
        full_info = _build_full_map(obj)
        tree = get_category_tree(obj)
        top_labels = {e["label"] for e in tree if e["kind"] == "top"}
        is_top = cat_filter in top_labels

        if is_top:
            # Top-level selected: include all keys whose parent matches,
            # regardless of which sub-category they belong to
            def _matches(kb_name):
                d = full_info.get(kb_name, {})
                return d.get("parent") == cat_filter
        else:
            # Sub-category selected: exact sub match only, never parent match
            def _matches(kb_name):
                d = full_info.get(kb_name, {})
                return d.get("sub") == cat_filter

        keys = [(i, kb) for i, kb in keys if _matches(kb.name)]

    # Text search
    query = props.search_filter.strip().lower()
    if query:
        keys = [(i, kb) for i, kb in keys if query in kb.name.lower()]

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


def get_display_items(obj, props):
    """
    Return a list of display items for the current page.
    Each item is one of:
      ('KEY',      original_index, shape_key, filtered_list_position)
      ('CATEGORY', None,           label_str, None)

    Category header rows are injected when sort_mode is NONE and
    category_filter is ALL, so the user sees the natural grouped layout.
    In any other sort mode or when filtered to a single category, headers
    are omitted (the category column in the row is enough context).
    """
    filtered = get_filtered_keys(obj, props)

    # Inject category headers only in default order + all-categories view
    inject_headers = (
        props.sort_mode == 'NONE'
        and props.category_filter == 'ALL'
        and bool(get_categories(obj))
    )

    if not inject_headers:
        return filtered, filtered  # (display_items, step_pool)

    cat_map = assign_categories(obj)
    display = []
    last_cat = object()  # sentinel

    for item in filtered:
        _, kb = item
        cat = cat_map.get(kb.name, '')
        if cat != last_cat:
            display.append(('CATEGORY', cat))
            last_cat = cat
        display.append(('KEY', item))

    return display, filtered


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

    _, kb = filtered[new_fi]
    blocks = obj.data.shape_keys.key_blocks
    obj.active_shape_key_index = list(blocks.keys()).index(kb.name)

    if props.auto_preview:
        bpy.ops.skp.preview_key(key_name=kb.name)

    props.current_page = new_fi // props.page_size
    return {'FINISHED'}


# -----------------------------------------
#  Operators
# -----------------------------------------

class SKP_OT_PreviewKey(Operator):
    """Set this shape key to the preview value; optionally reset others.
    Category divider keys are never passed to this operator."""
    bl_idname = "skp.preview_key"
    bl_label = "Preview Shape Key"
    bl_options = {'REGISTER', 'UNDO'}

    key_name: StringProperty()

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

        if props.auto_reset:
            for kb in blocks:
                if kb.name != "Basis" and not is_category_divider(kb.name):
                    kb.value = 0.0

        if self.key_name in blocks:
            blocks[self.key_name].value = props.preview_value
            obj.active_shape_key_index = list(blocks.keys()).index(self.key_name)
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
        if self.key_name not in blocks:
            self.report({'WARNING'}, f"Shape key '{self.key_name}' not found.")
            return {'CANCELLED'}

        # Set as active then use Blender's built-in apply
        obj.active_shape_key_index = list(blocks.keys()).index(self.key_name)
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
        if self.key_name not in blocks:
            self.report({'WARNING'}, f"Shape key '{self.key_name}' not found.")
            return {'CANCELLED'}

        obj.active_shape_key_index = list(blocks.keys()).index(self.key_name)
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
    key_count: IntProperty()
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
        if not obj or not obj.data or not obj.data.shape_keys:
            return []
        blocks = obj.data.shape_keys.key_blocks
        full_info = _build_full_map(obj)
        tree = get_category_tree(obj)
        top_labels = {e["label"] for e in tree if e["kind"] == "top"}
        is_top = self.category_name in top_labels

        result = []
        for kb in blocks:
            d = full_info.get(kb.name, {})
            kind = d.get("kind")
            if is_top:
                # The top divider itself
                if kind == "top" and d.get("label") == self.category_name:
                    result.append(kb.name)
                # All sub-dividers that belong under this top category
                elif kind == "sub" and d.get("parent") == self.category_name:
                    result.append(kb.name)
                # All member keys under this top category
                elif kind == "key" and d.get("parent") == self.category_name:
                    result.append(kb.name)
            else:
                # The sub divider itself
                if kind == "sub" and d.get("label") == self.category_name:
                    result.append(kb.name)
                # Member keys directly in this sub-category
                elif kind == "key" and d.get("sub") == self.category_name:
                    result.append(kb.name)
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
            if name not in blocks:
                continue
            obj.active_shape_key_index = list(blocks.keys()).index(name)
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
        col.label(text=f"Delete entire category: {self.category_name}", icon='ERROR')
        col.alert = False

        layout.separator(factor=0.3)
        layout.label(text=f"This will permanently delete {self.key_count} shape key(s),")
        layout.label(text="including the category divider. This cannot be undone easily.")

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
        if not obj or not obj.data or not obj.data.shape_keys:
            return []
        return [
            kb.name
            for kb in obj.data.shape_keys.key_blocks
            if not is_category_divider(kb.name) and _is_key_empty(obj, kb)
        ]

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
            if name not in blocks:
                continue
            obj.active_shape_key_index = list(blocks.keys()).index(name)
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
    Category dividers are never passed to this operator."""
    bl_idname = "skp.select_and_preview"
    bl_label = "Select Key"

    key_name: StringProperty()

    def execute(self, context):
        obj = context.active_object
        props = context.scene.skp_props

        if not obj or not obj.data or not obj.data.shape_keys:
            return {'CANCELLED'}

        # Safety guard
        if is_category_divider(self.key_name):
            return {'CANCELLED'}

        blocks = obj.data.shape_keys.key_blocks
        if self.key_name not in blocks:
            return {'CANCELLED'}

        current_idx = obj.active_shape_key_index
        target_idx = list(blocks.keys()).index(self.key_name)
        already_active = (current_idx == target_idx)

        if already_active:
            # Deselect: zero this key's value and move active back to Basis
            blocks[self.key_name].value = 0.0
            basis_idx = list(blocks.keys()).index('Basis') if 'Basis' in blocks else 0
            obj.active_shape_key_index = basis_idx
            props.step_index = -1
        else:
            # Select normally
            obj.active_shape_key_index = target_idx
            filtered = get_filtered_keys(obj, props)
            for fi, (_, kb) in enumerate(filtered):
                if kb.name == self.key_name:
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

        categories = get_categories(obj)
        has_categories = bool(categories)

        # Stats
        total_all = len(obj.data.shape_keys.key_blocks)
        # Subtract category divider entries from the "total" count shown
        total_dividers = sum(1 for kb in obj.data.shape_keys.key_blocks if is_category_divider(kb.name))
        filtered = get_filtered_keys(obj, props)
        total_filtered = len(filtered)

        row = layout.row()
        row.label(
            text=f"Total: {total_all - total_dividers}  |  Shown: {total_filtered}"
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
                # Count exactly what will be deleted using the same logic as the operator
                full_info_del = _build_full_map(obj)
                tree_del = get_category_tree(obj)
                top_labels_del = {e["label"] for e in tree_del if e["kind"] == "top"}
                is_top_cat = active_cat in top_labels_del

                if is_top_cat:
                    # Top-level: count all keys whose parent matches + all sub-dividers + the top divider
                    n_members = sum(
                        1 for kb in obj.data.shape_keys.key_blocks
                        if full_info_del.get(kb.name, {}).get("parent") == active_cat
                        and full_info_del.get(kb.name, {}).get("kind") == "key"
                    )
                    n_subdividers = sum(
                        1 for kb in obj.data.shape_keys.key_blocks
                        if full_info_del.get(kb.name, {}).get("kind") == "sub"
                        and full_info_del.get(kb.name, {}).get("parent") == active_cat
                    )
                    n_total = n_members + n_subdividers + 1  # +1 for the top divider
                else:
                    # Sub-category: only keys in this sub + its own divider
                    n_members = sum(
                        1 for kb in obj.data.shape_keys.key_blocks
                        if full_info_del.get(kb.name, {}).get("sub") == active_cat
                        and full_info_del.get(kb.name, {}).get("kind") == "key"
                    )
                    n_total = n_members + 1  # +1 for the sub divider

                del_op = del_cat_row.operator(
                    "skp.delete_category",
                    text=f"Delete Category ({n_total})",
                    icon='TRASH',
                )
                del_op.category_name = active_cat
                del_op.key_count = n_total
            else:
                del_cat_row.operator(
                    "skp.delete_category",
                    text="Delete Category",
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
            tree = get_category_tree(obj)
            top_labels = {e['label'] for e in tree if e['kind'] == 'top'}
            viewing_top = cat_filter in top_labels  # parent selected
            viewing_all = cat_filter == 'ALL'

            inject_headers = (
                props.sort_mode == 'NONE'
                and (viewing_all or viewing_top)
                and has_categories
            )

            full_info = _build_full_map(obj) if inject_headers else {}
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
                            top_row.enabled = False
                            top_row.label(text=this_top, icon='OUTLINER_COLLECTION')
                        elif not this_top and not this_sub:
                            top_row = col.row()
                            top_row.enabled = False
                            top_row.label(text="(uncategorised)", icon='OUTLINER_COLLECTION')

                    # Always show sub-category header when it changes
                    if this_sub and this_sub != last_shown_sub:
                        last_shown_sub = this_sub
                        sub_row = col.row()
                        sub_row.enabled = False
                        indent = "    " if viewing_all else "  "
                        sub_row.label(text=f"{indent}{this_sub}", icon='DOT')

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
    SKP_PT_MainPanel,
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
