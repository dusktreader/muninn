default: help


## ==== Quality Control ================================================================================================

qa: qa/full  ## Shortcut for qa/full

qa/full: qa/test qa/types qa/lint  ## Run the full set of quality checks
	@echo "All quality checks pass!"

qa/test: build  ## Run unit tests
	@corepack pnpm test

qa/types: build  ## Run type checks
	@corepack pnpm typecheck

qa/lint: build  ## Run linter
	@corepack pnpm lint

e2e: install  ## Run end-to-end tests
	corepack pnpm playwright test

## ==== Project management =============================================================================================

install:  ## Install JavaScript dependencies from the lockfile
	corepack pnpm install --frozen-lockfile


build: install  ## Build the project
	corepack pnpm build

serve:  ## Start the local Vite development server
	VITE_MUNINN_API_URL=$${VITE_MUNINN_API_URL:-} corepack pnpm dev --host 0.0.0.0 --port 4173


## ==== Helpers ========================================================================================================

help:  ## Show help message
	@awk "$$PRINT_HELP_PREAMBLE" $(MAKEFILE_LIST)


# ..... Make configuration .............................................................................................

.ONESHELL:
SHELL:=/bin/bash
.PHONY: default check e2e serve install build help


# ..... Color table for pretty printing ................................................................................

RED    := \033[31m
GREEN  := \033[32m
YELLOW := \033[33m
BLUE   := \033[34m
TEAL   := \033[36m
GRAY   := \033[90m
CLEAR  := \033[0m
ITALIC := \033[3m


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
