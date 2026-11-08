"""Deterministic, fail-closed classification of proposed browser actions."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, Literal
from urllib.parse import urljoin, urlsplit

from gba_agent.api.contracts_compat import ActionDecision, BrowserAction
from gba_agent.guards import reason_codes as rc

_COMMIT = re.compile(
    r"\b(?:submit|e-?file|file|e-?sign|sign|send|confirm|approve|accept|delete|remove|"
    r"cancel|save|update|change|register|apply|authorize|agree|post|publish|transfer)\b",
    re.IGNORECASE,
)
_SPEND = re.compile(
    r"\b(?:pay|purchase|buy|checkout|check out|add to cart|place order|order|donate|"
    r"subscribe|card)\b",
    re.IGNORECASE,
)
_LOGIN = re.compile(r"\b(?:log ?in|sign ?in|continue with|sso)\b", re.IGNORECASE)
_UPLOAD = re.compile(r"\b(?:upload|attach|choose file|browse)\b", re.IGNORECASE)
_SEARCH = re.compile(
    r"\b(?:search|find|filter|apply filters|go|next|previous|prev|more|show|view|"
    r"results|page)\b",
    re.IGNORECASE,
)
_SECRET = re.compile(
    r"(?:password|passwd|cvv|cvc|card|cc-|ssn|social[\s_-]*security|secret|token)",
    re.IGNORECASE,
)
_ActionClass = Literal["READ", "SEARCH", "LOGIN", "SPEND", "UPLOAD", "COMMIT"]
_ClassifiedAction = _ActionClass | Literal["unclassified"]
_READ_ACTIONS = {
    "navigate",
    "back",
    "forward",
    "scroll",
    "wait",
    "new_tab",
    "close_tab",
    "download",
}


def _field(value: object, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _strings(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return tuple(item for item in value if isinstance(item, str))
    return ()


def _normalize_host(host: str) -> str:
    try:
        return host.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError:
        return ""


def _resolve_host(url: object, base_url: str) -> str | None:
    if not isinstance(url, str):
        return None
    try:
        parsed = urlsplit(urljoin(base_url, url.strip()))
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            return None
    except ValueError:
        return None
    normalized = _normalize_host(parsed.hostname)
    return normalized or None


def _site_allowed(action: object, site_entry: object | None) -> bool:
    if site_entry is None:
        return False
    if _field(site_entry, "entry_id") != _field(action, "site_id"):
        return False
    domains = {
        _normalize_host(domain) for domain in _strings(_field(site_entry, "base_domains", ()))
    }
    domains.discard("")
    if not domains:
        return False

    page_url = _field(action, "page_url")
    page_host = _resolve_host(page_url, "")
    if page_host is None or page_host not in domains:
        return False

    target = _field(action, "target")
    candidate_urls = (
        _field(target, "href"),
        _field(target, "form_action"),
    )
    for candidate in candidate_urls:
        if candidate is not None:
            host = _resolve_host(candidate, page_url)
            if host is None or host not in domains:
                return False

    action_type = _field(action, "action_type")
    value = _field(action, "value")
    if action_type in {"navigate", "download", "new_tab"} and value:
        host = _resolve_host(value, page_url)
        if host is None or host not in domains:
            return False
    return True


def _target_text(target: object | None) -> str:
    if target is None:
        return ""
    return " ".join(
        value
        for key in ("name", "label", "text")
        if isinstance((value := _field(target, key)), str)
    )


def _secret_field(action: object) -> bool:
    if _field(action, "action_type") not in {"type", "select"}:
        return False
    target = _field(action, "target")
    if target is None:
        return False
    if str(_field(target, "input_type", "")).casefold() == "password":
        return True
    secret_values = (
        _field(target, "name"),
        _field(target, "label"),
        _field(target, "text"),
        _field(target, "autocomplete"),
    )
    return any(isinstance(value, str) and _SECRET.search(value) for value in secret_values)


def _is_search_form(target: object | None, site_config: object | None) -> bool:
    if target is None or site_config is None:
        return False
    configured = set(_strings(_field(site_config, "search_forms", ())))
    if not configured:
        return False
    target_values = (
        _field(target, "selector"),
        _field(target, "form_action"),
        _field(target, "name"),
    )
    return any(isinstance(value, str) and value in configured for value in target_values)


def _label_classes(label: str) -> tuple[bool, bool, bool, bool, bool]:
    return (
        bool(_COMMIT.search(label)),
        bool(_SPEND.search(label)),
        bool(_LOGIN.search(label)),
        bool(_UPLOAD.search(label)),
        bool(_SEARCH.search(label)),
    )


def _is_submit_control(target: object | None) -> bool:
    if target is None:
        return False
    tag = str(_field(target, "tag", "")).casefold()
    input_type = str(_field(target, "input_type", "")).casefold()
    return tag == "button" or (tag == "input" and input_type in {"submit", "button", "image"})


def _classify(action: object, site_config: object) -> _ClassifiedAction:
    action_type = _field(action, "action_type")
    target = _field(action, "target")

    if action_type in _READ_ACTIONS:
        return "READ"
    if action_type == "type" or action_type == "select":
        return "READ"
    if action_type == "press_key" and str(_field(action, "value", "")).strip().casefold() not in {
        "enter",
        "return",
    }:
        return "READ"
    if action_type == "click" and target is not None:
        if str(_field(target, "tag", "")).casefold() == "a" and _field(target, "href"):
            return "READ"
    if action_type == "upload":
        return "UPLOAD"
    if action_type != "press_key" and not (action_type == "click" and _is_submit_control(target)):
        return "unclassified"

    label = _target_text(target)
    commit, spend, login, upload, search = _label_classes(label)

    if commit and _is_search_form(target, site_config):
        # A configured search form may use a lone "save" label for filtering;
        # no other commit wording is relaxed.
        commit_terms = [match.group(0).casefold() for match in _COMMIT.finditer(label)]
        if commit_terms and all(term == "save" for term in commit_terms):
            commit = False
            search = True
    if commit:
        return "COMMIT"
    if spend:
        return "SPEND"
    if login:
        return "LOGIN"
    if upload:
        return "UPLOAD"

    form_method = str(_field(target, "form_method", "")).casefold()
    if form_method == "get" or _is_search_form(target, site_config) or search:
        return "SEARCH"
    if form_method == "post":
        return "COMMIT"
    return "unclassified"


def _upload_is_approved(action: object, site_config: object) -> bool:
    value = _field(action, "value")
    if not isinstance(value, str) or not value:
        return False
    approved_uploads = _strings(_field(site_config, "approved_uploads", ()))
    if value not in approved_uploads:
        return False

    target = _field(action, "target")
    destination = _field(target, "form_action")
    if not isinstance(destination, str) or not destination:
        return False
    page_url = _field(action, "page_url")
    try:
        resolved_url = urljoin(page_url, destination)
        parsed = urlsplit(resolved_url)
    except ValueError:
        return False
    host = _normalize_host(parsed.hostname or "")
    approved_destinations = _strings(_field(site_config, "approved_destinations", ()))
    for approved in approved_destinations:
        try:
            if "://" in approved:
                approved_url = approved
            elif "/" in approved:
                approved_url = urljoin(page_url, approved)
            else:
                approved_url = f"https://{approved}"
            approved_parts = urlsplit(approved_url)
        except ValueError:
            continue
        approved_host = _normalize_host(approved_parts.hostname or approved)
        if (
            parsed.scheme.casefold() == approved_parts.scheme.casefold()
            and host == approved_host
            and parsed.port == approved_parts.port
            and parsed.path == approved_parts.path
            and parsed.query == approved_parts.query
        ):
            return True
        if host and approved_host and host == approved_host and approved_parts.path in {"", "/"}:
            return True
    return False


def _decision(
    action_class: _ClassifiedAction,
    allowed: bool,
    requires_human: bool,
    reason_code: str,
    reason_text: str,
) -> ActionDecision:
    # "unclassified" is absent from Doc 00's closed ActionClass enum. COMMIT
    # is the contract-valid fail-closed class; the exact unclassified reason
    # preserves the decision-table outcome without extending the shared schema.
    if action_class == "unclassified":
        action_class = "COMMIT"
    return ActionDecision(
        action_class=action_class,
        allowed=allowed,
        requires_human=requires_human,
        reason_code=reason_code,
        reason_text=reason_text,
    )


def classify(
    action: BrowserAction,
    site_entry: object | None,
    site_config: object | None,
    run_config: object | None,
) -> ActionDecision:
    """Classify a proposed browser action without network or model calls."""
    del run_config
    if not _site_allowed(action, site_entry):
        return _decision(
            "COMMIT",
            False,
            False,
            rc.AG_SITE_NOT_ALLOWED,
            "The action or one of its destinations is outside the registered site domains.",
        )
    if _secret_field(action):
        return _decision(
            "COMMIT",
            False,
            False,
            rc.AG_SECRET_FIELD_FORBIDDEN,
            "Typing or selecting into a secret field is forbidden; credentials use the vault flow.",
        )

    action_class = _classify(action, site_config)
    if action_class == "READ":
        return _decision("READ", True, False, rc.AG_READ_OK, "Read-only site action is permitted.")
    if action_class == "SEARCH":
        permitted = "SEARCH" in _strings(_field(site_entry, "allowed_action_classes", ()))
        return _decision(
            "SEARCH",
            permitted,
            False,
            rc.AG_SEARCH_OK if permitted else rc.AG_SEARCH_NOT_PERMITTED,
            "Search action is permitted by the site registry."
            if permitted
            else "Search is not permitted by the site registry.",
        )
    if action_class == "LOGIN":
        return _decision(
            "LOGIN",
            False,
            False,
            rc.AG_LOGIN_VIA_VAULT_ONLY,
            "Login actions are refused; authentication must use the controlled vault flow.",
        )
    if action_class == "SPEND":
        return _decision(
            "SPEND",
            False,
            True,
            rc.AG_SPEND_NEEDS_HUMAN,
            "Spending actions are not executed by the agent and require a human.",
        )
    if action_class == "UPLOAD":
        approved = site_config is not None and _upload_is_approved(action, site_config)
        return _decision(
            "UPLOAD",
            approved,
            not approved,
            rc.AG_UPLOAD_APPROVED if approved else rc.AG_UPLOAD_NEEDS_HUMAN,
            "The upload matches the site allowlist."
            if approved
            else "The upload is not fully approved for this site and destination.",
        )
    if action_class == "COMMIT":
        return _decision(
            "COMMIT",
            False,
            True,
            rc.AG_COMMIT_FORBIDDEN,
            "Submitting or changing information is forbidden for the agent.",
        )
    return _decision(
        "unclassified",
        False,
        True,
        rc.AG_UNCLASSIFIED_DENY,
        "The action could not be classified safely and is denied.",
    )
