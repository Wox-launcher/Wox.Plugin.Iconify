import math
import re
from dataclasses import dataclass

_UNITS_SPLIT = re.compile(r"(-?[0-9.]*[0-9]+[0-9.]*)")
_UNITS_TEST = re.compile(r"^-?[0-9.]*[0-9]+[0-9.]*$")
_PROPS = ("left", "top", "width", "height", "body", "hidden")
_HEX_COLOR = re.compile(r"#([0-9a-fA-F]{3,8})\b")
_RGB_COLOR = re.compile(r"rgba?\(\s*([0-9.]+)\s*,\s*([0-9.]+)\s*,\s*([0-9.]+)", re.IGNORECASE)
_HSL_COLOR = re.compile(r"hsla?\(\s*[0-9.]+\s*,\s*([0-9.]+)%", re.IGNORECASE)
_PAINT_ATTR = re.compile(r"""(?:fill|stroke|stop-color|flood-color|color)\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_MONO_NAMES = {
    "currentcolor",
    "none",
    "transparent",
    "black",
    "white",
    "gray",
    "grey",
    "silver",
    "dimgray",
    "dimgrey",
    "darkgray",
    "darkgrey",
    "lightgray",
    "lightgrey",
    "gainsboro",
    "whitesmoke",
}


@dataclass(frozen=True)
class IconRecord:
    prefix: str
    name: str
    body: str
    width: int
    height: int
    left: int
    top: int
    rotate: int
    h_flip: bool
    v_flip: bool


def calculate_size(size: str | int | float, ratio: float, precision: int = 100) -> str | float:
    """Match Iconify's calculateSize, used when only one SVG axis is set."""
    if ratio == 1:
        return size
    if isinstance(size, bool):
        return size
    if isinstance(size, (int, float)):
        return math.ceil(size * ratio * precision - 1e-9) / precision

    parts = _UNITS_SPLIT.split(size)
    if not parts:
        return size

    rendered: list[str] = []
    is_number = bool(_UNITS_TEST.fullmatch(parts[0]))
    index = 0
    while True:
        code = parts[index]
        if is_number:
            try:
                number = float(code)
            except ValueError:
                rendered.append(code)
            else:
                rendered.append(_format_scaled(number, ratio, precision))
        else:
            rendered.append(code)
        index += 1
        if index >= len(parts):
            return "".join(rendered)
        is_number = not is_number


def icon_to_svg(icon: IconRecord, color: str | None = None) -> str:
    """Build an SVG the same way Iconify's iconToSVG does, then apply color."""
    left = float(icon.left)
    top = float(icon.top)
    width = float(icon.width or 16)
    height = float(icon.height or 16)
    if width <= 0:
        width = 16
    if height <= 0:
        height = 16
    rotate = icon.rotate
    transforms: list[str] = []

    if icon.h_flip:
        if icon.v_flip:
            rotate += 2
        else:
            transforms.append(f"translate({_num(width + left)} {_num(0 - top)})")
            transforms.append("scale(-1 1)")
            top = 0
            left = 0
    elif icon.v_flip:
        transforms.append(f"translate({_num(0 - left)} {_num(height + top)})")
        transforms.append("scale(1 -1)")
        top = 0
        left = 0

    if rotate < 0:
        rotate -= math.floor(rotate / 4) * 4
    rotate %= 4

    if rotate == 1:
        pivot = height / 2 + top
        transforms.insert(0, f"rotate(90 {_num(pivot)} {_num(pivot)})")
    elif rotate == 2:
        transforms.insert(0, f"rotate(180 {_num(width / 2 + left)} {_num(height / 2 + top)})")
    elif rotate == 3:
        pivot = width / 2 + left
        transforms.insert(0, f"rotate(-90 {_num(pivot)} {_num(pivot)})")

    if rotate % 2 == 1:
        if left != top:
            left, top = top, left
        if width != height:
            width, height = height, width

    body = icon.body
    if transforms:
        defs, content = _split_svg_defs(body)
        wrapped = f'<g transform="{" ".join(transforms)}">{content}</g>'
        body = f"<defs>{defs}</defs>{wrapped}" if defs else wrapped

    height_attr = "1em"
    width_attr = calculate_size(height_attr, width / height)
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{width_attr}" height="{height_attr}" '
        f'viewBox="{_num(left)} {_num(top)} {_num(width)} {_num(height)}">{body}</svg>'
    )
    if color:
        svg = svg.replace("currentColor", color)
    return svg


def iter_icons(data: dict, prefix_hint: str) -> list[IconRecord]:
    """Resolve one IconifyJSON icon set, including alias transforms."""
    icons = data.get("icons")
    aliases = data.get("aliases") or {}
    if not isinstance(icons, dict):
        return []
    if not isinstance(aliases, dict):
        aliases = {}

    prefix = data.get("prefix")
    if not isinstance(prefix, str) or not prefix.strip():
        prefix = prefix_hint
    prefix = prefix.strip().casefold()

    resolved: dict[str, list[str] | None] = {}

    def resolve(name: str) -> list[str] | None:
        if name in resolved:
            return resolved[name]
        if name in icons:
            resolved[name] = []
            return []
        # Mark before walking the parent so an alias cycle stops instead of recursing forever.
        resolved[name] = None
        alias = aliases.get(name)
        parent = alias.get("parent") if isinstance(alias, dict) else None
        if not isinstance(parent, str) or not parent:
            return None
        parent_chain = resolve(parent)
        if parent_chain is None:
            return None
        chain = [parent, *parent_chain]
        resolved[name] = chain
        return chain

    names = [name for name in icons if isinstance(name, str)]
    names.extend(name for name in aliases if isinstance(name, str) and name not in icons)

    records: list[IconRecord] = []
    seen: set[str] = set()
    for name in names:
        if name in seen:
            continue
        chain = resolve(name)
        if chain is None:
            continue
        props: dict = {}
        for item_name in (name, *chain):
            source = icons.get(item_name)
            if not isinstance(source, dict):
                source = aliases.get(item_name)
            if not isinstance(source, dict):
                source = {}
            props = _merge_icon_data(source, props)
        props = _merge_icon_data(data, props)
        if props.get("hidden"):
            continue
        body = props.get("body")
        if not isinstance(body, str) or not body:
            continue
        seen.add(name)
        width = _as_int(props.get("width"), 16) or 16
        height = _as_int(props.get("height"), 16) or 16
        records.append(
            IconRecord(
                prefix=prefix,
                name=name,
                body=body,
                width=width,
                height=height,
                left=_as_int(props.get("left"), 0),
                top=_as_int(props.get("top"), 0),
                rotate=_as_int(props.get("rotate"), 0) % 4,
                h_flip=bool(props.get("hFlip")),
                v_flip=bool(props.get("vFlip")),
            )
        )
    return records


