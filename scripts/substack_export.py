#!/usr/bin/env python3
"""Export Quartz article posts to a Substack-importable RSS 2.0 feed.

The exporter walks the Quartz ``content/`` tree, selects the public article
posts (section landing pages, the profile page, the FINMA202 reviewer, and
anything filed under an ``assets/`` directory are excluded), converts each post
from Markdown to HTML, rewrites every relative image and link URL to an
absolute ``https://kvallespin.github.io`` URL, and emits:

* an RSS 2.0 feed whose ``<content:encoded>`` bodies carry the full article
  HTML inside CDATA, and
* a JSON manifest recording the source path, canonical URL, SHA-256, and date
  of every exported post.

Both outputs are deterministic: running the exporter twice over an unchanged
content tree produces byte-identical files. Nothing derived from wall-clock
time is written.

Usage::

    python3 scripts/substack_export.py \
        --content-dir content \
        --output dist/substack/substack-feed.xml \
        --manifest dist/substack/substack-manifest.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import posixpath
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence
from urllib.parse import unquote, urlsplit
from xml.sax.saxutils import escape as xml_escape

import markdown
import yaml

__all__ = [
    "ExportError",
    "Post",
    "DEFAULT_BASE_URL",
    "SECTIONS",
    "EXCLUDED_RELATIVE_PATHS",
    "slugify",
    "split_frontmatter",
    "strip_leading_h1",
    "markdown_to_html",
    "is_absolute_url",
    "absolutise_url",
    "rewrite_urls",
    "iter_post_paths",
    "build_post",
    "build_posts",
    "render_feed",
    "build_manifest",
    "export",
    "main",
]

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

DEFAULT_BASE_URL = "https://kvallespin.github.io"

#: Sections whose Markdown files are treated as publishable articles.
SECTIONS: tuple[str, ...] = (
    "data-science",
    "design",
    "engineering",
    "finance",
    "projects",
)

#: Paths (relative to the content directory, POSIX separators) never exported.
EXCLUDED_RELATIVE_PATHS: frozenset[str] = frozenset(
    {
        "index.md",
        "about-me/profile.md",
        "projects/fm2-reviewer.md",
    }
)

#: Directory name whose Markdown files are attachments, not articles.
ASSETS_DIR_NAME = "assets"

FEED_TITLE = "Ken Vallespin"
FEED_DESCRIPTION = (
    "Public notes on engineering, finance, data science, design, and projects."
)
FEED_LANGUAGE = "en-us"
FEED_AUTHOR = "Kenneth Vallespin"
GENERATOR = "scripts/substack_export.py"
MANIFEST_SCHEMA_VERSION = 1

MARKDOWN_EXTENSIONS = ("tables", "fenced_code", "sane_lists", "attr_list")

IMAGE_SUFFIXES = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".avif", ".bmp", ".ico"}
)

#: URL schemes and forms that are already absolute or non-locatable.
_ABSOLUTE_URL_RE = re.compile(r"^(?:[a-zA-Z][a-zA-Z0-9+.-]*:|//)")

#: Matches a whole start tag so attribute rewriting never touches prose.
#: Markdown escapes ``<`` inside code spans and fences, so fenced examples that
#: contain ``src="..."`` are left alone.
_TAG_RE = re.compile(r"<[a-zA-Z][^>]*>")

#: Matches a ``src=`` or ``href=`` attribute inside an already-matched tag.
_ATTR_RE = re.compile(
    r"""(?P<prefix>\b(?:src|href)\s*=\s*)(?P<quote>["'])(?P<url>[^"']*)(?P=quote)"""
)

_LEADING_H1_RE = re.compile(r"^\s*#[ \t]+(?P<text>.+?)[ \t]*#*[ \t]*$")

#: A leading HTML ``<h1>`` optionally preceded by comments/whitespace.
_LEADING_HTML_H1_RE = re.compile(
    r"^(?P<lead>(?:\s|<!--.*?-->)*)(?P<h1><h1\b[^>]*>(?P<text>.*?)</h1>)",
    re.DOTALL | re.IGNORECASE,
)

_TAG_STRIP_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")
_SLUG_SEPARATOR_RE = re.compile(r"[\s_]+")
_SLUG_INVALID_RE = re.compile(r"[^a-z0-9-]+")
_SLUG_COLLAPSE_RE = re.compile(r"-{2,}")

#: Typographic characters normalised before comparing a heading to a title.
_QUOTE_TRANSLATION = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "‚": "'",
        "“": '"',
        "”": '"',
        "–": "-",
        "—": "-",
        " ": " ",
    }
)


