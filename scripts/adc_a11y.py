#!/usr/bin/env python3
"""adc accessibility engine: reads and drives applications through AT-SPI2 (no external CLI).

Run with the target display's environment (DISPLAY / AT_SPI_BUS_ADDRESS). Every command prints
one JSON object: {"ok": bool, "command": ..., "data"|"error": ...}.

  list-apps
  snapshot [--app NAME] [--window TITLE] [-i|--interactive] [--compact] [--max-depth N] [--max-nodes N]
  find [--app NAME] [--role ROLE] [--name TEXT] [--exact] [--limit N]
  get REF [--property bounds|name|role|states|text|value|actions|all]
  is REF visible|enabled|focused|checked|expanded|selected|editable
  wait [--app NAME] [--role ROLE] [--name TEXT] [--gone] [--timeout MS]
  click|right-click|focus|scroll-to|toggle|check|uncheck|expand|collapse REF
  type REF TEXT | set-value REF VALUE | clear REF | select REF OPTION
  close-app NAME [--timeout S]

References look like @<snapshot>:e<n>. They resolve through the stored child path and fall back
to a role + name search when the interface changed since the snapshot.
"""
import json
import os
import re
import secrets
import subprocess
import sys
import time

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi  # noqa: E402

RUN = os.path.join(os.environ.get("XDG_RUNTIME_DIR", f"/tmp/adc-{os.getuid()}"), "adc")
REFS = os.path.join(os.environ.get("ADC_STATE_DIR", RUN), "refs.json")
REF_RE = re.compile(r"^@([A-Za-z0-9]+):e(\d+)$")
KEEP_SNAPSHOTS = 12

ROLE_ALIAS = {
    "push button": "button", "toggle button": "togglebutton", "check box": "checkbox",
    "radio button": "radio", "page tab": "tab", "page tab list": "tablist", "menu item": "menuitem",
    "check menu item": "menuitemcheckbox", "radio menu item": "menuitemradio", "combo box": "combobox",
    "password text": "password", "spin button": "spinbutton", "list item": "listitem",
    "tree item": "treeitem", "table cell": "cell", "document web": "document",
    "document frame": "document", "entry": "textbox", "menu bar": "menubar", "tool bar": "toolbar",
    "scroll bar": "scrollbar", "status bar": "statusbar", "split pane": "splitpane",
    "column header": "columnheader", "row header": "rowheader", "table row": "row",
    "tree table": "treegrid", "dialog": "dialog", "alert": "alert", "link": "link", "slider": "slider",
    "heading": "heading", "label": "label", "static": "text", "paragraph": "paragraph", "image": "image",
}
INTERACTIVE = {"button", "togglebutton", "checkbox", "radio", "tab", "menuitem", "menuitemcheckbox",
               "menuitemradio", "combobox", "password", "spinbutton", "listitem", "treeitem", "link",
               "slider", "textbox", "switch", "menu"}
LANDMARK = {"frame", "window", "dialog", "alert", "document", "heading", "menubar", "toolbar",
            "tablist", "list", "table", "treegrid", "form", "file chooser", "notification"}
TEXTUAL = {"text", "label", "paragraph", "heading", "static", "caption", "description value"}
STATE_NAMES = {
    "focused": Atspi.StateType.FOCUSED, "checked": Atspi.StateType.CHECKED,
    "selected": Atspi.StateType.SELECTED, "expanded": Atspi.StateType.EXPANDED,
    "editable": Atspi.StateType.EDITABLE, "enabled": Atspi.StateType.ENABLED,
    "visible": Atspi.StateType.SHOWING, "pressed": Atspi.StateType.PRESSED,
    "expandable": Atspi.StateType.EXPANDABLE, "focusable": Atspi.StateType.FOCUSABLE,
    "sensitive": Atspi.StateType.SENSITIVE, "modal": Atspi.StateType.MODAL,
    "required": Atspi.StateType.REQUIRED, "invalid": Atspi.StateType.INVALID_ENTRY,
    "busy": Atspi.StateType.BUSY, "active": Atspi.StateType.ACTIVE,
}
SHOWN_STATES = ("focused", "checked", "selected", "expanded", "pressed", "required", "invalid", "busy",
                "modal")
CLICK_ACTIONS = ("click", "press", "activate", "jump", "toggle", "open", "select", "invoke")


