from __future__ import annotations

from pathlib import Path


FRONTEND = Path(__file__).resolve().parents[1] / "web" / "frontend" / "src"


def test_frontend_never_calls_random_uuid_without_lan_safe_wrapper():
    offenders = []
    for path in FRONTEND.rglob("*"):
        if path.suffix not in {".ts", ".tsx", ".js", ".jsx"}:
            continue
        if path.name == "id.ts":
            continue
        if "crypto.randomUUID" in path.read_text(encoding="utf-8", errors="ignore"):
            offenders.append(str(path.relative_to(FRONTEND)))
    assert offenders == []


def test_accounts_uses_lan_safe_id_helper():
    source = (FRONTEND / "components" / "workspace" / "Accounts.tsx").read_text(encoding="utf-8")
    assert "from '../../lib/id'" in source
    assert "useRef(newId())" in source
    assert "creationKey.current = newId()" in source
