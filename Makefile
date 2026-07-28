# AgentCare operator lifecycle. Terraform owns infrastructure. GitHub Actions
# owns application releases. See docs/deployment-gcp.md and docs/ci-cd.md.

SHELL := /bin/bash
.DEFAULT_GOAL := help

PROJECT_ID ?=
REGION ?= europe-west3
GCS_LOCATION ?= $(REGION)
NETWORK_NAME ?= default
SUBNETWORK_NAME ?= default
ENABLE_CLOUD_SQL ?= true
ENABLE_MODEL_ARMOR ?= true
ENABLE_VERTEX_AI ?= false
# Variable sync defaults to keeping delivery disabled. A release must opt in
# with ENABLE_DELIVERY=true in the same command that dispatches it.
ENABLE_DELIVERY ?= false
TF_STATE_BUCKET ?= $(PROJECT_ID)-agentcare-tfstate

PUBLIC_URL ?=
LLM_PROFILE ?= groq
LANGFUSE_PUBLIC_KEY ?=
LANGFUSE_BASE_URL ?= https://cloud.langfuse.com
LANGFUSE_SAMPLE_RATE ?= 0

GITHUB_REPOSITORY_ID ?=
GITHUB_OWNER_ID ?=

BOOTSTRAP_DIR := infra/bootstrap
TERRAFORM_DIR := infra/terraform
K8S_OVERLAY := infra/k8s/overlays/gcp
K8S_PLATFORM := infra/k8s/platform

TF_VAR_ARGS := \
	-var="project_id=$(PROJECT_ID)" \
	-var="region=$(REGION)" \
	-var="gcs_location=$(GCS_LOCATION)" \
	-var="network_name=$(NETWORK_NAME)" \
	-var="subnetwork_name=$(SUBNETWORK_NAME)" \
	-var="enable_cloud_sql=$(ENABLE_CLOUD_SQL)" \
	-var="enable_model_armor=$(ENABLE_MODEL_ARMOR)" \
	-var="enable_vertex_ai=$(ENABLE_VERTEX_AI)"

define require_var
@if [ -z "$($(1))" ]; then \
	printf 'error: set %s=... for target %s\n' "$(1)" "$@" >&2; \
	exit 1; \
fi
endef

define require_project
@if [[ ! "$(PROJECT_ID)" =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$$ ]]; then \
	printf 'error: PROJECT_ID is not a valid Google Cloud project id: %s\n' "$(PROJECT_ID)" >&2; \
	exit 1; \
fi
endef

.PHONY: help gcp-bootstrap gcp-up gcp-release gcp-deploy gcp-status \
	gcp-down gcp-cleanup gcp-github-vars

help:
	@echo "AgentCare Google Cloud lifecycle"
	@echo ""
	@echo "  gcp-bootstrap    one-time APIs, remote state and keyless GitHub deploy identity"
	@echo "  gcp-up           review and apply the Terraform infrastructure plan"
	@echo "  gcp-release      sync disabled config, preflight, arm, dispatch ci.yml and wait (requires ENABLE_DELIVERY=true)"
	@echo "  gcp-deploy       run gcp-up then gcp-release"
	@echo "  gcp-status       show Terraform, the exact GKE cluster and optional public health"
	@echo "  gcp-down         review and destroy the application infrastructure"
	@echo "  gcp-cleanup      retry delayed service-networking deletion (Google can retain private service-networking for days)"
	@echo "  gcp-github-vars  sync non-secret release configuration and leave delivery disabled"
	@echo ""
	@echo "Required: PROJECT_ID (all lifecycle targets). gcp-bootstrap also requires GITHUB_REPOSITORY_ID and GITHUB_OWNER_ID."
	@echo "gcp-github-vars, gcp-release and gcp-deploy also require PUBLIC_URL; gcp-release and gcp-deploy require ENABLE_DELIVERY=true."
	@echo "Defaults: REGION, GCS_LOCATION, NETWORK_NAME, SUBNETWORK_NAME, TF_STATE_BUCKET, ENABLE_CLOUD_SQL, ENABLE_MODEL_ARMOR, ENABLE_VERTEX_AI, LLM_PROFILE and LANGFUSE_* (release targets require ENABLE_MODEL_ARMOR=true)."

