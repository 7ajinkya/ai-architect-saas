# Key-value table capability.
#
# The contract takes scalar key names and scalar key types, and derives the
# attribute declarations itself. Callers therefore never express Terraform
# `attribute` blocks, which is precisely why the frozen 4A structured
# parameters can be projected into plain scalars instead of being
# interpreted as HCL.

locals {
  sort_key_enabled = var.range_key_name != null

  attributes = concat(
    [{ name = var.hash_key_name, type = var.hash_key_type }],
    var.range_key_name == null ? [] : [{
      name = var.range_key_name
      type = var.range_key_type
    }],
  )
}

resource "aws_dynamodb_table" "this" {
  name         = var.table_name
  billing_mode = var.billing_mode
  hash_key     = var.hash_key_name
  range_key    = var.range_key_name

  dynamic "attribute" {
    for_each = local.attributes

    content {
      name = attribute.value.name
      type = attribute.value.type
    }
  }

  point_in_time_recovery {
    enabled = var.point_in_time_recovery
  }
}