class ExportError(Exception):
    """Raised when the content tree cannot be exported as requested."""


@dataclass(frozen=True)
class Post:
    """One exported article."""

    source_path: str
    section: str
    slug: str
    title: str
    description: str
    created: dt.date
    canonical_url: str
    html: str
    source_sha256: str
    images: tuple[str, ...] = field(default=())

    @property
    def html_sha256(self) -> str:
        return hashlib.sha256(self.html.encode("utf-8")).hexdigest()

    @property
    def date(self) -> str:
        return self.created.isoformat()

    @property
    def pub_date(self) -> str:
        """RFC 2822 timestamp pinned to midnight GMT, so it never drifts."""
        stamp = dt.datetime.combine(self.created, dt.time(0, 0, 0))
        weekday = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")[stamp.weekday()]
        month = (
            "Jan", "Feb", "Mar", "Apr", "May", "Jun",
            "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
        )[stamp.month - 1]
        return (
            f"{weekday}, {stamp.day:02d} {month} {stamp.year:04d} "
            f"{stamp.hour:02d}:{stamp.minute:02d}:{stamp.second:02d} GMT"
        )


# --------------------------------------------------------------------------
# Frontmatter and slugs
# --------------------------------------------------------------------------


def slugify(text: str) -> str:
    """Return the Quartz-compatible slug for a file stem or title."""
    slug = text.strip().lower()
    slug = slug.translate(_QUOTE_TRANSLATION)
    slug = _SLUG_SEPARATOR_RE.sub("-", slug)
    slug = _SLUG_INVALID_RE.sub("", slug)
    slug = _SLUG_COLLAPSE_RE.sub("-", slug)
    return slug.strip("-")


def split_frontmatter(text: str) -> tuple[dict, str]:
    """Split ``text`` into its YAML frontmatter mapping and Markdown body.

    The frontmatter is parsed with :func:`yaml.safe_load`, so tags such as
    ``!!python/object/apply`` are rejected instead of being constructed.
    """
    if not text.startswith("---"):
        raise ExportError("missing YAML frontmatter")

    match = re.match(r"^---[ \t]*\r?\n(?P<meta>.*?)\r?\n---[ \t]*(?:\r?\n|$)", text, re.DOTALL)
    if match is None:
        raise ExportError("unterminated YAML frontmatter")

    try:
        meta = yaml.safe_load(match.group("meta"))
    except yaml.YAMLError as exc:  # pragma: no cover - message varies by input
        raise ExportError(f"invalid YAML frontmatter: {exc}") from exc

    if meta is None:
        meta = {}
    if not isinstance(meta, dict):
        raise ExportError("YAML frontmatter must be a mapping")

    return meta, text[match.end():]


def _coerce_date(value: object) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        try:
            return dt.date.fromisoformat(value.strip()[:10])
        except ValueError as exc:
            raise ExportError(f"unparseable 'created' date: {value!r}") from exc
    raise ExportError(f"unsupported 'created' value: {value!r}")


def _normalise_heading(text: str) -> str:
    text = _TAG_STRIP_RE.sub("", text)
    text = text.translate(_QUOTE_TRANSLATION)
    text = _WHITESPACE_RE.sub(" ", text)
    return text.strip().casefold()


