# Vendored packages

Both packages are copied unmodified from their PyPI sdists. Do not edit them;
upgrade by replacing the directory and re-running the test suite.

| Package | Version | License | Source | Notes |
| --- | --- | --- | --- | --- |
| aiowebostv | 0.10.0 | Apache-2.0 | https://pypi.org/project/aiowebostv/0.10.0/ | `import aiohttp` is redirected to `resources/lib/aiohttp_shim` via `sys.modules` in `resources/lib/bootstrap.py`. No source edits. |
| websockets | 17.2 | BSD-3-Clause | https://pypi.org/project/websockets/17.2/ | `speedups.c` / `speedups.pyi` omitted so the pure-Python `apply_mask` fallback is always used. Nothing else removed. |

Each package directory keeps its upstream `LICENSE` file.

Both require Python >= 3.11.
