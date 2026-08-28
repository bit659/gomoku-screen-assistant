# Changelog

All notable changes to this project are documented in this file. The project
uses [Semantic Versioning](https://semver.org/).

## [1.0.0] - 2026-08-28

### Added

- Windows desktop UI for selecting and capturing a target Gomoku window.
- Automatic 15x15 and 19x19 grid detection plus manual board calibration.
- Black and white stone recognition, including a red last-move marker.
- Bundled Rapfi `250615` engine, freestyle NNUE weights, matching source, and
  complete third-party notices.
- Background engine search with 0.5, 2, and 5 second strength presets.
- Optional single-step and continuous play, including same-point double click
  confirmation for clients that require a select-then-confirm interaction.

### Safety

- Mouse input is disabled by default and requires explicit user permission.
- `F8` revokes both continuous play and click permission, including between
  the two clicks of a confirmation action.
- Every click requires a fresh board frame, live grid validation, legal turn
  counts, an empty target point, and unchanged window geometry.
- The assistant stops instead of falling back to a weaker engine when Rapfi
  fails, times out, or returns an invalid move.

### Verified

- 59 automated tests cover board detection, asset integrity, grid liveness, engine protocol,
  both side-to-move perspectives, click confirmation, and fail-closed states.
- The bundled AVX2 and SSE4.1 executables support both 15x15 and 19x19 boards.
