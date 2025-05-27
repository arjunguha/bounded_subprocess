.PHONY: test build publish

build:
	uv build

publish:
	 uv publish

test:
	uv run python -m pytest -m "not unsafe"
