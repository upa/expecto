IMAGE ?= expecto-e2e

.PHONY: test e2e-test

test:
	uv run pytest tests/ -v

e2e-test:
	podman build -t $(IMAGE) -f e2e-tests/Dockerfile .
	podman run --rm $(IMAGE)
