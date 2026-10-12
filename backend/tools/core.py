"""SDK-agnostic tool logic — pure async functions, no Pydantic AI types."""


async def do_submit_flag(platform, challenge_name: str, flag: str) -> tuple[str, bool]:
    """Submit a flag. Returns (display_message, is_confirmed)."""
    flag = flag.strip()
    if not flag:
        return "Empty flag — nothing to submit.", False

    try:
        result = await platform.submit_flag(challenge_name, flag)
        is_confirmed = result.status in ("correct", "already_solved")
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


# Image extensions recognized by vision-capable models (kept for prompt hints).
IMAGE_EXTS_FOR_VISION: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".tiff": "image/tiff",
    ".tif": "image/tiff",
    ".webp": "image/webp",
}