gcp-bootstrap:
	$(call require_var,PROJECT_ID)
	$(call require_project)
	$(call require_var,GITHUB_REPOSITORY_ID)
	$(call require_var,GITHUB_OWNER_ID)
	@set -euo pipefail; \
	umask 077; \
	plan_dir="$$(mktemp -d)"; \
	plan_file="$$plan_dir/bootstrap.tfplan"; \
	cleanup() { rm -f "$$plan_file"; rmdir "$$plan_dir"; }; \
	trap cleanup EXIT; \
	echo "+ gcloud services enable serviceusage.googleapis.com --project=$(PROJECT_ID)"; \
	gcloud services enable serviceusage.googleapis.com --project="$(PROJECT_ID)"; \
	echo "+ terraform -chdir=$(BOOTSTRAP_DIR) init"; \
	terraform -chdir=$(BOOTSTRAP_DIR) init; \
	echo "+ terraform -chdir=$(BOOTSTRAP_DIR) plan -out=$$plan_file"; \
	terraform -chdir=$(BOOTSTRAP_DIR) plan \
		-out="$$plan_file" \
		-var="project_id=$(PROJECT_ID)" \
		-var="region=$(REGION)" \
		-var="github_repository_id=$(GITHUB_REPOSITORY_ID)" \
		-var="github_repository_owner_id=$(GITHUB_OWNER_ID)"; \
	terraform -chdir=$(BOOTSTRAP_DIR) show -no-color "$$plan_file"; \
	printf 'type apply to use this bootstrap plan: '; \
	read -r confirmation; \
	if [ "$$confirmation" != "apply" ]; then \
		echo "bootstrap cancelled"; \
		exit 1; \
	fi; \
	terraform -chdir=$(BOOTSTRAP_DIR) apply "$$plan_file"; \
	terraform -chdir=$(BOOTSTRAP_DIR) output; \
	echo ""; \
	echo "Next: export TF_STATE_BUCKET from terraform_state_bucket, then run make gcp-up."

gcp-up:
	$(call require_var,PROJECT_ID)
	$(call require_project)
	@set -euo pipefail; \
	umask 077; \
	plan_dir="$$(mktemp -d)"; \
	plan_file="$$plan_dir/infrastructure.tfplan"; \
	cleanup() { rm -f "$$plan_file"; rmdir "$$plan_dir"; }; \
	trap cleanup EXIT; \
	echo "+ terraform -chdir=$(TERRAFORM_DIR) init -backend-config=bucket=$(TF_STATE_BUCKET)"; \
	terraform -chdir=$(TERRAFORM_DIR) init -input=false -reconfigure \
		-backend-config="bucket=$(TF_STATE_BUCKET)"; \
	echo "+ terraform plan using the canonical AgentCare variable set"; \
	terraform -chdir=$(TERRAFORM_DIR) plan -input=false \
		-out="$$plan_file" $(TF_VAR_ARGS); \
	terraform -chdir=$(TERRAFORM_DIR) show -no-color "$$plan_file"; \
	printf 'type apply to use this exact infrastructure plan: '; \
	read -r confirmation; \
	if [ "$$confirmation" != "apply" ]; then \
		echo "infrastructure apply cancelled"; \
		exit 1; \
	fi; \
	terraform -chdir=$(TERRAFORM_DIR) apply -input=false "$$plan_file"; \
	cluster="$$(terraform -chdir=$(TERRAFORM_DIR) output -raw gke_cluster_name)"; \
	location="$$(terraform -chdir=$(TERRAFORM_DIR) output -raw gke_cluster_location)"; \
	gcloud container clusters get-credentials "$$cluster" \
		--region "$$location" --project "$(PROJECT_ID)"; \
	context="gke_$(PROJECT_ID)_$${location}_$${cluster}"; \
	echo "+ kubectl --context $$context kustomize $(K8S_PLATFORM) | kubectl apply -f -"; \
	kubectl --context "$$context" kustomize $(K8S_PLATFORM) \
		| sed "s/PROJECT_ID_PLACEHOLDER/$(PROJECT_ID)/g" \
		| kubectl --context "$$context" --namespace=agentcare apply -f -; \
	terraform -chdir=$(TERRAFORM_DIR) output; \
	echo ""; \
	echo "Infrastructure is ready. Database credentials, agentcare-secrets and DNS stay outside Terraform."; \
	echo "Complete those one-time steps in docs/deployment-gcp.md, then run make gcp-release ENABLE_DELIVERY=true."

