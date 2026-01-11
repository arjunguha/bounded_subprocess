.PHONY: test build publish docs

build:
	uv build

publish:
	 uv publish

test:
	uv run python -m pytest -m "not unsafe"

docs:
	uv run mkdocs build
