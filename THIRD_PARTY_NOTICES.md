# Third-party notices

The project MIT license covers Research Loop's own code and documentation.

- **markdown-it** is vendored in `src/research_loop/dashboard/vendor/markdown-it/`.
  Its license is retained in [LICENSE](src/research_loop/dashboard/vendor/markdown-it/LICENSE).

Historical static assets remain under `integrations/protok/assets/` for existing
read-only deployments. **KaTeX** and the earlier **markdown-it** copy retain their
licenses in `vendor/katex/LICENSE` and `vendor/markdown-it/LICENSE` there. They are
not dependencies of the packaged dashboard.

The Cactus fixture references external MLE-bench preparation/grading sources and
a prepared dataset mirror. These are not bundled with this repository; their
pinned sources and access conditions are documented in
[the adapter README](benchmarks/cactus/README.md). The project license does not
grant rights to external datasets, checkpoints or task repositories.