def strip_leading_h1(body: str, title: str) -> str:
    """Drop a leading H1 when it repeats ``title``; leave every other heading.

    Substack renders the post title itself, so a duplicated leading H1 shows up
    twice. Only the *first* heading is considered, and only when its text
    matches the frontmatter title.
    """
    wanted = _normalise_heading(title)
    if not wanted:
        return body

    stripped = body.lstrip("\n")
    leading_newlines = len(body) - len(stripped)

    lines = stripped.split("\n")
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        match = _LEADING_H1_RE.match(line)
        if match and _normalise_heading(match.group("text")) == wanted:
            remainder = lines[:index] + lines[index + 1:]
            return ("\n" * leading_newlines) + "\n".join(remainder).lstrip("\n")
        break

    html_match = _LEADING_HTML_H1_RE.match(stripped)
    if html_match and _normalise_heading(html_match.group("text")) == wanted:
        remainder = stripped[: html_match.start("h1")] + stripped[html_match.end("h1"):]
        return ("\n" * leading_newlines) + remainder

    return body


def markdown_to_html(text: str) -> str:
    """Convert Markdown to HTML, leaving inline HTML blocks untouched."""
    converter = markdown.Markdown(extensions=list(MARKDOWN_EXTENSIONS))
    return converter.convert(text)


# --------------------------------------------------------------------------
# URL rewriting
# --------------------------------------------------------------------------


def is_absolute_url(url: str) -> bool:
    """True when ``url`` needs no rewriting (scheme, protocol-relative, or anchor)."""
    if not url:
        return True
    if url.startswith("#"):
        return True
    return bool(_ABSOLUTE_URL_RE.match(url))


def absolutise_url(url: str, doc_dir: str, base_url: str) -> tuple[str, str | None]:
    """Resolve ``url`` against ``doc_dir`` and return ``(absolute_url, local_path)``.

    ``doc_dir`` is the POSIX directory of the source document relative to the
    content root (``""`` for the root itself). ``local_path`` is the content
    relative filesystem path the URL points at, or ``None`` when the URL is
    external and therefore not backed by a file in the content tree.
    """
    if is_absolute_url(url):
        return url, None

    parts = urlsplit(url)
    path = parts.path
    if not path:
        # Query-only or otherwise pathless reference; nothing to resolve.
        return url, None

    if path.startswith("/"):
        joined = path.lstrip("/")
    else:
        joined = posixpath.join(doc_dir, path) if doc_dir else path

    trailing_slash = path.endswith("/")
    normalised = posixpath.normpath(joined)
    if normalised == ".":
        normalised = ""
    if normalised.startswith(".."):
        raise ExportError(f"link escapes the content directory: {url!r}")
    if trailing_slash and normalised:
        normalised += "/"

    absolute = f"{base_url.rstrip('/')}/{normalised}"
    if parts.query:
        absolute += f"?{parts.query}"
    if parts.fragment:
        absolute += f"#{parts.fragment}"

    local_path = unquote(normalised.rstrip("/")) or None
    return absolute, local_path


def _looks_like_image(local_path: str) -> bool:
    return Path(local_path).suffix.lower() in IMAGE_SUFFIXES


def rewrite_urls(html: str, doc_dir: str, base_url: str) -> tuple[str, list[str]]:
    """Rewrite every relative ``src``/``href`` in ``html`` to an absolute URL.

    Returns the rewritten HTML together with the content-relative paths of the
    local images it references, in first-appearance order.
    """
    images: list[str] = []
    seen: set[str] = set()

    def rewrite_tag(tag_match: re.Match[str]) -> str:
        tag = tag_match.group(0)
        is_img = re.match(r"<img\b", tag, re.IGNORECASE) is not None

        def rewrite_attr(attr_match: re.Match[str]) -> str:
            url = attr_match.group("url")
            absolute, local_path = absolutise_url(url, doc_dir, base_url)
            if local_path is not None and (is_img or _looks_like_image(local_path)):
                if local_path not in seen:
                    seen.add(local_path)
                    images.append(local_path)
            quote = attr_match.group("quote")
            return f"{attr_match.group('prefix')}{quote}{absolute}{quote}"

        return _ATTR_RE.sub(rewrite_attr, tag)

    return _TAG_RE.sub(rewrite_tag, html), images


# --------------------------------------------------------------------------
# Discovery and post construction
# --------------------------------------------------------------------------


