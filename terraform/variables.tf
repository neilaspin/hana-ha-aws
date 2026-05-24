variable "aws_region" {
  default = "us-east-2"
}

variable "key_pair_name" {
  default = "HANA_DEP"
}

variable "hana_media_bucket" {
  default = "cloudhanabucket"
}

variable "private_zone_name" {
  default = "hana.internal"
}

variable "hana_sid" {
  default = "HDB"
}

variable "hana_instance" {
  default = "00"
}
