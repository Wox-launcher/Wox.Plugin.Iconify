.PHONY: help install clean build publish

DIST_DIR := dist
SRC_DIR := src

help:
	@echo "Available commands:"
	@echo "  make install  - Install project dependencies"
	@echo "  make clean   - Clean build directory"
	@echo "  make build   - Build project"
	@echo "  make publish - Build and publish plugin package"

install:
	uv sync --all-extras

clean:
	rm -rf $(DIST_DIR)

lint:
	uv run ruff check src
	uv run mypy src

format:
	uv run ruff format src

build: lint format
	rm -rf $(DIST_DIR)
	mkdir -p $(DIST_DIR)/replaceme_with_projectname
	mkdir -p $(DIST_DIR)/dependencies
	uv pip freeze > requirements.txt
	uv pip install -r requirements.txt --target $(DIST_DIR)/dependencies
	rm requirements.txt
	cp -r $(SRC_DIR)/* $(DIST_DIR)/replaceme_with_projectname/
	find $(DIST_DIR)/dependencies -type d -name "*.dist-info" -o -name "*.egg-info" | xargs rm -rf
	find $(DIST_DIR)/dependencies -type f -name "__editable__*" -o -name ".lock" | xargs rm -f
	rm -rf $(DIST_DIR)/dependencies/*mypy*
	rm -rf $(DIST_DIR)/dependencies/ruff
	rm -rf $(DIST_DIR)/dependencies/bin
	echo 'import os\nimport sys\n\n# Add dependencies directory to Python path\ndeps_dir = os.path.join(os.path.dirname(__file__), "dependencies")\nif deps_dir not in sys.path:\n    sys.path.insert(0, deps_dir)\n\n# Import your actual plugin code\nfrom .replaceme_with_projectname.main import plugin\n\n__all__ = ["plugin"]' > $(DIST_DIR)/__init__.py
	cp plugin.json $(DIST_DIR)/plugin.json
	mkdir -p $(DIST_DIR)/image
	cp image/* $(DIST_DIR)/image/

test:
	uv run python -m unittest tests/test_friendly_names.py

publish: build
	cd $(DIST_DIR) && zip -r ../wox.plugin.replaceme_with_projectname.wox .
	rm -rf $(DIST_DIR)
