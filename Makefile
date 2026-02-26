.SECONDARY:
SHELL := /bin/bash
.SHELLFLAGS := -o pipefail -ec
.DEFAULT_GOAL := help

MAKEFLAGS += --no-print-directory
MAKEFLAGS += --no-builtin-rules
MAKEFLAGS += --no-builtin-variables

ruff := uvx ruff@0.15.2

.PHONY: ci
ci:
	$(ruff) check api/
	$(ruff) format --check api/

.PHONY: fix
fix:
	$(ruff) check --fix api/
	$(ruff) format api/

.PHONY: help
help:
	@echo "Usage: make [target]"
	@echo ""
	@echo "Targets:"
	@echo "  ci        Run all checks (ruff lint + format)"
	@echo "  fix       Auto-fix lint and format issues"
