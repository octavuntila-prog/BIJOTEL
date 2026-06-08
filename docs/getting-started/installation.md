# Installation

## Basic install

```bash
pip install bijotel
```

This gives you the library, CLI, and HMAC chain machinery — enough to
seal spans and run `bijotel verify`.

## With extras

BIJOTEL ships modular extras so you only pull what you need.

```bash
# REST API + dashboard (FastAPI + uvicorn + python-multipart)
pip install "bijotel[api]"

# Anthropic instrumentor + SDK
pip install "bijotel[anthropic]"

# OpenAI SDK (works with xAI, Together, DeepSeek, etc. via base_url)
pip install "bijotel[openai]"

# Semantic fingerprinting (sentence-transformers, ~1 GB download)
pip install "bijotel[fingerprint]"

# AST code safety (tree-sitter + tree-sitter-bash)
pip install "bijotel[ast]"

# Everything
pip install "bijotel[all]"

# Development (pytest, ruff, build, twine)
pip install "bijotel[dev]"
```

## Requirements

- Python **≥ 3.11** (tested on 3.11 + 3.12 in CI)
- No external services (SQLite-based, WAL mode)
- Works on x86_64 and aarch64 (cross-arch portability validated)

## Docker

Pull a prebuilt image — no local build required:

```bash
docker run -p 8080:8080 \
  -e BIJOTEL_HMAC_SECRET=your-secret-min-16-chars \
  -v bijotel-data:/data \
  ghcr.io/octavuntila-prog/bijotel:latest
```

The image bundles the `[api,fingerprint,ast]` extras and runs
`bijotel serve --dashboard --host 0.0.0.0 --port 8080` by default.
Browser users get the dashboard at `/`; integrations call `/api/*`.

Tags: `:2.7.0`, `:latest` — host at
[`ghcr.io/octavuntila-prog/bijotel`](https://github.com/octavuntila-prog/BIJOTEL/pkgs/container/bijotel).

To build locally instead (e.g. to bundle Anthropic/OpenAI adapters
yourself):

```bash
git clone https://github.com/octavuntila-prog/BIJOTEL.git
cd BIJOTEL
python -m build --wheel        # produces dist/bijotel-2.7.0-...whl
docker build -t bijotel:local .
```

The Dockerfile copies whatever wheel sits in `dist/` so a version bump
in `pyproject.toml` doesn't require a Dockerfile edit.

## Verify the install

```bash
bijotel --help
python -c "import bijotel; print(bijotel.__version__)"
```

You should see `2.15.0` (or newer) and the CLI subcommand list:
`verify`, `inspect`, `stats`, `list`, `export`, `verify-export`,
`regression`, `energy`, `serve`.

## Next

- [5-Minute Quickstart](quickstart.md) — instrument your first LLM call
- [First Verification](first-verification.md) — verify a real chain
