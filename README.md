# 📣 Social Media Manager — (repository updated)

![Python CI](https://github.com/abucaria-afk/Social-media/actions/workflows/python-ci.yml/badge.svg?branch=main) ![CodeQL](https://github.com/abucaria-afk/Social-media/actions/workflows/codeql.yml/badge.svg?branch=main) ![License](https://img.shields.io/github/license/abucaria-afk/Social-media)

This repository contains the Python package "auteur" (an autonomous cinematic editor) and a demo for generating synthetic footage.

Note: earlier README content referenced a C++ Social Media Manager. That C++ project is not present in this repository. The README has been updated to reflect the actual contents (Python package + demo).

Badges

- Python CI: https://github.com/abucaria-afk/Social-media/actions/workflows/python-ci.yml
- CodeQL: https://github.com/abucaria-afk/Social-media/security/code-scanning

Quick summary

- Package: auteur (see pyproject.toml)
- Language: Python (requires Python >= 3.10)
- Demo: demo/make_footage.py — generates synthetic clips and a music track for testing

Getting started

Prerequisites

- Python 3.10+ (3.11/3.12 recommended)
- ffmpeg (system ffmpeg recommended for full codec support) or the optional ffmpeg-binaries wheel

Install runtime dependencies

```bash
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Run the demo to generate synthetic footage

```bash
python demo/make_footage.py ./rushes
```

Run tests

```bash
pytest -q
```

Developer tools

- Install pre-commit hooks:

```bash
pip install pre-commit
pre-commit install
pre-commit run --all-files
```

Repository Layout (relevant parts)

```
├── auteur/           # Python package
├── demo/             # demo scripts (make_footage.py)
├── pyproject.toml
├── requirements.txt
├── .github/workflows # CI workflows (python-ci + others)
```

CI and quality checks added in this branch

- Python CI: pytest matrix (3.10–3.12)
- Lint & format checks: ruff + black
- Dependency audit: pip-audit
- Code scanning: CodeQL
- Dependabot: automatic dependency updates for Python

If you want me to revert the README to the original C++-focused content or move the auteur package into a subdirectory/repo, tell me and I will adjust.
