# AgentCare operator lifecycle.
#
# Thin wrappers around terraform, gcloud, kubectl and gh. Every target prints
# the command before running it, so the Makefile stays readable next to
# docs/deployment-gcp.md and docs/ci-cd.md and never becomes a second source
# of infrastructure truth. Change infrastructure in infra/terraform, not here.
#
# Usage:
#   make help
#   make gcp-bootstrap PROJECT_ID=... GITHUB_REPOSITORY_ID=... GITHUB_OWNER_ID=...
#   make gcp-up        PROJECT_ID=...
#   make gcp-status    PROJECT_ID=... PUBLIC_URL=https://...
#   make gcp-down      PROJECT_ID=...

SHELL := /bin/bash
.DEFAULT_GOAL := help

# Configuration comes from the environment or the command line.
PROJECT_ID ?=
REGION ?= europe-west3
TF_STATE_BUCKET ?= $(PROJECT_ID)-agentcare-tfstate
PUBLIC_URL ?=

# Required by gcp-bootstrap only. Both are the immutable numeric GitHub IDs
# from: gh api repos/OWNER/REPOSITORY --jq '{repository_id: .id, owner_id: .owner.id}'
GITHUB_REPOSITORY_ID ?=
GITHUB_OWNER_ID ?=

BOOTSTRAP_DIR := infra/bootstrap
TERRAFORM_DIR := infra/terraform
K8S_OVERLAY := infra/k8s/overlays/gcp

BOOTSTRAP_PLAN := /tmp/agentcare-bootstrap.tfplan
MAIN_PLAN := /tmp/agentcare.tfplan
DESTROY_PLAN := /tmp/agentcare-destroy.tfplan

DOCUMENTS_BUCKET_URI = gs://$(PROJECT_ID)-agentcare-documents

# Fails the target before any command touches Google Cloud.
define require_var
@if [ -z "$($(1))" ]; then \
	printf 'error: set %s=... for target %s\n' "$(1)" "$@" >&2; \
	printf 'example: make %s %s=VALUE\n' "$@" "$(1)" >&2; \
	exit 1; \
fi
endef

# Destructive targets ask for the project id in full. A typo stops the run.
define confirm_project
@printf 'this destroys AgentCare infrastructure in project %s\n' "$(PROJECT_ID)"; \
printf 'GKE, Cloud SQL, the load balancer, the documents bucket and its objects go away\n'; \
read -r -p "type the project id to continue: " confirmation; \
if [ "$$confirmation" != "$(PROJECT_ID)" ]; then \
	printf 'confirmation did not match %s. nothing was deleted\n' "$(PROJECT_ID)" >&2; \
	exit 1; \
fi
endef

.PHONY: help gcp-bootstrap gcp-up gcp-status gcp-down gcp-cleanup gcp-github-vars

help:
	@echo "AgentCare operator targets. All of them need PROJECT_ID and accept REGION (default $(REGION))."
	@echo ""
	@echo "  gcp-bootstrap     once per Google account and GitHub repository: state bucket, APIs, deploy and infrastructure identities"
	@echo "  gcp-up            plan and apply the main stack, print outputs, then dispatch the application release"
	@echo "  gcp-status        Terraform outputs, cluster, workloads and public health"
	@echo "  gcp-down          delete the application and destroy the main stack after typed confirmation"
	@echo "  gcp-cleanup       retry the destroy for resources Google released later, safe to repeat"
	@echo "  gcp-github-vars   copy Terraform outputs into the GitHub production environment and enable deploys"
	@echo "  help              this list"
	@echo ""
	@echo "Runbooks: docs/deployment-gcp.md (infrastructure) and docs/ci-cd.md (releases)."

