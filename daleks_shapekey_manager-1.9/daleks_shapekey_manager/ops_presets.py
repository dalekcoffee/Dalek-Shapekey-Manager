# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2025 Dalek (https://dalek.coffee)

"""Named blendshape-list presets.

A preset stores just the shape-key NAMES that should exist on a target
mesh - no shape data. Applying a preset adds keys missing on the target
by pulling them from the Reference mesh, and deletes target keys that
aren't in the preset (Basis and category dividers are preserved). This
lets a user 'snap' a target back to a canonical key list after editing.

Presets are stored per-scene at `context.scene.skp_presets` (the
collection lives in __init__'s register()). The Save/Update operators
capture target's current names; Apply is the destructive one that gates
on the shared cooldown dialog.
"""

import time

import bpy
from bpy.props import BoolProperty, IntProperty, StringProperty
from bpy.types import Operator

from . import _CooldownMixin, _shape_key_names, is_category_divider
from .ops_sync import (
    _sync_copy_key,
    _sync_delete_target_keys,
    _sync_fixup_relative_keys,
    _sync_validate,
)


def _preset_capture_target_keys(target):
    """Collect the list of names that should be saved into a preset from
    target. Skips Basis (rest pose, not a deformation). Keeps category
    dividers - they're shape keys too and define the target's category
    structure, which should round-trip via the preset."""
    if target is None or target.data is None or target.data.shape_keys is None:
        return []
    return [kb.name for kb in target.data.shape_keys.key_blocks
            if kb.name != "Basis"]


def _preset_plan(preset, reference, target):
    """Compute what an Apply Preset would do to target:
      to_add:     names on preset not on target (must exist on reference)
      to_delete:  names on target not on preset (excluding Basis + dividers)
      missing_on_ref: preset names that don't exist on reference - skipped
    Returns (to_add, to_delete, missing_on_ref)."""
    preset_names = {k.name for k in preset.keys}
    ref_names = _shape_key_names(reference, exclude_basis=False) if reference else set()
    tgt_names = _shape_key_names(target, exclude_basis=False) if target else set()

    to_add = []
    missing_on_ref = []
    for n in preset_names:
        if n in tgt_names:
            continue
        if n in ref_names:
            to_add.append(n)
        else:
            missing_on_ref.append(n)

    to_delete = []
    if target and target.data and target.data.shape_keys:
        for kb in target.data.shape_keys.key_blocks:
            if kb.name == "Basis":
                continue
            if is_category_divider(kb.name):
                continue
            if kb.name not in preset_names:
                to_delete.append(kb.name)

    return to_add, to_delete, missing_on_ref


def _preset_unique_name(presets, base):
    """Return a name that doesn't collide with any existing preset name."""
    existing = {p.name for p in presets}
    if base not in existing:
        return base
    i = 2
    while f"{base} ({i})" in existing:
        i += 1
    return f"{base} ({i})"


class SKP_OT_PresetSave(Operator):
    """Save the Target's current shape-key list as a preset (just the names -
    no shape data is copied). Later you can Apply the preset to snap the
    Target back to exactly this set, pulling any missing keys from the
    Reference mesh."""
    bl_idname = "skp.preset_save"
    bl_label = "Save Target as Preset"
    bl_options = {'REGISTER', 'UNDO'}

    name: StringProperty(
        name="Preset Name",
        description="Identifier for the new preset",
        default="",
    )

    def invoke(self, context, event):
        props = context.scene.skp_props
        target = props.sync_target
        if target is None or target.data is None or target.data.shape_keys is None:
            self.report({'WARNING'},
                        "Pick a Target with shape keys before saving a preset.")
            return {'CANCELLED'}
        presets = context.scene.skp_presets
        # Suggest a name based on the target's name, deduplicated.
        suggested = f"{target.name} preset"
        self.name = _preset_unique_name(presets, suggested)
        return context.window_manager.invoke_props_dialog(self, width=360)

    def draw(self, context):
        layout = self.layout
        target = context.scene.skp_props.sync_target
        keys = _preset_capture_target_keys(target)
        layout.prop(self, "name")
        info = layout.column(align=True)
        info.label(text=f"Target: {target.name if target else '(none)'}",
                   icon='OBJECT_DATA')
        info.label(text=f"Will save {len(keys)} key name(s).",
                   icon='SHAPEKEY_DATA')

    def execute(self, context):
        props = context.scene.skp_props
        target = props.sync_target
        if target is None or target.data is None or target.data.shape_keys is None:
            self.report({'WARNING'}, "Target lost or has no shape keys.")
            return {'CANCELLED'}
        name = self.name.strip()
        if not name:
            self.report({'WARNING'}, "Preset name cannot be empty.")
            return {'CANCELLED'}

        presets = context.scene.skp_presets
        # Tolerate duplicate user-entered names by deduplicating silently;
        # the in-panel rename field lets them fix the suffix afterwards.
        name = _preset_unique_name(presets, name)

        names = _preset_capture_target_keys(target)
        new = presets.add()
        new.name = name
        new.source_reference = props.sync_reference.name if props.sync_reference else ""
        for n in names:
            it = new.keys.add()
            it.name = n
        # Select the newly created preset.
        context.scene.skp_preset_index = len(presets) - 1
        self.report({'INFO'},
                    f"Saved preset '{name}' with {len(names)} key(s).")
        return {'FINISHED'}


