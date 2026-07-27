# State backend: local by default, so `terraform init -backend=false && terraform
# validate` (or a first `terraform plan`) works with zero setup and no bucket has
# to exist first. For a shared environment, provision a protected state
# bucket through the organization's bootstrap process, set its exact name in
# the commented block, then run `terraform init -migrate-state`.
terraform {
  backend "local" {}

  # backend "gcs" {
  #   bucket = "agentcare-terraform-state" # create by hand once, outside this config
  #   prefix = "terraform/state"
  # }
}