# One-time trust setup. The bootstrap state stays local and ignored, so this
# runs from a checkout that already has an authenticated gcloud and ADC.
gcp-bootstrap:
	$(call require_var,PROJECT_ID)
	$(call require_var,GITHUB_REPOSITORY_ID)
	$(call require_var,GITHUB_OWNER_ID)
	@echo "+ gcloud services enable serviceusage.googleapis.com --project=$(PROJECT_ID)"
	@gcloud services enable serviceusage.googleapis.com --project="$(PROJECT_ID)"
	@echo "+ terraform -chdir=$(BOOTSTRAP_DIR) init"
	@terraform -chdir=$(BOOTSTRAP_DIR) init
	@echo "+ terraform -chdir=$(BOOTSTRAP_DIR) plan -out=$(BOOTSTRAP_PLAN) -var=project_id=$(PROJECT_ID) -var=region=$(REGION) -var=github_repository_id=$(GITHUB_REPOSITORY_ID) -var=github_repository_owner_id=$(GITHUB_OWNER_ID)"
	@terraform -chdir=$(BOOTSTRAP_DIR) plan \
		-out=$(BOOTSTRAP_PLAN) \
		-var="project_id=$(PROJECT_ID)" \
		-var="region=$(REGION)" \
		-var="github_repository_id=$(GITHUB_REPOSITORY_ID)" \
		-var="github_repository_owner_id=$(GITHUB_OWNER_ID)"
	@echo "+ terraform -chdir=$(BOOTSTRAP_DIR) apply $(BOOTSTRAP_PLAN)"
	@terraform -chdir=$(BOOTSTRAP_DIR) apply $(BOOTSTRAP_PLAN)
	@echo "+ terraform -chdir=$(BOOTSTRAP_DIR) output"
	@terraform -chdir=$(BOOTSTRAP_DIR) output
	@echo ""
	@echo "set these once in GitHub, then never again:"
	@echo "  environment production        GCP_WORKLOAD_IDENTITY_PROVIDER = workload_identity_provider"
	@echo "  environment production        GCP_DEPLOYER_SERVICE_ACCOUNT   = deployer_service_account_email"
	@echo "  environment production-infra  GCP_WORKLOAD_IDENTITY_PROVIDER = workload_identity_provider"
	@echo "  environment production-infra  GCP_INFRA_SERVICE_ACCOUNT      = infra_service_account_email"
	@echo "  environment production-infra  TF_STATE_BUCKET                = terraform_state_bucket"
	@echo "both environments also need GCP_PROJECT_ID and GCP_REGION, which are your choices rather than outputs."
	@echo "production-infra needs a required reviewer. it can plan, apply and destroy infrastructure."
	@echo "keep infra_service_account_email out of the production environment. only the infrastructure workflow may use it."
	@echo ""
	@echo "keep the state bucket name in your shell for the next targets:"
	@echo "  export TF_STATE_BUCKET=\"\$$(terraform -chdir=$(BOOTSTRAP_DIR) output -raw terraform_state_bucket)\""
	@echo "next: make gcp-up PROJECT_ID=$(PROJECT_ID)"

# Create or update the main stack, then ask GitHub for an application release.
gcp-up:
	$(call require_var,PROJECT_ID)
	@echo "+ terraform -chdir=$(TERRAFORM_DIR) init -input=false -backend-config=\"bucket=$(TF_STATE_BUCKET)\""
	@terraform -chdir=$(TERRAFORM_DIR) init -input=false -backend-config="bucket=$(TF_STATE_BUCKET)"
	@echo "+ terraform -chdir=$(TERRAFORM_DIR) plan -out=$(MAIN_PLAN) -var=project_id=$(PROJECT_ID) -var=region=$(REGION)"
	@terraform -chdir=$(TERRAFORM_DIR) plan \
		-out=$(MAIN_PLAN) \
		-var="project_id=$(PROJECT_ID)" \
		-var="region=$(REGION)"
	@echo "+ terraform -chdir=$(TERRAFORM_DIR) apply $(MAIN_PLAN)"
	@terraform -chdir=$(TERRAFORM_DIR) apply $(MAIN_PLAN)
	@echo "+ terraform -chdir=$(TERRAFORM_DIR) output"
	@terraform -chdir=$(TERRAFORM_DIR) output
	@echo ""
	@echo "point the public host at ingress_ip_address before the first release."
	@echo "run 'make gcp-github-vars PROJECT_ID=$(PROJECT_ID)' if the cluster, bucket or Model Armor template changed."
	@echo "+ gh auth status"
	@if gh auth status >/dev/null 2>&1; then \
		echo "+ gh workflow run ci.yml --ref main"; \
		if gh workflow run ci.yml --ref main; then \
			echo "dispatched ci.yml on main. it runs every gate, builds both images from that commit, runs the migration Job and waits for the backend and frontend rollouts"; \
			echo "the deploy job is skipped while the repository variable DEPLOY_ENABLED is not exactly 'true'"; \
			echo "watch it: gh run watch"; \
		else \
			echo "gh could not dispatch ci.yml. push a commit to main instead, or rerun: gh workflow run ci.yml --ref main"; \
		fi; \
	else \
		echo "gh is not authenticated. run 'gh auth login', then 'gh workflow run ci.yml --ref main', or push a commit to main"; \
	fi

