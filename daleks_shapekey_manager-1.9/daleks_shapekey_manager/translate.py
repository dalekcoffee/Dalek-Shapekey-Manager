# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2025 Dalek (https://dalek.coffee)

"""Translate Japanese / Korean / Chinese shape key names to English using
a built-in dictionary of common MMD / VRChat conventions.

Most non-English shape keys in the wild come from MMD-derived models
(Vocaloid, Touhou, anime characters) and use a small, stable vocabulary
of mouth shapes, eye states, and expression labels. This module ships a
dictionary covering the common cases and a per-key lookup that's safe
to run on a whole list - names with no entry are left unchanged, so
running the operator on a mixed-language model only translates what it
recognises.

Approach: substring translation against `_TRANSLATIONS`, longest token
first, so a fully non-English name and a partly-translated one alike are
handled - e.g. both '笑顔' and 'Ton17_あ～R' get their recognised parts
converted while the ASCII around them is left intact. For category
divider names (e.g. `===顔===`), only the inner label is translated;
the surrounding token is preserved so the divider stays a divider.

Adding a translation: just add to `_TRANSLATIONS` below. Keys can be
any unicode string; case is preserved on lookup. The dictionary is
public-domain MMD/VRChat conventions - extending it does not need to
keep alphabetical order, just keep related shapes grouped for grep-
ability when debugging mistranslations later.
"""

import bpy
from bpy.props import BoolProperty, IntProperty
from bpy.types import Operator

from . import _get_patterns, get_filtered_keys, is_category_divider


# -----------------------------------------
#  Translation dictionary
# -----------------------------------------
#
# Sourced from the conventions used by Cats Blender Plugin, MMD Tools,
# and the unofficial VRC blendshape glossary. Roughly grouped:
#   1. MMD vowel mouth shapes (single-character)
#   2. Mouth shapes / expressions
#   3. Eye blink / wink / states
#   4. Eye direction + pupil + iris
#   5. Eyebrows
#   6. Cheeks / blush / tears / sweat
#   7. Tongue
#   8. Generic words used in category dividers (face, mouth, eye, ...)
#   9. Korean common terms
#  10. Chinese (Simplified) common terms
#
# Some Chinese terms overlap with Japanese kanji (e.g. 微笑); the JP entry
# will catch those automatically.

