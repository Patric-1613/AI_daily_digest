"""Deterministic extraction of useful metadata and prose from an HTML article."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any

_IGNORED_TAGS = frozenset({"script", "style", "noscript", "svg", "nav", "footer"})
# HTML5 void elements: they never have a matching end tag, so Python's
# html.parser calls handle_starttag() for an ordinary void element and calls
# both callbacks for a self-closing spelling ("<img ... />"). Neither form
# represents a nesting level.
# The depth counters below assume every handle_starttag() is eventually
# balanced by a handle_endtag() -- without this list, an ordinary
# "<img>"/"<br>"/"<meta>" with no trailing slash inside <nav>/<article>/etc.
# would permanently inflate that region's depth counter, either leaking
# unrelated trailing page content into the extracted body or (if it
# happens inside an ignored region such as <nav>) silently discarding the
# real article that follows it in the document.
_VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)
_TITLE_META_NAMES = frozenset({"og:title", "twitter:title"})
_DATE_META_NAMES = frozenset(
    {"article:published_time", "datepublished", "date", "publish-date", "publish_date"}
)
_HUMAN_DATE = re.compile(
    r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
    r"(?:uary|ruary|ch|il|e|y|ust|tember|ober|ember)?\s+\d{1,2},\s+\d{4}\b"
)


class ArticleParseError(Exception):
    """The response is not a usable article page."""


@dataclass(frozen=True, slots=True)
class ParsedArticle:
    """Article fields required by the ingestion persistence boundary."""

    title: str
    body: str
    published_at: datetime
    canonical_url: str | None
    authors: tuple[str, ...]
    tags: tuple[str, ...]


def _attrs(values: list[tuple[str, str | None]]) -> dict[str, str]:
    return {name.lower(): value or "" for name, value in values}


def _clean_text(parts: list[str]) -> str:
    return " ".join(" ".join(parts).split())


class _ArticleHtmlParser(HTMLParser):  # pylint: disable=too-many-instance-attributes
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta_title: str | None = None
        self.meta_dates: list[str] = []
        self.canonical_url: str | None = None
        self.authors: list[str] = []
        self.tags: list[str] = []
        self.time_dates: list[str] = []
        self.title_parts: list[str] = []
        self.h1_parts: list[str] = []
        self.article_parts: list[str] = []
        self.main_parts: list[str] = []
        self.all_parts: list[str] = []
        self.json_ld_parts: list[str] = []
        self._ignored_depth = 0
        self._article_depth = 0
        self._main_depth = 0
        self._title_depth = 0
        self._h1_depth = 0
        self._json_ld_depth = 0

    def handle_starttag(  # pylint: disable=too-many-branches
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        tag = tag.lower()
        values = _attrs(attrs)
        if tag == "meta":
            name = (values.get("property") or values.get("name") or "").lower()
            content = values.get("content", "").strip()
            if name in _TITLE_META_NAMES and content and self.meta_title is None:
                self.meta_title = content
            elif name in _DATE_META_NAMES and content:
                self.meta_dates.append(content)
            elif name == "author" and content:
                self.authors.append(content)
            elif name in {"article:tag", "keywords"} and content:
                self.tags.extend(part.strip() for part in content.split(","))
        elif tag == "link" and "canonical" in values.get("rel", "").lower().split():
            href = values.get("href", "").strip()
            if href and self.canonical_url is None:
                self.canonical_url = href
        elif tag == "time":
            date_value = values.get("datetime", "").strip()
            if date_value:
                self.time_dates.append(date_value)

        if tag in _VOID_TAGS:
            # No matching end tag will ever arrive for this one (see
            # _VOID_TAGS's comment) -- never treat it as opening a new
            # nesting level in any of the depth counters below.
            return

        if tag == "script" and values.get("type", "").lower() == "application/ld+json":
            self._json_ld_depth = 1
            return
        if self._json_ld_depth:
            self._json_ld_depth += 1
            return

        if self._ignored_depth or tag in _IGNORED_TAGS:
            self._ignored_depth += 1
            return
        if self._article_depth or tag == "article":
            self._article_depth += 1
        if self._main_depth or tag == "main":
            self._main_depth += 1
        if self._title_depth or tag == "title":
            self._title_depth += 1
        if self._h1_depth or tag == "h1":
            self._h1_depth += 1

    def handle_endtag(self, tag: str) -> None:
        # HTMLParser's default handle_startendtag() dispatches both callbacks.
        # The start callback deliberately did not increment for a void element,
        # so its synthetic end callback must not decrement the parent region.
        if tag.lower() in _VOID_TAGS:
            return
        if self._json_ld_depth:
            self._json_ld_depth -= 1
            return
        if self._ignored_depth:
            self._ignored_depth -= 1
            return
        if self._article_depth:
            self._article_depth -= 1
        if self._main_depth:
            self._main_depth -= 1
        if self._title_depth:
            self._title_depth -= 1
        if self._h1_depth:
            self._h1_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._json_ld_depth:
            self.json_ld_parts.append(data)
            return
        if self._ignored_depth or not data.strip():
            return
        self.all_parts.append(data)
        if self._article_depth:
            self.article_parts.append(data)
        if self._main_depth:
            self.main_parts.append(data)
        if self._title_depth:
            self.title_parts.append(data)
        if self._h1_depth:
            self.h1_parts.append(data)


def _json_ld_values(  # pylint: disable=too-many-nested-blocks
    parser: _ArticleHtmlParser,
) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {"headline": [], "datePublished": [], "author": []}
    for raw in parser.json_ld_parts:
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        stack: list[Any] = [payload]
        while stack:
            value = stack.pop()
            if isinstance(value, dict):
                for key, child in value.items():
                    if key in {"headline", "datePublished"} and isinstance(child, str):
                        result[key].append(child)
                    elif key == "author":
                        if isinstance(child, str):
                            result[key].append(child)
                        elif isinstance(child, dict) and isinstance(child.get("name"), str):
                            result[key].append(child["name"])
                    stack.append(child)
            elif isinstance(value, list):
                stack.extend(value)
    return result


def _parse_datetime(values: list[str], visible_text: str) -> datetime:
    for raw in values:
        candidate = raw.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    # Some official pages render their publication date as visible text
    # beside the headline without a <time> element or date metadata. The
    # first visible date is the deterministic fallback; structured metadata
    # above always takes precedence.
    match = _HUMAN_DATE.search(visible_text)
    if match:
        for date_format in ("%B %d, %Y", "%b %d, %Y"):
            try:
                return datetime.strptime(match.group(), date_format).replace(tzinfo=UTC)
            except ValueError:
                continue
    raise ArticleParseError("article has no parseable publication timestamp")


def parse_article(body: bytes) -> ParsedArticle:
    """Parse one HTML response, preferring article/main prose over page chrome."""
    parser = _ArticleHtmlParser()
    try:
        parser.feed(body.decode("utf-8-sig", errors="replace"))
        parser.close()
    except (ValueError, TypeError) as exc:
        raise ArticleParseError("malformed HTML") from exc

    json_ld = _json_ld_values(parser)
    title = (
        parser.meta_title
        or _clean_text(parser.h1_parts)
        or (json_ld["headline"][0] if json_ld["headline"] else None)
        or _clean_text(parser.title_parts)
    )
    if not title:
        raise ArticleParseError("article has no title")

    candidates = (
        _clean_text(parser.article_parts),
        _clean_text(parser.main_parts),
        _clean_text(parser.all_parts),
    )
    article_body = next((candidate for candidate in candidates if len(candidate) >= 80), "")
    if not article_body:
        raise ArticleParseError("article has no substantive body")

    published_at = _parse_datetime(
        [*parser.meta_dates, *parser.time_dates, *json_ld["datePublished"]],
        _clean_text(parser.all_parts),
    )
    authors = tuple([*parser.authors, *json_ld["author"]])
    return ParsedArticle(
        title=title.strip(),
        body=article_body,
        published_at=published_at,
        canonical_url=parser.canonical_url,
        authors=authors,
        tags=tuple(parser.tags),
    )
