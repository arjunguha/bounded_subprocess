build:
	python3 -m build

publish:
	 python3 -m twine upload dist/*

test:
	python3 -m pytest

build_deps:
	python3 -m pip install build twine
