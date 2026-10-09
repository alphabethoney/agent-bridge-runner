"""codex 适配器：目前 codex exec 没有结构化输出，先按行搬运。"""
import json


def parse(line: str):
    line = line.rstrip("\n")
    if not line.strip():
        return None
    s = line.strip()
    if s.startswith("{"):
        try:
            obj = json.loads(s)
            if isinstance(obj, dict):
                for k in ("text", "message", "content", "delta"):
                    v = obj.get(k)
                    if isinstance(v, str) and v.strip():
                        return ("text", v.strip(), {"json": True})
                return ("raw", s, {"json": True})
        except Exception:
            pass
    low = s.lower()
    if "error" in low or s.startswith("ERROR"):
        return ("error", s, {})
    return ("text", s, {})
