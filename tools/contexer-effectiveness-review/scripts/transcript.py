"""Host-specific transcript parsing into one ordered event list.

Events: ("call", name, args, call_id, category, contexer_tool)
        ("result", call_id, text)
        ("hook_context", text)
Only record shapes each host really writes are read; arbitrary dicts inside tool inputs,
tool schemas or replayed history are never treated as calls.
"""
import ast
import json
import re
import shlex

CATEGORIES = {
    "read": {"read", "read_file", "view_image", "notebookread", "readfile"},
    "search": {"grep", "glob", "semanticsearch", "search", "codebase_search", "list_dir", "ls",
               "websearch", "webfetch"},
    "shell": {"shell", "bash", "exec", "exec_command", "local_shell", "container.exec",
              "awaitshell", "js"},
    "edit": {"strreplace", "write", "edit", "multiedit", "apply_patch", "delete", "editnotebook",
             "notebookedit"},
    "subagent": {"task", "agent", "spawn_agent", "followup_task", "send_message", "wait_agent"},
}
EXPLORATION = ("read", "search", "shell")
CONTEXER_NAMESPACE = re.compile(r"^(user-)?contexer$|^mcp__contexer$")
CONTEXER_MCP_NAME = re.compile(r"^mcp__contexer__(\w+)$")
DISCOVERY = {"getdynamictools", "toolsearch", "tool_search_call"}

# Output capabilities per host, fixed by what each host's transcript format carries.
TOOL_RESULTS_VISIBLE = {"cursor": False, "claude": True, "codex": True}
HOOK_CONTEXT_VISIBLE = {"cursor": False, "claude": True, "codex": False}


def category(name):
    lowered = name.lower()
    return next((c for c, names in CATEGORIES.items() if lowered in names), "other")


def _text(value):
    """Flatten a tool-result or hook-context payload to plain text."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(_text(v) for v in value)
    if isinstance(value, dict):
        for key in ("text", "content", "output"):
            if key in value:
                return _text(value[key])
    return ""


def _args(raw):
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {"raw": raw}
        except ValueError:
            return {"raw": raw}
    return {}


def _call(name, args, call_id, namespace=None):
    """Normalise one tool call; unwrap Cursor's CallDynamicTool to its real tool."""
    contexer_tool = None
    real = name
    if name == "CallDynamicTool":
        namespace = str(args.get("namespace", ""))
        real = str(args.get("toolName", name))
    if namespace and CONTEXER_NAMESPACE.match(namespace):
        contexer_tool = real
    else:
        m = CONTEXER_MCP_NAME.match(real)
        if m:
            contexer_tool = m.group(1)
    cat = "contexer" if contexer_tool else (
        "discovery" if real.lower() in DISCOVERY else category(real))
    return ("call", real, args, call_id, cat, contexer_tool)


def parse(text, host):
    events = []
    for line in text.splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        if host == "codex":
            events.extend(_codex(rec))
        elif host == "claude":
            events.extend(_claude(rec))
        else:
            events.extend(_cursor(rec))
    return events


def _content(rec):
    msg = rec.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    return content if isinstance(content, list) else []


def _cursor(rec):
    if rec.get("role") != "assistant":
        return []
    out = []
    for item in _content(rec):
        if isinstance(item, dict) and item.get("type") == "tool_use" and isinstance(
                item.get("name"), str):
            out.append(_call(item["name"], _args(item.get("input")), item.get("id")))
    return out


def _claude(rec):
    if rec.get("isSidechain"):
        return []
    kind = rec.get("type")
    out = []
    if kind == "assistant":
        for item in _content(rec):
            if isinstance(item, dict) and item.get("type") == "tool_use" and isinstance(
                    item.get("name"), str):
                out.append(_call(item["name"], _args(item.get("input")), item.get("id")))
    elif kind == "user":
        for item in _content(rec):
            if isinstance(item, dict) and item.get("type") == "tool_result":
                out.append(("result", item.get("tool_use_id"), _text(item.get("content"))))
    elif kind == "attachment":
        att = rec.get("attachment")
        if isinstance(att, dict) and att.get("type") == "hook_additional_context":
            out.append(("hook_context", _text(att.get("content"))))
    return out


