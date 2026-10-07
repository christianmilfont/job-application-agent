from __future__ import annotations

import re

from bs4 import BeautifulSoup

_BLOCKS = ["p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "tr", "section", "article"]
BULLET = "• "
HEADING = "## "


def html_to_text(html: str) -> str:
    """HTML -> texto com uma linha por bloco, bullets ("• ") e cabeçalhos ("## ") preservados."""
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    # Cabeçalhos: <hN> ou bloco cujo conteúdo inteiro está em <strong>/<b>
    for tag in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        tag.insert(0, HEADING)
    for tag in soup.find_all(["p", "div"]):
        if tag.find(_BLOCKS) or tag.find("li"):
            continue
        strong = tag.find(["strong", "b"])
        text = tag.get_text(" ", strip=True)
        if strong and text and strong.get_text(" ", strip=True) == text and len(text) <= 90:
            tag.insert(0, HEADING)
    for li in soup.find_all("li"):
        li.insert(0, "\n" + BULLET)
        li.append("\n")
    for tag in soup.find_all(_BLOCKS):
        tag.insert_before("\n")
        tag.append("\n")
    return normalize_lines(soup.get_text())


def normalize_lines(text: str) -> str:
    lines = []
    for line in text.replace("\r", "").split("\n"):
        line = re.sub(r"[ \t ]+", " ", line).strip()
        line = re.sub(r"^[-*·▪●◦]\s+", BULLET, line)
        line = re.sub(r"^(##\s*)+", HEADING, line)
        if line and line.strip() not in (BULLET.strip(), HEADING.strip()):
            lines.append(line)
    return "\n".join(lines)
