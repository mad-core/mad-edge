BATS := ./tests/.bats-core/bin/bats
DISTRO ?= ubuntu

.PHONY: test test-unit test-integration test-all-distros install-bats clean-test

## Run all tests (unit + integration for default distro)
test: test-unit test-integration

## Run unit tests only (no Docker required)
test-unit: install-bats
	$(BATS) tests/unit/

## Run integration tests for a single distro (default: ubuntu)
test-integration: install-bats
	MAD_TEST_DISTRO=$(DISTRO) $(BATS) tests/integration/

## Run integration tests for all distros
test-all-distros: test-unit
	@for d in ubuntu alpine debian; do \
		echo ""; \
		echo "=== Testing $$d ==="; \
		MAD_TEST_DISTRO=$$d $(BATS) tests/integration/ || exit 1; \
	done

## Ensure bats submodules are initialized
install-bats:
	@if [ ! -f tests/.bats-core/bin/bats ]; then \
		echo "Initializing bats submodules..."; \
		git submodule update --init --recursive; \
	fi

## Clean up test containers and volumes
clean-test:
	@echo "Cleaning up test containers and volumes..."
	@docker ps -a --format '{{.Names}}' | grep 'mad-test' | xargs -r docker rm -f 2>/dev/null || true
	@docker volume ls --format '{{.Name}}' | grep 'mad-test' | xargs -r docker volume rm 2>/dev/null || true