gcp-github-vars:
	$(call require_var,PROJECT_ID)
	$(call require_project)
	$(call require_var,PUBLIC_URL)
	@gh auth status
	@gh variable set DEPLOY_ENABLED --body "false"
	@set -euo pipefail; \
	if [ "$(ENABLE_MODEL_ARMOR)" != "true" ]; then \
		echo "error: gcp-github-vars requires ENABLE_MODEL_ARMOR=true; release configuration cannot read a disabled Model Armor output" >&2; \
		exit 1; \
	fi
	@terraform -chdir=$(TERRAFORM_DIR) init -input=false -reconfigure \
		-backend-config="bucket=$(TF_STATE_BUCKET)"
	@set -euo pipefail; \
	cluster="$$(terraform -chdir=$(TERRAFORM_DIR) output -raw gke_cluster_name)"; \
	bucket="$$(terraform -chdir=$(TERRAFORM_DIR) output -raw documents_bucket_name)"; \
	template="$$(terraform -chdir=$(TERRAFORM_DIR) output -raw model_armor_template_name)"; \
	provider="$$(terraform -chdir=$(BOOTSTRAP_DIR) output -raw workload_identity_provider)"; \
	deployer="$$(terraform -chdir=$(BOOTSTRAP_DIR) output -raw deployer_service_account_email)"; \
	repository="$$(gh repo view --json nameWithOwner --jq .nameWithOwner)"; \
	for value in "$$cluster" "$$bucket" "$$template" "$$provider" "$$deployer" "$$repository"; do \
		if [ -z "$$value" ] || [ "$$value" = "null" ]; then \
			echo "error: required Terraform or GitHub output is empty" >&2; \
			exit 1; \
		fi; \
	done; \
	gh api --method PUT "repos/$$repository/environments/production" --silent; \
	gh variable set GCP_PROJECT_ID --env production --body "$(PROJECT_ID)"; \
	gh variable set GCP_REGION --env production --body "$(REGION)"; \
	gh variable set GCP_WORKLOAD_IDENTITY_PROVIDER --env production --body "$$provider"; \
	gh variable set GCP_DEPLOYER_SERVICE_ACCOUNT --env production --body "$$deployer"; \
	gh variable set GKE_CLUSTER --env production --body "$$cluster"; \
	gh variable set DOCUMENTS_BUCKET --env production --body "$$bucket"; \
	gh variable set MODEL_ARMOR_TEMPLATE --env production --body "$$template"; \
	gh variable set PUBLIC_URL --env production --body "$(PUBLIC_URL)"; \
	gh variable set LLM_PROFILE --env production --body "$(LLM_PROFILE)"; \
	if [ -n "$(LANGFUSE_PUBLIC_KEY)" ]; then \
		gh variable set LANGFUSE_PUBLIC_KEY --env production --body "$(LANGFUSE_PUBLIC_KEY)"; \
	else \
		gh variable delete LANGFUSE_PUBLIC_KEY --env production >/dev/null 2>&1 || true; \
	fi; \
	gh variable set LANGFUSE_BASE_URL --env production --body "$(LANGFUSE_BASE_URL)"; \
	gh variable set LANGFUSE_SAMPLE_RATE --env production --body "$(LANGFUSE_SAMPLE_RATE)"; \
	echo "GitHub production variables are synchronized. Delivery remains disabled; gcp-release arms it only after its preflights. No secret value was read or written."

