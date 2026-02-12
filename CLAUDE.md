# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Development server (includes drafts)
hugo server -D

# Production build
hugo --minify

# Hugo is installed via Homebrew; if not in PATH:
export PATH="/opt/homebrew/bin:$PATH"
```

## Project Overview

Personal portfolio and tech blog for Simon Sun (AI/Backend engineer).
**Stack:** Hugo static site generator + PaperMod theme + GitHub Actions → GitHub Pages.

## Architecture

### Multilingual Setup
- `defaultContentLanguage = "en"`, `defaultContentLanguageInSubdir = false`
- English content at root (`/`), Chinese content under `/zh/`
- **English files:** `content/foo.md` → `/foo/`
- **Chinese files:** `content/foo.zh.md` → `/zh/foo/`
- **Chinese-only posts with images** use Page Bundle directories (`content/posts/openim-1/index.zh.md`). Images live in `static/posts/<slug>/` and are referenced with absolute paths (e.g. `/posts/openim-1/image.png`) because Hugo does not copy Page Bundle resources without a default-language `index.md`.

### Theme Customization (Override Points)
PaperMod is a git submodule at `themes/PaperMod/`. Never edit theme files directly. Override via:

| File | Purpose |
|---|---|
| `layouts/partials/extend_head.html` | Mermaid JS injection + theme-aware initialization |
| `layouts/partials/extend_footer.html` | medium-zoom (click-to-zoom images) |
| `layouts/_default/_markup/render-codeblock-mermaid.html` | Converts ` ```mermaid ` fences → `<div class="mermaid">` |
| `assets/css/extended/*.css` | Injected **after** PaperMod's CSS bundle in alphabetical order |

### Code Syntax Highlighting
Configured with `noClasses = false` (CSS-class mode, not inline styles).
PaperMod's `chroma-mod.css` forces `.chroma { background-color: unset !important }` — overriding this requires `!important` in the extended CSS.
Dark theme selector: `[data-theme="dark"]` on `<html>` (not `body.dark`).

### Key Config (`hugo.toml`)
- `[markup.goldmark.renderer] unsafe = true` — required for raw HTML in Markdown (SVG `<img>` tags in posts)
- `[markup.highlight] noClasses = false` — enables CSS-class syntax highlighting
- `outputs.home = ["HTML", "RSS", "JSON"]` — JSON required for search (Fuse.js)
- Language-specific `socialIcons` must be defined per-language in `[languages.xx.params]`; global `[[params.socialIcons]]` is overridden by language-level definitions

### Special Pages
`content/search.md` / `content/search.zh.md` — `layout: "search"` (Fuse.js full-text search)
`content/archives.md` / `content/archives.zh.md` — `layout: "archives"` (chronological post list)

### Deployment
`.github/workflows/hugo.yml` — builds on push to `main`, deploys to GitHub Pages.
`baseURL = "/"` in `hugo.toml`; the Actions workflow injects the real domain via `--baseURL "${{ steps.pages.outputs.base_url }}/"`.
