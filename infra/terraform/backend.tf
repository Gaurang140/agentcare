# State backend: local by default, so `tofu init -backend=false && tofu
# validate` (or a first `tofu plan`) works with zero setup and no bucket has
# to exist first. Switch to the commented `gcs` block once the one-time
# state-bucket step in docs/deployment-gcp.md has been run, then
# `tofu init -migrate-state`.
terraform {
  backend "local" {}

  # backend "gcs" {
  #   bucket = "agentcare-tofu-state" # create by hand once, outside this config
  #   prefix = "terraform/state"
  # }
}