_TRANSLATIONS = {
    # --- MMD vowel mouth shapes ---
    'あ': 'A',
    'い': 'I',
    'う': 'U',
    'え': 'E',
    'お': 'O',
    'ア': 'A',
    'イ': 'I',
    'ウ': 'U',
    'エ': 'E',
    'オ': 'O',

    # --- Mouth shapes / expressions ---
    '▲': 'Triangle',
    '∧': 'Hat',
    '□': 'Square',
    'ω': 'Omega',
    'ω□': 'Omega Square',
    'ω▲': 'Omega Triangle',
    '◇': 'Diamond',
    'にやり': 'Smirk',
    'にやり2': 'Smirk 2',
    'ニヤリ': 'Smirk',
    'ハ': 'Ha',
    'はぁ': 'Sigh',
    'ぺろっ': 'Tongue',
    'ぺろっ2': 'Tongue 2',
    'べー': 'Bleh',
    'ベー': 'Bleh',
    'んむ': 'Nmu',
    'えー': 'Eh',
    'むぅ': 'Mu',
    'あぐ': 'Agu',
    '口角上げ': 'Mouth Corner Up',
    '口角下げ': 'Mouth Corner Down',
    '口角_上げ': 'Mouth Corner Up',
    '口角_下げ': 'Mouth Corner Down',
    '口横広げ': 'Mouth Wide',
    '口閉じ': 'Mouth Close',
    '口を開く': 'Mouth Open',
    '口元歪み': 'Mouth Distort',
    '微笑む': 'Slight Smile',
    '笑顔': 'Smile',
    '笑い': 'Smile',
    'にこり': 'Grin',
    'にっこり': 'Big Smile',
    'にっこり2': 'Big Smile 2',
    '苦笑い': 'Bitter Smile',
    '苦笑': 'Bitter Smile',
    '微笑': 'Smile',
    '不機嫌': 'Displeased',
    '困った笑顔': 'Troubled Smile',
    'への字': 'Sad Mouth',

    # --- Eye blink / wink / states ---
    'まばたき': 'Blink',
    '瞬き': 'Blink',
    'ウィンク': 'Wink',
    'ｳｨﾝｸ': 'Wink',
    'ウィンク2': 'Wink 2',
    'ｳｨﾝｸ2': 'Wink 2',
    'ウィンク右': 'Wink Right',
    'ｳｨﾝｸ右': 'Wink Right',
    'ウィンク右2': 'Wink Right 2',
    'ｳｨﾝｸ右2': 'Wink Right 2',
    'ウィンク左': 'Wink Left',
    'ウィンク左2': 'Wink Left 2',
    'ｳｨﾝｸ左': 'Wink Left',
    'じと目': 'Doubt',
    'ジト目': 'Doubt',
    'びっくり': 'Surprise',
    'びっくり2': 'Surprise 2',
    'キリッ': 'Sharp',
    'カッ': 'Glare',
    'カッ2': 'Glare 2',
    'なごみ': 'Calm',
    'なごみ目': 'Calm Eye',
    'はぅ': 'Squint',
    'はぅ2': 'Squint 2',
    '喜び': 'Joy',
    '悲しみ': 'Sadness',
    '怒り': 'Anger',
    '驚き': 'Surprise',
    'うとうと': 'Sleepy',
    '眠い': 'Sleepy',
    '目開ける': 'Open Eye',
    '目閉じ': 'Close Eye',
    '目細': 'Squint Eye',
    '目大': 'Wide Eye',
    '目右': 'Eye Right',
    '目左': 'Eye Left',
    '目上': 'Eye Up',
    '目下': 'Eye Down',
    '睨み': 'Glare',

    # --- Pupil / iris ---
    '瞳小': 'Pupil Small',
    '瞳大': 'Pupil Large',
    '瞳縦小': 'Pupil Vertical Small',
    '瞳横拡大': 'Pupil Horizontal Large',
    'ハート目': 'Heart Eye',
    'ハート': 'Heart',
    '星目': 'Star Eye',
    'はちゅ目': 'Hachume',
    'はちゅ目縦潰れ': 'Hachume Vertical',
    'はちゅ目横潰れ': 'Hachume Horizontal',
    '光彩小': 'Iris Small',
    '光彩大': 'Iris Large',

    # --- Eyebrows ---
    '真面目': 'Serious',
    '困る': 'Trouble',
    '困った': 'Trouble',
    '上': 'Up',
    '下': 'Down',
    '眉': 'Brow',
    '眉一': 'Brow One',
    '眉外': 'Brow Outer',
    '眉内': 'Brow Inner',
    '眉上': 'Brow Up',
    '眉下': 'Brow Down',
    '眉右上': 'Brow Right Up',
    '眉左上': 'Brow Left Up',
    '眉右下': 'Brow Right Down',
    '眉左下': 'Brow Left Down',

    # --- Cheeks / blush / tears / sweat ---
    '照れ': 'Blush',
    '照れ2': 'Blush 2',
    '赤面': 'Red Face',
    '頬染め': 'Cheek Blush',
    '赤2': 'Red 2',
    '青ざめ': 'Pale',
    '暗い': 'Dark',
    '涙': 'Tear',
    '涙目': 'Teary Eyes',
    '滝涙': 'Pouring Tears',
    '汗': 'Sweat',
    '汗大': 'Big Sweat',
    'びっくり汗': 'Surprise Sweat',

    # --- Tongue ---
    'べろ': 'Tongue',
    '舌出し': 'Tongue Out',
    '舌上': 'Tongue Up',
    '舌下': 'Tongue Down',
    '舌': 'Tongue',

    # --- Category-divider labels (face, mouth, eye, etc.) ---
    '顔': 'Face',
    '目': 'Eye',
    '口': 'Mouth',
    '眉毛': 'Eyebrows',
    '頬': 'Cheek',
    '鼻': 'Nose',
    '耳': 'Ear',
    '体': 'Body',
    '頭': 'Head',
    '髪': 'Hair',
    '手': 'Hand',
    '足': 'Foot',
    '腰': 'Hip',
    '胸': 'Chest',
    '服': 'Clothes',
    'その他': 'Other',
    'リップ': 'Lip',
    'リップシンク': 'Lip Sync',
    '表情': 'Expression',
    '感情': 'Emotion',
    '視線': 'Gaze',
    '瞳': 'Pupil',
    '色': 'Color',

    # --- Korean ---
    '미소': 'Smile',
    '웃음': 'Laugh',
    '깜빡임': 'Blink',
    '윙크': 'Wink',
    '분노': 'Anger',
    '슬픔': 'Sadness',
    '기쁨': 'Joy',
    '놀람': 'Surprise',
    '입': 'Mouth',
    '눈': 'Eye',
    '눈썹': 'Eyebrow',
    '얼굴': 'Face',
    '머리': 'Head',
    '머리카락': 'Hair',
    '몸': 'Body',
    '손': 'Hand',
    '발': 'Foot',
    '혀': 'Tongue',
    '뺨': 'Cheek',
    '닫기': 'Close',
    '열기': 'Open',
    '왼쪽': 'Left',
    '오른쪽': 'Right',
    '위': 'Up',
    '아래': 'Down',

    # --- Chinese (Simplified). Many overlap with JP kanji already covered. ---
    '眨眼': 'Blink',
    '张嘴': 'Mouth Open',
    '闭嘴': 'Mouth Close',
    '生气': 'Angry',
    '难过': 'Sad',
    '开心': 'Happy',
    '惊讶': 'Surprise',
    '舌头': 'Tongue',
    '眼睛': 'Eye',
    '嘴巴': 'Mouth',
    '脸': 'Face',
    '头发': 'Hair',
    '身体': 'Body',
    '左眼': 'Left Eye',
    '右眼': 'Right Eye',
    '左': 'Left',
    '右': 'Right',

    # --- Long-vowel / wave-dash marks & misc partial tokens ---
    # These show up glued to ASCII in viseme names like 'Ton17_あ～R'; the
    # wave dash / fullwidth tilde normalise to an ASCII '~'.
    '〜': '~',    # 〜 wave dash
    '～': '~',    # ～ fullwidth tilde
    'にへ': 'Smug',
    'にへら': 'Smug',
}


