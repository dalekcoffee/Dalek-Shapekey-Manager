# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2025 Dalek (https://dalek.coffee)

"""Mesh-to-mesh shape-key transfer: copy shape keys from a Reference
mesh into a Target mesh, delete extras from a Target, and the Transfer
panel that wraps both flows in a per-key browser with filter, category,
and pagination.

The transfer feature lets users keep multiple LOD or wardrobe variants
of a character in lockstep with a single source-of-truth Reference mesh:
pick two meshes by dropdown, see which keys diverge, copy or delete in
bulk or per-row. Vertex counts must match for copy operations; topology
mismatches are surfaced as a non-blocking Basis-drift warning.

NOTE: this module's filename and the operator bl_idnames retain the
"sync" naming for backwards compatibility with existing user keybindings
and any scripts that drive these operators. Only the user-visible labels
were renamed to "Transfer" in v1.1.9.
"""

import os
import time

import bpy
import numpy as np
from bpy.app.handlers import persistent
from bpy.props import (
    BoolProperty,
    EnumProperty,
    IntProperty,
    StringProperty,
)
from bpy.types import Operator, Panel

from . import (
    _CooldownMixin,
    _SKP_REF_NONE,
    _decode_category_filter,
    _get_cache,
    _reference_file_missing,
    _shape_key_names,
    get_sync_filtered_keys,
    is_category_divider,
    total_pages,
)

# Custom-property tag stamped on temp Reference objects appended from an
# external .blend so they can be reliably found and purged later.
_SKP_TEMP_REF_TAG = "__skp_temp_ref__"


# -----------------------------------------
#  External-file Reference: load / purge
# -----------------------------------------

def _skp_purge_temp_reference(context=None):
    """Remove every temp Reference object previously appended for file-mode,
    plus their now-orphaned mesh data. Detaches any sync_reference pointer
    first. Safe to call anytime; returns the number of objects removed."""
    # Drop pointers so we don't leave a dangling reference behind.
    for scene in bpy.data.scenes:
        props = getattr(scene, "skp_props", None)
        if props is None:
            continue
        ref = props.sync_reference
        if ref is not None and ref.get(_SKP_TEMP_REF_TAG):
            props.sync_reference = None

    removed = 0
    for obj in list(bpy.data.objects):
        if not obj.get(_SKP_TEMP_REF_TAG):
            continue
        mesh = obj.data if obj.type == 'MESH' else None
        for coll in list(obj.users_collection):
            coll.objects.unlink(obj)
        bpy.data.objects.remove(obj, do_unlink=True)
        removed += 1
        if mesh is not None and mesh.users == 0:
            bpy.data.meshes.remove(mesh)
    return removed


def _skp_load_temp_reference(props):
    """Append props.sync_reference_object from props.sync_reference_file as a
    hidden, tagged temp Reference and assign it to props.sync_reference.

    Returns (obj, None) on success or (None, error_message) on failure. Any
    existing temp Reference is purged first. The appended object is NOT linked
    into a collection (so it stays invisible and clutter-free); raw shape-key
    and vertex data are still fully readable."""
    raw = props.sync_reference_file or ""
    filepath = bpy.path.abspath(raw)
    obj_name = props.sync_reference_object

    if not raw:
        return None, "Pick a .blend file first."
    if not os.path.isfile(filepath):
        return None, f"File not found: {raw}"
    if not obj_name or obj_name == _SKP_REF_NONE:
        return None, "Pick an object from the file."

    _skp_purge_temp_reference()

    try:
        with bpy.data.libraries.load(filepath, link=False) as (data_from, data_to):
            if obj_name not in data_from.objects:
                return None, f"'{obj_name}' not found in file."
            data_to.objects = [obj_name]
    except Exception as exc:  # noqa: BLE001 - surface any loader failure
        return None, f"Failed to load: {exc}"

    appended = [o for o in data_to.objects if o is not None]
    if not appended:
        return None, "Object could not be appended."
    obj = appended[0]

    if obj.type != 'MESH' or obj.data is None:
        bpy.data.objects.remove(obj, do_unlink=True)
        return None, f"'{obj_name}' is not a usable mesh."
    if obj.data.shape_keys is None:
        mesh = obj.data
        bpy.data.objects.remove(obj, do_unlink=True)
        if mesh.users == 0:
            bpy.data.meshes.remove(mesh)
        return None, f"'{obj_name}' has no shape keys."

    obj[_SKP_TEMP_REF_TAG] = True
    obj.name = f"[SKP TempRef] {obj_name}"
    props.sync_reference = obj
    return obj, None


@persistent
def _skp_save_pre_handler(_dummy):
    """Strip the temp Reference before the file is written so it never gets
    saved into the user's working file."""
    _skp_purge_temp_reference()


@persistent
def _skp_save_post_handler(_dummy):
    """Re-append the temp Reference after a save so the live session is
    uninterrupted (it was removed by the save_pre handler)."""
    for scene in bpy.data.scenes:
        props = getattr(scene, "skp_props", None)
        if props is None:
            continue
        if (getattr(props, "sync_reference_mode", 'SCENE') == 'FILE'
                and props.sync_reference_file
                and props.sync_reference_object
                and props.sync_reference_object != _SKP_REF_NONE):
            _skp_load_temp_reference(props)  # best-effort; ignore errors
            break


@persistent
def _skp_load_post_handler(_dummy):
    """Purge any stray temp Reference that somehow survived into a freshly
    opened file (defensive; save_pre should have prevented it)."""
    _skp_purge_temp_reference()


