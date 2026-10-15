"""SDK-agnostic tool logic — pure async functions, no Pydantic AI types."""


async def do_submit_flag(platform, challenge_name: str, flag: str) -> tuple[str, bool]:
    """Submit a flag. Returns (display_message, is_confirmed).

    is_confirmed means "this challenge is now fully solved" — a correct flag on
    a multi-flag task is only *partial* credit and must NOT end the run early.
    """
    flag = flag.strip()
    if not flag:
        return "Empty flag — nothing to submit.", False

    try:
        result = await platform.submit_flag(challenge_name, flag)
        is_confirmed = result.status in ("correct", "already_solved") and (
            result.challenge_complete
            if hasattr(result, "challenge_complete")
            else True
        )
        return result.display, is_confirmed
    except Exception as e:
        return f"submit_flag error: {e}", False


async def do_check_findings(message_bus, model_spec: str) -> str:
    """Get unread findings from sibling solvers."""
    if not message_bus:
        return "No message bus available."
    findings = await message_bus.check(model_spec)
    if not findings:
        return "No new findings from other agents."
    return message_bus.format_unread(findings)


async def do_search_findings(message_bus, model_spec: str, query: str) -> str:
    """Keyword-search ALL sibling findings on this task (repeatable, on-demand).

    Complements do_check_findings: check returns only the unread backlog once
    and advances the cursor; search never touches cursors, so a solver can
    re-query a specific term (an endpoint, a credential, an error string) at
    any point. Pure homogeneous-worker knowledge sharing — no role split.
    """
    if not message_bus:
        return "No message bus available."
    if not (query or "").strip():
        return "search_findings: empty query."
    matches = await message_bus.search(query, model_spec)
    if not matches:
        return f"No sibling findings match {query!r}."
    return message_bus.format_search(matches, query.strip())