def _codex(rec):
    if rec.get("type") != "response_item":
        return []
    p = rec.get("payload")
    if not isinstance(p, dict):
        return []
    kind = p.get("type")
    if kind == "function_call" and isinstance(p.get("name"), str):
        return [_call(p["name"], _args(p.get("arguments")), p.get("call_id"), p.get("namespace"))]
    if kind == "custom_tool_call" and isinstance(p.get("name"), str):
        return [_call(p["name"], _args(p.get("input")), p.get("call_id"), p.get("namespace"))]
    if kind in ("function_call_output", "custom_tool_call_output"):
        return [("result", p.get("call_id"), _text(p.get("output")))]
    return []


def command_text(args):
    for key in ("command", "cmd", "raw", "script", "code"):
        value = args.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return " ".join(str(v) for v in value)
    return ""


_SHELL_TOKEN = re.compile(r'''(?:[^\s;&|()'"\\]+|\\.|"(?:\\.|[^"\\])*"|'[^']*')+|[;&|()\n]+''')
_JS_LITERAL = re.compile(r'''//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`''', re.S)


def _literal_shell_scripts(code):
    """Read literal shell arguments, never arbitrary quoted prose in a code wrapper.

    This records command intent only. Dynamic code and execution of inner tool calls are
    not observable from the wrapper; observe() separately marks their counts incomplete.
    """
    for match in _JS_LITERAL.finditer(code):
        raw = match.group()
        if raw.startswith(("//", "/*")):
            continue
        before = code[:match.start()]
        if not (re.search(r"\b(?:sh|shell|exec_command)\(\s*$", before)
                or re.search(r"\bexec_command\(\s*\{[^{}]*\b(?:cmd|command)\s*:\s*$", before)):
            continue
        if raw.startswith('`'):
            if '${' not in raw:
                yield raw[1:-1]
            continue
        try:
            value = ast.literal_eval(raw)
        except (SyntaxError, ValueError):
            continue
        if isinstance(value, str):
            yield value


def _without_heredoc_bodies(script):
    out, delimiters = [], []
    for line in script.splitlines(keepends=True):
        if delimiters:
            if line.strip() == delimiters[0]:
                delimiters.pop(0)
            continue
        out.append(line)
        tokens = [m.group() for m in _SHELL_TOKEN.finditer(line)]
        for i, token in enumerate(tokens):
            if token.startswith('#'):
                break
            if not token.startswith('<<') or token.startswith('<<<'):
                continue
            raw = token[2:].removeprefix('-') or (tokens[i + 1] if i + 1 < len(tokens) else '')
            try:
                parsed = shlex.split(raw)
            except ValueError:
                continue
            if parsed:
                delimiters.append(parsed[0])
    return ''.join(out)


def shell_commands(args, wrapped=False):
    script = command_text(args)
    scripts = _literal_shell_scripts(script) if wrapped else [script]
    commands = []
    for source in scripts:
        words, comment = [], False
        for match in _SHELL_TOKEN.finditer(_without_heredoc_bodies(source)):
            raw = match.group()
            if raw.startswith('#'):
                comment = True
            if re.fullmatch(r'[;&|()\n]+', raw):
                if words:
                    commands.append(' '.join(words))
                    words = []
                if '\n' in raw:
                    comment = False
            elif not comment:
                try:
                    values = shlex.split(raw)
                except ValueError:
                    continue
                if values:
                    if not words and re.match(r'^\w+=', values[0]):
                        continue
                    words.extend(values)
        if words:
            commands.append(' '.join(words))
    return commands
