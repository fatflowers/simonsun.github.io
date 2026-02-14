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
- **Posts with images** use Page Bundle directories (e.g. `content/posts/openim-1/index.md`). Images live alongside `index.md` in the same bundle directory and are referenced with **relative paths** (e.g. `![alt](image.png)`). This works as long as a default-language `index.md` exists in the bundle — Hugo will then copy all sibling resources.

### Theme Customization (Override Points)
PaperMod is a git submodule at `themes/PaperMod/`. Never edit theme files directly. Override via:

| File | Purpose |
|---|---|
| `layouts/partials/extend_head.html` | Markmap + Mermaid JS injection; Mermaid theme-aware initialization |
| `layouts/partials/extend_footer.html` | medium-zoom (click-to-zoom images) |
| `layouts/_default/_markup/render-codeblock-mermaid.html` | Converts ` ```mermaid ` fences → `<div class="mermaid">` |
| `layouts/_default/_markup/render-codeblock-markmap.html` | Converts ` ```markmap ` fences → markmap `<div>` container |
| `assets/css/extended/markmap.css` | Markmap container sizing (height, background) |
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

### Markmap (Mind Maps)

**What it is:** Markmap renders a Markdown outline as an interactive, zoomable mind-map SVG directly in the browser. Users can pan, zoom (scroll wheel / pinch), and click nodes to collapse/expand branches.

**How it works in this site:**
- `extend_head.html` loads `markmap-autoloader` from CDN, which bundles the transformer (Markdown → tree) and the renderer.
- `render-codeblock-markmap.html` converts every ` ```markmap ` fenced block into a `<div class="markmap">` wrapper containing a `<script type="text/template">` with the raw Markdown. The autoloader scans for these on `DOMContentLoaded` and renders them.
- `assets/css/extended/markmap.css` sets the container to `height: 80vh; min-height: 600px` so the diagram has enough space.
- `data-options='{"initialExpandLevel":2}'` on the wrapper limits the initial render to 2 levels deep, preventing the full tree from being compressed into a tiny view.

**How to add a markmap to a post:**

````markdown
```markmap
# Root Topic
## Branch A
### Leaf 1
### Leaf 2
## Branch B
- Item 1
- Item 2
```
````

**Tips:**
- Keep depth ≤ 3–4 levels for a readable initial view; deeper nodes are hidden and expand on click.
- If the diagram looks too small, reduce the number of top-level branches or increase `initialExpandLevel` in `render-codeblock-markmap.html`.
- The mind map supports full Markdown inline syntax (bold, links, code) inside node labels.

### Deployment
`.github/workflows/hugo.yml` — builds on push to `main`, deploys to GitHub Pages.
`baseURL = "/"` in `hugo.toml`; the Actions workflow injects the real domain via `--baseURL "${{ steps.pages.outputs.base_url }}/"`.