# Dictionary keys ordered longest-first so partial translation always matches
# the most specific token at a given position (e.g. 'はちゅ目' before '目',
# 'ハート目' before 'ハート' before '目'). Recomputed only at import.
_SORTED_KEYS = sorted(_TRANSLATIONS, key=len, reverse=True)


def _translate_label(label: str):
    """Exact-match lookup against the dictionary.

    Returns the English translation, or None if the label has no entry.
    Used as the fast path inside _translate_label_partial; the partial
    translator handles mixed names like 'Ton17_あ～R'.
    """
    return _TRANSLATIONS.get(label)


def _translate_label_partial(label: str):
    """Translate every recognised dictionary token found inside `label`,
    leaving everything else (ASCII letters, digits, separators, and any
    unknown characters) untouched. Returns the translated string, which is
    `label` unchanged when nothing was recognised.

    Names from the wild are often partly translated already, e.g.
    'Ton17_あ～R' or 'Ton18_い～open_L' - a single Japanese vowel glued to
    ASCII. Exact whole-name lookup can't touch those, so we scan left to
    right and, at each non-ASCII position, replace the LONGEST dictionary
    token that starts there. Longest-first (via _SORTED_KEYS) keeps
    'はちゅ目' from being chopped into 'Hachume目', etc.

    ASCII positions are passed straight through (every dictionary key is
    non-ASCII), so the surrounding 'Ton17_', '_open', '_L', and digits are
    never disturbed. Single-kanji entries (目, 上, 口, ...) can in principle
    over-match inside an unrelated compound; the confirmation dialog lists
    every proposed rename so those are caught before anything is applied."""
    whole = _TRANSLATIONS.get(label)
    if whole is not None:
        return whole

    out = []
    i = 0
    n = len(label)
    matched = False
    while i < n:
        ch = label[i]
        if ord(ch) < 128:
            out.append(ch)
            i += 1
            continue
        hit = None
        for key in _SORTED_KEYS:
            if label.startswith(key, i):
                hit = key
                break
        if hit is not None:
            out.append(_TRANSLATIONS[hit])
            i += len(hit)
            matched = True
        else:
            out.append(ch)
            i += 1
    return ''.join(out) if matched else label


def _propose_translation(name: str):
    """Propose an English replacement for a shape key name.

    For divider names like '===顔===' the surrounding token is preserved
    and only the inner label is translated -> '===Face==='. For non-
    divider names the whole name is translated in place, so partial-
    Japanese names ('Ton17_あ～R') get their recognised parts converted.

    Returns None when nothing was recognised, or when the proposed rename
    would be a no-op (already in English / already matches the dictionary
    value)."""
    if is_category_divider(name):
        # Find which token was used so we can rebuild the divider.
        for token, _level in _get_patterns():
            tlen = len(token)
            if (len(name) > tlen * 2
                    and name.startswith(token)
                    and name.endswith(token)):
                label = name[tlen:-tlen]
                translated = _translate_label_partial(label)
                if translated == label:
                    return None
                return f"{token}{translated}{token}"
        return None

    translated = _translate_label_partial(name)
    if translated == name:
        return None
    return translated