class Fail(Exception):
    def __init__(self, code, message, suggestion=None):
        super().__init__(message)
        self.code, self.message, self.suggestion = code, message, suggestion


# ------------------------------------------------------------------ node helpers
def role_raw(a):
    try:
        return a.get_role_name() or ""
    except Exception:  # noqa: BLE001
        return ""


def role_of(a):
    r = role_raw(a)
    return ROLE_ALIAS.get(r, r.replace(" ", ""))


def name_of(a):
    try:
        n = a.get_name() or ""
    except Exception:  # noqa: BLE001
        n = ""
    return " ".join(n.split())


def label_of(a, depth=3, limit=80):
    """Name, or the text of the first labelled descendants (GTK4 menu items, list rows)."""
    n = name_of(a)
    if n:
        return n
    parts = []
    try:
        for rel in a.get_relation_set() or []:
            if rel.get_relation_type() == Atspi.RelationType.LABELLED_BY:
                for i in range(rel.get_n_targets()):
                    t = rel.get_target(i)
                    s = name_of(t) or text_of(t, limit)
                    if s and s not in parts:
                        parts.append(s)
    except Exception:  # noqa: BLE001
        pass
    if parts:
        return " ".join(parts)[:limit]

    def rec(node, d):
        if d > depth or sum(map(len, parts)) > limit:
            return
        for _, c in children(node)[:12]:
            t = name_of(c) or (text_of(c, 80) if role_of(c) in TEXTUAL else "")
            if t and t not in parts:
                parts.append(t)
            else:
                rec(c, d + 1)
    rec(a, 1)
    return " ".join(parts)[:limit]


def states_of(a):
    try:
        ss = a.get_state_set()
    except Exception:  # noqa: BLE001
        return set()
    return {k for k, v in STATE_NAMES.items() if ss.contains(v)}


_ORIGIN = {}


def top_of(a):
    """Top-level window (child of the application) that contains a."""
    node, last = a, a
    for _ in range(200):
        try:
            p = node.get_parent()
        except Exception:  # noqa: BLE001
            return last
        if p is None or role_raw(p) in ("application", "desktop frame"):
            return node
        last, node = node, p
    return node


def _xwin_origin(pid, title):
    """Screen position of the X11 window of pid whose title matches (needed for GTK4, which only
    reports window-relative coordinates)."""
    try:
        ids = subprocess.run(["xdotool", "search", "--pid", str(pid)], capture_output=True, text=True,
                             timeout=3).stdout.split()
    except (OSError, subprocess.TimeoutExpired):
        return None
    best = None
    for x in ids:
        o = subprocess.run(["xdotool", "getwindowname", x, "getwindowgeometry", x], capture_output=True,
                           text=True).stdout
        m = re.search(r"Position:\s*(-?\d+),(-?\d+)", o)
        g = re.search(r"Geometry:\s*(\d+)x(\d+)", o)
        if not m or not g or int(g.group(1)) < 20:
            continue
        name = o.splitlines()[0] if o else ""
        cand = (int(m.group(1)), int(m.group(2)))
        if name == title:
            return cand
        best = best or cand
    return best


def screen_origin(top):
    """None when the toolkit reports real screen coordinates; otherwise the window origin to add to
    window-relative coordinates."""
    key = id(top)
    if key in _ORIGIN:
        return _ORIGIN[key]
    org = None
    try:
        e = top.get_extents(Atspi.CoordType.SCREEN)
        if (e.x, e.y) == (0, 0):
            org = _xwin_origin(top.get_process_id(), name_of(top)) or (0, 0)
    except Exception:  # noqa: BLE001
        org = None
    _ORIGIN[key] = org
    return org


def bounds_of(a, top=None):
    try:
        if not a.get_component_iface():
            return None
        org = screen_origin(top if top is not None else top_of(a))
        if org is None:
            e = a.get_extents(Atspi.CoordType.SCREEN)
            return [e.x, e.y, e.width, e.height]
        e = a.get_extents(Atspi.CoordType.WINDOW)
        return [e.x + org[0], e.y + org[1], e.width, e.height]
    except Exception:  # noqa: BLE001
        return None


def text_of(a, limit=200, raw=False):
    try:
        if not a.get_text_iface():
            return ""
        n = Atspi.Text.get_character_count(a)
        t = (Atspi.Text.get_text(a, 0, min(n, limit) if limit else n) or "").replace("￼", "")
        return t if raw else " ".join(t.split())
    except Exception:  # noqa: BLE001
        return ""