gcp-release:
	$(call require_var,PROJECT_ID)
	$(call require_project)
	$(call require_var,PUBLIC_URL)
	@set -euo pipefail; \
	if [ "$(ENABLE_DELIVERY)" != "true" ]; then \
		echo "error: gcp-release requires ENABLE_DELIVERY=true to arm and dispatch production delivery" >&2; \
		exit 1; \
	fi; \
	if [ "$(ENABLE_MODEL_ARMOR)" != "true" ]; then \
		echo "error: gcp-release requires ENABLE_MODEL_ARMOR=true before reading the Model Armor Terraform output" >&2; \
		exit 1; \
	fi
	@$(MAKE) --no-print-directory gcp-github-vars ENABLE_DELIVERY="$(ENABLE_DELIVERY)"
	@set -euo pipefail; \
	cluster="$$(terraform -chdir=$(TERRAFORM_DIR) output -raw gke_cluster_name)"; \
	location="$$(terraform -chdir=$(TERRAFORM_DIR) output -raw gke_cluster_location)"; \
	gcloud container clusters get-credentials "$$cluster" \
		--region "$$location" --project "$(PROJECT_ID)"; \
	context="gke_$(PROJECT_ID)_$${location}_$${cluster}"; \
	kubectl --context "$$context" --namespace=agentcare get secret agentcare-secrets -o name >/dev/null || { \
		echo "error: agentcare-secrets is missing. Follow docs/deployment-gcp.md before release." >&2; \
		exit 1; \
	}; \
	local_sha="$$(git rev-parse main)"; \
	remote_sha="$$(git ls-remote origin refs/heads/main | awk '{print $$1}')"; \
	if [ "$$local_sha" != "$$remote_sha" ]; then \
		echo "error: local main is not the commit on origin/main. Push the reviewed commit first." >&2; \
		exit 1; \
	fi; \
	previous="$$(gh run list --workflow ci.yml --branch main --event workflow_dispatch \
		--limit 1 --json databaseId --jq '.[0].databaseId // ""')"; \
	delivery_armed=false; \
	release_committed=false; \
	rollback_delivery() { \
		status=$$?; \
		if [ "$$delivery_armed" != "true" ]; then \
			exit "$$status"; \
		fi; \
		if [ "$$release_committed" = "true" ]; then \
			exit "$$status"; \
		fi; \
		echo "::error::release failed after delivery was armed; attempting to disable it" >&2; \
		if gh variable set DEPLOY_ENABLED --body "false"; then \
			echo "delivery was disabled after the failed release" >&2; \
		else \
			echo "::error::automatic delivery may still be armed; manual recovery: gh variable set DEPLOY_ENABLED --body false" >&2; \
			if [ "$$status" -eq 0 ]; then exit 1; fi; \
		fi; \
		exit "$$status"; \
	}; \
	trap rollback_delivery EXIT; \
	delivery_armed=true; \
	gh variable set DEPLOY_ENABLED --body "true"; \
	gh workflow run ci.yml --ref main; \
	run_id=""; \
	for attempt in $$(seq 1 30); do \
		run_id="$$(gh run list --workflow ci.yml --branch main --event workflow_dispatch \
			--limit 1 --json databaseId --jq '.[0].databaseId // ""')"; \
		if [ -n "$$run_id" ] && [ "$$run_id" != "$$previous" ]; then break; fi; \
		sleep 2; \
	done; \
	if [ -z "$$run_id" ] || [ "$$run_id" = "$$previous" ]; then \
		echo "error: GitHub did not expose the dispatched run within 60 seconds" >&2; \
		exit 1; \
	fi; \
	echo "watching GitHub Actions run $$run_id"; \
	gh run watch "$$run_id" --exit-status; \
	gh run view "$$run_id" --json url --jq .url; \
	release_committed=true

gcp-deploy:
	@set -euo pipefail; \
	if [ "$(ENABLE_DELIVERY)" != "true" ]; then \
		echo "error: gcp-deploy requires ENABLE_DELIVERY=true because it ends by dispatching production delivery" >&2; \
		exit 1; \
	fi; \
	if [ "$(ENABLE_MODEL_ARMOR)" != "true" ]; then \
		echo "error: gcp-deploy requires ENABLE_MODEL_ARMOR=true because its release reads the Model Armor Terraform output" >&2; \
		exit 1; \
	fi
	@$(MAKE) --no-print-directory gcp-up
	@$(MAKE) --no-print-directory gcp-release

gcp-status:
	$(call require_var,PROJECT_ID)
	$(call require_project)
	@terraform -chdir=$(TERRAFORM_DIR) init -input=false -reconfigure \
		-backend-config="bucket=$(TF_STATE_BUCKET)"
	@terraform -chdir=$(TERRAFORM_DIR) output
	@set -euo pipefail; \
	cluster="$$(terraform -chdir=$(TERRAFORM_DIR) output -raw gke_cluster_name)"; \
	location="$$(terraform -chdir=$(TERRAFORM_DIR) output -raw gke_cluster_location)"; \
	gcloud container clusters get-credentials "$$cluster" \
		--region "$$location" --project "$(PROJECT_ID)"; \
	context="gke_$(PROJECT_ID)_$${location}_$${cluster}"; \
	echo "+ kubectl --context $$context --namespace=agentcare get deployment,job,ingress"; \
	kubectl --context "$$context" --namespace=agentcare get deployment,job,ingress; \
	if [ -n "$(PUBLIC_URL)" ]; then \
		echo "+ health check $(PUBLIC_URL)/api/health"; \
		curl -fsS --max-time 10 "$(PUBLIC_URL)/api/health"; \
		echo ""; \
	else \
		echo "PUBLIC_URL is unset. Public health was not checked."; \
	fi

