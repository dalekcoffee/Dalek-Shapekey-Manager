# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2025 Dalek (https://dalek.coffee)

"""
Shared Resonite / ARKit face-tracking target library.

This module is the single source of truth for which shape-key names a Resonite
avatar auto-binds to for visemes, eye tracking and facial-expression tracking.
The DATA lives in the bundled ``resonite-face-targets.json`` (the same file kept
under the project's ``Data/`` folder); the MATCHING LOGIC lives here, so every
part of the add-on - redundant-key protection, the main-list face-tracking
indicator, and any future coverage/fix tool - classifies names identically.

Why some keys are "load-bearing": Resonite's DirectVisemeDriver, EyeLinearDriver
and AvatarExpressionDriver auto-bind to keys *by name* on import. Deleting one
(or deleting the canonically-named copy of a duplicate pair and keeping a
differently-named identical twin) silently breaks lip-sync / eye / face tracking
in-game. The redundant-key tool uses :func:`classify` to flag those and never
auto-selects them for deletion.

Two matchers, two purposes:
  * :func:`classify` - CONSERVATIVE. Answers "is this name *intended* as a
    face-tracking target?" Used to flag/protect keys without drowning the user in
    false positives. Resonite's real viseme matcher is substring-within-segment
    and so greedy (single letters a/e/o/n/s/h...) it would match almost every
    English word; we tighten it (see the viseme rules below).
  * :func:`auto_assign_visemes` - FAITHFUL. Replays Resonite's *documented*
    greedy viseme matcher to answer "which key would Resonite actually pick for
    each viseme?" Use this for coverage analysis ("is every viseme covered?").

Public API (stable - safe for other modules/tools to import):
  Data:
    VISEME_TARGETS      list[(viseme, tokens, ideal_name)]
    VISEME_IDEAL        dict viseme -> ideal "vrc.v_*" name
    ARKIT52             list[str] - the 52 canonical ARKit names
    SUGGESTED_NAMES     sorted list[str] - UE/ARKit names from eye+expression tables
    ALL_TRACKING_NAMES  sorted list[str] - ARKit52 + suggested (eye/expression set)
    EYE_SLOTS           raw eye target slots (from JSON)
    EXPRESSION_TARGETS  raw expression targets (from JSON)
  Classification (conservative):
    classify(name) -> FaceTarget | None
    is_face_target(name) -> bool
    face_target_info(name) -> (bool, reason)        # back-compat shape
  Coverage (faithful to Resonite's documented matcher):
    auto_assign_visemes(names) -> dict viseme -> chosen name (or None)
    missing_visemes(names) -> list[viseme]
    missing_arkit(names) -> list[str]
    present_tracking_names(names) -> list[str]       # canonical names found
"""

import os
import re
import json
import functools
from collections import namedtuple


# --------------------------------------------------------------------------
#  Load the bundled data (single source of truth)
# --------------------------------------------------------------------------

_DATA_PATH = os.path.join(os.path.dirname(__file__), "resonite-face-targets.json")


