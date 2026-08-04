default: help


## ==== Quality Control ================================================================================================

qa: qa/full  ## Shortcut for qa/full

qa/full: qa/lint  ## Run the full set of quality checks
	@echo "All quality checks pass!"

qa/lint:  ## Lint the helm charts
	helm lint helm/muninn
	helm template muninn helm/muninn --values helm/muninn/values-homelab.yaml >/dev/null


## ==== Deployment =====================================================================================================

deploy: qa/lint  ## Deploy?
	helm upgrade --install muninn helm/muninn --namespace muninn --create-namespace --values helm/muninn/values-homelab.yaml


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
.PHONY: qa/full qa/lint \
	deploy \
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