def value_of(a):
    try:
        if a.get_value_iface():
            return Atspi.Value.get_current_value(a)
    except Exception:  # noqa: BLE001
        pass
    return None


def actions_of(a):
    try:
        if not a.get_action_iface():
            return []
        return [Atspi.Action.get_action_name(a, i) for i in range(Atspi.Action.get_n_actions(a))]
    except Exception:  # noqa: BLE001
        return []


def children(a):
    try:
        n = a.get_child_count()
    except Exception:  # noqa: BLE001
        return []
    out = []
    for i in range(min(n, 2000)):
        try:
            c = a.get_child_at_index(i)
        except Exception:  # noqa: BLE001
            c = None
        if c is not None:
            out.append((i, c))
    return out


def is_interactive(a, role, states):
    if role in INTERACTIVE or "editable" in states:
        return True
    return "focusable" in states and bool(actions_of(a)) and role not in ("frame", "window", "document",
                                                                          "panel", "filler", "section")


def apps():
    desk = Atspi.get_desktop(0)
    res = []
    for _, app in children(desk):
        try:
            pid = app.get_process_id()
        except Exception:  # noqa: BLE001
            pid = None
        res.append((app, name_of(app), pid))
    return res


def app_windows(app):
    return [(i, w) for i, w in children(app) if role_of(w) in ("frame", "window", "dialog", "alert", "filechooser")]


def match_app(app, name, needle):
    needle = needle.lower()
    if needle in (name or "").lower():
        return True
    return any(needle in name_of(w).lower() for _, w in app_windows(app))


def pick_apps(needle=None):
    al = apps()
    if needle:
        al = [x for x in al if match_app(x[0], x[1], needle)]
        if not al:
            raise Fail("APP_NOT_FOUND", f"no accessible application matches {needle!r}",
                       "run `adc list-apps`; Chrome needs --force-renderer-accessibility (adc browser)")
    return al


# ------------------------------------------------------------------ reference store
def load_refs():
    try:
        with open(REFS) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"order": [], "snaps": {}}


def save_refs(db):
    os.makedirs(os.path.dirname(REFS), mode=0o700, exist_ok=True)
    while len(db["order"]) > KEEP_SNAPSHOTS:
        db["snaps"].pop(db["order"].pop(0), None)
    tmp = REFS + f".{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(db, f)
    os.replace(tmp, REFS)


class Snap:
    def __init__(self):
        self.id = secrets.token_hex(2)
        self.refs = {}

    def add(self, pid, appname, path, a):
        n = len(self.refs) + 1
        self.refs[str(n)] = {"pid": pid, "app": appname, "path": path, "role": role_raw(a), "name": name_of(a)}
        return f"@{self.id}:e{n}"

    def commit(self):
        db = load_refs()
        db["snaps"][self.id] = self.refs
        db["order"].append(self.id)
        save_refs(db)


def _search(root, role, name, limit=6000):
    stack, seen = [root], 0
    while stack and seen < limit:
        a = stack.pop()
        seen += 1
        if role_raw(a) == role and name_of(a) == name:
            return a
        stack.extend(c for _, c in reversed(children(a)))
    return None


def resolve(ref):
    m = REF_RE.match(ref or "")
    if not m:
        raise Fail("INVALID_REF", f"not a reference: {ref!r}", "references look like @ab12:e7 (from snapshot/find)")
    rec = load_refs()["snaps"].get(m.group(1), {}).get(m.group(2))
    if not rec:
        raise Fail("STALE_REF", f"{ref} is unknown or expired", "take a new snapshot")
    app = next((x[0] for x in apps() if x[2] == rec["pid"]), None)
    if app is None:
        raise Fail("STALE_REF", f"application of {ref} ({rec['app']}) is gone", "take a new snapshot")
    a = app
    for idx in rec["path"]:
        try:
            a = a.get_child_at_index(idx)
        except Exception:  # noqa: BLE001
            a = None
        if a is None:
            break
    if a is not None and role_raw(a) == rec["role"] and name_of(a) == rec["name"]:
        return a, False
    found = _search(app, rec["role"], rec["name"])
    if found is None:
        raise Fail("STALE_REF", f"{ref} ({rec['role']} {rec['name']!r}) no longer exists", "take a new snapshot")
    return found, True


