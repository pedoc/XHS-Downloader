import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

import pytest

GUI_ROOT = Path(__file__).resolve().parents[1] / "static" / "GUI"


class LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: tuple[dict[str, str | None], ...] = ()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "link":
            self.links = (*self.links, dict(attrs))


@pytest.fixture(scope="module")
def gui_links() -> tuple[dict[str, str | None], ...]:
    parser = LinkParser()
    parser.feed((GUI_ROOT / "index.html").read_text(encoding="utf-8"))
    parser.close()
    return parser.links


@pytest.fixture(scope="module")
def gui_css() -> str:
    return (GUI_ROOT / "styles.css").read_text(encoding="utf-8")


@pytest.mark.parametrize("relation", ("preconnect", "dns-prefetch", "preload", "stylesheet"))
def test_gui_font_resource_links_stay_local(
    gui_links: tuple[dict[str, str | None], ...], relation: str
) -> None:
    for link in gui_links:
        if relation not in (link.get("rel") or "").lower().split():
            continue
        resource = urlsplit((link.get("href") or "").strip())
        assert not (resource.scheme or resource.netloc), link


def test_gui_font_links_do_not_use_inline_load_handlers(
    gui_links: tuple[dict[str, str | None], ...],
) -> None:
    assert not [link for link in gui_links if "onload" in link]


def test_gui_keeps_its_local_stylesheet(
    gui_links: tuple[dict[str, str | None], ...],
) -> None:
    assert any(
        link.get("rel") == "stylesheet"
        and link.get("href") == "./styles.css"
        and link.get("media", "all") == "all"
        for link in gui_links
    )
    assert (GUI_ROOT / "styles.css").is_file()


def test_gui_font_stack_has_local_system_fallbacks(gui_css: str) -> None:
    declaration = re.search(r"--font-ui\s*:\s*([^;]+);", gui_css)
    assert declaration is not None
    families = tuple(
        family.strip(" \t\r\n\"'") for family in declaration.group(1).split(",")
    )
    assert "Noto Sans SC" in families
    assert "PingFang SC" in families
    assert "Microsoft YaHei" in families
    assert "system-ui" in families
    assert families[-1] == "sans-serif"
    assert re.search(r"font-family\s*:\s*var\(--font-ui\)\s*;", gui_css)


def test_gui_css_does_not_fetch_remote_fonts(gui_css: str) -> None:
    font_rules = re.findall(
        r"@import\b[^;]+;|@font-face\b[^}]*\}", gui_css, re.IGNORECASE
    )
    assert not re.search(
        r"(?:@import\s+|url\s*\(\s*)[\"']?\s*(?:https?:)?//",
        "\n".join(font_rules),
        re.IGNORECASE,
    )
