"""Safe RSS 2.0 parsing.

`defusedxml.ElementTree` (not stdlib `xml.etree`) because an RSS feed is
untrusted external content (AGENTS.md) and stdlib ElementTree is
documented as "not secure against maliciously constructed data" --
`defusedxml` blocks entity expansion, external entities, and DTD
retrieval.

Parsing is deliberately lenient about *entries*: a `<item>` with a
missing or empty field yields `None` for that field rather than an
error. Deciding whether an entry is usable (has a link, has a title, has
a parseable date) is `normalize.py`'s job, so a single bad entry is
recorded as an item failure while its siblings continue
(`docs/ARCHITECTURE.md`: "One source failure does not abort others").
Only a feed that will not parse at all raises `RssParseError` -- a
source-level failure.
"""

from __future__ import annotations

from dataclasses import dataclass

# Type-only import. Every actual parse goes through `defusedxml` below;
# the `Element` name is used solely for annotations, so the class itself
# poses no XML-attack surface here.
from xml.etree.ElementTree import Element  # nosec B405

from defusedxml.common import DefusedXmlException as _DefusedXmlException
from defusedxml.ElementTree import ParseError as _DefusedParseError
from defusedxml.ElementTree import fromstring as _defused_fromstring

# The `content:encoded` element's fully-qualified name.
_CONTENT_ENCODED = "{http://purl.org/rss/1.0/modules/content/}encoded"
# The Dublin Core `dc:creator` element -- some feeds use it instead of
# the plain RSS `<author>`.
_DC_CREATOR = "{http://purl.org/dc/elements/1.1/}creator"


class RssParseError(Exception):
    """The response body is not parseable as an RSS/XML document -- a
    source-level failure (the whole fetch is unusable), distinct from a
    single malformed `<item>`."""


@dataclass(frozen=True, slots=True)
class RssEntry:  # pylint: disable=too-many-instance-attributes
    """One `<item>` as read from the feed, before normalization. Every
    field is optional here; `normalize.py` enforces what a usable entry
    needs. `raw_index` is the item's 0-based position in the feed, used
    to identify it in a failure report when it has no usable guid/link."""

    raw_index: int
    title: str | None
    link: str | None
    description: str | None
    guid: str | None
    categories: tuple[str, ...]
    authors: tuple[str, ...]
    pub_date_raw: str | None


@dataclass(frozen=True, slots=True)
class RssFeed:
    """A parsed feed: the channel title (for diagnostics) and its
    entries in document order."""

    channel_title: str | None
    entries: tuple[RssEntry, ...]


def parse_rss(body: bytes) -> RssFeed:
    """Parse `body` as RSS 2.0. Raise `RssParseError` if it is not
    well-formed XML, triggers a blocked XML feature (entity/DTD/external
    reference), is not an `<rss>` document, or has no `<channel>`."""
    try:
        root = _defused_fromstring(body)
    except (_DefusedParseError, _DefusedXmlException, ValueError) as exc:
        raise RssParseError(f"could not parse feed as XML: {type(exc).__name__}") from exc

    if _localname(root.tag) != "rss":
        raise RssParseError(f"root element is <{_localname(root.tag)}>, expected <rss>")

    channel = root.find("channel")
    if channel is None:
        raise RssParseError("feed has no <channel> element")

    entries = tuple(_parse_entry(item, index) for index, item in enumerate(channel.findall("item")))
    return RssFeed(channel_title=_text(channel.find("title")), entries=entries)


def _parse_entry(item: Element, index: int) -> RssEntry:
    description = _text(item.find("description"))
    encoded = _text(item.find(_CONTENT_ENCODED))
    return RssEntry(
        raw_index=index,
        title=_text(item.find("title")),
        link=_text(item.find("link")),
        # Prefer the richer <content:encoded> when a feed provides it;
        # OpenAI's feed does not, so <description> is used.
        description=encoded if encoded is not None else description,
        guid=_text(item.find("guid")),
        categories=tuple(
            value for value in (_text(node) for node in item.findall("category")) if value
        ),
        # `<author>` and `<dc:creator>` in document order; normalization
        # (trim / NFC / dedupe) is `normalize.py`'s job.
        authors=tuple(
            value
            for value in (
                _text(node) for node in (*item.findall("author"), *item.findall(_DC_CREATOR))
            )
            if value
        ),
        pub_date_raw=_text(item.find("pubDate")),
    )


def _text(node: Element | None) -> str | None:
    """The stripped text content of `node`, or `None` when the element is
    absent or empty. `ElementTree` already resolves `<![CDATA[...]]>` into
    plain `.text`, so no special handling is needed."""
    if node is None or node.text is None:
        return None
    stripped = node.text.strip()
    return stripped or None


def _localname(tag: str) -> str:
    """`"{ns}rss"` -> `"rss"`; a plain tag is returned unchanged."""
    return tag.rsplit("}", 1)[-1]