# ------------------------------------------------------------------ snapshot / find
def node_line(a, role, name, states, ref, compact, top=None):
    parts = [role]
    if name:
        parts.append(json.dumps(name[:120], ensure_ascii=False))
    if role in TEXTUAL or "editable" in states:
        t = text_of(a, 160)
        if t and t != name:
            parts.append("text=" + json.dumps(t, ensure_ascii=False))
    v = value_of(a)
    if v is not None and role in ("slider", "spinbutton", "scrollbar", "progressbar"):
        parts.append(f"value={v:g}")
    parts.append(ref)
    if not compact:
        b = bounds_of(a, top)
        if b:
            parts.append("[%d,%d %dx%d]" % tuple(b))
    st = [s for s in SHOWN_STATES if s in states]
    if "enabled" not in states and "sensitive" not in states and role in INTERACTIVE:
        st.append("disabled")
    if st:
        parts.append("{" + ",".join(st) + "}")
    return " ".join(parts)


def snapshot(opts):
    snap = Snap()
    lines, count = [], [0]
    interactive, compact = opts["interactive"], opts["compact"]
    max_depth, max_nodes = opts["max_depth"], opts["max_nodes"]
    wneedle = (opts.get("window") or "").lower()

    def walk(a, pid, appname, path, depth, ind, top=None):
        top = top if top is not None else a
        if count[0] >= max_nodes or depth > max_depth:
            return
        role, states = role_of(a), states_of(a)
        if interactive and "visible" not in states and (depth > 1 or not name_of(a)):
            return
        inter = is_interactive(a, role, states)
        name = label_of(a) if inter else name_of(a)
        if interactive:
            show = inter or (role in ("frame", "window", "dialog", "alert", "document") and depth >= 1)
        elif compact:
            show = inter or role in LANDMARK or (role in TEXTUAL and (name or text_of(a, 40)))
        else:
            show = True
        if show:
            count[0] += 1
            lines.append("  " * ind + "- " + node_line(a, role, name, states, snap.add(pid, appname, path, a), compact,
                                                     top))
        if interactive and inter and "expanded" not in states and \
                role not in ("combobox", "menu", "listitem", "treeitem", "tab", "menuitem"):
            return  # children of a collapsed button/entry add noise in interactive mode
        for i, c in children(a):
            walk(c, pid, appname, path + [i], depth + 1, ind + (1 if show else 0), top)

    for app, appname, pid in pick_apps(opts.get("app")):
        wins = app_windows(app)
        if wneedle:
            wins = [(i, w) for i, w in wins if wneedle in name_of(w).lower()]
            if not wins:
                continue
        if not wins and interactive:
            continue
        lines.append(f"app {json.dumps(appname)} pid={pid}")
        for i, w in (wins if (wins or wneedle) else children(app)):
            walk(w, pid, appname, [i], 1, 1)
    if wneedle and len(lines) == 0:
        raise Fail("WINDOW_NOT_FOUND", f"no window title contains {opts['window']!r}")
    snap.commit()
    return {"snapshot": snap.id, "nodes": count[0], "truncated": count[0] >= max_nodes, "tree": "\n".join(lines)}


def find(opts):
    snap = Snap()
    role_q = (opts.get("role") or "").lower().replace(" ", "")
    name_q = (opts.get("name") or "").lower()
    res = []
    for app, appname, pid in pick_apps(opts.get("app")):
        stack = [(c, [i], c) for i, c in reversed(app_windows(app) or children(app))]
        seen = 0
        while stack and len(res) < opts["limit"] and seen < 20000:
            a, path, top = stack.pop()
            seen += 1
            raw, role = role_raw(a), role_of(a)
            if role_q and role_q not in (role, raw.replace(" ", "")):
                ok_role = False
            else:
                ok_role = True
            if ok_role:
                nm = label_of(a, 2) if role in INTERACTIVE else name_of(a)
                hay = (nm + " " + (text_of(a, 300) if not nm or role in TEXTUAL else "")).lower()
                hit = (nm.lower() == name_q) if opts["exact"] else (name_q in hay)
                if hit and (role_q or name_q):
                    st = states_of(a)
                    res.append({"ref": snap.add(pid, appname, path, a), "role": role, "name": nm,
                                "app": appname, "bounds": bounds_of(a, top),
                                "states": sorted(s for s in st if s in SHOWN_STATES or s == "visible")})
            stack.extend((c, path + [i], top) for i, c in reversed(children(a)))
    snap.commit()
    res.sort(key=lambda r: "visible" not in r["states"])  # on-screen matches first
    return {"matches": res, "count": len(res)}


