default: help

DEVSPACE_NS ?= muninn-local

## ==== Quality Control ================================================================================================

qa: qa/full  ## Shortcut for qa/full

qa/full: qa/api qa/web qa/mobile  ## Run the full set of quality checks
	@echo "All quality checks pass!"

qa/api:  ## Run api quality checks
	$(MAKE) -C api qa

qa/web:  ## Run web quality checks
	$(MAKE) -C web-ui check

qa/mobile:  ## Run mobile quality checks
	$(MAKE) -C mobile-ui qa


## ==== Local Development ==============================================================================================

dev/up: web-ui/build dev/keycloak-setup  ## Build and start the local API, dependencies, and web client
	@set -e
	if pgrep -f '^devspace dev .*--namespace $(DEVSPACE_NS)$$' >/dev/null; then
		printf '%s\n' 'A Muninn DevSpace session is already running; stop it with make dev/down.' >&2
		exit 1
	fi
	devspace dev --force-build --force-deploy --tag local-$(shell date +%s) --config devspace.yaml --profile local --namespace $(DEVSPACE_NS) &
	devspace_pid=$$!
	web_pid=
	cleanup() {
		if [[ -n "$$web_pid" ]]; then kill "$$web_pid" 2>/dev/null || true; wait "$$web_pid" 2>/dev/null || true; fi
		kill -INT "$$devspace_pid" 2>/dev/null || true
		wait "$$devspace_pid" 2>/dev/null || true
	}
	trap cleanup EXIT INT TERM
	api_ip=""
	for attempt in {1..120}; do
		api_ip=$$(kubectl get service muninn-api --namespace $(DEVSPACE_NS) -o jsonpath='{.spec.clusterIP}' 2>/dev/null || true)
		if [[ -n "$$api_ip" ]] && curl --max-time 1 -fsS "http://$$api_ip:5000/readyz" >/dev/null; then break; fi
		sleep 1
	done
	if [[ -z "$$api_ip" ]] || ! curl --max-time 1 -fsS "http://$$api_ip:5000/readyz" >/dev/null; then
		printf '%s\n' 'Muninn API did not become ready in DevSpace.' >&2
		exit 1
	fi
	(cd web-ui && MUNINN_API_PROXY_TARGET="http://$$api_ip:5000" exec corepack pnpm dev --host 0.0.0.0 --port 4173) &
	web_pid=$$!
	wait "$$devspace_pid"


dev/down:  ## Force the devspace containers to stop
	@set -e
	pids=$$(pgrep -f '^devspace dev .*--namespace $(DEVSPACE_NS)$$' || true)
	if [[ -z "$$pids" ]]; then
		printf '%s\n' 'No Muninn DevSpace session is running in $(DEVSPACE_NS).'
		exit 0
	fi
	for pid in $$pids; do kill -INT "$$pid" 2>/dev/null || true; done
	for attempt in {1..20}; do
		if ! pgrep -f '^devspace dev .*--namespace $(DEVSPACE_NS)$$' >/dev/null; then
			printf '%s\n' 'Stopped the Muninn DevSpace session.'
			exit 0
		fi
		sleep .25
	done
	printf '%s\n' 'DevSpace is still running; stop it from its owning terminal.' >&2
	exit 1


dev/secrets:
	@set -euo pipefail
	if [[ -z "$${ASSEMBLYAI_API_KEY:-}" ]]; then
		printf '%s\n' 'Set ASSEMBLYAI_API_KEY before running make dev/secrets.' >&2
		exit 1
	fi
	kubectl create namespace $(DEVSPACE_NS) --dry-run=client -o yaml | kubectl apply -f -
	printf '%s' "$$ASSEMBLYAI_API_KEY" | kubectl create secret generic muninn-assemblyai --namespace $(DEVSPACE_NS) --from-file=api-key=/dev/stdin --dry-run=client -o yaml | kubectl apply -f -


dev/keycloak-setup:
	@DEVSPACE_NS=$(DEVSPACE_NS) bash infra/dev/keycloak/setup.sh


dev/logs:  ## Tail logs from all local Muninn pods (e.g. make dev/logs pod=api)
	@set -euo pipefail
	case "$(pod)" in
		"") selector='app in (muninn-api,muninn-job-manager,muninn-worker,muninn-postgresql,muninn-minio,muninn-nats)' ;;
		api|job-manager|worker|postgresql|minio|nats) selector="app=muninn-$(pod)" ;;
		*) printf '%s\n' 'Unknown pod. Use api, job-manager, worker, postgresql, minio, or nats.' >&2; exit 2 ;;
	esac
	kubectl logs --namespace "$(DEVSPACE_NS)" --selector "$$selector" --all-containers=true --prefix=true --follow=true --ignore-errors=true --tail=30 --max-log-requests=10


## ==== Subfolder Dispatch =============================================================================================

api/%:  ## Dispatch to api Makefile
	@$(MAKE) -C api $*

docs/%:  ## Dispatch to docs Makefile
	@$(MAKE) -C docs $*

infra/%:  ## Dispatch to infra Makefile
	@$(MAKE) -C infra $*

mobile-ui/%:  ## Dispatch to mobile-ui Makefile
	@$(MAKE) -C mobile-ui $*

web-ui/%:  ## Dispatch to web-ui Makefile
	@$(MAKE) -C web-ui $*


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
.PHONY: qa/full qa/api qa/web qa/mobile \
	dev/up dev/down dev/secrets dev/keycloak-setup dev/logs \
	infra mobile-ui\
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