def _sync_copy_key(reference, target, name, *, replace_existing):
    """Copy one shape key from reference into target.

    Reproduces the *deformation* of the source key on target, not just its
    raw vertex positions. Concretely:

      target_kb.co[i] = target_basis.co[i] + (ref_kb.co[i] - ref_basis.co[i])

    This delta-from-Basis approach means the key looks correct on target
    even when target's Basis has drifted from reference's Basis (e.g. one
    of them had a transform applied or got minor sculpting on Basis).

    Uses `foreach_get`/`foreach_set` over a flat numpy buffer, so a 100k-
    vertex mesh copies in a millisecond instead of seconds.

    Also propagates:
      - slider min/max + interpolation type
      - the `vertex_group` mask (when target has a same-named group)
      - the `relative_key` relationship (when target has a same-named key
        to point at). The 'Basis-relative' default falls through naturally.

    Returns one of 'added', 'replaced', 'skipped', 'failed'. Assumes the
    caller already validated equal vertex counts.
    """
    if reference is None or target is None:
        return 'failed'
    if reference.data.shape_keys is None:
        return 'failed'
    ref_blocks = reference.data.shape_keys.key_blocks
    if name not in ref_blocks:
        return 'failed'
    ref_kb = ref_blocks[name]
    ref_basis = reference.data.shape_keys.reference_key
    if ref_basis is None:
        return 'failed'

    # Make sure target has a Basis so we have an anchor for the delta.
    if target.data.shape_keys is None:
        target.shape_key_add(name="Basis", from_mix=False)
    tgt_blocks = target.data.shape_keys.key_blocks
    tgt_basis = target.data.shape_keys.reference_key
    if tgt_basis is None:
        return 'failed'

    # Guard: the foreach copy over flat buffers assumes equal vertex counts.
    # A topology mismatch (or a target Basis whose count differs) would make
    # foreach_set raise mid-write and abort a bulk batch partway. Refuse cleanly
    # here so the low-level helper is self-safe even though _sync_validate
    # already checks mesh vertex counts up front. Checked BEFORE inserting so a
    # mismatch never leaves a half-created empty key behind.
    n = len(ref_kb.data)
    if len(ref_basis.data) != n or len(tgt_basis.data) != n:
        return 'failed'

    # Decide insert vs. replace before doing any expensive work.
    if name in tgt_blocks:
        if not replace_existing:
            return 'skipped'
        tgt_kb = tgt_blocks[name]
        result = 'replaced'
    else:
        tgt_kb = target.shape_key_add(name=name, from_mix=False)
        result = 'added'

    # Fast-path coord copy using foreach_get/set on a flat buffer.
    ref_co = np.empty(n * 3, dtype=np.float32)
    ref_basis_co = np.empty(n * 3, dtype=np.float32)
    tgt_basis_co = np.empty(n * 3, dtype=np.float32)
    ref_kb.data.foreach_get('co', ref_co)
    ref_basis.data.foreach_get('co', ref_basis_co)
    tgt_basis.data.foreach_get('co', tgt_basis_co)
    new_co = tgt_basis_co + (ref_co - ref_basis_co)
    tgt_kb.data.foreach_set('co', new_co)

    # Slider properties
    tgt_kb.slider_min = ref_kb.slider_min
    tgt_kb.slider_max = ref_kb.slider_max
    tgt_kb.interpolation = ref_kb.interpolation

    # Vertex-group mask: only carry the name when target has a matching group;
    # otherwise clear to avoid leaving a stale name that silently fails to mask.
    vg_name = ref_kb.vertex_group or ""
    if vg_name and vg_name in target.vertex_groups:
        tgt_kb.vertex_group = vg_name
    else:
        tgt_kb.vertex_group = ""

    # Relative-key pointer: if reference's key is relative to *another* key
    # (not Basis), try to mirror that on target. Falls back to default
    # (target's Basis) when no same-named key exists yet - a bulk fixup
    # pass after a multi-key copy can resolve forward references that
    # weren't present at the moment this key was inserted.
    ref_rel = ref_kb.relative_key
    if ref_rel is not None and ref_rel != ref_basis:
        if ref_rel.name in tgt_blocks:
            tgt_kb.relative_key = tgt_blocks[ref_rel.name]
        # else: leave whatever shape_key_add defaulted to (Basis).

    if result == 'added':
        tgt_kb.value = 0.0
    # On 'replaced', preserve existing tgt_kb.value so drivers / NLA tracks
    # referencing this slider don't snap.

    return result


def _sync_fixup_relative_keys(reference, target, names):
    """After a bulk copy, re-resolve relative_key pointers for the named
    keys on target. Handles the case where key A is relative to key B and
    both were copied in the same batch, but A was processed before B."""
    if reference is None or target is None:
        return
    if reference.data.shape_keys is None or target.data.shape_keys is None:
        return
    ref_blocks = reference.data.shape_keys.key_blocks
    tgt_blocks = target.data.shape_keys.key_blocks
    ref_basis = reference.data.shape_keys.reference_key
    for name in names:
        if name not in ref_blocks or name not in tgt_blocks:
            continue
        ref_kb = ref_blocks[name]
        ref_rel = ref_kb.relative_key
        if ref_rel is None or ref_rel == ref_basis:
            continue
        if ref_rel.name in tgt_blocks:
            tgt_blocks[name].relative_key = tgt_blocks[ref_rel.name]


def _sync_basis_drift(reference, target):
    """Max per-vertex distance between reference and target Basis positions.
    Returns None when either side lacks a Basis or vertex counts differ -
    in those cases the caller has bigger problems to surface anyway."""
    if reference is None or target is None:
        return None
    if reference.data.shape_keys is None or target.data.shape_keys is None:
        return None
    ref_basis = reference.data.shape_keys.reference_key
    tgt_basis = target.data.shape_keys.reference_key
    if ref_basis is None or tgt_basis is None:
        return None
    n = len(ref_basis.data)
    if n != len(tgt_basis.data):
        return None
    a = np.empty(n * 3, dtype=np.float32)
    b = np.empty(n * 3, dtype=np.float32)
    ref_basis.data.foreach_get('co', a)
    tgt_basis.data.foreach_get('co', b)
    diff = (a - b).reshape(n, 3)
    return float(np.linalg.norm(diff, axis=1).max())


def _sync_preview_one(reference, name, value, auto_reset):
    """Drive one shape key's slider on the reference mesh.
    Optionally zero all other non-Basis, non-divider keys (auto_reset).
    Does NOT change active_object."""
    if reference is None or reference.data is None or reference.data.shape_keys is None:
        return False
    sk = reference.data.shape_keys
    blocks = sk.key_blocks
    # Resolve the target FIRST: a stale/missing name must not zero the user's
    # posed sliders as a side effect before we bail.
    idx = blocks.find(name)
    if idx < 0:
        return False
    basis_name = sk.reference_key.name if sk.reference_key else "Basis"
    if auto_reset:
        for kb in blocks:
            if kb.name != basis_name and not is_category_divider(kb.name):
                kb.value = 0.0
    blocks[idx].value = value
    return True


def _sync_validate(reference, target, *, require_same_vcount=False, require_target_keys=False):
    """Preflight checks shared by the two sync operators.
    Returns (ok: bool, msg: str)."""
    if reference is None or target is None:
        return False, "Pick both a Reference and a Target mesh."
    if reference == target:
        return False, "Reference and Target must be different meshes."
    if reference.type != 'MESH' or target.type != 'MESH':
        return False, "Both objects must be meshes."
    if reference.data.shape_keys is None:
        return False, "Reference has no shape keys."
    if require_target_keys and target.data.shape_keys is None:
        return False, "Target has no shape keys to delete from."
    if require_same_vcount:
        rn = len(reference.data.vertices)
        tn = len(target.data.vertices)
        if rn != tn:
            return False, (
                f"Vertex count mismatch: reference has {rn}, target has {tn}. "
                f"Cannot copy shape keys between meshes with different topology."
            )
    return True, ""


def _sync_collect_extras(reference, target):
    """Names on target that are absent on reference (excluding Basis and dividers)."""
    ref_names = _shape_key_names(reference, exclude_basis=False, exclude_dividers=False)
    if target is None or target.data is None or target.data.shape_keys is None:
        return []
    sk = target.data.shape_keys
    basis_name = sk.reference_key.name if sk.reference_key else "Basis"
    return [
        kb.name for kb in sk.key_blocks
        if kb.name != basis_name
        and not is_category_divider(kb.name)
        and kb.name not in ref_names
    ]


