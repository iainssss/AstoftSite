import argparse
import re
from pathlib import Path
from urllib.parse import quote, unquote

from bs4 import BeautifulSoup, Doctype
from bs4.element import Comment, Tag


LEGACY_FONT_SIZES = {
    "1": "0.7em",
    "2": "0.8em",
    "3": "1em",
    "4": "1.2em",
    "5": "1.5em",
    "6": "2em",
    "7": "3em",
}


def add_style(tag, name, value):
    declarations = [
        declaration.strip()
        for declaration in tag.get("style", "").split(";")
        if declaration.strip()
    ]
    declarations = [
        declaration
        for declaration in declarations
        if declaration.split(":", 1)[0].strip().lower() != name
    ]
    declarations.append(f"{name}: {value}")
    tag["style"] = "; ".join(declarations)


def css_dimension(value):
    value = str(value).strip()
    if re.fullmatch(r"\d+(?:\.\d+)?%", value):
        return value
    if re.fullmatch(r"\d+(?:\.\d+)?", value):
        return f"{value}px"
    return value if re.fullmatch(r"(?:auto|inherit|initial|unset)", value) else None


def unwrap_frontpage_markup(soup):
    for comment in soup.find_all(string=lambda value: isinstance(value, Comment)):
        if "msnavigation" in comment.lower():
            comment.extract()
    for tag in list(soup.find_all("msnavigation")):
        tag.unwrap()


def clean_metadata(soup, path):
    if soup.html is None:
        soup.append(soup.new_tag("html"))
    if soup.head is None:
        soup.html.insert(0, soup.new_tag("head"))
    if soup.body is None:
        soup.html.append(soup.new_tag("body"))

    html = soup.html
    html.attrs.pop("xmlns", None)
    head = soup.head
    language = html.get("lang")
    for meta in list(head.find_all("meta")):
        name = str(meta.get("name", "")).lower()
        http_equiv = str(meta.get("http-equiv", "")).lower()
        if name in {"generator", "progid", "microsoft border"}:
            meta.decompose()
        elif http_equiv == "content-language":
            language = meta.get("content") or language
            meta.decompose()
        elif http_equiv == "content-type":
            meta.decompose()
        elif http_equiv == "refresh":
            meta["http-equiv"] = "refresh"
            if meta.get("content"):
                meta["content"] = re.sub(
                    r";\s*url\s*=", "; url=", meta["content"], flags=re.IGNORECASE
                )

    for meta in list(head.find_all("meta", charset=True)):
        meta.decompose()
    head.insert(0, soup.new_tag("meta", charset="utf-8"))
    head.insert(1, soup.new_tag("meta", attrs={"name": "viewport", "content": "width=device-width, initial-scale=1"}))
    html["lang"] = (str(language).split("-")[0].lower() if language else "en")

    title = head.find("title")
    for duplicate in list(head.find_all("title"))[1:]:
        duplicate.decompose()
    if title is None:
        title = soup.new_tag("title")
        head.append(title)
    if not title.get_text(strip=True):
        title.string = path.stem.replace("_", " ").replace("-", " ").title()


def modernize_font(tag, soup):
    styles = {}
    if tag.get("color"):
        styles["color"] = tag["color"]
    if tag.get("face"):
        styles["font-family"] = tag["face"]
    if tag.get("size"):
        size = str(tag["size"]).strip()
        if size in LEGACY_FONT_SIZES:
            styles["font-size"] = LEGACY_FONT_SIZES[size]
        elif size.startswith(("+", "-")) and size[1:] in LEGACY_FONT_SIZES:
            base = int(size[1:])
            adjusted = max(1, min(7, 3 + base * (1 if size[0] == "+" else -1)))
            styles["font-size"] = LEGACY_FONT_SIZES[str(adjusted)]

    span = soup.new_tag("span")
    for name, value in styles.items():
        add_style(span, name, value)
    tag.wrap(span)
    tag.unwrap()


