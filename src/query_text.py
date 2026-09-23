import re

_COLOR_RE = re.compile(r"\s+(#[0-9a-fA-F]{3}|#[0-9a-fA-F]{6})$")
_COMMON_COLORS = {"red", "green", "blue", "black", "white", "yellow", "orange", "purple", "gray", "grey"}
_SAFE_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$|^(?:red|green|blue|black|white|yellow|orange|purple|gray|grey)$")


def parse_query(query_str: str) -> tuple[str, str | None]:
    """Split a trailing hex or CSS color name off the icon search text."""
    match = _COLOR_RE.search(query_str)
    if match:
        color = match.group(1)
        term = query_str[: match.start()].strip()
        return term, color if _SAFE_COLOR_RE.fullmatch(color) else None

    parts = query_str.split()
    if len(parts) > 1 and parts[-1].lower() in _COMMON_COLORS:
        return " ".join(parts[:-1]), parts[-1].lower()

    return query_str.strip(), None


def escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class IconQuery:
    def __init__(self, text: str, tokens: list[str], prefix: str, name: str) -> None:
        self.text = text
        self.tokens = tokens
        self.prefix = prefix
        self.name = name


def parse_icon_query(term: str) -> IconQuery | None:
    """Parse `dog`, `dog side`, or `mdi:dog` into a local catalog query."""
    text = term.strip().casefold()
    if not text:
        return None

    if ":" in text:
        prefix, name = text.split(":", 1)
        prefix = prefix.strip()
        name = name.strip()
        if prefix:
            return IconQuery(text=name or prefix, tokens=[name] if name else [], prefix=prefix, name=name)

    tokens = [token for token in text.split() if token]
    if not tokens:
        return None
    return IconQuery(text=text, tokens=tokens, prefix="", name="")