# Read-only. Every step tolerates a missing stack, cluster or context.
gcp-status:
	$(call require_var,PROJECT_ID)
	@echo "+ terraform -chdir=$(TERRAFORM_DIR) init -input=false -backend-config=\"bucket=$(TF_STATE_BUCKET)\""
	@terraform -chdir=$(TERRAFORM_DIR) init -input=false -backend-config="bucket=$(TF_STATE_BUCKET)" \
		|| echo "state bucket $(TF_STATE_BUCKET) is not reachable. run make gcp-bootstrap first"
	@echo "+ terraform -chdir=$(TERRAFORM_DIR) output"
	@terraform -chdir=$(TERRAFORM_DIR) output \
		|| echo "no Terraform outputs. the main stack has not been applied from this state"
	@echo "+ gcloud container clusters list --project=$(PROJECT_ID) --filter=name:agentcare"
	@gcloud container clusters list --project="$(PROJECT_ID)" --filter="name:agentcare" \
		|| echo "could not list clusters. check gcloud auth and the project"
	@echo "+ kubectl get deployment,job,ingress"
	@kubectl get deployment,job,ingress \
		|| echo "no working kubectl context. run: gcloud container clusters get-credentials CLUSTER --region $(REGION) --project $(PROJECT_ID)"
	@if [ -n "$(PUBLIC_URL)" ]; then \
		echo "+ curl -fsS --max-time 10 \"$(PUBLIC_URL)/api/health\""; \
		curl -fsS --max-time 10 "$(PUBLIC_URL)/api/health" && echo "" \
			|| echo "public health check failed. the managed certificate or DNS may still be pending"; \
	else \
		echo "PUBLIC_URL is not set, skipping the public health check. set PUBLIC_URL=https://your-host to include it"; \
	fi

# Destroy the environment. The bootstrap state bucket and the GitHub identity
# are a separate stack and stay, so the application can come back later.
# The documents bucket keeps deletion protection while it holds objects, so the
# objects go first and Terraform then removes the empty bucket.
gcp-down:
	$(call require_var,PROJECT_ID)
	$(call confirm_project)
	@echo "+ kubectl delete -k $(K8S_OVERLAY) --ignore-not-found=true"
	@kubectl delete -k $(K8S_OVERLAY) --ignore-not-found=true \
		|| echo "could not delete the application overlay. continuing, the Terraform destroy still runs"
	@echo "+ kubectl delete job backend-migrate --ignore-not-found=true"
	@kubectl delete job backend-migrate --ignore-not-found=true \
		|| echo "could not delete the migration Job. continuing"
	@echo "deleting document objects is irreversible. the bucket refuses deletion while objects remain."
	@echo "+ gcloud storage rm --recursive \"$(DOCUMENTS_BUCKET_URI)/**\""
	@gcloud storage rm --recursive "$(DOCUMENTS_BUCKET_URI)/**" || true
	@echo "+ terraform -chdir=$(TERRAFORM_DIR) init -input=false -backend-config=\"bucket=$(TF_STATE_BUCKET)\""
	@terraform -chdir=$(TERRAFORM_DIR) init -input=false -backend-config="bucket=$(TF_STATE_BUCKET)"
	@echo "+ terraform -chdir=$(TERRAFORM_DIR) plan -destroy -out=$(DESTROY_PLAN) -var=project_id=$(PROJECT_ID) -var=region=$(REGION)"
	@terraform -chdir=$(TERRAFORM_DIR) plan -destroy \
		-out=$(DESTROY_PLAN) \
		-var="project_id=$(PROJECT_ID)" \
		-var="region=$(REGION)"
	@echo "+ terraform -chdir=$(TERRAFORM_DIR) apply $(DESTROY_PLAN)"
	@terraform -chdir=$(TERRAFORM_DIR) apply $(DESTROY_PLAN)
	@echo ""
	@echo "the bootstrap state bucket and the Workload Identity provider are untouched, so make gcp-up can rebuild this environment."
	@echo "Cloud SQL leaves its private producer network behind for a while. run make gcp-cleanup later."
	@echo "confirm the expensive resources are gone: gcloud container clusters list, gcloud sql instances list, gcloud compute forwarding-rules list"

