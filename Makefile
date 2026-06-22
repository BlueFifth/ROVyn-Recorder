# Local equivalent of .github/workflows/deploy.yml, which delegates to the
# BlueOS-community/Deploy-BlueOS-Extension@v1.5.0 composite action.
# Mirrors that action's build/push logic so the same image can be built
# and pushed to Docker Hub from a local machine.

DOCKER_USERNAME   ?=
DOCKER_PASSWORD   ?=

IMAGE_PREFIX      ?= rovyn-
IMAGE_NAME        ?= recorder
IMAGE_TAG         ?= latest
PLATFORMS         ?= linux/arm/v7

CONTEXT           ?= .
DOCKERFILE        ?= Dockerfile

AUTHOR            ?= Author Name
AUTHOR_EMAIL      ?=
MAINTAINER        ?= BlueFifth
MAINTAINER_EMAIL  ?= maintainer.email@example.com
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
.PHONY: help check-username check-credentials qemu builder warm-cache build image login push inspect deploy clean

help:
	@echo "Targets:"
	@echo "  make builder      Create/select the docker-container buildx builder"
	@echo "  make qemu         Install QEMU emulation for cross-platform builds"
	@echo "  make build        Build the image for \$$(PLATFORMS) (no push, not exported)"
	@echo "  make image        Build for \$$(PLATFORMS) and export as a .tar for sideloading"
	@echo "  make login        Log in to Docker Hub (needs DOCKER_USERNAME/DOCKER_PASSWORD)"
	@echo "  make push         Build and push the image to Docker Hub"
	@echo "  make inspect      Inspect the pushed manifest"
	@echo "  make deploy       Full pipeline: builder, qemu, build, login, push, inspect"
	@echo "  make clean        Remove the buildx builder and local layer cache"
	@echo ""
	@echo "Override variables as needed, e.g.:"
	@echo "  make deploy DOCKER_USERNAME=me DOCKER_PASSWORD=*** IMAGE_TAG=1.2.3"

check-username:
	@[ -n "$(DOCKER_USERNAME)" ] || { echo "Error: DOCKER_USERNAME is not set" >&2; exit 1; }

check-credentials: check-username
	@[ -n "$(DOCKER_PASSWORD)" ] || { echo "Error: DOCKER_PASSWORD is not set" >&2; exit 1; }

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

login: check-credentials
	echo "$(DOCKER_PASSWORD)" | docker login --username "$(DOCKER_USERNAME)" --password-stdin

push: builder login
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
