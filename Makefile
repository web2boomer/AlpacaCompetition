.PHONY: install migrate test lint typecheck verify replay serve

install:
	python3.12 -m venv .venv
	.venv/bin/python -m pip install -e '.[dev]'

migrate:
	.venv/bin/money-machine db upgrade

test:
	.venv/bin/pytest

lint:
	.venv/bin/ruff check .
	.venv/bin/ruff format --check .

typecheck:
	.venv/bin/mypy

verify: test lint typecheck

replay: migrate
	.venv/bin/money-machine replay

serve:
	.venv/bin/money-machine serve --host 127.0.0.1 --port 8000
