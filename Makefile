.PHONY: test typecheck docs

test:
	uv run python -m pytest -m "not unsafe"

typecheck:
	uv run ty check src

docs:
	uv run mkdocs build
