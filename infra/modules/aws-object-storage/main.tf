# Durable object storage capability.
#
# Versioning, encryption and public-access blocking are implemented here
# rather than as sibling resources because they are properties of the
# capability, not independent choices. That is the structural reason this
# is a verified module instead of raw HCL: the security posture cannot be
# partially applied by a caller who forgets one of four arguments.

locals {
  block_public_access = var.access_posture == "all_blocked"
  versioning_status   = title(var.versioning_status)
  sse_algorithm       = upper(var.sse_algorithm)
}

resource "aws_s3_bucket" "this" {
  bucket = var.bucket_name
}

resource "aws_s3_bucket_versioning" "this" {
  bucket = aws_s3_bucket.this.id

  versioning_configuration {
    status = local.versioning_status
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "this" {
  bucket = aws_s3_bucket.this.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = local.sse_algorithm
    }
  }
}

resource "aws_s3_bucket_public_access_block" "this" {
  bucket = aws_s3_bucket.this.id

  block_public_acls       = local.block_public_access
  block_public_policy     = local.block_public_access
  ignore_public_acls      = local.block_public_access
  restrict_public_buckets = local.block_public_access
}