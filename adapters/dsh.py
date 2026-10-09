"""dsh 适配器：把 dsh --json 吐出的 NDJSON 事件翻成统一事件。

统一事件类型：session / status / thinking / text / tool / tool_result /
usage / final / error / raw
"""
import json

_TEXT_KEYS = ("text", "message", "content")
_CMD_KEYS = ("command", "cmd", "path", "file_path")


def _text_of(obj: dict) -> str:
    for k in _TEXT_KEYS:
        v = obj.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _brief(v, limit=200) -> str:
    if v is None:
        return ""
    if not isinstance(v, str):
        v = json.dumps(v, ensure_ascii=False)
    v = v.replace("\r", " ").replace("\n", " ").strip()
    return v if len(v) <= limit else v[:limit] + "…"


def parse(line: str):
    """返回 (类型, 文本, 附加字段) 或 None。"""
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except Exception:
        return ("raw", line, {})
    if not isinstance(obj, dict):
        return ("raw", line, {})

    t = str(obj.get("type") or "")
    if t == "session":
        return ("session", obj.get("sessionId") or "", {"cwd": obj.get("cwd")})
    if t == "status":
        usage = obj.get("usage")
        if usage:
            return ("usage", obj.get("phase") or "", {"usage": usage})
        return ("status", obj.get("phase") or "", {})
    if t == "thinking":
        return ("thinking", _text_of(obj), {})
    if t == "text":
        return ("text", _text_of(obj), {})
    if t in ("final", "result", "answer"):
        return ("final", _text_of(obj), {})
    if t in ("tool_call", "tool", "tool_use"):
        name = obj.get("tool") or obj.get("name") or ""
        inp = obj.get("input") or {}
        detail = ""
        if isinstance(inp, dict):
            for k in _CMD_KEYS:
                if inp.get(k):
                    detail = _brief(inp.get(k))
                    break
            if not detail:
                detail = _brief(inp, 120)
        return ("tool", str(name), {"command": detail, "call_id": obj.get("callId")})
    if t in ("tool_result",):
        return ("tool_result", _brief(obj.get("result"), 300),
                {"status": obj.get("status"), "call_id": obj.get("callId")})
    if t in ("error", "failure"):
        return ("error", _text_of(obj) or _brief(obj), {})
    return ("raw", _brief(obj, 200), {"raw_type": t})
