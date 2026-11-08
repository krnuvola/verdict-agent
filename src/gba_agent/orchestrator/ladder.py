"""Exact Doc 00 section 9.2 configuration mapping (security remains mandatory)."""

from gba_agent.api.contracts_compat import RunConfig

_LEVELS = (
    (),
    ("access",),
    ("access", "source_gate"),
    ("access", "source_gate", "claim_verifier"),
    ("access", "source_gate", "claim_verifier", "scope", "temporal"),
    ("access", "source_gate", "claim_verifier", "scope", "temporal", "gnn"),
)


def ladder_config(level: str) -> RunConfig:
    if level not in {f"A{i}" for i in range(7)}:
        raise ValueError("Unknown ladder level")
    index = int(level[1])
    return RunConfig(guards_enabled=list(_LEVELS[min(index, 5)]), use_workbench=index == 6)
