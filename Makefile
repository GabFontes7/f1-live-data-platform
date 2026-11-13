.PHONY: install dev test lint record help

PYTHON ?= python3
SESSION ?= 2026-las-vegas-fp1

help:
	@echo "install  install the package"
	@echo "dev      install with test and lint tooling"
	@echo "test     run the test suite"
	@echo "lint     run ruff"
	@echo "record   record a live session (SESSION=<slug>)"

install:
	$(PYTHON) -m pip install -e .

dev:
	$(PYTHON) -m pip install -e ".[dev]"

test:
	$(PYTHON) -m pytest -q

lint:
	$(PYTHON) -m ruff check src tests

record:
	$(PYTHON) -m f1platform.record --session $(SESSION)
