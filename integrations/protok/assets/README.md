# Local dashboard renderers

These files are served from the dashboard release itself; viewing research records does not contact a CDN.

- KaTeX **0.19.1**, official npm package `katex`, `dist/katex.min.js`, `dist/katex.min.css`, `dist/fonts/`. MIT license in `vendor/katex/LICENSE`. Formula rendering uses `trust: false`, bounded expansion/size and caught parse failures. Documentation: https://katex.org/docs/options.html
- markdown-it **15.0.3**, official npm package `markdown-it`, `dist/browser/markdown-it.umd.min.js`. MIT license in `vendor/markdown-it/LICENSE`. Raw HTML, image rendering and non-HTTP(S) links are disabled.

The explanation UI accepts structured escaped text and source references, never model-authored HTML. Cache writers run separately from the read-only HTTP server.