def iter_post_paths(content_dir: Path) -> list[Path]:
    """Return the article Markdown files under ``content_dir``, sorted."""
    content_dir = Path(content_dir)
    if not content_dir.is_dir():
        raise ExportError(f"content directory not found: {content_dir}")

    selected: list[Path] = []
    for path in sorted(content_dir.rglob("*.md")):
        relative = path.relative_to(content_dir)
        parts = relative.parts
        if ASSETS_DIR_NAME in parts[:-1]:
            continue
        if path.name == "index.md":
            continue
        if relative.as_posix() in EXCLUDED_RELATIVE_PATHS:
            continue
        if len(parts) < 2 or parts[0] not in SECTIONS:
            continue
        selected.append(path)
    return selected


def build_post(path: Path, content_dir: Path, base_url: str = DEFAULT_BASE_URL) -> Post:
    """Read one Markdown file and render it into a :class:`Post`."""
    content_dir = Path(content_dir)
    path = Path(path)
    relative = path.relative_to(content_dir)
    label = relative.as_posix()

    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:  # pragma: no cover - content is UTF-8
        raise ExportError(f"{label}: not valid UTF-8") from exc

    try:
        meta, body = split_frontmatter(text)
    except ExportError as exc:
        raise ExportError(f"{label}: {exc}") from exc

    title = meta.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ExportError(f"{label}: frontmatter is missing a non-empty 'title'")
    title = title.strip()

    if "created" not in meta or meta["created"] in (None, ""):
        raise ExportError(f"{label}: frontmatter is missing 'created'")
    try:
        created = _coerce_date(meta["created"])
    except ExportError as exc:
        raise ExportError(f"{label}: {exc}") from exc

    description = meta.get("description") or ""
    if not isinstance(description, str):
        raise ExportError(f"{label}: 'description' must be a string")
    description = description.strip()

    section = relative.parts[0]
    slug = slugify(path.stem)
    if not slug:
        raise ExportError(f"{label}: filename does not produce a usable slug")
    canonical_url = f"{base_url.rstrip('/')}/{section}/{slug}"

    body = strip_leading_h1(body, title)
    html = markdown_to_html(body)

    doc_dir = relative.parent.as_posix()
    if doc_dir == ".":
        doc_dir = ""
    try:
        html, images = rewrite_urls(html, doc_dir, base_url)
    except ExportError as exc:
        raise ExportError(f"{label}: {exc}") from exc

    missing = [image for image in images if not (content_dir / image).is_file()]
    if missing:
        listed = ", ".join(sorted(missing))
        raise ExportError(f"{label}: image(s) not found under {content_dir}: {listed}")

    return Post(
        # Recorded relative to the repository so the manifest is portable.
        source_path=f"{content_dir.name}/{label}",
        section=section,
        slug=slug,
        title=title,
        description=description,
        created=created,
        canonical_url=canonical_url,
        html=html,
        source_sha256=hashlib.sha256(raw).hexdigest(),
        images=tuple(images),
    )


def build_posts(content_dir: Path, base_url: str = DEFAULT_BASE_URL) -> list[Post]:
    """Build every exportable post, newest first, with a stable tie-break."""
    content_dir = Path(content_dir)
    posts = [build_post(path, content_dir, base_url) for path in iter_post_paths(content_dir)]
    posts.sort(key=lambda post: (-post.created.toordinal(), post.canonical_url))
    return posts


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def _cdata(text: str) -> str:
    """Wrap ``text`` in CDATA, splitting any embedded ``]]>`` terminator."""
    return "<![CDATA[" + text.replace("]]>", "]]]]><![CDATA[>") + "]]>"


