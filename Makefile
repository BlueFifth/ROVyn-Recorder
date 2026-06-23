# Local equivalent of .github/workflows/deploy.yml, which delegates to the
# BlueOS-community/Deploy-BlueOS-Extension@v1.5.0 composite action.
# Mirrors that action's build/push logic so the same image can be built
# and pushed to Docker Hub from a local machine.

DOCKER_USERNAME   ?= bluefifth
DOCKER_PASSWORD   ?=

IMAGE_PREFIX      ?= rovyn-
IMAGE_NAME        ?= recorder
IMAGE_TAG         ?= vibe
PLATFORMS         ?= linux/arm/v7

CONTEXT           ?= .
DOCKERFILE        ?= Dockerfile

AUTHOR            ?= Gavin Foster
AUTHOR_EMAIL      ?= bluefifth@duck.com
MAINTAINER        ?= Gavin Foster
MAINTAINER_EMAIL  ?= bluefifth@duck.com
REPO              ?= BlueFifth/ROVyn-Recorder
OWNER             ?= BlueFifth

BUILDX_BUILDER    ?= rovyn-builder
CACHE_DIR         ?= /tmp/.buildx-cache

DOCKER_IMAGE      := $(DOCKER_USERNAME)/$(IMAGE_PREFIX)$(IMAGE_NAME)
PREFIXED_IMAGE    := $(IMAGE_PREFIX)$(IMAGE_NAME)
GIT_HASH_SHORT    := $(shell git rev-parse --short HEAD)
ARCH_SLUG         := $(subst /,-,$(patsubst linux/%,%,$(PLATFORMS)))
ARTIFACT_FILE     := $(PREFIXED_IMAGE)-docker-image-$(GIT_HASH_SHORT)-$(ARCH_SLUG).tar

TAGS := --tag $(DOCKER_IMAGE):$(IMAGE_TAG)
ifneq (,$(shell echo $(IMAGE_TAG) | grep -Eq '^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$$' && echo match))
TAGS += --tag $(DOCKER_IMAGE):latest
endif

BUILD_ARGS := \
	--build-arg IMAGE_NAME='$(IMAGE_NAME)' \
	--build-arg AUTHOR='$(AUTHOR)' \
	--build-arg AUTHOR_EMAIL='$(AUTHOR_EMAIL)' \
	--build-arg MAINTAINER='$(MAINTAINER)' \
	--build-arg MAINTAINER_EMAIL='$(MAINTAINER_EMAIL)' \
	--build-arg REPO='$(REPO)' \
	--build-arg OWNER='$(OWNER)'

CACHE_ARGS := --cache-from type=local,src=$(CACHE_DIR) --cache-to type=local,dest=$(CACHE_DIR)

.DEFAULT_GOAL := help
.PHONY: help check-username qemu builder warm-cache build image load login push inspect deploy clean

help:
	@echo "Targets:"
	@echo "  make builder      Create/select the docker-container buildx builder"
	@echo "  make qemu         Install QEMU emulation for cross-platform builds"
	@echo "  make build        Build the image for \$$(PLATFORMS) (no push, not exported)"
	@echo "  make image        Build for \$$(PLATFORMS) and export as a .tar for sideloading"
	@echo "  make load         Build for the host platform and load it into the local Docker daemon"
	@echo "  make login        Log in to Docker Hub (only needed if not already logged in)"
	@echo "  make push         Build and push the image to Docker Hub (assumes you're already logged in)"
	@echo "  make inspect      Inspect the pushed manifest"
	@echo "  make deploy       Full pipeline: builder, qemu, build, push, inspect (assumes you're already logged in)"
	@echo "  make clean        Remove the buildx builder and local layer cache"
	@echo ""
	@echo "Override variables as needed, e.g.:"
	@echo "  make deploy DOCKER_USERNAME=me IMAGE_TAG=1.2.3   # prompts for password"
	@echo "  export DOCKER_PASSWORD=*** ; make deploy DOCKER_USERNAME=me   # non-interactive"

