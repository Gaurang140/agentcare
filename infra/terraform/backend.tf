# State backend: local by default, so `tofu init -backend=false && tofu
# validate` (or a first `tofu plan`) works with zero setup and no bucket has
# to exist first. For a shared environment, provision a protected state
# bucket through the organization's bootstrap process, set its exact name in
# the commented block, then run `tofu init -migrate-state`.
terraform {
  backend "local" {}

  # backend "gcs" {
  #   bucket = "agentcare-tofu-state" # create by hand once, outside this config
  #   prefix = "terraform/state"
  # }
}