# ------------------------------------------------------------------ actions
def _do_named(a, names):
    acts = actions_of(a)
    for want in names:
        for i, n in enumerate(acts):
            if (n or "").lower() == want:
                return Atspi.Action.do_action(a, i), n
    return None, None


def act_click(a, right=False):
    if right:
        ok, n = _do_named(a, ("showmenu", "menu", "context menu"))
        if ok is None:
            raise Fail("ACTION_UNSUPPORTED", "element has no context-menu action")
        return {"action": n}
    ok, n = _do_named(a, CLICK_ACTIONS)
    if ok is None:
        acts = actions_of(a)
        if acts:
            ok, n = Atspi.Action.do_action(a, 0), acts[0]
    if ok is None:
        raise Fail("ACTION_UNSUPPORTED", "element exposes no clickable action",
                   "nested mode falls back to a real click at its centre; otherwise use bounds")
    return {"action": n}


def act_focus(a):
    try:
        return {"focused": bool(Atspi.Component.grab_focus(a))}
    except Exception as e:  # noqa: BLE001
        raise Fail("ACTION_UNSUPPORTED", f"cannot focus: {e}")


def act_set_text(a, text, mode):
    if not a.get_editable_text_iface():
        if mode == "set" and a.get_value_iface():
            try:
                Atspi.Value.set_current_value(a, float(text))
                return {"value": value_of(a)}
            except ValueError:
                pass
        raise Fail("ACTION_UNSUPPORTED", "element is not editable text",
                   "nested mode: click it and use `adc write`")
    if mode == "set":
        Atspi.EditableText.set_text_contents(a, text)
    else:
        try:
            pos = Atspi.Text.get_caret_offset(a)
        except Exception:  # noqa: BLE001
            pos = -1
        if pos is None or pos < 0:
            pos = Atspi.Text.get_character_count(a)
        Atspi.EditableText.insert_text(a, pos, text, len(text.encode("utf-8")))
    return {"text": text_of(a, 500)}


def act_state_toggle(a, want_state, desired):
    st = states_of(a)
    if desired is not None and ((want_state in st) == desired):
        return {"changed": False, want_state: desired}
    names = {"checked": ("toggle", "check", "uncheck", "click", "press", "activate"),
             "expanded": ("expand or contract", "expand", "collapse", "toggle", "click", "press", "activate")}
    ok, n = _do_named(a, names[want_state])
    if ok is None:
        raise Fail("ACTION_UNSUPPORTED", f"element cannot change {want_state}")
    time.sleep(0.15)
    return {"changed": True, "action": n, want_state: want_state in states_of(a)}


def act_select(a, option):
    opt = option.lower()
    # expand combo boxes so their items exist, then search descendants
    if role_of(a) == "combobox" and "expanded" not in states_of(a):
        _do_named(a, ("press", "click", "expand or contract", "open"))
        time.sleep(0.3)
    stack, hit = [(a, None, None)], None
    while stack and hit is None:
        node, parent, idx = stack.pop()
        if node is not a and (name_of(node).lower() == opt or text_of(node, 200).lower() == opt):
            hit = (node, parent, idx)
            break
        stack.extend((c, node, i) for i, c in reversed(children(node)))
    if hit is None:
        raise Fail("OPTION_NOT_FOUND", f"no option named {option!r}")
    node, parent, idx = hit
    via = None
    if "selected" in states_of(node):
        via = "already-selected"  # acting again would toggle it off in some toolkits
    else:
        via = _select_node(node, parent, idx)
    if via is None:
        raise Fail("ACTION_UNSUPPORTED", f"cannot select {option!r}")
    deadline = time.time() + 2  # toolkits (Chrome) apply the choice asynchronously
    while time.time() < deadline and "selected" not in states_of(node):
        time.sleep(0.1)
    time.sleep(0.1)
    res = {"selected": name_of(node) or text_of(node), "via": via}
    if role_of(a) == "combobox" and "expanded" in states_of(a):
        _do_named(a, ("press", "click", "expand or contract", "open", "collapse"))
        time.sleep(0.2)
    res["popup_open"] = role_of(a) == "combobox" and "expanded" in states_of(a)
    return res