check-username:
	@[ -n "$(DOCKER_USERNAME)" ] || { echo "Error: DOCKER_USERNAME is not set" >&2; exit 1; }

qemu:
	docker run --rm --privileged tonistiigi/binfmt:qemu-v7.0.0-28 --install all

builder:
	docker buildx inspect $(BUILDX_BUILDER) >/dev/null 2>&1 || \
		docker buildx create --name $(BUILDX_BUILDER) --driver docker-container
	docker buildx use $(BUILDX_BUILDER)

warm-cache:
	docker pull --platform $(PLATFORMS) $(DOCKER_IMAGE):main || true

build: builder check-username warm-cache
	docker buildx build \
		--builder $(BUILDX_BUILDER) \
		--output "type=image,push=false" \
		--platform '$(PLATFORMS)' \
		$(BUILD_ARGS) \
		$(CACHE_ARGS) \
		$(TAGS) \
		--file '$(CONTEXT)/$(DOCKERFILE)' '$(CONTEXT)'

# Single-platform build for $(PLATFORMS), exported as a .tar for sideloading
# onto the target device (e.g. `docker load -i <file>.tar`). Mirrors the
# action's "Create image artifact" step.
image: builder
	docker buildx build \
		--builder $(BUILDX_BUILDER) \
		--platform '$(PLATFORMS)' \
		$(BUILD_ARGS) \
		$(CACHE_ARGS) \
		--tag $(PREFIXED_IMAGE):$(GIT_HASH_SHORT) \
		--output "type=docker,dest=$(ARTIFACT_FILE)" \
		--file '$(CONTEXT)/$(DOCKERFILE)' '$(CONTEXT)'
	@echo "Wrote $(ARTIFACT_FILE)"

# Build for the host's own architecture and load it straight into the local
# Docker daemon so it can be run with `docker run` for local testing.
load: builder
	docker buildx build \
		--builder $(BUILDX_BUILDER) \
		--load \
		$(BUILD_ARGS) \
		$(CACHE_ARGS) \
		--tag $(PREFIXED_IMAGE):$(IMAGE_TAG) \
		--file '$(CONTEXT)/$(DOCKERFILE)' '$(CONTEXT)'
	@echo "Loaded $(PREFIXED_IMAGE):$(IMAGE_TAG) - run with: docker run --rm -p 8000:8000 $(PREFIXED_IMAGE):$(IMAGE_TAG)"

# If DOCKER_PASSWORD is exported in the environment, it's piped to
# `docker login` via a shell variable ($$DOCKER_PASSWORD), never substituted
# into the command line by Make, so it can't leak through `ps` or Make's
# command echo. If it's not set, Docker prompts for it interactively
# instead (the most secure option for local/manual use).
login: check-username
	@if [ -n "$$DOCKER_PASSWORD" ]; then \
		echo "$$DOCKER_PASSWORD" | docker login --username "$(DOCKER_USERNAME)" --password-stdin; \
	else \
		docker login --username "$(DOCKER_USERNAME)"; \
	fi

# Doesn't depend on `login` - if you've already authenticated with
# `docker login` in your own shell, Docker reuses those cached credentials
# from ~/.docker/config.json automatically. Run `make login` first only if
# you haven't already signed in to Docker Hub.
push: builder check-username
	docker buildx build \
		--builder $(BUILDX_BUILDER) \
		--output "type=image,push=true" \
		--platform '$(PLATFORMS)' \
		$(BUILD_ARGS) \
		$(CACHE_ARGS) \
		$(TAGS) \
		--file '$(CONTEXT)/$(DOCKERFILE)' '$(CONTEXT)'

inspect:
	docker buildx imagetools inspect $(DOCKER_IMAGE):$(IMAGE_TAG)

deploy: builder qemu build push inspect

clean:
	-docker buildx rm $(BUILDX_BUILDER)
	rm -rf $(CACHE_DIR)