gcp-down:
	$(call require_var,PROJECT_ID)
	$(call require_project)
	@set -euo pipefail; \
	umask 077; \
	plan_dir="$$(mktemp -d)"; \
	plan_file="$$plan_dir/destroy.tfplan"; \
	cleanup() { rm -f "$$plan_file"; rmdir "$$plan_dir"; }; \
	trap cleanup EXIT; \
	terraform -chdir=$(TERRAFORM_DIR) init -input=false -reconfigure \
		-backend-config="bucket=$(TF_STATE_BUCKET)"; \
	terraform -chdir=$(TERRAFORM_DIR) plan -destroy -input=false \
		-out="$$plan_file" $(TF_VAR_ARGS); \
	terraform -chdir=$(TERRAFORM_DIR) show -no-color "$$plan_file"; \
	printf 'this destroys AgentCare in project %s\ntype the full project id to continue: ' "$(PROJECT_ID)"; \
	read -r confirmation; \
	if [ "$$confirmation" != "$(PROJECT_ID)" ]; then \
		echo "confirmation did not match. nothing was deleted" >&2; \
		exit 1; \
	fi; \
	cluster="$$(terraform -chdir=$(TERRAFORM_DIR) output -raw gke_cluster_name)"; \
	location="$$(terraform -chdir=$(TERRAFORM_DIR) output -raw gke_cluster_location)"; \
	bucket="$$(terraform -chdir=$(TERRAFORM_DIR) output -raw documents_bucket_name)"; \
	if [ "$$bucket" != "$(PROJECT_ID)-agentcare-documents" ]; then \
		echo "error: refusing unexpected documents bucket $$bucket" >&2; \
		exit 1; \
	fi; \
	gcloud container clusters get-credentials "$$cluster" \
		--region "$$location" --project "$(PROJECT_ID)"; \
	context="gke_$(PROJECT_ID)_$${location}_$${cluster}"; \
	kubectl --context "$$context" --namespace=agentcare delete -k $(K8S_OVERLAY) --ignore-not-found=true; \
	kubectl --context "$$context" --namespace=agentcare delete job backend-migrate --ignore-not-found=true; \
	if gcloud storage ls "gs://$$bucket/**" >/dev/null 2>&1; then \
		echo "deleting every object from gs://$$bucket"; \
		gcloud storage rm "gs://$$bucket/**"; \
	else \
		echo "documents bucket is empty"; \
	fi; \
	terraform -chdir=$(TERRAFORM_DIR) apply -input=false "$$plan_file"; \
	echo "Main infrastructure is destroyed. Bootstrap state and keyless deployment trust remain."; \
	echo "If Google retains private service networking, retry with make gcp-cleanup."

gcp-cleanup:
	$(call require_var,PROJECT_ID)
	$(call require_project)
	@set -euo pipefail; \
	umask 077; \
	plan_dir="$$(mktemp -d)"; \
	plan_file="$$plan_dir/destroy.tfplan"; \
	cleanup() { rm -f "$$plan_file"; rmdir "$$plan_dir"; }; \
	trap cleanup EXIT; \
	terraform -chdir=$(TERRAFORM_DIR) init -input=false -reconfigure \
		-backend-config="bucket=$(TF_STATE_BUCKET)"; \
	terraform -chdir=$(TERRAFORM_DIR) plan -destroy -input=false \
		-out="$$plan_file" $(TF_VAR_ARGS); \
	terraform -chdir=$(TERRAFORM_DIR) show -no-color "$$plan_file"; \
	printf 'this destroys AgentCare in project %s\ntype the full project id to continue: ' "$(PROJECT_ID)"; \
	read -r confirmation; \
	if [ "$$confirmation" != "$(PROJECT_ID)" ]; then \
		echo "confirmation did not match. nothing was deleted" >&2; \
		exit 1; \
	fi; \
	terraform -chdir=$(TERRAFORM_DIR) apply -input=false "$$plan_file"