def _select_node(node, parent, idx):
    via = None
    try:
        act_click(node)
        via = "action"
    except Fail:
        if parent is not None and parent.get_selection_iface():
            try:
                if Atspi.Selection.select_child(parent, idx):
                    via = "selection"
            except Exception:  # noqa: BLE001
                pass
    return via


def describe(a, prop="all"):
    d = {"role": role_of(a), "name": label_of(a), "bounds": bounds_of(a), "states": sorted(states_of(a))}
    if prop in ("all", "text"):
        d["text"] = text_of(a, 20000, raw=True)
    if prop in ("all", "value"):
        d["value"] = value_of(a)
    if prop in ("all", "actions"):
        d["actions"] = actions_of(a)
    if prop != "all":
        return {prop: d.get(prop), "bounds": d["bounds"]}
    return d


# ------------------------------------------------------------------ apps
def close_app(name, timeout, force=False):
    closed = []
    for app, appname, pid in pick_apps(name):
        if not pid:
            continue
        xids = subprocess.run(["xdotool", "search", "--pid", str(pid)], capture_output=True, text=True).stdout.split()
        for x in xids:
            subprocess.run(["wmctrl", "-i", "-c", hex(int(x))], capture_output=True)
        deadline = time.time() + timeout
        gone = False
        while time.time() < deadline:
            try:
                os.kill(pid, 0)
            except OSError:
                gone = True
                break
            time.sleep(0.2)
        entry = {"app": appname, "pid": pid, "windows": len(xids), "exited": gone}
        if not gone and force:
            try:
                os.kill(pid, 15)
                entry["forced"] = True
            except OSError:
                pass
        elif not gone:
            entry["hint"] = "the application is still open (unsaved changes?): snapshot it and answer its dialog, or pass --force"
        closed.append(entry)
    return {"closed": closed}


def list_apps():
    res = []
    for app, appname, pid in apps():
        wins = app_windows(app)
        res.append({"name": appname, "pid": pid, "windows": [name_of(w) for _, w in wins],
                    "presentation": "window" if wins else "background"})
    return {"apps": res}


# ------------------------------------------------------------------ cli
def parse(argv, flags):
    pos, kv, i = [], {}, 0
    while i < len(argv):
        a = argv[i]
        if a in flags:
            if flags[a]:
                if i + 1 >= len(argv):
                    raise Fail("INVALID_ARGS", f"{a} needs a value")
                kv[a.lstrip("-")] = argv[i + 1]
                i += 2
            else:
                kv[a.lstrip("-")] = True
                i += 1
        else:
            pos.append(a)
            i += 1
    return pos, kv


def need_ref(pos, cmd, n=1):
    if len(pos) < n:
        raise Fail("INVALID_ARGS", f"`{cmd}` needs a reference" + (" and a value" if n > 1 else ""))
    return resolve(pos[0])


