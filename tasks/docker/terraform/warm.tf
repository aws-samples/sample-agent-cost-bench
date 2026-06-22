# Providers to pre-warm into the offline mirror at image-build time.
# Tasks should use compatible constraints (e.g. aws ~> 5.0) so the mirrored
# versions satisfy `terraform init` offline.
terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    null = {
      source  = "hashicorp/null"
      version = "~> 3.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.0"
    }
  }
}