def _sync_collect_missing(reference, target):
    """Names on reference that are absent on target (excluding Basis)."""
    if reference is None or reference.data is None or reference.data.shape_keys is None:
        return []
    sk = reference.data.shape_keys
    basis_name = sk.reference_key.name if sk.reference_key else "Basis"
    tgt_names = _shape_key_names(target, exclude_basis=False, exclude_dividers=False)
    return [
        kb.name for kb in sk.key_blocks
        if kb.name != basis_name
        and kb.name not in tgt_names
    ]


def _sync_delete_target_keys(context, target, names):
    """Remove the named shape keys from target. Uses `temp_override` +
    `bpy.ops.object.shape_key_remove` so undo and depsgraph updates fire
    correctly. Saves and restores the previous active object/mode.
    Returns the count actually deleted."""
    if not names or target is None or target.data is None or target.data.shape_keys is None:
        return 0
    prev_active = context.view_layer.objects.active
    # Capture the target's own mode unconditionally - even when target isn't the
    # active object it may be in EDIT mode (multi-object edit), and the override
    # below switches it to OBJECT. Restoring only when target==prev_active would
    # leave a non-active target stuck in OBJECT mode.
    prev_mode = target.mode
    deleted = 0
    try:
        with context.temp_override(
            active_object=target,
            object=target,
            selected_objects=[target],
            selected_editable_objects=[target],
        ):
            if target.mode != 'OBJECT':
                bpy.ops.object.mode_set(mode='OBJECT')
            for name in names:
                blocks = target.data.shape_keys.key_blocks
                idx = blocks.find(name)
                if idx < 0:
                    continue
                target.active_shape_key_index = idx
                bpy.ops.object.shape_key_remove(all=False, apply_mix=False)
                deleted += 1
    finally:
        if prev_active is not None:
            context.view_layer.objects.active = prev_active
        if target.mode != prev_mode:
            # mode_set restore can legitimately fail (e.g. object got
            # unlinked, view layer changed). Narrow to RuntimeError -
            # everything else should propagate. Surface a console line
            # so a stuck mode is at least diagnosable.
            try:
                with context.temp_override(active_object=target, object=target):
                    bpy.ops.object.mode_set(mode=prev_mode)
            except RuntimeError as err:
                print(f"[Dalek's Shapekey Manager] mode restore failed for "
                      f"'{target.name}' -> {prev_mode}: {err}")
    return deleted


def _view_focus_object(context, obj):
    """Make obj the active+only selected object and frame it in the 3D
    viewport. Returns (ok, msg).

    The active-object side effect is INTENTIONAL: it's how the Manager
    panel ends up showing obj's keys, which is half the point of this
    helper. The camera frame is the other half."""
    if obj is None:
        return False, "No mesh picked."
    if obj.name not in context.view_layer.objects:
        return False, f"'{obj.name}' isn't in the active view layer."
    if context.mode != 'OBJECT':
        return False, "Focus only works in Object Mode."

    # Deselect all (avoid bpy.ops.object.select_all so we don't depend on
    # a specific context override).
    for o in context.view_layer.objects:
        try:
            o.select_set(False)
        except RuntimeError:
            # Locked / hidden objects can refuse selection state changes;
            # skip silently rather than abort the whole operation.
            pass
    try:
        obj.select_set(True)
    except RuntimeError:
        return False, f"'{obj.name}' can't be selected (locked or hidden)."
    context.view_layer.objects.active = obj

    # Frame the now-selected obj in the first available 3D viewport. If
    # there is no 3D viewport on this screen, leave selection where it is -
    # the Manager-panel update is the load-bearing half of the job.
    for area in context.screen.areas:
        if area.type == 'VIEW_3D':
            for region in area.regions:
                if region.type == 'WINDOW':
                    with context.temp_override(area=area, region=region):
                        bpy.ops.view3d.view_selected(use_all_regions=False)
                    break
            break

    return True, f"Focused on '{obj.name}'."


class SKP_OT_SyncDeleteExtras(_CooldownMixin, Operator):
    """Delete shape keys from the Target mesh that don't exist on the Reference mesh.
    Category dividers on the Target are always preserved.
    Opens a timed confirmation dialog with a scrollable preview."""
    bl_idname = "skp.sync_delete_extras"
    bl_label = "Delete Extras from Target"
    bl_options = {'REGISTER', 'UNDO'}

    key_count: IntProperty()
    show_keys_toggle: BoolProperty(name="Show Keys", default=False)

    # _start_time lives on the mixin (instance attr, class fallback).
    _keys_to_delete: list = []

    def execute(self, context):
        if self._seconds_remaining() > 0:
            self.report({'WARNING'}, "Please wait for the cooldown before confirming.")
            return {'CANCELLED'}

        props = context.scene.skp_props
        reference = props.sync_reference
        target = props.sync_target
        ok, msg = _sync_validate(reference, target, require_target_keys=True)
        if not ok:
            self.report({'WARNING'}, msg)
            return {'CANCELLED'}

        # Recompute from current state rather than trusting the list captured
        # at invoke. The modal dialog blocks edits in between, so this matches
        # what was shown; it also makes redo-after-undo work (execute runs again
        # without invoke, so a stale/cleared class list would otherwise no-op).
        to_delete = _sync_collect_extras(reference, target)
        if not to_delete:
            self.report({'INFO'}, "No extra shape keys to delete.")
            return {'CANCELLED'}

        deleted = _sync_delete_target_keys(context, target, to_delete)

        self.report({'INFO'},
                    f"Deleted {deleted} extra shape key(s) from '{target.name}'.")
        SKP_OT_SyncDeleteExtras._keys_to_delete = []
        context.scene.skp_delete_preview.clear()
        return {'FINISHED'}

    def invoke(self, context, event):
        props = context.scene.skp_props
        reference = props.sync_reference
        target = props.sync_target
        ok, msg = _sync_validate(reference, target, require_target_keys=True)
        if not ok:
            self.report({'WARNING'}, msg)
            return {'CANCELLED'}

        self._start_time = time.time()
        SKP_OT_SyncDeleteExtras._keys_to_delete = _sync_collect_extras(reference, target)
        self.key_count = len(SKP_OT_SyncDeleteExtras._keys_to_delete)

        if self.key_count == 0:
            self.report({'INFO'}, "No extra shape keys on target.")
            return {'CANCELLED'}

        col = context.scene.skp_delete_preview
        col.clear()
        for name in SKP_OT_SyncDeleteExtras._keys_to_delete:
            item = col.add()
            item.name = name
            item.is_divider = False

        context.scene.skp_delete_preview_index = 0
        context.scene.skp_delete_filter = ""
        return context.window_manager.invoke_props_dialog(self, width=460)

    def draw(self, context):
        layout = self.layout
        props = context.scene.skp_props

        col = layout.column()
        col.alert = True
        col.label(text=f"Delete {self.key_count} extra shape key(s) from target",
                  icon='ERROR')
        col.alert = False

        layout.separator(factor=0.3)
        info = layout.column(align=True)
        info.label(text=f"Reference: {props.sync_reference.name if props.sync_reference else '(none)'}",
                   icon='OBJECT_DATA')
        info.label(text=f"Target:    {props.sync_target.name if props.sync_target else '(none)'}",
                   icon='OBJECT_DATA')

        layout.separator(factor=0.3)
        layout.label(text="These keys exist on the Target but NOT on the Reference.")
        layout.label(text="They will be permanently removed from the Target.")
        layout.label(text="Category dividers on the Target are preserved.", icon='INFO')

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