class SKP_OT_PresetUpdate(Operator):
    """Replace the named preset's key list with the Target's current keys.
    Lets you 'recapture' a preset after editing the Target."""
    bl_idname = "skp.preset_update"
    bl_label = "Update Preset from Target"
    bl_options = {'REGISTER', 'UNDO'}

    index: IntProperty(default=-1)

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        presets = context.scene.skp_presets
        if not (0 <= self.index < len(presets)):
            self.report({'WARNING'}, "Preset index out of range.")
            return {'CANCELLED'}
        target = context.scene.skp_props.sync_target
        if target is None or target.data is None or target.data.shape_keys is None:
            self.report({'WARNING'}, "Pick a Target with shape keys to update from.")
            return {'CANCELLED'}
        preset = presets[self.index]
        names = _preset_capture_target_keys(target)
        preset.keys.clear()
        for n in names:
            it = preset.keys.add()
            it.name = n
        preset.source_reference = (
            context.scene.skp_props.sync_reference.name
            if context.scene.skp_props.sync_reference else ""
        )
        self.report({'INFO'},
                    f"Updated preset '{preset.name}' ({len(names)} key(s)).")
        return {'FINISHED'}


class SKP_OT_PresetDelete(Operator):
    """Delete this preset (does not touch any mesh or shape keys)."""
    bl_idname = "skp.preset_delete"
    bl_label = "Delete Preset"
    bl_options = {'REGISTER', 'UNDO'}

    index: IntProperty(default=-1)

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        presets = context.scene.skp_presets
        if not (0 <= self.index < len(presets)):
            self.report({'WARNING'}, "Preset index out of range.")
            return {'CANCELLED'}
        name = presets[self.index].name
        presets.remove(self.index)
        # Clamp the selected-preset index after removal.
        if context.scene.skp_preset_index >= len(presets):
            context.scene.skp_preset_index = max(0, len(presets) - 1)
        self.report({'INFO'}, f"Deleted preset '{name}'.")
        return {'FINISHED'}


