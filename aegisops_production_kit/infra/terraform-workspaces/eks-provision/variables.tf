variable "region" {
  type    = string
  default = "us-east-1"
}

variable "cluster_name" {
  type = string
}

variable "kubernetes_version" {
  type    = string
  default = "1.29"
}

variable "vpc_id" {
  type = string
}

variable "subnet_ids" {
  type = list(string)
}

variable "instance_types" {
  type    = list(string)
  default = ["m6i.xlarge"]
}

variable "desired_size" {
  type    = number
  default = 3
}

variable "min_size" {
  type    = number
  default = 3
}

variable "max_size" {
  type    = number
  default = 6
}
