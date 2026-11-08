"""Doc 01 section 2 import restrictions, including relative and aliased imports."""

import ast
from importlib.util import resolve_name
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "src" / "gba_agent"


def test_access_and_guard_import_boundaries() -> None:
    violations = []
    for path in ROOT.rglob("*.py"):
        parts = path.relative_to(ROOT).with_suffix("").parts
        module = ".".join(("gba_agent", *parts))
        package = module.rsplit(".", 1)[0]
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
            if isinstance(node, ast.Import):
                imports = [item.name for item in node.names]
            elif isinstance(node, ast.ImportFrom):
                name = node.module or ""
                prefix = resolve_name("." * node.level + name, package) if node.level else name
                imports = [prefix, *(f"{prefix}.{item.name}" for item in node.names)]
            else:
                continue
            for imported in imports:
                matches_vault = imported == "gba_agent.access.vault"
                matches_login = imported == "gba_agent.access.login"
                forbidden = (
                    (matches_vault and parts != ("access", "login"))
                    or (
                        parts[0] in {"operator", "orchestrator"}
                        and (matches_vault or matches_login)
                    )
                    or (
                        parts[0] == "guards"
                        and imported.split(".")[:2]
                        in (["gba_agent", "operator"], ["gba_agent", "orchestrator"])
                    )
                )
                if forbidden:
                    violations.append(f"{path.relative_to(ROOT)}:{node.lineno} imports {imported}")
    assert not violations, "\n".join(violations)