class SKP_OT_PresetApply(_CooldownMixin, Operator):
    """Snap the Target's shape-key list to exactly match this preset.
    Adds keys missing on Target by copying them from the Reference mesh.
    Deletes keys on Target that are not in the preset (Basis and category
    dividers are preserved). Opens a timed confirmation dialog."""
    bl_idname = "skp.preset_apply"
    bl_label = "Apply Preset to Target"
    bl_options = {'REGISTER', 'UNDO'}

    index: IntProperty(default=-1)
    show_keys_toggle: BoolProperty(name="Show Keys", default=False)

    # _start_time lives on the mixin (instance attr, class fallback).
    _to_add: list = []
    _to_delete: list = []
    _missing_on_ref: list = []

    def invoke(self, context, event):
        presets = context.scene.skp_presets
        if not (0 <= self.index < len(presets)):
            self.report({'WARNING'}, "Preset index out of range.")
            return {'CANCELLED'}

        props = context.scene.skp_props
        reference = props.sync_reference
        target = props.sync_target
        ok, msg = _sync_validate(reference, target, require_same_vcount=True)
        if not ok:
            self.report({'WARNING'}, msg)
            return {'CANCELLED'}

        preset = presets[self.index]
        to_add, to_delete, missing_on_ref = _preset_plan(preset, reference, target)

        if not to_add and not to_delete:
            if missing_on_ref:
                self.report({'INFO'},
                            f"Target already matches the preset. {len(missing_on_ref)} "
                            f"preset key(s) are not on the reference and were skipped.")
            else:
                self.report({'INFO'}, "Target already matches this preset.")
            return {'CANCELLED'}

        self._start_time = time.time()
        SKP_OT_PresetApply._to_add = to_add
        SKP_OT_PresetApply._to_delete = to_delete
        SKP_OT_PresetApply._missing_on_ref = missing_on_ref

        # Reuse the standard delete-preview collection to render the
        # combined add+delete list in the confirmation dialog.
        col = context.scene.skp_delete_preview
        col.clear()
        for n in to_add:
            it = col.add()
            it.name = f"+  {n}"   # leading sign tells the user 'add'
            it.is_divider = is_category_divider(n)
        for n in to_delete:
            it = col.add()
            it.name = f"-  {n}"
            it.is_divider = False
        context.scene.skp_delete_preview_index = 0
        context.scene.skp_delete_filter = ""
        return context.window_manager.invoke_props_dialog(self, width=480)

    def draw(self, context):
        layout = self.layout
        presets = context.scene.skp_presets
        if not (0 <= self.index < len(presets)):
            layout.label(text="(preset gone)", icon='ERROR')
            return
        preset = presets[self.index]
        props = context.scene.skp_props

        head = layout.column()
        head.label(text=f"Apply preset '{preset.name}'", icon='PRESET')

        layout.separator(factor=0.3)
        info = layout.column(align=True)
        info.label(text=f"Reference: {props.sync_reference.name if props.sync_reference else '(none)'}",
                   icon='OBJECT_DATA')
        info.label(text=f"Target:    {props.sync_target.name if props.sync_target else '(none)'}",
                   icon='OBJECT_DATA')

        layout.separator(factor=0.3)
        n_add = len(SKP_OT_PresetApply._to_add)
        n_del = len(SKP_OT_PresetApply._to_delete)
        n_skip = len(SKP_OT_PresetApply._missing_on_ref)
        breakdown = layout.column(align=True)
        breakdown.label(text=f"Add: {n_add}   Delete: {n_del}")
        # Loud guard: an empty preset (no keys) snaps the target to nothing,
        # i.e. deletes every real key. Easy to trigger by Saving a preset on a
        # Basis-only target, so call it out explicitly.
        if len(preset.keys) == 0:
            ewarn = layout.box()
            ewarn.alert = True
            ewarn.label(text="This preset has NO keys - applying deletes ALL "
                             "non-divider keys on the target!", icon='ERROR')
        if n_del:
            warn = layout.row()
            warn.alert = True
            warn.label(
                text=f"{n_del} key(s) will be permanently deleted from target.",
                icon='ERROR',
            )
        if n_skip:
            note = layout.row()
            note.alert = True
            note.label(
                text=(
                    f"{n_skip} preset key(s) are not on the reference and "
                    f"will be skipped."
                ),
                icon='INFO',
            )

        layout.separator(factor=0.5)

        toggle_icon = 'TRIA_DOWN' if self.show_keys_toggle else 'TRIA_RIGHT'
        toggle_text = (
            f"Hide changes ({n_add + n_del})"
            if self.show_keys_toggle else
            f"Show changes ({n_add + n_del})"
        )
        layout.prop(self, "show_keys_toggle", text=toggle_text,
                    toggle=True, icon=toggle_icon)

        if self.show_keys_toggle:
            row = layout.row(align=True)
            row.prop(context.scene, "skp_delete_filter", text="",
                     icon='VIEWZOOM', placeholder="Filter...")
            if context.scene.skp_delete_filter:
                row.operator("skp.delete_filter_clear", text="", icon='X')

            rows = max(5, min(30, n_add + n_del))
            layout.template_list(
                "SKP_UL_delete_preview", "",
                context.scene, "skp_delete_preview",
                context.scene, "skp_delete_preview_index",
                rows=rows,
            )

        layout.separator(factor=0.5)
        self._draw_cooldown_footer(layout, include_warning_hint=False)

    def execute(self, context):
        if self._seconds_remaining() > 0:
            self.report({'WARNING'},
                        "Please wait for the cooldown before confirming.")
            return {'CANCELLED'}

        presets = context.scene.skp_presets
        if not (0 <= self.index < len(presets)):
            self.report({'WARNING'}, "Preset index out of range.")
            return {'CANCELLED'}

        props = context.scene.skp_props
        reference = props.sync_reference
        target = props.sync_target
        ok, msg = _sync_validate(reference, target, require_same_vcount=True)
        if not ok:
            self.report({'WARNING'}, msg)
            return {'CANCELLED'}

        # Recompute the plan from the CURRENT preset/reference/target rather
        # than trusting the lists cached at invoke. The modal dialog blocks
        # edits in between (so this matches what was shown), and recomputing
        # makes redo-after-undo work and prevents applying a stale plan if the
        # preset list changed during the cooldown.
        preset = presets[self.index]
        to_add, to_delete, _missing = _preset_plan(preset, reference, target)
        if not to_add and not to_delete:
            self.report({'INFO'}, "Target already matches this preset.")
            return {'CANCELLED'}

        # 1. Add missing keys from reference. Use replace_existing=False so
        #    a key that magically appeared on target between dialog open
        #    and confirm isn't overwritten.
        added = 0
        touched = []
        for name in to_add:
            if _sync_copy_key(reference, target, name, replace_existing=False) == 'added':
                added += 1
                touched.append(name)
        _sync_fixup_relative_keys(reference, target, touched)

        # 2. Delete extras (not in preset). Skips Basis/dividers via the
        #    plan; this is the standard delete-extras pipe.
        deleted = _sync_delete_target_keys(context, target, to_delete)

        self.report(
            {'INFO'},
            f"Preset applied: {added} added, {deleted} deleted on '{target.name}'.",
        )

        SKP_OT_PresetApply._to_add = []
        SKP_OT_PresetApply._to_delete = []
        SKP_OT_PresetApply._missing_on_ref = []
        context.scene.skp_delete_preview.clear()
        return {'FINISHED'}


CLASSES = (
    SKP_OT_PresetSave,
    SKP_OT_PresetApply,
    SKP_OT_PresetUpdate,
    SKP_OT_PresetDelete,
)