def run(cmd, argv):
    if cmd == "list-apps":
        return list_apps()
    if cmd == "snapshot":
        _, kv = parse(argv, {"--app": True, "--window": True, "-i": False, "--interactive": False,
                             "--compact": False, "-c": False, "--max-depth": True, "--max-nodes": True})
        return snapshot({"app": kv.get("app"), "window": kv.get("window"),
                         "interactive": bool(kv.get("i") or kv.get("interactive")),
                         "compact": bool(kv.get("compact") or kv.get("c")),
                         "max_depth": int(kv.get("max-depth", 40)), "max_nodes": int(kv.get("max-nodes", 1500))})
    if cmd == "find":
        pos, kv = parse(argv, {"--app": True, "--role": True, "--name": True, "--exact": False, "--limit": True})
        name = kv.get("name") or (" ".join(pos) if pos else None)
        if not (name or kv.get("role")):
            raise Fail("INVALID_ARGS", "find needs --name and/or --role")
        return find({"app": kv.get("app"), "role": kv.get("role"), "name": name, "exact": bool(kv.get("exact")),
                     "limit": int(kv.get("limit", 25))})
    if cmd == "wait":
        pos, kv = parse(argv, {"--app": True, "--role": True, "--name": True, "--gone": False, "--timeout": True,
                               "--exact": False})
        name = kv.get("name") or (" ".join(pos) if pos else None)
        deadline = time.time() + int(kv.get("timeout", 10000)) / 1000
        while True:
            try:
                r = find({"app": kv.get("app"), "role": kv.get("role"), "name": name,
                          "exact": bool(kv.get("exact")), "limit": 1})
            except Fail as e:
                if e.code != "APP_NOT_FOUND":
                    raise
                r = {"matches": [], "count": 0}
            if bool(r["count"]) != bool(kv.get("gone")):
                return dict(r, waited=True)
            if time.time() > deadline:
                raise Fail("TIMEOUT", "condition not met before timeout")
            time.sleep(0.25)
    if cmd == "get":
        pos, kv = parse(argv, {"--property": True, "-p": True})
        a, rec = need_ref(pos, cmd)
        return dict(describe(a, kv.get("property") or kv.get("p") or "all"), recovered=rec)
    if cmd == "is":
        a, rec = need_ref(pos := argv, cmd, 2)
        st = pos[1]
        if st not in STATE_NAMES:
            raise Fail("INVALID_ARGS", f"unknown state {st!r}; use one of {sorted(STATE_NAMES)}")
        return {"state": st, "value": st in states_of(a)}
    if cmd in ("click", "right-click"):
        a, rec = need_ref(argv, cmd)
        return dict(act_click(a, cmd == "right-click"), bounds=bounds_of(a), recovered=rec)
    if cmd == "focus":
        a, rec = need_ref(argv, cmd)
        return dict(act_focus(a), bounds=bounds_of(a))
    if cmd == "scroll-to":
        a, rec = need_ref(argv, cmd)
        try:
            Atspi.Component.scroll_to(a, Atspi.ScrollType.ANYWHERE)
        except Exception as e:  # noqa: BLE001
            raise Fail("ACTION_UNSUPPORTED", f"cannot scroll to element: {e}")
        before, b = None, bounds_of(a)
        for _ in range(10):  # wait for the scroll to settle
            time.sleep(0.1)
            before, b = b, bounds_of(a)
            if b == before:
                break
        return {"bounds": b, "visible": "visible" in states_of(a)}
    if cmd in ("type", "set-value"):
        a, rec = need_ref(argv, cmd, 2)
        return dict(act_set_text(a, " ".join(argv[1:]), "set" if cmd == "set-value" else "insert"), bounds=bounds_of(a))
    if cmd == "clear":
        a, rec = need_ref(argv, cmd)
        return dict(act_set_text(a, "", "set"), bounds=bounds_of(a))
    if cmd in ("toggle", "check", "uncheck"):
        a, rec = need_ref(argv, cmd)
        return dict(act_state_toggle(a, "checked", {"toggle": None, "check": True, "uncheck": False}[cmd]),
                    bounds=bounds_of(a))
    if cmd in ("expand", "collapse"):
        a, rec = need_ref(argv, cmd)
        return dict(act_state_toggle(a, "expanded", cmd == "expand"), bounds=bounds_of(a))
    if cmd == "select":
        a, rec = need_ref(argv, cmd, 2)
        return dict(act_select(a, " ".join(argv[1:])), bounds=bounds_of(a))
    if cmd == "close-app":
        pos, kv = parse(argv, {"--timeout": True, "--force": False})
        if not pos:
            raise Fail("INVALID_ARGS", "close-app needs an application name")
        return close_app(" ".join(pos), float(kv.get("timeout", 6)), bool(kv.get("force")))
    raise Fail("UNKNOWN_COMMAND", f"unknown command {cmd!r}", "adc --help")


def main():
    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd = argv[0]
    Atspi.init()
    try:
        Atspi.set_timeout(4000, 15000)
    except Exception:  # noqa: BLE001
        pass
    try:
        data = run(cmd, argv[1:])
        print(json.dumps({"ok": True, "command": cmd, "data": data}, ensure_ascii=False))
        return 0
    except Fail as e:
        err = {"code": e.code, "message": e.message}
        if e.suggestion:
            err["suggestion"] = e.suggestion
        print(json.dumps({"ok": False, "command": cmd, "error": err}, ensure_ascii=False))
        return 1
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"ok": False, "command": cmd, "error": {"code": "INTERNAL", "message": str(e)}},
                         ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