class SKP_OT_SyncCopyMissing(_CooldownMixin, Operator):
    """Copy shape keys that exist on the Reference but not on the Target
    into the Target. Requires matching vertex counts between the two meshes.
    Opens a timed confirmation dialog with a scrollable preview."""
    bl_idname = "skp.sync_copy_missing"
    bl_label = "Copy Missing to Target"
    bl_options = {'REGISTER', 'UNDO'}

    key_count: IntProperty()
    show_keys_toggle: BoolProperty(name="Show Keys", default=False)

    # _start_time lives on the mixin (instance attr, class fallback).
    _keys_to_copy: list = []

    def execute(self, context):
        if self._seconds_remaining() > 0:
            self.report({'WARNING'}, "Please wait for the cooldown before confirming.")
            return {'CANCELLED'}

        props = context.scene.skp_props
        reference = props.sync_reference
        target = props.sync_target
        ok, msg = _sync_validate(reference, target, require_same_vcount=True)
        if not ok:
            self.report({'WARNING'}, msg)
            return {'CANCELLED'}

        # Recompute from current state (see SyncDeleteExtras for rationale -
        # matches the shown list and makes redo-after-undo work).
        to_copy = _sync_collect_missing(reference, target)
        if not to_copy:
            self.report({'INFO'}, "No missing shape keys to copy.")
            return {'CANCELLED'}

        added = 0
        failed = 0
        touched = []
        for name in to_copy:
            # SyncCopyMissing only ever targets missing keys, so
            # replace_existing is moot - skip-on-collision keeps behaviour
            # predictable if state changed since the dialog opened.
            res = _sync_copy_key(reference, target, name, replace_existing=False)
            if res == 'added':
                added += 1
                touched.append(name)
            elif res == 'failed':
                failed += 1

        # Resolve relative_key pointers that referred to keys also copied
        # in this batch but inserted after the dependent key.
        _sync_fixup_relative_keys(reference, target, touched)

        if failed:
            self.report(
                {'WARNING'},
                f"Copied {added} shape key(s) into '{target.name}'; {failed} "
                f"failed (vertex-count mismatch?).",
            )
        else:
            self.report({'INFO'},
                        f"Copied {added} shape key(s) into '{target.name}'.")
        SKP_OT_SyncCopyMissing._keys_to_copy = []
        context.scene.skp_delete_preview.clear()
        return {'FINISHED'}

    def invoke(self, context, event):
        props = context.scene.skp_props
        reference = props.sync_reference
        target = props.sync_target
        ok, msg = _sync_validate(reference, target, require_same_vcount=True)
        if not ok:
            self.report({'WARNING'}, msg)
            return {'CANCELLED'}

        self._start_time = time.time()
        SKP_OT_SyncCopyMissing._keys_to_copy = _sync_collect_missing(reference, target)
        self.key_count = len(SKP_OT_SyncCopyMissing._keys_to_copy)

        if self.key_count == 0:
            self.report({'INFO'}, "No missing shape keys to copy.")
            return {'CANCELLED'}

        col = context.scene.skp_delete_preview
        col.clear()
        for name in SKP_OT_SyncCopyMissing._keys_to_copy:
            item = col.add()
            item.name = name
            item.is_divider = is_category_divider(name)

        context.scene.skp_delete_preview_index = 0
        context.scene.skp_delete_filter = ""
        return context.window_manager.invoke_props_dialog(self, width=460)

    def draw(self, context):
        layout = self.layout
        props = context.scene.skp_props

        col = layout.column()
        col.label(text=f"Copy {self.key_count} shape key(s) into target",
                  icon='IMPORT')

        layout.separator(factor=0.3)
        info = layout.column(align=True)
        info.label(text=f"Reference: {props.sync_reference.name if props.sync_reference else '(none)'}",
                   icon='OBJECT_DATA')
        info.label(text=f"Target:    {props.sync_target.name if props.sync_target else '(none)'}",
                   icon='OBJECT_DATA')

        layout.separator(factor=0.3)
        layout.label(text="These keys exist on the Reference but NOT on the Target.")
        layout.label(text="Vertex positions are copied per-vertex from the Reference.")
        layout.label(text="Slider min/max and interpolation are also copied; value resets to 0.",
                     icon='INFO')

        layout.separator(factor=0.5)

        toggle_icon = 'TRIA_DOWN' if self.show_keys_toggle else 'TRIA_RIGHT'
        toggle_text = (
            f"Hide keys to be added ({self.key_count})"
            if self.show_keys_toggle else
            f"Show keys to be added ({self.key_count})"
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

        self._draw_cooldown_footer(layout, include_warning_hint=False)


class SKP_OT_SyncCopyOne(Operator):
    """Copy a single shape key from the Reference mesh into the Target mesh.
    Always replaces the key on the Target if it already exists."""
    bl_idname = "skp.sync_copy_one"
    bl_label = "Copy Shape Key to Target"
    bl_options = {'REGISTER', 'UNDO'}

    key_name: StringProperty()

    def execute(self, context):
        props = context.scene.skp_props
        reference = props.sync_reference
        target = props.sync_target
        ok, msg = _sync_validate(reference, target, require_same_vcount=True)
        if not ok:
            self.report({'WARNING'}, msg)
            return {'CANCELLED'}

        result = _sync_copy_key(reference, target, self.key_name,
                                replace_existing=True)
        if result == 'added':
            self.report({'INFO'}, f"Added '{self.key_name}' to '{target.name}'.")
        elif result == 'replaced':
            self.report({'INFO'},
                        f"Replaced '{self.key_name}' on '{target.name}'.")
        else:
            self.report({'WARNING'},
                        f"Could not copy '{self.key_name}' ({result}).")
            return {'CANCELLED'}
        return {'FINISHED'}


class SKP_OT_SyncCopyFiltered(_CooldownMixin, Operator):
    """Copy every key currently shown in the Transfer browser (after filter,
    category, and only-missing toggle). Respects 'Skip Existing'.
    Opens a timed confirmation dialog with a scrollable preview."""
    bl_idname = "skp.sync_copy_filtered"
    bl_label = "Copy Visible Keys to Target"
    bl_options = {'REGISTER', 'UNDO'}

    key_count: IntProperty()
    show_keys_toggle: BoolProperty(name="Show Keys", default=False)

    # _start_time lives on the mixin (instance attr, class fallback).
    _keys_to_copy: list = []
    _will_replace: int = 0
    _will_add: int = 0
    _will_skip: int = 0

    def _collect(self, context):
        props = context.scene.skp_props
        reference = props.sync_reference
        target = props.sync_target
        visible = get_sync_filtered_keys(reference, target, props)
        tgt_names = _shape_key_names(target, exclude_basis=False)
        ref_sk = reference.data.shape_keys if reference and reference.data else None
        basis_name = (ref_sk.reference_key.name
                      if ref_sk and ref_sk.reference_key else "Basis")
        skip = props.sync_skip_existing
        to_copy = []
        will_replace = 0
        will_add = 0
        will_skip = 0
        for _i, kb in visible:
            if kb.name == basis_name:
                continue
            exists = kb.name in tgt_names
            if exists and skip:
                will_skip += 1
                continue
            to_copy.append(kb.name)
            if exists:
                will_replace += 1
            else:
                will_add += 1
        return to_copy, will_add, will_replace, will_skip

    def execute(self, context):
        if self._seconds_remaining() > 0:
            self.report({'WARNING'}, "Please wait for the cooldown before confirming.")
            return {'CANCELLED'}

        props = context.scene.skp_props
        reference = props.sync_reference
        target = props.sync_target
        ok, msg = _sync_validate(reference, target, require_same_vcount=True)
        if not ok:
            self.report({'WARNING'}, msg)
            return {'CANCELLED'}

        # Recompute from current state (see SyncDeleteExtras for rationale).
        to_copy, _wa, _wr, _ws = self._collect(context)
        if not to_copy:
            self.report({'INFO'}, "Nothing to copy.")
            return {'CANCELLED'}

        replace = not props.sync_skip_existing
        added = replaced = failed = 0
        touched = []
        for name in to_copy:
            result = _sync_copy_key(reference, target, name,
                                    replace_existing=replace)
            if result == 'added':
                added += 1
                touched.append(name)
            elif result == 'replaced':
                replaced += 1
                touched.append(name)
            elif result == 'failed':
                failed += 1

        _sync_fixup_relative_keys(reference, target, touched)

        parts = []
        if added:
            parts.append(f"{added} added")
        if replaced:
            parts.append(f"{replaced} replaced")
        if failed:
            parts.append(f"{failed} failed")
        summary = ", ".join(parts) if parts else "nothing copied"
        self.report({'WARNING'} if failed else {'INFO'}, f"Transfer: {summary}.")

        SKP_OT_SyncCopyFiltered._keys_to_copy = []
        context.scene.skp_delete_preview.clear()
        return {'FINISHED'}

    def invoke(self, context, event):
        props = context.scene.skp_props
        reference = props.sync_reference
        target = props.sync_target
        ok, msg = _sync_validate(reference, target, require_same_vcount=True)
        if not ok:
            self.report({'WARNING'}, msg)
            return {'CANCELLED'}

        to_copy, will_add, will_replace, will_skip = self._collect(context)
        self._start_time = time.time()
        SKP_OT_SyncCopyFiltered._keys_to_copy = to_copy
        SKP_OT_SyncCopyFiltered._will_add = will_add
        SKP_OT_SyncCopyFiltered._will_replace = will_replace
        SKP_OT_SyncCopyFiltered._will_skip = will_skip
        self.key_count = len(to_copy)

        if self.key_count == 0:
            if will_skip:
                self.report({'INFO'},
                            f"All visible keys already exist on target ({will_skip} skipped).")
            else:
                self.report({'INFO'}, "No keys to copy.")
            return {'CANCELLED'}

        col = context.scene.skp_delete_preview
        col.clear()
        for name in to_copy:
            item = col.add()
            item.name = name
            item.is_divider = is_category_divider(name)

        context.scene.skp_delete_preview_index = 0
        context.scene.skp_delete_filter = ""
        return context.window_manager.invoke_props_dialog(self, width=460)

    def draw(self, context):
        layout = self.layout
        props = context.scene.skp_props

        col = layout.column()
        col.label(text=f"Copy {self.key_count} key(s) to target", icon='IMPORT')

        layout.separator(factor=0.3)
        info = layout.column(align=True)
        info.label(text=f"Reference: {props.sync_reference.name if props.sync_reference else '(none)'}",
                   icon='OBJECT_DATA')
        info.label(text=f"Target:    {props.sync_target.name if props.sync_target else '(none)'}",
                   icon='OBJECT_DATA')

        layout.separator(factor=0.3)
        breakdown = layout.column(align=True)
        breakdown.label(
            text=f"Add: {SKP_OT_SyncCopyFiltered._will_add}   "
                 f"Replace: {SKP_OT_SyncCopyFiltered._will_replace}   "
                 f"Skip (already on target): {SKP_OT_SyncCopyFiltered._will_skip}",
        )
        if SKP_OT_SyncCopyFiltered._will_replace and not props.sync_skip_existing:
            warn = layout.row()
            warn.alert = True
            warn.label(
                text=(
                    f"{SKP_OT_SyncCopyFiltered._will_replace} existing key(s) "
                    f"will be OVERWRITTEN."
                ),
                icon='ERROR',
            )

        layout.separator(factor=0.5)

        toggle_icon = 'TRIA_DOWN' if self.show_keys_toggle else 'TRIA_RIGHT'
        toggle_text = (
            f"Hide keys to be copied ({self.key_count})"
            if self.show_keys_toggle else
            f"Show keys to be copied ({self.key_count})"
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

        self._draw_cooldown_footer(layout, include_warning_hint=False)


class SKP_OT_SyncPreviewKey(Operator):
    """Drive a shape key slider on the Reference mesh to the preview value.
    Respects Auto Reset and Preview Value from the Manager panel's preview
    settings. Does NOT change which object is active."""
    bl_idname = "skp.sync_preview_key"
    bl_label = "Preview Reference Shape Key"
    bl_options = {'REGISTER', 'UNDO'}

    key_name: StringProperty()
    extend: BoolProperty(default=False, options={'SKIP_SAVE'})

    def invoke(self, context, event):
        self.extend = event.shift
        return self.execute(context)

    def execute(self, context):
        props = context.scene.skp_props
        reference = props.sync_reference
        if reference is None:
            self.report({'WARNING'}, "No reference mesh picked.")
            return {'CANCELLED'}
        if is_category_divider(self.key_name):
            self.report({'WARNING'}, "Cannot preview a category divider.")
            return {'CANCELLED'}
        ok = _sync_preview_one(
            reference,
            self.key_name,
            props.preview_value,
            auto_reset=props.auto_reset and not self.extend,
        )
        if not ok:
            self.report({'WARNING'},
                        f"Could not preview '{self.key_name}' on reference.")
            return {'CANCELLED'}
        return {'FINISHED'}


class SKP_OT_SyncResetReference(Operator):
    """Reset every shape key on the Reference mesh to 0 (except Basis and
    category dividers)."""
    bl_idname = "skp.sync_reset_reference"
    bl_label = "Reset Reference Sliders"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        reference = context.scene.skp_props.sync_reference
        if reference is None or reference.data is None or reference.data.shape_keys is None:
            self.report({'WARNING'}, "Reference has no shape keys.")
            return {'CANCELLED'}
        sk = reference.data.shape_keys
        basis_name = sk.reference_key.name if sk.reference_key else "Basis"
        for kb in sk.key_blocks:
            if kb.name != basis_name and not is_category_divider(kb.name):
                kb.value = 0.0
        self.report({'INFO'}, f"Reset all sliders on '{reference.name}'.")
        return {'FINISHED'}


class SKP_OT_SyncFilterClear(Operator):
    """Clear the Transfer browser's text filter."""
    bl_idname = "skp.sync_filter_clear"
    bl_label = "Clear Transfer Filter"

    def execute(self, context):
        context.scene.skp_props.sync_filter = ""
        return {'FINISHED'}


class SKP_OT_SyncPageNext(Operator):
    bl_idname = "skp.sync_page_next"
    bl_label = "Transfer: Next Page"

    def execute(self, context):
        props = context.scene.skp_props
        filtered = get_sync_filtered_keys(props.sync_reference, props.sync_target, props)
        max_page = total_pages(filtered, props.page_size) - 1
        clamped = min(props.sync_current_page, max_page)
        props.sync_current_page = min(clamped + 1, max_page)
        return {'FINISHED'}


class SKP_OT_SyncPagePrev(Operator):
    bl_idname = "skp.sync_page_prev"
    bl_label = "Transfer: Previous Page"

    def execute(self, context):
        props = context.scene.skp_props
        filtered = get_sync_filtered_keys(props.sync_reference, props.sync_target, props)
        max_page = total_pages(filtered, props.page_size) - 1
        clamped = min(props.sync_current_page, max_page)
        props.sync_current_page = max(clamped - 1, 0)
        return {'FINISHED'}


class SKP_OT_SyncPageFirst(Operator):
    bl_idname = "skp.sync_page_first"
    bl_label = "Transfer: First Page"

    def execute(self, context):
        context.scene.skp_props.sync_current_page = 0
        return {'FINISHED'}


class SKP_OT_SyncPageLast(Operator):
    bl_idname = "skp.sync_page_last"
    bl_label = "Transfer: Last Page"

    def execute(self, context):
        props = context.scene.skp_props
        filtered = get_sync_filtered_keys(props.sync_reference, props.sync_target, props)
        props.sync_current_page = total_pages(filtered, props.page_size) - 1
        return {'FINISHED'}


class SKP_OT_SyncFocusMesh(Operator):
    """Frame the picked Reference or Target mesh in the 3D viewport and
    make it the active object. The Shapekey Manager panel will switch to
    show that mesh's keys - useful for validating copies made via the
    Transfer panel."""
    bl_idname = "skp.sync_focus_mesh"
    bl_label = "Focus Mesh"
    bl_options = {'REGISTER', 'UNDO'}

    mesh_role: EnumProperty(
        items=[
            ('reference', "Reference", "Focus the picked Reference mesh"),
            ('target',    "Target",    "Focus the picked Target mesh"),
        ],
        default='reference',
    )

    @classmethod
    def description(cls, context, properties):
        role = properties.mesh_role
        if role == 'reference':
            return (
                "Frame the Reference mesh in the viewport and make it the "
                "active object.\n"
                "The Shapekey Manager panel will switch to show "
                "Reference's keys."
            )
        return (
            "Frame the Target mesh in the viewport and make it the active "
            "object.\n"
            "The Shapekey Manager panel will switch to show Target's keys "
            "- use this to validate that copies landed."
        )

    def execute(self, context):
        props = context.scene.skp_props
        obj = (props.sync_reference if self.mesh_role == 'reference'
               else props.sync_target)
        ok, msg = _view_focus_object(context, obj)
        self.report({'INFO'} if ok else {'WARNING'}, msg)
        return {'FINISHED'} if ok else {'CANCELLED'}


class SKP_OT_SyncStatusInfo(Operator):
    """Hover for status info. Click to print the status to the Info area.

    Used as the per-row status indicator in the reference browser so that
    each icon (checkmark / plus) carries a hoverable tooltip - which a
    plain layout.label() cannot do."""
    bl_idname = "skp.sync_status_info"
    bl_label = "Status"
    bl_options = {'INTERNAL'}

    key_name: StringProperty()
    exists: BoolProperty(default=False)

    @classmethod
    def description(cls, context, properties):
        # Dynamic tooltip: tells the user exactly what would happen if they
        # clicked Copy on this row, named for the specific key under cursor.
        n = properties.key_name or "this key"
        if properties.exists:
            return (
                f"'{n}' already exists on the Target.\n"
                f"Clicking Copy on this row will OVERWRITE the target's "
                f"version with the reference's data."
            )
        return (
            f"'{n}' is MISSING from the Target.\n"
            f"Clicking Copy on this row will ADD this key, pulling the "
            f"shape data from the reference."
        )

    def execute(self, context):
        state = "exists on target" if self.exists else "missing from target"
        self.report({'INFO'}, f"'{self.key_name}' {state}.")
        return {'FINISHED'}


class SKP_OT_SyncLoadReference(Operator):
    """Append the chosen mesh from the external .blend file as a temporary,
    hidden Reference. Run it again to refresh from disk after editing the
    source file. The temp Reference is never written into your working file."""
    bl_idname = "skp.sync_load_reference"
    bl_label = "Load / Refresh Reference from File"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.skp_props
        obj, err = _skp_load_temp_reference(props)
        if err:
            self.report({'ERROR'}, err)
            return {'CANCELLED'}
        n_keys = len(obj.data.shape_keys.key_blocks)
        n_verts = len(obj.data.vertices)
        self.report(
            {'INFO'},
            f"Loaded '{props.sync_reference_object}': {n_keys} key(s), "
            f"{n_verts} verts (temporary).",
        )
        return {'FINISHED'}


class SKP_OT_SyncReleaseReference(Operator):
    """Remove the temporary file-loaded Reference and free its data."""
    bl_idname = "skp.sync_release_reference"
    bl_label = "Release Reference"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        n = _skp_purge_temp_reference(context)
        self.report(
            {'INFO'},
            "Released temporary Reference." if n
            else "No temporary Reference was loaded.",
        )
        return {'FINISHED'}


# -----------------------------------------
#  Sync panel + presets sub-panel
# -----------------------------------------

class SKP_PT_SyncPanel(Panel):
    """Top-level sibling of the Shapekey Manager panel. Transfers shape
    keys between two meshes: delete extras from a Target, or selectively
    copy keys from a Reference into a Target with filter / category /
    per-row control."""
    bl_label = "Dalek's Shapekey Transfer"
    bl_idname = "SKP_PT_sync"
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = "data"
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 2

    @classmethod
    def poll(cls, context):
        # Looser than the Manager panel: this panel works on meshes picked
        # from dropdowns, so the active mesh just needs to exist (Properties
        # > Data already filters to mesh-data context).
        obj = context.active_object
        return obj is not None and obj.type == 'MESH'

    def draw(self, context):
        layout = self.layout
        props = context.scene.skp_props

        layout.label(text="Read from Reference, write to Target.", icon='INFO')

        # --- Mesh pickers ---
        box = layout.box()
        col = box.column(align=True)

        mode_row = col.row(align=True)
        mode_row.prop(props, "sync_reference_mode", expand=True)

        if props.sync_reference_mode == 'SCENE':
            row = col.row(align=True)
            row.prop(props, "sync_reference", text="Reference")
            focus_ref = row.operator(
                "skp.sync_focus_mesh", text="", icon='ZOOM_SELECTED',
            )
            focus_ref.mesh_role = 'reference'
        else:
            # --- From .blend file ---
            col.prop(props, "sync_reference_file", text="File")
            has_file = bool(props.sync_reference_file)
            file_missing = _reference_file_missing(props.sync_reference_file)

            if file_missing:
                err = col.row()
                err.alert = True
                err.label(text="File not found - check the path.", icon='ERROR')

            file_ok = has_file and not file_missing

            obj_row = col.row(align=True)
            obj_row.enabled = file_ok
            obj_row.prop(props, "sync_reference_object", text="Mesh")

            load_row = col.row(align=True)
            load_row.enabled = file_ok
            load_row.operator(
                "skp.sync_load_reference", text="Load / Refresh", icon='IMPORT',
            )
            ref = props.sync_reference
            is_loaded = ref is not None and ref.get("__skp_temp_ref__")
            if is_loaded:
                load_row.operator(
                    "skp.sync_release_reference", text="", icon='X',
                )
                status = col.row()
                status.label(
                    text=f"Loaded: {props.sync_reference_object} (temporary)",
                    icon='CHECKMARK',
                )
            elif file_ok:
                status = col.row()
                status.enabled = False
                status.label(
                    text="Not loaded - click Load / Refresh.", icon='INFO',
                )

        row = col.row(align=True)
        row.prop(props, "sync_target", text="Target")
        focus_tgt = row.operator(
            "skp.sync_focus_mesh", text="", icon='ZOOM_SELECTED',
        )
        focus_tgt.mesh_role = 'target'

        hint = box.row()
        hint.enabled = False
        hint.label(
            text="Tip: Focus buttons frame the mesh AND switch Manager to it.",
            icon='INFO',
        )

        reference = props.sync_reference
        target = props.sync_target

        # --- Status ---
        status_box = layout.box()
        status_col = status_box.column(align=True)

        ok, msg = _sync_validate(reference, target)
        if not ok:
            row = status_col.row()
            row.alert = True
            row.label(text=msg, icon='ERROR')
            return

        ref_count = len(_shape_key_names(reference, exclude_basis=False))
        tgt_count = len(_shape_key_names(target, exclude_basis=False))
        status_col.label(
            text=f"Ref: {ref_count} key(s)   |   Tgt: {tgt_count} key(s)",
            icon='SHAPEKEY_DATA',
        )

        extras = _sync_collect_extras(reference, target)
        missing = _sync_collect_missing(reference, target)
        status_col.label(
            text=f"Missing on tgt: {len(missing)}   |   Extras on tgt: {len(extras)}",
        )

        vcount_ok = len(reference.data.vertices) == len(target.data.vertices)
        if not vcount_ok:
            row = status_col.row()
            row.alert = True
            row.label(
                text=(
                    f"Vertex count differs (ref {len(reference.data.vertices)}, "
                    f"tgt {len(target.data.vertices)}) - Copy disabled."
                ),
                icon='ERROR',
            )
        else:
            # Topology-mismatch sniff test: same vert count is necessary but
            # not sufficient. If the two Bases disagree by more than a small
            # threshold, surface a non-blocking warning. Either the meshes
            # legitimately have drifted Basis (copy still works thanks to
            # delta-from-Basis), or vertex indices map to different physical
            # points (copy will produce wrong shapes).
            drift = _sync_basis_drift(reference, target)
            if drift is not None and drift > 1e-4:
                row = status_col.row()
                row.alert = True
                row.label(
                    text=(
                        f"Basis vertices differ by up to {drift:.4f} - "
                        f"meshes may have diverged. Verify a copy before "
                        f"trusting bulk operations."
                    ),
                    icon='ERROR',
                )

        # --- Bulk Delete (kept simple) ---
        layout.separator(factor=0.4)
        layout.label(text="Delete Extras from Target", icon='TRASH')
        del_row = layout.row(align=True)
        del_row.enabled = len(extras) > 0
        del_row.operator(
            "skp.sync_delete_extras",
            text=f"Delete All Extras ({len(extras)})",
            icon='TRASH',
        )

        # --- Reference browser ---
        layout.separator(factor=0.4)
        layout.label(text="Browse Reference & Copy", icon='IMPORT')

        ref_cache = _get_cache(reference)
        ref_has_categories = bool(ref_cache and ref_cache.get('has_categories'))

        filter_box = layout.box()
        row = filter_box.row(align=True)
        row.prop(props, "sync_filter", text="", icon='VIEWZOOM',
                 placeholder="Filter reference keys...")
        if props.sync_filter:
            row.operator("skp.sync_filter_clear", text="", icon='X')

        row = filter_box.row(align=True)
        row.prop(props, "sync_sort_mode", text="")
        if ref_has_categories:
            row.prop(props, "sync_category_filter", text="",
                     icon='OUTLINER_COLLECTION')

        row = filter_box.row(align=True)
        row.prop(props, "sync_skip_existing", toggle=True)
        row.prop(props, "sync_show_only_missing", toggle=True)

        # Compute the visible set once for status, pagination, and rendering
        visible = get_sync_filtered_keys(reference, target, props)
        n_visible = len(visible)
        tgt_names = _shape_key_names(target, exclude_basis=False)

        # Visible breakdown (informational)
        v_missing = sum(1 for _i, kb in visible if kb.name not in tgt_names)
        v_existing = n_visible - v_missing
        filter_box.label(
            text=f"Visible: {n_visible}   (missing: {v_missing}, existing: {v_existing})",
        )

        # --- Pagination ---
        num_pages = total_pages(visible, props.page_size)
        current_page = max(0, min(props.sync_current_page, num_pages - 1))
        page_start = current_page * props.page_size
        page_end = page_start + props.page_size

        if num_pages > 1:
            pager = layout.row(align=True)
            pager.operator("skp.sync_page_first", text="", icon='REW')
            pager.operator("skp.sync_page_prev",  text="", icon='TRIA_LEFT')
            pager.label(text=f"Page {current_page + 1} / {num_pages}")
            pager.operator("skp.sync_page_next",  text="", icon='TRIA_RIGHT')
            pager.operator("skp.sync_page_last",  text="", icon='FF')

        # --- Key list ---
        layout.separator(factor=0.2)

        # Legend row so users don't have to hover the icons to learn what
        # the per-row status symbols mean. Hover-tooltips on the icons
        # themselves still work (via SKP_OT_SyncStatusInfo).
        legend = layout.row(align=True)
        legend.enabled = False
        legend.label(text="Legend:")
        legend.label(text="on target", icon='CHECKMARK')
        legend.label(text="missing", icon='ADD')

        page_keys = visible[page_start:page_end]

        if not page_keys:
            layout.label(text="No reference keys match the current filter.",
                         icon='INFO')
        else:
            col = layout.column(align=True)

            # Inject category headers when sort is NONE and All/top is selected,
            # mirroring the Manager panel's behaviour for visual consistency.
            cat_filter = props.sync_category_filter
            _vkind, _vlabel = _decode_category_filter(cat_filter, ref_cache) if ref_cache else (None, cat_filter)
            viewing_top = _vkind == 'top' and cat_filter != 'ALL'
            viewing_all = cat_filter == 'ALL'
            inject_headers = (
                props.sync_sort_mode == 'NONE'
                and (viewing_all or viewing_top)
                and ref_has_categories
            )
            full_info = ref_cache['full_info'] if (inject_headers and ref_cache) else {}
            last_shown_top = object()
            last_shown_sub = object()

            for idx, kb in page_keys:
                if inject_headers:
                    d = full_info.get(kb.name, {})
                    this_top = d.get('parent') or ''
                    this_sub = d.get('sub') or ''
                    if viewing_all and this_top != last_shown_top:
                        last_shown_top = this_top
                        last_shown_sub = object()
                        if this_top:
                            hdr = col.row()
                            hdr.enabled = False
                            hdr.label(text=this_top, icon='OUTLINER_COLLECTION')
                    if this_sub and this_sub != last_shown_sub:
                        last_shown_sub = this_sub
                        hdr = col.row()
                        hdr.enabled = False
                        indent = "    " if viewing_all else "  "
                        hdr.label(text=f"{indent}{this_sub}", icon='DOT')

                exists = kb.name in tgt_names
                status_icon = 'CHECKMARK' if exists else 'ADD'

                row = col.row(align=True)

                # Status icon as an emboss-less operator so that hovering
                # surfaces a per-key tooltip explaining the state.
                status = row.row()
                status.ui_units_x = 1.0
                status.alert = not exists
                info_op = status.operator(
                    "skp.sync_status_info", text="", icon=status_icon, emboss=False,
                )
                info_op.key_name = kb.name
                info_op.exists = exists

                name_col = row.row()
                name_col.label(text=kb.name, icon='SHAPEKEY_DATA')

                if not is_category_divider(kb.name):
                    prev_op = row.operator("skp.sync_preview_key", text="", icon='PLAY')
                    prev_op.key_name = kb.name

                    copy_btn = row.row()
                    copy_btn.enabled = vcount_ok
                    cop = copy_btn.operator(
                        "skp.sync_copy_one", text="", icon='IMPORT',
                    )
                    cop.key_name = kb.name

        # --- Bulk action row ---
        layout.separator(factor=0.4)
        action_row = layout.row(align=True)
        action_row.operator(
            "skp.sync_reset_reference", text="Reset Reference", icon='LOOP_BACK',
        )

        cf_col = action_row.column(align=True)
        cf_col.enabled = vcount_ok and n_visible > 0
        cf_col.operator(
            "skp.sync_copy_filtered",
            text=f"Copy Visible ({n_visible})",
            icon='IMPORT',
        )

        cm_col = action_row.column(align=True)
        cm_col.enabled = vcount_ok and len(missing) > 0
        cm_col.operator(
            "skp.sync_copy_missing",
            text=f"Copy All Missing ({len(missing)})",
            icon='IMPORT',
        )

        layout.separator(factor=0.2)
        page_hint = layout.row()
        page_hint.enabled = False
        if n_visible:
            shown_end = min(page_end, n_visible)
            page_hint.label(
                text=f"Showing {page_start + 1}-{shown_end} of {n_visible}",
                icon='INFO',
            )


class SKP_PT_SyncPresets(Panel):
    """Named blendshape-list presets for a target mesh. Each preset stores
    just the shape-key NAMES that should exist on the target - actual shape
    data is pulled from the Reference at apply time."""
    bl_label = "Presets"
    bl_idname = "SKP_PT_sync_presets"
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = "data"
    bl_parent_id = "SKP_PT_sync"
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return SKP_PT_SyncPanel.poll(context)

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        props = scene.skp_props
        presets = scene.skp_presets

        # Save action - capture current target as a new preset.
        save_row = layout.row(align=True)
        target_ok = (
            props.sync_target is not None
            and props.sync_target.data is not None
            and props.sync_target.data.shape_keys is not None
        )
        save_row.enabled = target_ok
        save_row.operator(
            "skp.preset_save",
            text="Save Current Target as Preset",
            icon='ADD',
        )

        if not target_ok:
            note = layout.row()
            note.enabled = False
            note.label(
                text="Pick a Target mesh with shape keys to enable saving.",
                icon='INFO',
            )

        layout.separator(factor=0.3)

        # Existing presets list
        if not presets:
            empty = layout.column()
            empty.enabled = False
            empty.label(text="No presets saved yet.", icon='INFO')
            return

        reference = props.sync_reference
        target = props.sync_target
        sync_ok, _msg = _sync_validate(reference, target, require_same_vcount=True)

        for i, preset in enumerate(presets):
            box = layout.box()
            row = box.row(align=True)

            # Editable name (in-place rename).
            row.prop(preset, "name", text="")
            row.label(text=f"({len(preset.keys)})")

            # Per-row actions.
            actions = box.row(align=True)

            apply_col = actions.column(align=True)
            apply_col.enabled = sync_ok
            apply_op = apply_col.operator(
                "skp.preset_apply", text="Apply", icon='CHECKMARK',
            )
            apply_op.index = i

            upd_col = actions.column(align=True)
            upd_col.enabled = target_ok
            upd_op = upd_col.operator(
                "skp.preset_update", text="Update", icon='FILE_REFRESH',
            )
            upd_op.index = i

            del_op = actions.operator(
                "skp.preset_delete", text="", icon='X',
            )
            del_op.index = i

            if preset.source_reference:
                hint = box.row()
                hint.enabled = False
                hint.label(text=f"captured from: {preset.source_reference}",
                           icon='OBJECT_DATA')


CLASSES = (
    SKP_OT_SyncDeleteExtras,
    SKP_OT_SyncCopyMissing,
    SKP_OT_SyncCopyOne,
    SKP_OT_SyncCopyFiltered,
    SKP_OT_SyncPreviewKey,
    SKP_OT_SyncResetReference,
    SKP_OT_SyncFilterClear,
    SKP_OT_SyncPageNext,
    SKP_OT_SyncPagePrev,
    SKP_OT_SyncPageFirst,
    SKP_OT_SyncPageLast,
    SKP_OT_SyncFocusMesh,
    SKP_OT_SyncStatusInfo,
    SKP_OT_SyncLoadReference,
    SKP_OT_SyncReleaseReference,
    SKP_PT_SyncPanel,
    SKP_PT_SyncPresets,
)