def _load_data():
    """Load the bundled JSON. On any failure, degrade gracefully to an empty
    library (the add-on still registers; face-tracking flags just go quiet) and
    print a console warning rather than crashing registration."""
    try:
        with open(_DATA_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception as err:  # pragma: no cover - defensive
        print(f"[Dalek's Shapekey Manager] WARNING: could not load "
              f"face-target library ({_DATA_PATH}): {err}. "
              f"Face-tracking detection disabled.")
        return {"visemes": {"targets": []}, "eyes": {"targets": []},
                "expressions": {"targets": []}, "arkit52": []}


_DATA = _load_data()


# --------------------------------------------------------------------------
#  Derived public data
# --------------------------------------------------------------------------

# [(viseme, [tokens], ideal_name), ...] in documented order.
VISEME_TARGETS = [
    (t["viseme"], list(t.get("tokens", [])), t.get("ideal", ""))
    for t in _DATA.get("visemes", {}).get("targets", [])
]
VISEME_IDEAL = {vis: ideal for vis, _toks, ideal in VISEME_TARGETS if ideal}

ARKIT52 = list(_DATA.get("arkit52", []))

# Union of every non-empty "suggested" source name across the eye + expression
# tables (these include the UE-only extras like EyeClosedLeft / BrowInnerUpLeft).
_suggested = set()
for _t in _DATA.get("eyes", {}).get("targets", []):
    _suggested.update(_t.get("suggested", []) or [])
for _t in _DATA.get("expressions", {}).get("targets", []):
    _suggested.update(_t.get("suggested", []) or [])
SUGGESTED_NAMES = sorted(_suggested)

# Every canonical eye/expression name Resonite's auto-detect looks for.
ALL_TRACKING_NAMES = sorted(set(ARKIT52) | _suggested)

# Raw tables, exposed for a future coverage/fix tool that may want slot meanings.
EYE_SLOTS = list(_DATA.get("eyes", {}).get("targets", []))
EXPRESSION_TARGETS = list(_DATA.get("expressions", {}).get("targets", []))


# --------------------------------------------------------------------------
#  Internal lookups for matching
# --------------------------------------------------------------------------

def _alnum(s):
    """Lowercase + strip every non-alphanumeric char, so eyeBlinkLeft,
    eye_blink_left and EyeBlink.Left all collapse to one comparable key."""
    return re.sub(r"[^a-z0-9]", "", s.lower())


# alnum form -> canonical display name, for the ARKit/UE name match.
_NAME_LOOKUP = {}
for _n in ALL_TRACKING_NAMES:
    _NAME_LOOKUP.setdefault(_alnum(_n), _n)

# token -> viseme label (first listing wins).
_VISEME_OF_TOKEN = {}
for _vis, _toks, _ideal in VISEME_TARGETS:
    for _t in _toks:
        _VISEME_OF_TOKEN.setdefault(_t, _vis)

# Kana phoneme tokens (non-ASCII single chars: あいうえお ん).
_KANA = {t for t in _VISEME_OF_TOKEN if not t.isascii()}
# Single ASCII tokens safe to match standalone: vowels a/e/o/u plus n. Single
# consonants (p/f/h/d/k/s/r) are excluded here - a bare "_R" is a Right-side
# marker far more often than the RR viseme - and only match inside a "vrc" key.
_SINGLE_FREE = {t for t in _VISEME_OF_TOKEN
                if len(t) == 1 and t.isascii() and (t in "aeou" or t == "n")}

# Letter runs (split on any non-letter, incl. digits/underscore), Unicode-aware.
_SEGMENT_RE = re.compile(r"[^\W\d_]+", re.UNICODE)

# Standalone single-char viseme matchers. The boundary classes are the crux of
# avoiding false positives:
#  - Latin vowels are bounded by [^\W_] (any Unicode letter OR digit). A vowel
#    glued to a digit ("E1_", an index) or to any letter incl. kana ("UるU",
#    "ωO") is NOT a viseme; only one set off by real separators ("Mth_A") is.
#  - Kana are bounded by [^\W\d_] (a Unicode letter only; digits allowed). A
#    lone kana phoneme marker ("M5_お1", "M1_あ", "Ton16_あ～") matches, but a
#    kana inside a Japanese word ("うるうる", "恐ろしい子", "はんっ") does not.
_LATIN_RE = (re.compile(r"(?<![^\W_])([" + "".join(sorted(_SINGLE_FREE)) + r"])(?![^\W_])")
             if _SINGLE_FREE else None)
_KANA_RE = (re.compile(r"(?<![^\W\d_])([" + "".join(sorted(_KANA)) + r"])(?![^\W\d_])")
            if _KANA else None)


# --------------------------------------------------------------------------
#  Classification (conservative) - "is this name meant as a face target?"
# --------------------------------------------------------------------------

# kind: 'viseme' | 'tracking'
#   viseme    -> a DirectVisemeDriver speech viseme (carries .viseme)
#   tracking  -> an ARKit / UE eye or expression name (carries .canonical)
FaceTarget = namedtuple("FaceTarget", ["kind", "reason", "viseme", "canonical"])


@functools.lru_cache(maxsize=8192)
def classify(name):
    """Return a :class:`FaceTarget` when *name* looks like a Resonite/ARKit
    face-tracking target Resonite would auto-bind, else ``None``.

    Cached: the main panel calls this for every visible row on every redraw.
    Names are stable strings; renamed keys simply create new cache entries.

    Conservative viseme rule: a whole letter-segment equals a multi-char token,
    OR a lone kana phoneme marker is present, OR a single vowel/n stands alone
    between real separators, OR the key is an explicit 'vrc' viseme key."""
    if not name:
        return None

    # 1) ARKit / UE exact name (case/separator-insensitive).
    canon = _NAME_LOOKUP.get(_alnum(name))
    if canon:
        return FaceTarget("tracking", f"Face tracking: {canon}", "", canon)

    low = name.lower()
    segments = _SEGMENT_RE.findall(low)

    # 2a) multi-char viseme token as a whole letter-segment (aa/th/sil/...).
    for seg in segments:
        if len(seg) >= 2:
            vis = _VISEME_OF_TOKEN.get(seg)
            if vis:
                return FaceTarget("viseme", f"Resonite viseme: {vis}", vis, "")

    # 2b) a lone kana phoneme marker (digits allowed as neighbours, letters not).
    if _KANA_RE is not None:
        m = _KANA_RE.search(low)
        if m:
            vis = _VISEME_OF_TOKEN[m.group(1)]
            return FaceTarget("viseme", f"Resonite viseme: {vis}", vis, "")

    # 2c) a separator-delimited single vowel/n (never glued to a digit/letter).
    if _LATIN_RE is not None:
        m = _LATIN_RE.search(low)
        if m:
            vis = _VISEME_OF_TOKEN[m.group(1)]
            return FaceTarget("viseme", f"Resonite viseme: {vis}", vis, "")

    # 2d) explicit 'vrc' viseme key - take the token segment (vrc.v_aa, vrc.v_p),
    #     skipping the 'vrc'/'v' connectors so 'vrc' itself isn't read as RR.
    if "vrc" in low:
        for seg in segments:
            if seg in ("vrc", "v"):
                continue
            vis = _VISEME_OF_TOKEN.get(seg)
            if vis:
                return FaceTarget("viseme", f"Resonite viseme: {vis}", vis, "")

    return None


def is_face_target(name):
    """True if *name* is classified as a Resonite/ARKit face-tracking target."""
    return classify(name) is not None


def face_target_info(name):
    """Back-compat shape used by the redundant-key tool: ``(is_protected,
    reason)``. ``reason`` is '' when not a target."""
    m = classify(name)
    return (True, m.reason) if m else (False, "")


# --------------------------------------------------------------------------
#  Coverage (faithful to Resonite's documented viseme matcher)
# --------------------------------------------------------------------------

def _documented_viseme_match(name, tokens):
    """Resonite's published rule: split *name* on any non-letter into segments;
    the viseme matches if any segment CONTAINS one of its tokens (substring,
    case-insensitive)."""
    segs = _SEGMENT_RE.findall(name.lower())
    for seg in segs:
        for tok in tokens:
            if tok in seg:
                return True
    return False


def auto_assign_visemes(names):
    """Replay Resonite's documented greedy viseme auto-assign over *names*.

    Returns ``dict[viseme -> chosen_name or None]``. Among matching candidates,
    a name containing the 'vrc' prefix wins (Resonite's documented priority);
    otherwise the first match in *names* order is taken. This is the faithful
    "what would Resonite pick" matcher - use it for coverage, NOT for flagging
    (it is intentionally greedy)."""
    names = list(names)
    result = {}
    for vis, tokens, _ideal in VISEME_TARGETS:
        chosen = None
        for nm in names:
            if _documented_viseme_match(nm, tokens):
                if "vrc" in nm.lower():
                    chosen = nm
                    break          # vrc-prefixed wins outright
                if chosen is None:
                    chosen = nm    # first non-vrc match, keep looking for a vrc
        result[vis] = chosen
    return result


def missing_visemes(names):
    """List of viseme labels with NO matching key in *names* (documented rule)."""
    assigned = auto_assign_visemes(names)
    return [vis for vis, chosen in assigned.items() if chosen is None]


def present_tracking_names(names):
    """Canonical ARKit/UE eye+expression names present in *names* (by the
    case/separator-insensitive name match)."""
    have = {_alnum(n) for n in names}
    return [c for c in ALL_TRACKING_NAMES if _alnum(c) in have]


def missing_arkit(names):
    """ARKit-52 names not present in *names* (case/separator-insensitive)."""
    have = {_alnum(n) for n in names}
    return [c for c in ARKIT52 if _alnum(c) not in have]
