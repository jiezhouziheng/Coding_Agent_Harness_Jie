.PHONY: test lint typecheck build verify

test:
	python -m pytest

lint:
	python -m ruff check .

typecheck:
	python -m mypy --strict src

build:
	python -m build --no-isolation

verify: test lint typecheck build