# Google holds the private Cloud SQL producer resources after the instance is
# deleted, documented as up to about four days, so the service networking
# connection and its reserved range can survive the first destroy. This target
# runs the same destroy again. It is safe to repeat, and it is a no-op once the
# plan reports nothing left to destroy.
gcp-cleanup:
	$(call require_var,PROJECT_ID)
	@echo "this re-runs the destroy for resources Google held back after make gcp-down."
	@echo "Google documents up to about four days before the Cloud SQL producer network can be removed."
	$(call confirm_project)
	@echo "+ terraform -chdir=$(TERRAFORM_DIR) init -input=false -backend-config=\"bucket=$(TF_STATE_BUCKET)\""
	@terraform -chdir=$(TERRAFORM_DIR) init -input=false -backend-config="bucket=$(TF_STATE_BUCKET)"
	@echo "+ terraform -chdir=$(TERRAFORM_DIR) plan -destroy -out=$(DESTROY_PLAN) -var=project_id=$(PROJECT_ID) -var=region=$(REGION)"
	@terraform -chdir=$(TERRAFORM_DIR) plan -destroy \
		-out=$(DESTROY_PLAN) \
		-var="project_id=$(PROJECT_ID)" \
		-var="region=$(REGION)"
	@echo "+ terraform -chdir=$(TERRAFORM_DIR) apply $(DESTROY_PLAN)"
	@terraform -chdir=$(TERRAFORM_DIR) apply $(DESTROY_PLAN)
	@echo ""
	@echo "a leftover google_service_networking_connection or reserved range is the documented wait, not a broken configuration. run this again tomorrow."

# Copy the three Terraform outputs the release reads into the GitHub
# production environment. Everything else in that environment is a decision,
# not an output, so it stays manual. DEPLOY_ENABLED is a repository variable
# rather than an environment variable because the deploy job reads it in a
# job-level if, which is evaluated before the environment is resolved.
gcp-github-vars:
	$(call require_var,PROJECT_ID)
	@echo "+ gh auth status"
	@gh auth status
	@echo "+ terraform -chdir=$(TERRAFORM_DIR) init -input=false -backend-config=\"bucket=$(TF_STATE_BUCKET)\""
	@terraform -chdir=$(TERRAFORM_DIR) init -input=false -backend-config="bucket=$(TF_STATE_BUCKET)"
	@cluster="$$(terraform -chdir=$(TERRAFORM_DIR) output -raw gke_cluster_name 2>/dev/null)"; \
	if [ -z "$$cluster" ]; then \
		echo "error: gke_cluster_name has no value. run make gcp-up first" >&2; \
		exit 1; \
	fi; \
	echo "+ gh variable set GKE_CLUSTER --env production --body \"$$cluster\""; \
	gh variable set GKE_CLUSTER --env production --body "$$cluster"
	@bucket="$$(terraform -chdir=$(TERRAFORM_DIR) output -raw documents_bucket_name 2>/dev/null)"; \
	if [ -z "$$bucket" ]; then \
		echo "error: documents_bucket_name has no value. run make gcp-up first" >&2; \
		exit 1; \
	fi; \
	echo "+ gh variable set DOCUMENTS_BUCKET --env production --body \"$$bucket\""; \
	gh variable set DOCUMENTS_BUCKET --env production --body "$$bucket"
	@template="$$(terraform -chdir=$(TERRAFORM_DIR) output -raw model_armor_template_name 2>/dev/null)"; \
	if [ -z "$$template" ]; then \
		echo "model_armor_template_name has no value: Model Armor is disabled in this stack. the deploy job requires MODEL_ARMOR_TEMPLATE to be non-empty, so releases stop at its configuration check until the stack is applied with enable_model_armor = true"; \
	else \
		echo "+ gh variable set MODEL_ARMOR_TEMPLATE --env production --body \"$$template\""; \
		gh variable set MODEL_ARMOR_TEMPLATE --env production --body "$$template"; \
	fi
	@echo "+ gh variable set DEPLOY_ENABLED --body true"
	@gh variable set DEPLOY_ENABLED --body "true"
	@echo "DEPLOY_ENABLED=true lets the application deploy job run. set it to anything else to keep CI while deployment stops."
	@echo ""
	@echo "these stay manual decisions and are not Terraform outputs of this stack:"
	@echo "  GCP_PROJECT_ID and GCP_REGION      the account and region you chose"
	@echo "  GCP_WORKLOAD_IDENTITY_PROVIDER     bootstrap output, set once"
	@echo "  GCP_DEPLOYER_SERVICE_ACCOUNT       bootstrap output, set once"
	@echo "  PUBLIC_URL                         the HTTPS origin you point at ingress_ip_address"
	@echo "  LLM_PROFILE                        a profile name from backend/llm.yaml"