def render_feed(posts: Sequence[Post], base_url: str = DEFAULT_BASE_URL) -> str:
    """Render an RSS 2.0 feed. The output depends only on ``posts``."""
    site = base_url.rstrip("/")
    lines: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0"'
        ' xmlns:content="http://purl.org/rss/1.0/modules/content/"'
        ' xmlns:dc="http://purl.org/dc/elements/1.1/"'
        ' xmlns:atom="http://www.w3.org/2005/Atom">',
        "  <channel>",
        f"    <title>{xml_escape(FEED_TITLE)}</title>",
        f"    <link>{xml_escape(site)}</link>",
        f"    <description>{xml_escape(FEED_DESCRIPTION)}</description>",
        f"    <language>{FEED_LANGUAGE}</language>",
        f"    <generator>{xml_escape(GENERATOR)}</generator>",
    ]

    for post in posts:
        lines.extend(
            [
                "    <item>",
                f"      <title>{xml_escape(post.title)}</title>",
                f"      <link>{xml_escape(post.canonical_url)}</link>",
                f'      <guid isPermaLink="true">{xml_escape(post.canonical_url)}</guid>',
                f"      <pubDate>{post.pub_date}</pubDate>",
                f"      <dc:creator>{_cdata(FEED_AUTHOR)}</dc:creator>",
                f"      <description>{_cdata(post.description)}</description>",
                f"      <content:encoded>{_cdata(post.html)}</content:encoded>",
                "    </item>",
            ]
        )

    lines.extend(["  </channel>", "</rss>", ""])
    return "\n".join(lines)


def build_manifest(
    posts: Sequence[Post], base_url: str, feed_xml: str
) -> dict:
    """Build the JSON-serialisable manifest describing the exported posts."""
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "generator": GENERATOR,
        "base_url": base_url.rstrip("/"),
        "post_count": len(posts),
        "feed_sha256": hashlib.sha256(feed_xml.encode("utf-8")).hexdigest(),
        "posts": [
            {
                "source_path": post.source_path,
                "canonical_url": post.canonical_url,
                "sha256": post.source_sha256,
                "date": post.date,
                "title": post.title,
                "description": post.description,
                "section": post.section,
                "slug": post.slug,
                "html_sha256": post.html_sha256,
                "image_count": len(post.images),
            }
            for post in posts
        ],
    }


def dump_manifest(manifest: dict) -> str:
    """Serialise the manifest deterministically."""
    return json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def export(
    content_dir: Path,
    output: Path,
    manifest_path: Path,
    base_url: str = DEFAULT_BASE_URL,
    dry_run: bool = False,
) -> tuple[list[Post], str, str]:
    """Build the feed and manifest, writing them unless ``dry_run`` is set."""
    posts = build_posts(content_dir, base_url)
    feed_xml = render_feed(posts, base_url)
    manifest_json = dump_manifest(build_manifest(posts, base_url, feed_xml))

    if not dry_run:
        for target, payload in ((output, feed_xml), (manifest_path, manifest_json)):
            target = Path(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(payload, encoding="utf-8", newline="\n")

    return posts, feed_xml, manifest_json


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def build_arg_parser() -> argparse.ArgumentParser:
    root = _repo_root()
    parser = argparse.ArgumentParser(
        prog="substack_export.py",
        description=(
            "Export Quartz article posts to a Substack-importable RSS 2.0 feed "
            "and a JSON manifest."
        ),
    )
    parser.add_argument(
        "--content-dir",
        type=Path,
        default=root / "content",
        help="Quartz content directory to export (default: %(default)s)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=root / "dist" / "substack" / "substack-feed.xml",
        help="path of the RSS feed to write (default: %(default)s)",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=root / "dist" / "substack" / "substack-manifest.json",
        help="path of the JSON manifest to write (default: %(default)s)",
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help="site origin used for absolute URLs (default: %(default)s)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate and report without writing any file",
    )
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_arg_parser().parse_args(list(argv) if argv is not None else None)

    try:
        posts, feed_xml, manifest_json = export(
            content_dir=args.content_dir,
            output=args.output,
            manifest_path=args.manifest,
            base_url=args.base_url,
            dry_run=args.dry_run,
        )
    except ExportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    mode = "dry run" if args.dry_run else "wrote"
    print(f"{mode}: {len(posts)} post(s) from {args.content_dir}")
    for post in posts:
        print(f"  {post.date}  {post.canonical_url}")
    print(f"  feed sha256:     {hashlib.sha256(feed_xml.encode('utf-8')).hexdigest()}")
    print(f"  manifest sha256: {hashlib.sha256(manifest_json.encode('utf-8')).hexdigest()}")
    if not args.dry_run:
        print(f"  feed:     {args.output}")
        print(f"  manifest: {args.manifest}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
