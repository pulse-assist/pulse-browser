"""Agent write policy: read-only default, allowlist, checkout confirm."""

from __future__ import annotations

from urllib.parse import urlparse


class PolicyError(Exception):
    """User-facing policy violation."""


DANGEROUS_DEFAULT = "checkout, /pay, /payment, /placeorder, /gp/buy, /order/confirm"


def parse_patterns(raw: str | None) -> list[str]:
    text = (raw or DANGEROUS_DEFAULT).strip()
    return [p.strip().lower() for p in text.split(",") if p.strip()]


def host_allowed(url: str, allowlist: list[str]) -> bool:
    if not allowlist:
        return True
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return False
    for rule in allowlist:
        rule = rule.strip().lower()
        if not rule:
            continue
        if rule.startswith("*."):
            suffix = rule[1:]  # .example.com
            if host.endswith(suffix) or host == rule[2:]:
                return True
        elif host == rule or host.endswith("." + rule):
            return True
    return False


def is_dangerous(url: str, patterns: list[str]) -> bool:
    needle = (url or "").lower()
    return any(p in needle for p in patterns)


def require_write(session: dict) -> None:
    if session.get("mode") != "write":
        raise PolicyError(
            "сессия в режиме read-only — агент не может кликать/печатать. "
            "Владелец: pulse browser session update --name … --mode write "
            "или кнопка на странице [Браузер](/p/browser/main)"
        )


def require_url_allowed(url: str, session: dict) -> None:
    allowlist = session.get("allowlist") or []
    if not host_allowed(url, allowlist):
        raise PolicyError(
            f"URL вне allowlist сессии «{session.get('name')}»: {url}. "
            f"Разрешены: {', '.join(allowlist)}"
        )


def check_mutation(
    session: dict,
    url: str,
    *,
    confirm_id: str | None,
    patterns: list[str],
    approved: set[str],
) -> str | None:
    """Return None if ok; raise PolicyError; or return needed confirm id token request.

    If dangerous and confirm_id missing/not approved — caller should create pending.
    Returns 'need_confirm' sentinel via raising a special case — we use return of
    'needs_confirm' string only when caller should create pending. Simpler: raise
    NeedConfirm from sessions layer.
    """
    require_write(session)
    require_url_allowed(url, session)
    if is_dangerous(url, patterns):
        if confirm_id and confirm_id in approved:
            return None
        return "needs_confirm"
    return None


class NeedConfirm(Exception):
    def __init__(self, message: str, pending_payload: dict):
        super().__init__(message)
        self.pending_payload = pending_payload