def convert_legacy_attributes(soup):
    for font in list(soup.find_all("font")):
        modernize_font(font, soup)

    for center in list(soup.find_all("center")):
        center.name = "div"
        add_style(center, "text-align", "center")

    anchor_names = {}
    for anchor in soup.find_all("a", attrs={"name": True}):
        old_name = str(anchor["name"])
        new_id = re.sub(r"\s+", "-", old_name.strip())
        if new_id:
            anchor_names[old_name] = new_id
            if not anchor.get("id"):
                anchor["id"] = new_id
        del anchor["name"]

    for custom_tag in list(soup.find_all(lambda element: ":" in element.name)):
        custom_tag.unwrap()

    for tag in soup.find_all(True):
        name = tag.name.lower()
        attrs = dict(tag.attrs)

        for attribute in list(tag.attrs):
            if not re.fullmatch(r"[A-Za-z_:][A-Za-z0-9_.:-]*", attribute) or ":" in attribute:
                del tag[attribute]

        if attrs.get("align"):
            alignment = str(attrs["align"]).lower()
            if alignment in {"left", "right", "center", "justify"}:
                if name == "img" and alignment == "center":
                    add_style(tag, "display", "block")
                    add_style(tag, "margin-inline", "auto")
                elif name == "table" and alignment in {"left", "right", "center"}:
                    add_style(tag, "margin-inline", "auto" if alignment == "center" else alignment)
                else:
                    add_style(tag, "text-align", alignment)
            del tag["align"]

        if attrs.get("valign"):
            value = str(attrs["valign"]).lower()
            if value in {"top", "middle", "bottom", "baseline"}:
                add_style(tag, "vertical-align", "middle" if value == "middle" else value)
            del tag["valign"]

        if attrs.get("bgcolor"):
            add_style(tag, "background-color", attrs["bgcolor"])
            del tag["bgcolor"]
        if attrs.get("bordercolor"):
            add_style(tag, "border-color", attrs["bordercolor"])
            del tag["bordercolor"]

        for dimension in ("width", "height"):
            if attrs.get(dimension) and not (name == "img" or name in {"iframe", "canvas", "video", "source"}):
                css_value = css_dimension(attrs[dimension])
                if css_value:
                    add_style(tag, dimension, css_value)
                del tag[dimension]

        if name == "table":
            if attrs.get("cellpadding"):
                padding = css_dimension(attrs["cellpadding"])
                if padding:
                    for cell in tag.find_all(["td", "th"], recursive=False):
                        add_style(cell, "padding", padding)
                del tag["cellpadding"]
            if attrs.get("cellspacing"):
                spacing = css_dimension(attrs["cellspacing"])
                if spacing:
                    add_style(tag, "border-spacing", spacing)
                del tag["cellspacing"]

        if attrs.get("border") and name in {"table", "img"}:
            border = str(attrs["border"]).strip()
            if border == "0":
                add_style(tag, "border", "0")
            elif border.isdigit():
                add_style(tag, "border", f"{border}px solid currentColor")
            del tag["border"]

        if attrs.get("nowrap") is not None:
            add_style(tag, "white-space", "nowrap")
            del tag["nowrap"]

        if tag.get("style"):
            tag["style"] = re.sub(
                r"(?i)(margin-(?:left|right|top|bottom)\s*:\s*(-?\d+(?:\.\d+)?))(\s*(?:;|$))",
                lambda match: f"{match.group(1)}px{match.group(3)}",
                tag["style"],
            )

        for attribute in list(tag.attrs):
            if attribute.lower().startswith("x-") or attribute.lower().startswith("xthumbnail-"):
                del tag[attribute]

        if name == "img" and not tag.has_attr("alt"):
            tag["alt"] = ""
        if name == "a" and tag.get("href"):
            href = tag["href"]
            base, separator, fragment = href.partition("#")
            if separator:
                decoded_fragment = unquote(fragment)
                decoded_fragment = anchor_names.get(
                    decoded_fragment, re.sub(r"\s+", "-", decoded_fragment.strip())
                )
                encoded_fragment = quote(decoded_fragment, safe="/:?@!$&'()*+,;=%[]")
                href = f"{base}#{encoded_fragment}"
            tag["href"] = quote(href, safe="/:#?@!$&'()*+,;=%[]")

        if tag.get("style"):
            tag["style"] = clean_css(tag["style"])

    for row in list(soup.find_all("tr")):
        if not row.find(["td", "th"], recursive=False):
            row.decompose()

    block_elements = {
        "address", "article", "aside", "blockquote", "div", "dl", "fieldset",
        "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header",
        "hr", "main", "nav", "ol", "p", "pre", "section", "table", "ul",
    }
    for span in list(soup.find_all("span")):
        if any(descendant.name in block_elements for descendant in span.find_all(True)):
            span.unwrap()
    for paragraph in list(soup.find_all("p")):
        if paragraph.find(["div", "p", "table"]):
            paragraph.unwrap()

    for style in soup.find_all("style"):
        style.string = clean_css(style.get_text())


def clean_css(css):
    declarations = []
    for declaration in css.split(";"):
        property_name, separator, value = declaration.partition(":")
        if not separator:
            if declaration.strip():
                declarations.append(declaration.strip())
            continue
        property_name = property_name.strip()
        value = value.strip()
        property_lower = property_name.lower()
        if property_lower.startswith("mso-"):
            continue
        if property_lower in {"orphans", "widows"} and value.lower() == "auto":
            continue
        if property_lower == "text-align" and value.lower() == "auto":
            continue
        value = re.sub(
            r"^(\d+(?:\.\d+)?)(\s+solid\b)", r"\1px\2", value, flags=re.IGNORECASE
        )
        declarations.append(f"{property_name}: {value}")
    return "; ".join(declarations)


def modernize_document(source, path):
    source = re.sub(r"(?is)<!--\s*msnavigation\s*-->", "", source)
    source = re.sub(r"(?is)</?msnavigation\b[^>]*>", "", source)
    source = re.sub(
        r"(?i)(</table>(?:\s*</(?:td|tr|tbody|table)>\s*)*)</p>", r"\1", source
    )
    soup = BeautifulSoup(source, "html5lib")
    unwrap_frontpage_markup(soup)
    clean_metadata(soup, path)
    convert_legacy_attributes(soup)

    doctype = next((node for node in soup.contents if isinstance(node, Doctype)), None)
    if doctype is not None:
        doctype.replace_with(Doctype("html"))
    else:
        soup.insert(0, Doctype("html"))
    return "<!doctype html>\n" + str(soup).split("\n", 1)[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--in-place", action="store_true")
    args = parser.parse_args()
    if args.in_place == bool(args.output_dir):
        parser.error("choose exactly one of --in-place or --output-dir")

    paths = []
    for item in args.files:
        if item.is_dir():
            paths.extend(sorted(item.rglob("*.htm")))
            paths.extend(sorted(item.rglob("*.html")))
        else:
            paths.append(item)

    for path in paths:
        raw = path.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            raw = raw[3:]
            source = raw.decode("utf-8")
        elif raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            source = raw.decode("utf-16")
        else:
            try:
                source = raw.decode("utf-8")
            except UnicodeDecodeError:
                source = raw.decode("windows-1252")
        output = modernize_document(source, path).encode("utf-8")
        destination = path if args.in_place else args.output_dir / path.resolve().relative_to(args.root.resolve())
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(output)
        print(f"{path} -> {destination}")


if __name__ == "__main__":
    main()