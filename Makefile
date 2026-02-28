.SECONDARY:
SHELL := /bin/bash
.SHELLFLAGS := -o pipefail -ec
.DEFAULT_GOAL := help

MAKEFLAGS += --no-print-directory
MAKEFLAGS += --no-builtin-rules
MAKEFLAGS += --no-builtin-variables

ruff := uvx ruff@0.15.2
ty := uvx ty@0.0.19
venv := .venv

# type-check deps: lightweight packages ty needs for import resolution
# (excludes librosa and its heavy compiled transitive deps — suppressed inline)
type_deps := fastapi==0.129.2 pydantic==2.12.5 'pywebpush>=2.0.0' aiofiles==24.1.0 numpy==1.26.4

$(venv)/.installed: api/requirements.txt
	uv venv $(venv) --python 3.12 -q
	uv pip install --python $(venv)/bin/python -q $(type_deps)
	@touch $@

.PHONY: typecheck
typecheck: $(venv)/.installed
	$(ty) check --project api --python $(venv)

.PHONY: ci
ci:
	$(ruff) check api/
	$(ruff) format --check api/
	$(MAKE) typecheck

.PHONY: fix
fix:
	$(ruff) check --fix api/
	$(ruff) format api/

.PHONY: clean
clean:
	rm -rf $(venv)

.PHONY: help
help:
	@echo "Usage: make [target]"
	@echo ""
	@echo "Targets:"
	@echo "  ci         Run all checks (ruff lint + format + ty typecheck)"
	@echo "  typecheck  Run ty type checking"
	@echo "  fix        Auto-fix lint and format issues"
	@echo "  clean      Remove type-check venv"
