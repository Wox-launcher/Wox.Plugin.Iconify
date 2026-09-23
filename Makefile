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
	mkdir -p $(DIST_DIR)/src
	mkdir -p $(DIST_DIR)/dependencies
	uv pip freeze > requirements.txt
	uv pip install -r requirements.txt --target $(DIST_DIR)/dependencies
	rm requirements.txt
	cp -r $(SRC_DIR)/* $(DIST_DIR)/src/
	find $(DIST_DIR)/dependencies -type d -name "*.dist-info" -o -name "*.egg-info" | xargs rm -rf
	find $(DIST_DIR)/dependencies -type f -name "__editable__*" -o -name ".lock" | xargs rm -f
	rm -rf $(DIST_DIR)/dependencies/*mypy*
	rm -rf $(DIST_DIR)/dependencies/ruff
	rm -rf $(DIST_DIR)/dependencies/bin
	find $(DIST_DIR) -type d -name "__pycache__" | xargs rm -rf
	cp plugin.json $(DIST_DIR)/plugin.json
	mkdir -p $(DIST_DIR)/image
	cp image/* $(DIST_DIR)/image/

test:
	uv run python -m unittest discover -s tests

publish: build
	cd $(DIST_DIR) && zip -r ../wox.plugin.iconify.wox .
	rm -rf $(DIST_DIR)
