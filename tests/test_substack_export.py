"""Tests for scripts/substack_export.py.

The suite mixes synthetic fixtures (for the edge cases the real content tree
does not contain) with integration checks against the repository's actual
``content/`` directory, which must export exactly the 13 article posts.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import substack_export as sx  # noqa: E402

REAL_CONTENT = Path(os.environ.get("SUBSTACK_CONTENT_DIR", REPO_ROOT.parent / "kvallespin.github.io" / "content")).resolve()

EXPECTED_SOURCE_PATHS = [
    "content/data-science/Designing a writing assistant that almost actually sounds like me.md",
    "content/data-science/a-hundred-million-tokens-later.md",
    "content/data-science/ai-slop-ste.md",
    "content/data-science/redesigning-the-redesign-nobody-asked-for.md",
    "content/data-science/septimana-mirabilis.md",
    "content/design/kv-design-system.md",
    "content/engineering/beyond-copy-paste-computer-engineering.md",
    "content/engineering/unlocking-the-apec-engineer.md",
    "content/engineering/unlocking-the-pe.md",
    "content/finance/an-engineers-valuation-of-the-mynt-gcash-ipo.md",
    "content/finance/ten-weeks-inside-aviation-finance.md",
    "content/finance/the-worlds-largest-lbo-why-pif-wanted-electronic-arts.md",
    "content/projects/the-philippines-under-pax-silica-a-10-year-simulation.md",
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def write_post(
    content_dir: Path,
    relative: str,
    *,
    frontmatter: str = "title: Sample post\ndescription: A sample.\ncreated: 2026-01-02",
    body: str = "Body text.\n",
) -> Path:
    """Write a Markdown file with frontmatter and return its path."""
    path = content_dir / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}\n---\n\n{body}", encoding="utf-8")
    return path


def write_image(content_dir: Path, relative: str) -> Path:
    path = content_dir / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n")
    return path


@pytest.fixture()
def content_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "content"
    directory.mkdir()
    return directory


@pytest.fixture(scope="module")
def real_posts() -> list[sx.Post]:
    return sx.build_posts(REAL_CONTENT)


# ---------------------------------------------------------------------------
# slugify
# ---------------------------------------------------------------------------


class TestSlugify:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("unlocking-the-pe", "unlocking-the-pe"),
            (
                "Designing a writing assistant that almost actually sounds like me",
                "designing-a-writing-assistant-that-almost-actually-sounds-like-me",
            ),
            ("The world’s largest LBO: why PIF wanted EA", "the-worlds-largest-lbo-why-pif-wanted-ea"),
            ("Mixed   Whitespace\tTabs", "mixed-whitespace-tabs"),
            ("snake_case_name", "snake-case-name"),
            ("  Leading and trailing  ", "leading-and-trailing"),
            ("Punctuation!!! Everywhere???", "punctuation-everywhere"),
            ("already--hyphenated", "already-hyphenated"),
            ("100% pure", "100-pure"),
        ],
    )
    def test_slugify(self, raw: str, expected: str) -> None:
        assert sx.slugify(raw) == expected

    def test_slugify_of_unusable_stem_is_empty(self) -> None:
        assert sx.slugify("!!!") == ""


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------


class TestSplitFrontmatter:
    def test_parses_mapping_and_body(self) -> None:
        meta, body = sx.split_frontmatter("---\ntitle: Hi\ncreated: 2026-01-02\n---\n\nBody\n")
        assert meta == {"title": "Hi", "created": dt.date(2026, 1, 2)}
        assert body == "\nBody\n"

    def test_created_is_parsed_as_a_date_object(self) -> None:
        meta, _ = sx.split_frontmatter("---\ncreated: 2026-06-26\n---\nx")
        assert isinstance(meta["created"], dt.date)

    def test_empty_frontmatter_yields_empty_mapping(self) -> None:
        meta, body = sx.split_frontmatter("---\n\n---\nBody")
        assert meta == {}
        assert body == "Body"

    def test_missing_frontmatter_raises(self) -> None:
        with pytest.raises(sx.ExportError, match="missing YAML frontmatter"):
            sx.split_frontmatter("# Just a heading\n")

    def test_unterminated_frontmatter_raises(self) -> None:
        with pytest.raises(sx.ExportError, match="unterminated"):
            sx.split_frontmatter("---\ntitle: Hi\n")

    def test_scalar_frontmatter_raises(self) -> None:
        with pytest.raises(sx.ExportError, match="must be a mapping"):
            sx.split_frontmatter("---\njust a string\n---\nBody")

    def test_yaml_is_parsed_safely(self) -> None:
        """An unsafe tag must be rejected, never constructed."""
        text = "---\ntitle: !!python/object/apply:os.system ['echo pwned']\n---\nBody"
        with pytest.raises(sx.ExportError, match="invalid YAML frontmatter"):
            sx.split_frontmatter(text)

    def test_malformed_yaml_raises_export_error(self) -> None:
        with pytest.raises(sx.ExportError, match="invalid YAML frontmatter"):
            sx.split_frontmatter("---\ntitle: [unclosed\n---\nBody")


class TestCoerceDate:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (dt.date(2026, 9, 1), dt.date(2026, 9, 1)),
            (dt.datetime(2026, 9, 1, 13, 45), dt.date(2026, 9, 1)),
            ("2026-09-01", dt.date(2026, 9, 1)),
            ("2026-09-01T10:00:00", dt.date(2026, 9, 1)),
            ("  2026-09-01  ", dt.date(2026, 9, 1)),
        ],
    )
    def test_accepted_forms(self, value: object, expected: dt.date) -> None:
        assert sx._coerce_date(value) == expected

    @pytest.mark.parametrize("value", ["not-a-date", "01/09/2026", 20260901, None])
    def test_rejected_forms(self, value: object) -> None:
        with pytest.raises(sx.ExportError):
            sx._coerce_date(value)


# ---------------------------------------------------------------------------
# Leading H1 handling
# ---------------------------------------------------------------------------


class TestStripLeadingH1:
    def test_strips_exact_match(self) -> None:
        body = "\n# Ten weeks inside aviation finance\n\nIntro.\n"
        assert sx.strip_leading_h1(body, "Ten weeks inside aviation finance").strip() == "Intro."

    def test_match_is_case_insensitive(self) -> None:
        body = "\n# Redesigning the Redesign Nobody Asked For\n\nIntro.\n"
        out = sx.strip_leading_h1(body, "Redesigning the redesign nobody asked for")
        assert "Redesigning the Redesign" not in out
        assert "Intro." in out

    def test_match_ignores_curly_quote_differences(self) -> None:
        body = "\n# The world's largest LBO\n\nIntro.\n"
        out = sx.strip_leading_h1(body, "The world’s largest LBO")
        assert "largest LBO" not in out

    def test_non_matching_h1_is_kept(self) -> None:
        body = "\n# A completely different heading\n\nIntro.\n"
        assert sx.strip_leading_h1(body, "Some title") == body

    def test_h2_is_never_stripped(self) -> None:
        body = "\n## Some title\n\nIntro.\n"
        assert sx.strip_leading_h1(body, "Some title") == body

    def test_later_h1_is_kept(self) -> None:
        body = "\nIntro paragraph.\n\n# Some title\n\nMore.\n"
        assert sx.strip_leading_h1(body, "Some title") == body

    def test_only_the_first_h1_is_stripped(self) -> None:
        body = "\n# Some title\n\nText.\n\n# Some title\n\nMore.\n"
        out = sx.strip_leading_h1(body, "Some title")
        assert out.count("# Some title") == 1

    def test_strips_leading_html_h1(self) -> None:
        body = "<h1>Septimana mirabilis</h1><p>Intro.</p>"
        out = sx.strip_leading_h1(body, "Septimana mirabilis")
        assert out == "<p>Intro.</p>"

    def test_strips_html_h1_behind_a_comment(self) -> None:
        body = "<!-- prettier-ignore-start --><h1>Title here</h1><p>Intro.</p>"
        out = sx.strip_leading_h1(body, "Title here")
        assert out == "<!-- prettier-ignore-start --><p>Intro.</p>"

    def test_non_matching_html_h1_is_kept(self) -> None:
        body = "<h1>Different</h1><p>Intro.</p>"
        assert sx.strip_leading_h1(body, "Title here") == body

    def test_closing_hashes_are_tolerated(self) -> None:
        body = "\n# Some title #\n\nIntro.\n"
        assert "Some title" not in sx.strip_leading_h1(body, "Some title")

    def test_empty_title_is_a_no_op(self) -> None:
        body = "\n# Anything\n"
        assert sx.strip_leading_h1(body, "") == body


# ---------------------------------------------------------------------------
# Markdown conversion
# ---------------------------------------------------------------------------


class TestMarkdownToHtml:
    def test_paragraphs_and_headings(self) -> None:
        html = sx.markdown_to_html("## Section\n\nHello.\n")
        assert "<h2>Section</h2>" in html
        assert "<p>Hello.</p>" in html

    def test_tables_are_rendered(self) -> None:
        html = sx.markdown_to_html("| A | B |\n|---|---|\n| 1 | 2 |\n")
        assert "<table>" in html and "<td>1</td>" in html

    def test_inline_html_is_preserved(self) -> None:
        source = "<p class='caption'><em>Caption</em></p>\n\nText.\n"
        html = sx.markdown_to_html(source)
        assert "<p class='caption'><em>Caption</em></p>" in html

    def test_inline_html_attributes_survive_verbatim(self) -> None:
        source = '<div style="background:#F2F2F2;padding:24px;">Boxed</div>\n'
        assert 'style="background:#F2F2F2;padding:24px;"' in sx.markdown_to_html(source)

    def test_code_fence_html_is_escaped(self) -> None:
        html = sx.markdown_to_html('```\n<img src="relative.png">\n```\n')
        assert "&lt;img" in html
        assert '<img src="relative.png">' not in html

    def test_conversion_is_repeatable(self) -> None:
        source = "# A\n\ntext *emphasis*\n"
        assert sx.markdown_to_html(source) == sx.markdown_to_html(source)


# ---------------------------------------------------------------------------
# URL handling
# ---------------------------------------------------------------------------


class TestIsAbsoluteUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com/x.png",
            "http://example.com",
            "//cdn.example.com/x.png",
            "mailto:someone@example.com",
            "data:image/png;base64,AAAA",
            "#section",
            "",
        ],
    )
    def test_absolute_or_non_locatable(self, url: str) -> None:
        assert sx.is_absolute_url(url) is True

    @pytest.mark.parametrize("url", ["assets/x.png", "../assets/x.png", "/assets/x.png", "./x"])
    def test_relative(self, url: str) -> None:
        assert sx.is_absolute_url(url) is False


class TestAbsolutiseUrl:
    BASE = "https://kvallespin.github.io"

    @pytest.mark.parametrize(
        ("url", "doc_dir", "expected_url", "expected_local"),
        [
            (
                "assets/pe-journey/x.jpg",
                "engineering",
                "https://kvallespin.github.io/engineering/assets/pe-journey/x.jpg",
                "engineering/assets/pe-journey/x.jpg",
            ),
            (
                "../assets/ai-slop-ste/banner.png",
                "data-science",
                "https://kvallespin.github.io/assets/ai-slop-ste/banner.png",
                "assets/ai-slop-ste/banner.png",
            ),
            (
                "/assets/ken-profile.jpg",
                "about-me",
                "https://kvallespin.github.io/assets/ken-profile.jpg",
                "assets/ken-profile.jpg",
            ),
            (
                "./nested/./x.png",
                "design",
                "https://kvallespin.github.io/design/nested/x.png",
                "design/nested/x.png",
            ),
            (
                "sibling-post",
                "finance",
                "https://kvallespin.github.io/finance/sibling-post",
                "finance/sibling-post",
            ),
        ],
    )
    def test_resolution(self, url, doc_dir, expected_url, expected_local) -> None:
        assert sx.absolutise_url(url, doc_dir, self.BASE) == (expected_url, expected_local)

    def test_external_url_is_untouched(self) -> None:
        assert sx.absolutise_url("https://example.com/a", "design", self.BASE) == (
            "https://example.com/a",
            None,
        )

    def test_fragment_only_url_is_untouched(self) -> None:
        assert sx.absolutise_url("#refs", "design", self.BASE) == ("#refs", None)

    def test_query_and_fragment_are_preserved(self) -> None:
        url, _ = sx.absolutise_url("page?a=1&b=2#frag", "design", self.BASE)
        assert url == "https://kvallespin.github.io/design/page?a=1&b=2#frag"

    def test_trailing_slash_is_preserved(self) -> None:
        url, local = sx.absolutise_url("../design/", "finance", self.BASE)
        assert url == "https://kvallespin.github.io/design/"
        assert local == "design"

    def test_percent_encoding_is_kept_in_url_and_decoded_for_disk(self) -> None:
        url, local = sx.absolutise_url("assets/a%20b.png", "design", self.BASE)
        assert url == "https://kvallespin.github.io/design/assets/a%20b.png"
        assert local == "design/assets/a b.png"

    def test_root_relative_document(self) -> None:
        url, local = sx.absolutise_url("assets/x.png", "", self.BASE)
        assert url == "https://kvallespin.github.io/assets/x.png"
        assert local == "assets/x.png"

    def test_base_url_trailing_slash_does_not_double(self) -> None:
        url, _ = sx.absolutise_url("assets/x.png", "design", "https://example.com/")
        assert url == "https://example.com/design/assets/x.png"

    def test_escaping_the_content_root_raises(self) -> None:
        with pytest.raises(sx.ExportError, match="escapes the content directory"):
            sx.absolutise_url("../../secret.png", "design", self.BASE)


class TestRewriteUrls:
    BASE = "https://kvallespin.github.io"

    def test_rewrites_img_and_anchor(self) -> None:
        html = '<p><img src="assets/a.png"><a href="other">link</a></p>'
        out, images = sx.rewrite_urls(html, "design", self.BASE)
        assert 'src="https://kvallespin.github.io/design/assets/a.png"' in out
        assert 'href="https://kvallespin.github.io/design/other"' in out
        assert images == ["design/assets/a.png"]

    def test_rewrites_iframe_src(self) -> None:
        html = '<iframe src="assets/deck.pdf" height="760"></iframe>'
        out, images = sx.rewrite_urls(html, "finance", self.BASE)
        assert 'src="https://kvallespin.github.io/finance/assets/deck.pdf"' in out
        assert images == []  # a PDF is not an image

    def test_single_quoted_attributes_keep_their_quote_style(self) -> None:
        html = "<img src='assets/a.png'>"
        out, _ = sx.rewrite_urls(html, "design", self.BASE)
        assert out == "<img src='https://kvallespin.github.io/design/assets/a.png'>"

    def test_external_urls_are_untouched(self) -> None:
        html = '<a href="https://example.com/x">x</a><img src="//cdn/x.png">'
        out, images = sx.rewrite_urls(html, "design", self.BASE)
        assert out == html
        assert images == []

    def test_anchor_to_a_local_image_is_collected(self) -> None:
        html = '<a href="assets/full.jpg">full size</a>'
        _, images = sx.rewrite_urls(html, "design", self.BASE)
        assert images == ["design/assets/full.jpg"]

    def test_escaped_html_in_code_blocks_is_not_rewritten(self) -> None:
        html = '<pre><code>&lt;img src="assets/a.png"&gt;</code></pre>'
        out, images = sx.rewrite_urls(html, "design", self.BASE)
        assert out == html
        assert images == []

    def test_images_are_deduplicated_in_first_appearance_order(self) -> None:
        html = '<img src="assets/b.png"><img src="assets/a.png"><img src="assets/b.png">'
        _, images = sx.rewrite_urls(html, "design", self.BASE)
        assert images == ["design/assets/b.png", "design/assets/a.png"]

    def test_other_attributes_are_left_alone(self) -> None:
        html = '<img src="assets/a.png" alt="assets/a.png" title="see assets/a.png">'
        out, _ = sx.rewrite_urls(html, "design", self.BASE)
        assert 'alt="assets/a.png"' in out
        assert 'title="see assets/a.png"' in out


# ---------------------------------------------------------------------------
# Discovery / selection
# ---------------------------------------------------------------------------


class TestIterPostPaths:
    def test_selects_only_section_articles(self, content_dir: Path) -> None:
        write_post(content_dir, "engineering/real-post.md")
        write_post(content_dir, "finance/another.md")
        write_post(content_dir, "index.md")
        write_post(content_dir, "engineering/index.md")
        write_post(content_dir, "about-me/index.md")
        write_post(content_dir, "about-me/profile.md")
        write_post(content_dir, "projects/fm2-reviewer.md")
        write_post(content_dir, "projects/assets/seed.md")
        write_post(content_dir, "engineering/assets/nested/note.md")
        write_post(content_dir, "scratch/not-a-section.md")

        names = [p.relative_to(content_dir).as_posix() for p in sx.iter_post_paths(content_dir)]
        assert names == ["engineering/real-post.md", "finance/another.md"]

    def test_result_is_sorted(self, content_dir: Path) -> None:
        for name in ("zeta", "alpha", "mid"):
            write_post(content_dir, f"design/{name}.md")
        names = [p.stem for p in sx.iter_post_paths(content_dir)]
        assert names == sorted(names)

    def test_missing_content_dir_raises(self, tmp_path: Path) -> None:
        with pytest.raises(sx.ExportError, match="content directory not found"):
            sx.iter_post_paths(tmp_path / "nope")

    def test_a_file_named_assets_md_is_still_exported(self, content_dir: Path) -> None:
        """Only files *inside* an assets directory are skipped."""
        write_post(content_dir, "design/assets.md")
        assert [p.name for p in sx.iter_post_paths(content_dir)] == ["assets.md"]


# ---------------------------------------------------------------------------
# build_post
# ---------------------------------------------------------------------------


class TestBuildPost:
    def test_metadata_is_preserved(self, content_dir: Path) -> None:
        path = write_post(
            content_dir,
            "engineering/my-post.md",
            frontmatter=(
                "title: My Post\ndescription: A description.\ncreated: 2026-03-04\n"
                "tags:\n  - one\n"
            ),
        )
        post = sx.build_post(path, content_dir)
        assert post.title == "My Post"
        assert post.description == "A description."
        assert post.created == dt.date(2026, 3, 4)
        assert post.section == "engineering"
        assert post.slug == "my-post"
        assert post.canonical_url == "https://kvallespin.github.io/engineering/my-post"
        assert post.source_path == "content/engineering/my-post.md"

    def test_source_sha256_matches_the_file_bytes(self, content_dir: Path) -> None:
        path = write_post(content_dir, "design/p.md")
        post = sx.build_post(path, content_dir)
        assert post.source_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()

    def test_missing_description_becomes_empty_string(self, content_dir: Path) -> None:
        path = write_post(content_dir, "design/p.md", frontmatter="title: T\ncreated: 2026-01-01")
        assert sx.build_post(path, content_dir).description == ""

    def test_missing_title_raises(self, content_dir: Path) -> None:
        path = write_post(content_dir, "design/p.md", frontmatter="created: 2026-01-01")
        with pytest.raises(sx.ExportError, match="missing a non-empty 'title'"):
            sx.build_post(path, content_dir)

    def test_blank_title_raises(self, content_dir: Path) -> None:
        path = write_post(content_dir, "design/p.md", frontmatter="title: '   '\ncreated: 2026-01-01")
        with pytest.raises(sx.ExportError, match="missing a non-empty 'title'"):
            sx.build_post(path, content_dir)

    def test_missing_created_raises(self, content_dir: Path) -> None:
        path = write_post(content_dir, "design/p.md", frontmatter="title: T")
        with pytest.raises(sx.ExportError, match="missing 'created'"):
            sx.build_post(path, content_dir)

    def test_non_string_description_raises(self, content_dir: Path) -> None:
        path = write_post(
            content_dir, "design/p.md", frontmatter="title: T\ncreated: 2026-01-01\ndescription: [a]"
        )
        with pytest.raises(sx.ExportError, match="'description' must be a string"):
            sx.build_post(path, content_dir)

    def test_error_messages_name_the_source_file(self, content_dir: Path) -> None:
        path = write_post(content_dir, "design/broken.md", frontmatter="title: T")
        with pytest.raises(sx.ExportError, match=r"design/broken\.md"):
            sx.build_post(path, content_dir)

    def test_leading_h1_matching_the_title_is_dropped(self, content_dir: Path) -> None:
        path = write_post(
            content_dir,
            "design/p.md",
            frontmatter="title: My Title\ncreated: 2026-01-01",
            body="# My Title\n\nIntro.\n",
        )
        html = sx.build_post(path, content_dir).html
        assert "<h1>" not in html
        assert "<p>Intro.</p>" in html

    def test_leading_h1_not_matching_the_title_is_kept(self, content_dir: Path) -> None:
        path = write_post(
            content_dir,
            "design/p.md",
            frontmatter="title: My Title\ncreated: 2026-01-01",
            body="# Another Heading\n\nIntro.\n",
        )
        assert "<h1>Another Heading</h1>" in sx.build_post(path, content_dir).html

    def test_images_are_resolved_and_rewritten(self, content_dir: Path) -> None:
        write_image(content_dir, "engineering/assets/pic.png")
        write_image(content_dir, "assets/shared.png")
        path = write_post(
            content_dir,
            "engineering/p.md",
            frontmatter="title: T\ncreated: 2026-01-01",
            body='![](assets/pic.png)\n\n<p><img src="../assets/shared.png"></p>\n',
        )
        post = sx.build_post(path, content_dir)
        assert "https://kvallespin.github.io/engineering/assets/pic.png" in post.html
        assert "https://kvallespin.github.io/assets/shared.png" in post.html
        assert post.images == ("engineering/assets/pic.png", "assets/shared.png")

    def test_missing_image_raises_and_names_the_path(self, content_dir: Path) -> None:
        path = write_post(
            content_dir,
            "engineering/p.md",
            frontmatter="title: T\ncreated: 2026-01-01",
            body="![](assets/ghost.png)\n",
        )
        with pytest.raises(sx.ExportError, match="engineering/assets/ghost.png"):
            sx.build_post(path, content_dir)

    def test_wrong_relative_depth_is_caught(self, content_dir: Path) -> None:
        """The ai-slop-ste regression: assets/ vs ../assets/."""
        write_image(content_dir, "assets/ai-slop-ste/banner.png")
        path = write_post(
            content_dir,
            "data-science/p.md",
            frontmatter="title: T\ncreated: 2026-01-01",
            body='<p><img src="assets/ai-slop-ste/banner.png"></p>\n',
        )
        with pytest.raises(sx.ExportError, match="data-science/assets/ai-slop-ste/banner.png"):
            sx.build_post(path, content_dir)

    def test_external_images_are_not_validated_against_disk(self, content_dir: Path) -> None:
        path = write_post(
            content_dir,
            "design/p.md",
            frontmatter="title: T\ncreated: 2026-01-01",
            body='<img src="https://example.com/remote.png">\n',
        )
        assert "https://example.com/remote.png" in sx.build_post(path, content_dir).html

    def test_custom_base_url_is_applied(self, content_dir: Path) -> None:
        write_image(content_dir, "design/assets/pic.png")
        path = write_post(
            content_dir,
            "design/p.md",
            frontmatter="title: T\ncreated: 2026-01-01",
            body="![](assets/pic.png)\n",
        )
        post = sx.build_post(path, content_dir, base_url="https://staging.example.com")
        assert post.canonical_url == "https://staging.example.com/design/p"
        assert "https://staging.example.com/design/assets/pic.png" in post.html

    def test_unusable_filename_raises(self, content_dir: Path) -> None:
        path = write_post(content_dir, "design/!!!.md")
        with pytest.raises(sx.ExportError, match="usable slug"):
            sx.build_post(path, content_dir)


class TestPostFields:
    def test_pub_date_is_midnight_gmt(self) -> None:
        post = sx.Post(
            source_path="content/design/p.md",
            section="design",
            slug="p",
            title="T",
            description="",
            created=dt.date(2026, 9, 1),
            canonical_url="https://kvallespin.github.io/design/p",
            html="<p>x</p>",
            source_sha256="0" * 64,
        )
        assert post.pub_date == "Tue, 01 Sep 2026 00:00:00 GMT"
        assert post.date == "2026-09-01"
        assert post.html_sha256 == hashlib.sha256(b"<p>x</p>").hexdigest()


# ---------------------------------------------------------------------------
# build_posts ordering
# ---------------------------------------------------------------------------


class TestBuildPosts:
    def test_sorted_newest_first_with_url_tiebreak(self, content_dir: Path) -> None:
        write_post(content_dir, "design/old.md", frontmatter="title: Old\ncreated: 2025-01-01")
        write_post(content_dir, "design/new.md", frontmatter="title: New\ncreated: 2026-05-05")
        write_post(content_dir, "finance/tie-b.md", frontmatter="title: B\ncreated: 2026-01-01")
        write_post(content_dir, "design/tie-a.md", frontmatter="title: A\ncreated: 2026-01-01")

        urls = [p.canonical_url for p in sx.build_posts(content_dir)]
        assert urls == [
            "https://kvallespin.github.io/design/new",
            "https://kvallespin.github.io/design/tie-a",
            "https://kvallespin.github.io/finance/tie-b",
            "https://kvallespin.github.io/design/old",
        ]

    def test_empty_content_tree_yields_no_posts(self, content_dir: Path) -> None:
        assert sx.build_posts(content_dir) == []


# ---------------------------------------------------------------------------
# Feed rendering
# ---------------------------------------------------------------------------


class TestRenderFeed:
    def _post(self, **overrides) -> sx.Post:
        defaults = dict(
            source_path="content/design/p.md",
            section="design",
            slug="p",
            title="Title",
            description="Desc",
            created=dt.date(2026, 1, 2),
            canonical_url="https://kvallespin.github.io/design/p",
            html="<p>Body</p>",
            source_sha256="0" * 64,
        )
        defaults.update(overrides)
        return sx.Post(**defaults)

    def test_channel_scaffolding(self) -> None:
        xml = sx.render_feed([self._post()])
        assert xml.startswith('<?xml version="1.0" encoding="UTF-8"?>')
        assert '<rss version="2.0"' in xml
        assert 'xmlns:content="http://purl.org/rss/1.0/modules/content/"' in xml
        assert "<channel>" in xml and "</rss>" in xml
        assert xml.endswith("</rss>\n")

    def test_item_fields(self) -> None:
        xml = sx.render_feed([self._post()])
        assert "<title>Title</title>" in xml
        assert "<link>https://kvallespin.github.io/design/p</link>" in xml
        assert '<guid isPermaLink="true">https://kvallespin.github.io/design/p</guid>' in xml
        assert "<pubDate>Fri, 02 Jan 2026 00:00:00 GMT</pubDate>" in xml

    def test_content_encoded_uses_cdata(self) -> None:
        xml = sx.render_feed([self._post(html="<p>Body & <em>more</em></p>")])
        assert "<content:encoded><![CDATA[<p>Body & <em>more</em></p>]]></content:encoded>" in xml

    def test_cdata_terminator_in_body_is_split(self) -> None:
        xml = sx.render_feed([self._post(html="before ]]> after")])
        assert "before ]]]]><![CDATA[> after" in xml
        # The only real CDATA terminators are the ones closing each section.
        assert "]]]]>" in xml

    def test_xml_special_characters_in_title_are_escaped(self) -> None:
        xml = sx.render_feed([self._post(title="A & B <C>")])
        assert "<title>A &amp; B &lt;C&gt;</title>" in xml

    def test_empty_description_still_emits_the_element(self) -> None:
        xml = sx.render_feed([self._post(description="")])
        assert "<description><![CDATA[]]></description>" in xml

    def test_base_url_appears_as_channel_link(self) -> None:
        xml = sx.render_feed([], base_url="https://example.com/")
        assert "<link>https://example.com</link>" in xml

    def test_rendering_is_deterministic(self) -> None:
        posts = [self._post(), self._post(slug="q", canonical_url="https://x/y")]
        assert sx.render_feed(posts) == sx.render_feed(posts)

    def test_no_build_timestamp_is_emitted(self) -> None:
        xml = sx.render_feed([self._post()])
        assert "lastBuildDate" not in xml


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


class TestManifest:
    def test_shape_and_fields(self, content_dir: Path) -> None:
        write_post(content_dir, "design/p.md", frontmatter="title: T\ndescription: D\ncreated: 2026-02-03")
        posts = sx.build_posts(content_dir)
        feed = sx.render_feed(posts)
        manifest = sx.build_manifest(posts, sx.DEFAULT_BASE_URL, feed)

        assert manifest["post_count"] == 1
        assert manifest["base_url"] == "https://kvallespin.github.io"
        assert manifest["schema_version"] == sx.MANIFEST_SCHEMA_VERSION
        assert manifest["feed_sha256"] == hashlib.sha256(feed.encode("utf-8")).hexdigest()

        entry = manifest["posts"][0]
        for key in ("source_path", "canonical_url", "sha256", "date"):
            assert key in entry
        assert entry["source_path"] == "content/design/p.md"
        assert entry["canonical_url"] == "https://kvallespin.github.io/design/p"
        assert entry["date"] == "2026-02-03"
        assert entry["sha256"] == hashlib.sha256((content_dir / "design/p.md").read_bytes()).hexdigest()

    def test_manifest_order_matches_feed_order(self, content_dir: Path) -> None:
        write_post(content_dir, "design/a.md", frontmatter="title: A\ncreated: 2026-01-01")
        write_post(content_dir, "design/b.md", frontmatter="title: B\ncreated: 2026-05-05")
        posts = sx.build_posts(content_dir)
        manifest = sx.build_manifest(posts, sx.DEFAULT_BASE_URL, sx.render_feed(posts))
        assert [e["canonical_url"] for e in manifest["posts"]] == [p.canonical_url for p in posts]

    def test_dump_is_deterministic_and_newline_terminated(self, content_dir: Path) -> None:
        write_post(content_dir, "design/p.md")
        posts = sx.build_posts(content_dir)
        manifest = sx.build_manifest(posts, sx.DEFAULT_BASE_URL, sx.render_feed(posts))
        dumped = sx.dump_manifest(manifest)
        assert dumped == sx.dump_manifest(manifest)
        assert dumped.endswith("\n")
        assert json.loads(dumped) == manifest

    def test_non_ascii_is_not_escaped(self, content_dir: Path) -> None:
        write_post(content_dir, "design/p.md", frontmatter="title: Café ’26\ncreated: 2026-01-01")
        posts = sx.build_posts(content_dir)
        dumped = sx.dump_manifest(sx.build_manifest(posts, sx.DEFAULT_BASE_URL, ""))
        assert "Café" in dumped


# ---------------------------------------------------------------------------
# export() and the CLI
# ---------------------------------------------------------------------------


class TestExport:
    def test_writes_both_files(self, content_dir: Path, tmp_path: Path) -> None:
        write_post(content_dir, "design/p.md")
        feed_path = tmp_path / "out" / "feed.xml"
        manifest_path = tmp_path / "out" / "manifest.json"
        sx.export(content_dir, feed_path, manifest_path)
        assert feed_path.read_text(encoding="utf-8").startswith("<?xml")
        assert json.loads(manifest_path.read_text(encoding="utf-8"))["post_count"] == 1

    def test_dry_run_writes_nothing(self, content_dir: Path, tmp_path: Path) -> None:
        write_post(content_dir, "design/p.md")
        feed_path = tmp_path / "out" / "feed.xml"
        manifest_path = tmp_path / "out" / "manifest.json"
        posts, feed, manifest = sx.export(content_dir, feed_path, manifest_path, dry_run=True)
        assert len(posts) == 1 and feed and manifest
        assert not feed_path.exists()
        assert not manifest_path.exists()
        assert not feed_path.parent.exists()

    def test_repeated_export_is_byte_identical(self, content_dir: Path, tmp_path: Path) -> None:
        write_post(content_dir, "design/p.md")
        first = sx.export(content_dir, tmp_path / "a.xml", tmp_path / "a.json")
        second = sx.export(content_dir, tmp_path / "b.xml", tmp_path / "b.json")
        assert (tmp_path / "a.xml").read_bytes() == (tmp_path / "b.xml").read_bytes()
        assert (tmp_path / "a.json").read_bytes() == (tmp_path / "b.json").read_bytes()
        assert first[1:] == second[1:]


class TestCli:
    def test_defaults_point_at_the_repository(self) -> None:
        args = sx.build_arg_parser().parse_args([])
        assert args.content_dir == REPO_ROOT / "content"
        assert args.base_url == sx.DEFAULT_BASE_URL
        assert args.dry_run is False

    def test_all_documented_flags_are_accepted(self, tmp_path: Path) -> None:
        args = sx.build_arg_parser().parse_args(
            [
                "--content-dir", "c",
                "--output", "o.xml",
                "--manifest", "m.json",
                "--base-url", "https://example.com",
                "--dry-run",
            ]
        )
        assert args.content_dir == Path("c")
        assert args.output == Path("o.xml")
        assert args.manifest == Path("m.json")
        assert args.base_url == "https://example.com"
        assert args.dry_run is True

    def test_main_writes_and_returns_zero(self, content_dir: Path, tmp_path: Path, capsys) -> None:
        write_post(content_dir, "design/p.md")
        feed_path = tmp_path / "feed.xml"
        manifest_path = tmp_path / "manifest.json"
        code = sx.main(
            ["--content-dir", str(content_dir), "--output", str(feed_path),
             "--manifest", str(manifest_path)]
        )
        assert code == 0
        assert feed_path.exists() and manifest_path.exists()
        assert "wrote: 1 post(s)" in capsys.readouterr().out

    def test_main_dry_run_reports_without_writing(self, content_dir: Path, tmp_path: Path, capsys) -> None:
        write_post(content_dir, "design/p.md")
        feed_path = tmp_path / "feed.xml"
        code = sx.main(
            ["--content-dir", str(content_dir), "--output", str(feed_path),
             "--manifest", str(tmp_path / "m.json"), "--dry-run"]
        )
        assert code == 0
        assert not feed_path.exists()
        assert "dry run: 1 post(s)" in capsys.readouterr().out

    def test_main_honours_base_url(self, content_dir: Path, tmp_path: Path) -> None:
        write_post(content_dir, "design/p.md")
        feed_path = tmp_path / "feed.xml"
        sx.main(
            ["--content-dir", str(content_dir), "--output", str(feed_path),
             "--manifest", str(tmp_path / "m.json"), "--base-url", "https://example.com"]
        )
        assert "https://example.com/design/p" in feed_path.read_text(encoding="utf-8")

    def test_main_reports_errors_and_returns_one(self, tmp_path: Path, capsys) -> None:
        code = sx.main(
            ["--content-dir", str(tmp_path / "missing"), "--output", str(tmp_path / "f.xml"),
             "--manifest", str(tmp_path / "m.json")]
        )
        assert code == 1
        assert "error:" in capsys.readouterr().err

    def test_main_fails_on_a_post_missing_created(self, content_dir: Path, tmp_path: Path, capsys) -> None:
        write_post(content_dir, "design/p.md", frontmatter="title: T")
        code = sx.main(
            ["--content-dir", str(content_dir), "--output", str(tmp_path / "f.xml"),
             "--manifest", str(tmp_path / "m.json")]
        )
        assert code == 1
        assert "missing 'created'" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Integration against the real content tree
# ---------------------------------------------------------------------------


class TestRealContentTree:
    def test_at_least_the_thirteen_baseline_posts(self, real_posts: list[sx.Post]) -> None:
        assert len(real_posts) >= 13

    def test_expected_source_files(self, real_posts: list[sx.Post]) -> None:
        assert set(EXPECTED_SOURCE_PATHS) <= {p.source_path for p in real_posts}

    def test_excluded_files_are_absent(self, real_posts: list[sx.Post]) -> None:
        paths = {p.source_path for p in real_posts}
        for excluded in (
            "content/index.md",
            "content/about-me/index.md",
            "content/about-me/profile.md",
            "content/projects/fm2-reviewer.md",
            "content/projects/assets/pax-silica-agent-manifest.md",
            "content/projects/assets/pax-silica-philippines-10y-mirofish-seed.md",
        ):
            assert excluded not in paths
        assert not any(path.endswith("/index.md") for path in paths)
        assert not any("/assets/" in path for path in paths)

    def test_only_the_five_sections_are_present(self, real_posts: list[sx.Post]) -> None:
        assert {p.section for p in real_posts} <= set(sx.SECTIONS)

    def test_every_post_has_a_title_and_created_date(self, real_posts: list[sx.Post]) -> None:
        for post in real_posts:
            assert post.title
            assert isinstance(post.created, dt.date)

    def test_canonical_urls_are_unique_and_well_formed(self, real_posts: list[sx.Post]) -> None:
        urls = [p.canonical_url for p in real_posts]
        assert len(set(urls)) == len(urls)
        for url in urls:
            assert url.startswith("https://kvallespin.github.io/")

    def test_two_staged_fixes_are_in_place(self, real_posts: list[sx.Post]) -> None:
        by_slug = {p.slug: p for p in real_posts}
        assert by_slug["ai-slop-ste"].created == dt.date(2026, 9, 1)
        assert by_slug["redesigning-the-redesign-nobody-asked-for"].created == dt.date(2026, 6, 26)

    def test_ai_slop_images_resolve_to_the_shared_assets_directory(
        self, real_posts: list[sx.Post]
    ) -> None:
        post = next(p for p in real_posts if p.slug == "ai-slop-ste")
        assert post.images == (
            "assets/ai-slop-ste/ai-slop-ste-main-banner.png",
            "assets/ai-slop-ste/asd-ste100-ai-white-paper-banner.png",
        )
        assert "https://kvallespin.github.io/assets/ai-slop-ste/" in post.html
        assert "/data-science/assets/ai-slop-ste/" not in post.html

    def test_every_referenced_image_exists_on_disk(self, real_posts: list[sx.Post]) -> None:
        for post in real_posts:
            for image in post.images:
                assert (REAL_CONTENT / image).is_file(), f"{post.source_path} -> {image}"

    def test_no_leading_h1_survives(self, real_posts: list[sx.Post]) -> None:
        for post in real_posts:
            assert "<h1" not in post.html, post.source_path

    def test_no_relative_urls_remain(self, real_posts: list[sx.Post]) -> None:
        pattern = re.compile(r'(?:src|href)=["\']([^"\']+)["\']')
        for post in real_posts:
            for url in pattern.findall(post.html):
                assert re.match(r"^(?:https?:|mailto:|data:|#)", url), (post.source_path, url)

    def test_inline_html_survives_conversion(self, real_posts: list[sx.Post]) -> None:
        septimana = next(p for p in real_posts if p.slug == "septimana-mirabilis")
        assert "<p class='caption'" in septimana.html
        gcash = next(p for p in real_posts if p.slug == "an-engineers-valuation-of-the-mynt-gcash-ipo")
        assert "<div style=" in gcash.html

    def test_feed_and_manifest_are_reproducible(self) -> None:
        first_posts = sx.build_posts(REAL_CONTENT)
        second_posts = sx.build_posts(REAL_CONTENT)
        first_feed = sx.render_feed(first_posts)
        second_feed = sx.render_feed(second_posts)
        assert first_feed == second_feed
        assert sx.dump_manifest(
            sx.build_manifest(first_posts, sx.DEFAULT_BASE_URL, first_feed)
        ) == sx.dump_manifest(
            sx.build_manifest(second_posts, sx.DEFAULT_BASE_URL, second_feed)
        )

    def test_feed_item_count_matches_posts(self, real_posts: list[sx.Post]) -> None:
        xml = sx.render_feed(real_posts)
        assert xml.count("<item>") == len(real_posts)
        assert xml.count("<content:encoded><![CDATA[") == len(real_posts)

    def test_manifest_hashes_match_the_source_files(self, real_posts: list[sx.Post]) -> None:
        manifest = sx.build_manifest(real_posts, sx.DEFAULT_BASE_URL, sx.render_feed(real_posts))
        for entry in manifest["posts"]:
            source = REAL_CONTENT.parent / entry["source_path"]
            assert entry["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
