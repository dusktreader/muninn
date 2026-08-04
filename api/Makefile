PACKAGE_TARGET:=src/muninn

default: help


## ==== Quality Control ================================================================================================

qa: qa/full  ## Shortcut for qa/full

qa/full: qa/unit qa/int qa/types qa/lint  ## Run the full set of quality checks
	@echo "All quality checks pass!"

qa/unit:  ## Run unit  tests
	@uv run pytest tests -m unit

qa/int:  ## Run integration tests only
	@uv run pytest tests/integration -m integration

qa/types:  ## Run static type checks
	@uv run ty check ${PACKAGE_TARGET} tests

qa/lint:  ## Run linters
	@uv run ruff check ${PACKAGE_TARGET} tests
	@uv run typos ${PACKAGE_TARGET} tests


## ==== Application ====================================================================================================

serve:  ## Run the local API server
	@uv run uvicorn muninn.api:app --host=0.0.0.0 --port=5000 --reload


## ==== Database =======================================================================================================

migrate:  ## Apply all pending database migrations
	@uv run alembic upgrade head


## ==== Helpers ========================================================================================================

clean:  ## Clean up build artifacts and other junk
	@uv run pyclean . --debris
	@rm -rf .venv
	@rm -rf dist
	@rm -rf .ruff_cache
	@rm -rf .pytest_cache
	@rm -rf .mypy_cache
	@rm -f .coverage*
	@rm -f .junit.xml

help:  ## Show help message
	@awk "$$PRINT_HELP_PREAMBLE" $(MAKEFILE_LIST)


# ..... Make configuration .............................................................................................

.ONESHELL:
SHELL:=/bin/bash
.PHONY: qa/unit qa/int qa/types qa/lint \
	serve \
	migrate\
	clean help



# ..... Color table for pretty printing ................................................................................

RED    := \033[31m
GREEN  := \033[32m
YELLOW := \033[33m
BLUE   := \033[34m
TEAL   := \033[36m
GRAY   := \033[90m
CLEAR  := \033[0m
ITALIC := \033[3m


# ..... Hidden auxiliary targets .......................................................................................

_confirm:  # Requires confirmation before proceeding (Do not use directly)
	@if [[ -z "$(CONFIRM)" ]]; then \
		echo -n "Are you sure? [y/N] " && read ans && [ $${ans:-N} = y ]; \
	fi


# ..... Help printer ...................................................................................................

define PRINT_HELP_PREAMBLE
BEGIN {
	print "Usage: $(YELLOW)make <target>$(CLEAR)"
	print
	print "Targets:"
}
/^## =+ .+( =+)?/ {
    s = $$0
    sub(/^## =+ /, "", s)
    sub(/ =+/, "", s)
	printf("\n  %s:\n", s)
}
/^## -+ .+( -+)?/ {
    s = $$0
    sub(/^## -+ /, "", s)
    sub(/ -+/, "", s)
	printf("\n    $(TEAL)> %s$(CLEAR)\n", s)
}
/^[$$()% 0-9a-zA-Z_\/-]+(\\:[$$()% 0-9a-zA-Z_\/-]+)*:.*?##/ {
    t = $$0
    sub(/:.*/, "", t)
    h = $$0
    sub(/.?*##/, "", h)
    printf("    $(YELLOW)%-19s$(CLEAR) $(GRAY)$(ITALIC)%s$(CLEAR)\n", t, h)
}
endef
export PRINT_HELP_PREAMBLE
