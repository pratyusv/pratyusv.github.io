# Blog Content Architecture

The blog is organized by durable engineering domains rather than publication year or transient projects.

## Source Layout

Each post belongs to exactly one folder under `_posts`:

- `distributed-systems`
- `system-design`
- `cpp-algorithms`
- `cpp-reference`
- `leetcode-patterns`
- `artificial-intelligence`
- `cloud-infrastructure`

The folder determines the post's `section` through scoped defaults in `_config.yml`. Section labels and descriptions live in `_data/blog_sections.yml`, which is the presentation contract for the blog index.

## URL Stability

Published URLs are independent of source folders because `_config.yml` defines:

```yaml
permalink: /blog/:year/:title/
```

Moving a post between domain folders therefore does not change its public URL.

## Adding a Post

Use the scaffolder to create a correctly named post with the section's canonical category:

```shell
ruby bin/new-post distributed-systems quorum-systems "Quorum Systems"
```

Set `POST_DATE=YYYY-MM-DD` to schedule a different publication date. Then add focused tags, write the article, and build with `bundle exec jekyll build`.

If a new durable domain is required, add its folder scope to `_config.yml` and its display metadata to `_data/blog_sections.yml`.
