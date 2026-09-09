# kvallespin-substack-feed

Substack-compatible RSS feed generated from the canonical Quartz blog at
https://kvallespin.github.io.

This repository is isolated from the live blog repository. It reads the public
blog source, applies feed-only normalizations in a temporary checkout, and
publishes only the generated feed artifacts.

## Published files

- `docs/substack-feed.xml`
- `docs/substack-manifest.json`

Expected public feed URL after GitHub Pages is enabled:

https://kvallespin.github.io/kvallespin-substack-feed/substack-feed.xml

## Source and normalization

The canonical source remains:

https://github.com/kvallespin/kvallespin.github.io

Feed-specific corrections are stored in:

`overrides/blog-normalization.patch`

The patch is applied only to a disposable checkout. It is not committed or
pushed to the canonical blog repository.

## Automated updates

`.github/workflows/update-feed.yml` checks the canonical blog twice per hour,
runs the exporter test suite, regenerates the feed, and commits changed feed
artifacts to this repository.

The workflow updates the RSS feed only. It does not directly publish or email
articles through Substack.

## Local validation

Use Python 3.11. Set `SUBSTACK_CONTENT_DIR` to a normalized temporary checkout
of the blog, then run:

    python -m pytest tests/test_substack_export.py -q

Generate the feed with:

    python scripts/substack_export.py \
      --content-dir <temporary-checkout>/content \
      --output docs/substack-feed.xml \
      --manifest docs/substack-manifest.json