def is_colorful(body: str) -> bool:
    """True when the icon paints a color other than black, white, gray, or currentColor."""
    for match in _HEX_COLOR.finditer(body):
        if _hex_is_chromatic(match.group(1)):
            return True
    for match in _RGB_COLOR.finditer(body):
        if _channels_are_chromatic(match.group(1), match.group(2), match.group(3)):
            return True
    for match in _HSL_COLOR.finditer(body):
        if float(match.group(1)) > 0:
            return True
    for match in _PAINT_ATTR.finditer(body):
        value = match.group(1).strip().lower()
        if value.startswith(("#", "rgb", "hsl", "url(", "var(")):
            continue
        if value not in _MONO_NAMES:
            return True
    return False


def download_icon_svg() -> str:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128" viewBox="0 0 128 128">'
        '<rect width="128" height="128" rx="28" fill="#1f6feb"/>'
        '<path d="M64 28v48" stroke="#fff" stroke-width="8" stroke-linecap="round"/>'
        '<path d="M42 62l22 22 22-22" fill="none" stroke="#fff" stroke-width="8" stroke-linecap="round" stroke-linejoin="round"/>'
        '<path d="M36 100h56" stroke="#fff" stroke-width="8" stroke-linecap="round"/>'
        "</svg>"
    )


def progress_icon_svg(fraction: float | None) -> str:
    if fraction is None:
        bar_width = 18
    else:
        clamped = min(1.0, max(0.0, fraction))
        bar_width = max(8, int(round(88 * clamped)))
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128" viewBox="0 0 128 128">'
        '<rect width="128" height="128" rx="28" fill="#1c2333"/>'
        '<rect x="20" y="58" width="88" height="14" rx="7" fill="#2e3a4f"/>'
        f'<rect x="20" y="58" width="{bar_width}" height="14" rx="7" fill="#4ea1ff"/>'
        "</svg>"
    )


def _merge_icon_data(parent: dict, child: dict) -> dict:
    """Merge Iconify icon or alias data. Child fields win; rotations and flips add up."""
    result: dict = {}
    if bool(parent.get("hFlip")) != bool(child.get("hFlip")):
        result["hFlip"] = True
    if bool(parent.get("vFlip")) != bool(child.get("vFlip")):
        result["vFlip"] = True
    rotate = (_as_int(parent.get("rotate"), 0) + _as_int(child.get("rotate"), 0)) % 4
    if rotate:
        result["rotate"] = rotate
    for key in _PROPS:
        if key in child and child[key] is not None:
            result[key] = child[key]
        elif key in parent and parent[key] is not None:
            result[key] = parent[key]
    return result


def _split_svg_defs(content: str, tag: str = "defs") -> tuple[str, str]:
    defs: list[str] = []
    while True:
        index = content.find("<" + tag)
        if index < 0:
            break
        start = content.find(">", index)
        end = content.find("</" + tag, start if start >= 0 else index)
        if start < 0 or end < 0:
            break
        end_end = content.find(">", end)
        if end_end < 0:
            break
        defs.append(content[start + 1 : end].strip())
        content = (content[:index].strip() + content[end_end + 1 :]).strip()
    return "".join(defs), content


def _format_scaled(number: float, ratio: float, precision: int) -> str:
    scaled = math.ceil(number * ratio * precision - 1e-9) / precision
    if scaled == int(scaled):
        return str(int(scaled))
    return f"{scaled:.10f}".rstrip("0").rstrip(".")


def _num(value: float) -> str:
    if value == int(value):
        return str(int(value))
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _hex_is_chromatic(value: str) -> bool:
    if len(value) == 3:
        channels = [float(int(channel * 2, 16)) for channel in value]
    elif len(value) == 4:
        channels = [float(int(channel * 2, 16)) for channel in value[:3]]
    elif len(value) in {6, 8}:
        channels = [float(int(value[index : index + 2], 16)) for index in (0, 2, 4)]
    else:
        return False
    return _channel_values_are_chromatic(channels)


def _channels_are_chromatic(red: str, green: str, blue: str) -> bool:
    try:
        channels = [float(red), float(green), float(blue)]
    except ValueError:
        return False
    return _channel_values_are_chromatic(channels)


def _channel_values_are_chromatic(channels: list[float]) -> bool:
    red, green, blue = channels
    return abs(red - green) > 12 or abs(green - blue) > 12 or abs(red - blue) > 12


def _as_int(value: object, default: int) -> int:
    if isinstance(value, bool) or value is None:
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return default
