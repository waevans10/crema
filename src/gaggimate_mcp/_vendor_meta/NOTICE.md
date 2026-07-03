# Vendored code

`src/gaggimate_mcp/` is vendored from **julianleopold/gaggimate-mcp**
(https://github.com/julianleopold/gaggimate-mcp), pinned to commit
`0af88ad4a5f99da73245de9868a803247cdb6226`.

It is MIT-licensed; the original license is preserved in `LICENSE` in this
directory. crema reuses its binary `.slog` parser, HTTP/WebSocket device clients,
and the AI-friendly shot transformer rather than reimplementing the binary format.

To update: re-clone upstream, copy `src/gaggimate_mcp/` over this directory, and
bump the commit hash above.
