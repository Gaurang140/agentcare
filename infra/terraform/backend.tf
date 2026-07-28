# The bootstrap stack creates the versioned bucket. Its name is supplied at
# init time with `-backend-config="bucket=$TF_STATE_BUCKET"` so this source
# never embeds an environment account or globally unique bucket name.
terraform {
  backend "gcs" {
    prefix = "agentcare/production"
  }
}
