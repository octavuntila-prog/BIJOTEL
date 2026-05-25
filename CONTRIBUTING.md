# Contributing to BIJOTEL

## Development Setup

```bash
git clone https://github.com/octavuntila-prog/BIJOTEL.git
cd BIJOTEL
python -m venv venv
source venv/bin/activate  # or venv\Scripts\activate on Windows
pip install -e ".[all,dev]"
python -m pytest
```

## Pull Requests

1. Fork the repo
2. Create a feature branch
3. Write tests for new functionality
4. Ensure `pytest` passes (686+ tests)
5. Ensure `ruff check src/` is clean
6. Submit PR with description

## Code Style

- ruff for linting
- Type hints encouraged
- Tests required for new features
- Coverage must not drop below 92%

## Reporting Issues

Use GitHub Issues with:
- BIJOTEL version (`bijotel --version`)
- Python version
- OS
- Steps to reproduce
- Expected vs actual behavior