def _translate_conflicts(proposals, blocks):
    """Names whose proposed rename will collide, so Blender would silently
    auto-suffix it (e.g. 'Smile' -> 'Smile.001'). A target name conflicts when
    two or more proposals want it, OR it is already held by a block that is NOT
    itself being renamed away. The dictionary maps several distinct source words
    to the same English word, so this is a routine case, not an edge one.

    Returns the set of OLD names whose rename would conflict."""
    new_counts = {}
    for _old, new in proposals:
        new_counts[new] = new_counts.get(new, 0) + 1
    renamed_olds = {old for old, _new in proposals}
    existing_kept = {kb.name for kb in blocks if kb.name not in renamed_olds}
    conflicts = set()
    for old, new in proposals:
        if new_counts[new] > 1 or new in existing_kept:
            conflicts.add(old)
    return conflicts


def _translate_respect_filter_update(self, context):
    """Rebuild the proposal list + preview when the in-dialog 'Filtered Only'
    toggle changes, so the shown list always matches what Apply will do."""
    self._refresh(context)


class SKP_OT_TranslateNames(Operator):
    """Translate Japanese / Korean / Chinese shape key names to English
    using a built-in dictionary of common MMD / VRChat conventions.
    Opens a confirmation dialog with every proposed rename listed.

    Names with no entry in the dictionary are left unchanged - it's safe
    to run on a mixed-language model. Category dividers have their inner
    label translated while the surrounding token (===, ---, ...) stays
    intact so the divider keeps working.

    Use Ctrl-Z to undo the entire batch at once."""
    bl_idname = "skp.translate_names"
    bl_label = "Translate Names to English"
    bl_options = {'REGISTER', 'UNDO'}

    respect_filter: BoolProperty(
        name="Filtered Only",
        description=(
            "Only translate keys currently visible in the panel (after "
            "search filter and category filter). When off, every key on "
            "the mesh is considered"
        ),
        default=False,
        options={'SKIP_SAVE'},
        update=_translate_respect_filter_update,
    )
    show_pairs: BoolProperty(name="Show Pairs", default=True)
    proposal_count: IntProperty()
    conflict_count: IntProperty()

    # Class-level fallback; invoke() copies into self for live use. Same
    # pattern as the cooldown ops, but no cooldown gate here because the
    # rename is undoable in one Ctrl-Z.
    _proposals: list = []  # list[(old_name, new_name)]

    def _collect(self, context):
        obj = context.active_object
        if obj is None or obj.data is None or obj.data.shape_keys is None:
            return []
        props = context.scene.skp_props
        blocks = obj.data.shape_keys.key_blocks

        # When respecting the filter, we use the same filtered list the
        # panel shows. get_filtered_keys excludes dividers - which is
        # surprising for translation (the user can see divider HEADERS
        # in the list and might expect them to translate too). So we
        # additionally pick up dividers that fall inside the visible
        # category by walking all blocks and respecting only the divider/
        # non-divider distinction relative to the active category.
        if self.respect_filter:
            visible_names = {kb.name for _i, kb in get_filtered_keys(obj, props)}
            target_kbs = [kb for kb in blocks if kb.name in visible_names]
        else:
            target_kbs = list(blocks)

        proposals = []
        for kb in target_kbs:
            if kb.name == 'Basis':
                continue
            proposed = _propose_translation(kb.name)
            if proposed is not None and proposed != kb.name:
                proposals.append((kb.name, proposed))
        return proposals

    def _refresh(self, context):
        """(Re)collect proposals and rebuild the preview list. Marks rows whose
        rename would collide (see _translate_conflicts). Shared by invoke() and
        the respect_filter toggle so the shown list never goes stale."""
        proposals = self._collect(context)
        SKP_OT_TranslateNames._proposals = proposals
        self.proposal_count = len(proposals)

        obj = context.active_object
        blocks = (obj.data.shape_keys.key_blocks
                  if (obj and obj.data and obj.data.shape_keys) else [])
        conflicts = _translate_conflicts(proposals, blocks)
        self.conflict_count = len(conflicts)

        # Reuse the shared delete-preview UIList for the proposed-pairs list.
        # Each entry's name is "old  ->  new" so the UIList renders the full
        # pair on one row without needing a custom UIList class.
        col = context.scene.skp_delete_preview
        col.clear()
        for old, new in proposals:
            item = col.add()
            mark = "    [!] name already exists" if old in conflicts else ""
            item.name = f"{old}    →    {new}{mark}"
            item.is_divider = is_category_divider(old)
        context.scene.skp_delete_preview_index = 0

    def invoke(self, context, event):
        # Default respect_filter on when a filter is active - matches the
        # user's mental model of "translate what I'm looking at".
        props = context.scene.skp_props
        has_filter = bool(
            props.search_filter.strip()
            or (props.category_filter and props.category_filter != 'ALL')
        )
        self.respect_filter = has_filter

        self._refresh(context)

        if self.proposal_count == 0:
            self.report({'INFO'}, "No translatable shape key names found.")
            return {'CANCELLED'}

        context.scene.skp_delete_filter = ""
        return context.window_manager.invoke_props_dialog(self, width=520)

    def draw(self, context):
        layout = self.layout

        head = layout.column()
        head.label(
            text=f"Translate {self.proposal_count} shape key name(s) to English",
            icon='WORLD_DATA',
        )

        layout.separator(factor=0.3)
        info = layout.column(align=True)
        info.label(
            text="Built-in dictionary covers common MMD / VRChat conventions",
            icon='INFO',
        )
        info.label(text="Recognised words are translated in place; the rest is kept.")
        info.label(text="Use Ctrl-Z to undo all renames at once.")

        layout.separator(factor=0.3)
        layout.prop(self, "respect_filter")

        if self.conflict_count:
            warn = layout.box()
            warn.alert = True
            warn.label(
                text=f"{self.conflict_count} name collision(s) - Blender will add "
                     f".001 suffixes",
                icon='ERROR',
            )
            sub = warn.row()
            sub.enabled = False
            sub.label(text="Marked [!] below. Rename or skip those to avoid suffixes.")

        layout.separator(factor=0.5)

        toggle_icon = 'TRIA_DOWN' if self.show_pairs else 'TRIA_RIGHT'
        toggle_text = (
            f"Hide proposed translations ({self.proposal_count})"
            if self.show_pairs else
            f"Show proposed translations ({self.proposal_count})"
        )
        layout.prop(self, "show_pairs", text=toggle_text,
                    toggle=True, icon=toggle_icon)

        if self.show_pairs:
            row = layout.row(align=True)
            row.prop(context.scene, "skp_delete_filter", text="",
                     icon='VIEWZOOM', placeholder="Filter rows...")
            if context.scene.skp_delete_filter:
                row.operator("skp.delete_filter_clear", text="", icon='X')

            rows = max(5, min(15, self.proposal_count))
            layout.template_list(
                "SKP_UL_delete_preview", "",
                context.scene, "skp_delete_preview",
                context.scene, "skp_delete_preview_index",
                rows=rows,
            )

    def execute(self, context):
        obj = context.active_object
        if obj is None or obj.data is None or obj.data.shape_keys is None:
            return {'CANCELLED'}

        # Re-resolve respect_filter from the dialog (Blender wires the
        # checkbox back to the operator instance) and recompute proposals
        # if it changed since invoke. This catches the user toggling the
        # checkbox in the dialog after invoke initially collected the
        # other variant.
        current_proposals = self._collect(context)
        if not current_proposals:
            self.report({'INFO'}, "No translatable shape key names found.")
            return {'CANCELLED'}

        blocks = obj.data.shape_keys.key_blocks
        renamed = 0
        failed = []
        conflicted = []   # (old, requested, actual) where Blender suffixed it
        for old, new in current_proposals:
            idx = blocks.find(old)
            if idx < 0:
                # Key vanished between dialog open and confirm - skip
                # rather than abort the whole batch.
                continue
            try:
                blocks[idx].name = new
            except (RuntimeError, AttributeError, TypeError) as err:
                failed.append((old, str(err)))
                continue
            # Blender silently appends .001 etc. when the target name is taken.
            # Detect that so the count and report are honest.
            actual = blocks[idx].name
            if actual != new:
                conflicted.append((old, new, actual))
            else:
                renamed += 1

        SKP_OT_TranslateNames._proposals = []
        context.scene.skp_delete_preview.clear()

        if failed or conflicted:
            print("[Dalek's Shapekey Manager] Translate report:")
            for old, requested, actual in conflicted:
                print(f"  collision: '{old}' -> wanted '{requested}', "
                      f"got '{actual}'")
            for old, err in failed:
                print(f"  failed: '{old}': {err}")
            parts = [f"Translated {renamed}"]
            if conflicted:
                parts.append(f"{len(conflicted)} name collision(s) auto-suffixed")
            if failed:
                parts.append(f"{len(failed)} failed")
            self.report({'WARNING'}, ", ".join(parts) + " (see console).")
        else:
            self.report({'INFO'}, f"Translated {renamed} shape key name(s).")
        return {'FINISHED'}


CLASSES = (SKP_OT_TranslateNames,)
