.PHONY: test typecheck build publish docs

build:
	uv build

publish:
	 uv publish

test:
	uv run python -m pytest -m "not unsafe"

typecheck:
	uv run ty check src

docs:
	uv run mkdocs build
