# Contributing

Contributions are welcome when they preserve the project's fail-closed click
model and third-party licensing requirements.

## Development setup

This application targets Windows 10/11 and Python 3.11.

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:PYTHONPATH = (Get-Location).Path
py -3.11 app.py --smoke-test
```

> 本发布版已移除开发期单元测试目录（`tests/`），上述命令仅做启动自检。

## Pull requests

- Keep changes focused and explain user-visible behavior in the description.
- Add regression tests for detector, engine-protocol, or click-state changes.
- Never make mouse input enabled by default.
- Never bypass turn validation, grid liveness, target-window checks, or the
  post-click visual confirmation state.
- Do not add screenshots, logs, or fixtures containing personal information.
- Run `python -m compileall -q app.py gomoku_assistant` before opening a PR.

## Updating Rapfi

Engine updates require more than replacing an executable:

1. Use an official upstream tag or release from `dhbloo/rapfi`.
2. Bundle the matching GPL source and license text.
3. Record the release URL, commit, and SHA-256 hashes in
   `vendor/rapfi/THIRD_PARTY_NOTICES.md`.
4. Verify the NNUE weight license and keep its complete terms.
5. Run the real-engine tests for 15x15, 19x19, black, white, immediate wins,
   invalid coordinates, and process reuse.

## Scope

The current engine integration is freestyle Gomoku. Standard Gomoku or Renju
rules must be implemented as explicit modes with matching engine configuration
and tests; do not silently reuse freestyle settings.
