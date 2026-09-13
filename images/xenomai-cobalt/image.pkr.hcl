packer {
  required_version = "= 1.16.0"
  required_plugins {
    amazon = {
      source  = "github.com/hashicorp/amazon"
      version = "= 1.8.2"
    }
  }
}

variable "region" { type = string }
variable "source_ami" { type = string }
variable "instance_type" { type = string }
variable "subnet_id" { type = string }
variable "security_group_id" { type = string }
variable "builder_profile_name" { type = string }
variable "root_device_name" { type = string }
variable "root_volume_gib" { type = number }
variable "ami_name" { type = string }
variable "recipe_directory" { type = string }
variable "output_directory" { type = string }
variable "build_tags" { type = map(string) }
variable "candidate_tags" { type = map(string) }
variable "associate_public_ip_address" { type = bool }
variable "ssh_keypair_name" { type = string }
variable "ssh_private_key_file" { type = string }
variable "stock_only" {
  type    = bool
  default = false
}

source "amazon-ebs" "kernel" {
  region                      = var.region
  source_ami                  = var.source_ami
  instance_type               = var.instance_type
  subnet_id                   = var.subnet_id
  security_group_id           = var.security_group_id
  iam_instance_profile        = var.builder_profile_name
  associate_public_ip_address = var.associate_public_ip_address
  ssh_username                = "ubuntu"
  ssh_interface               = "session_manager"
  ssh_timeout                 = "15m"
  ssh_keypair_name            = var.ssh_keypair_name
  ssh_private_key_file        = var.ssh_private_key_file
  user_data_file              = "images/common/packer-user-data.sh"
  ami_name                    = var.ami_name
  ami_description             = "Disposable custom kernel qualification candidate"
  ami_virtualization_type     = "hvm"
  skip_create_ami             = var.stock_only
  shutdown_behavior           = "stop"
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
    instance_metadata_tags      = "disabled"
  }
  launch_block_device_mappings {
    device_name           = var.root_device_name
    volume_type           = "gp3"
    volume_size           = var.root_volume_gib
    encrypted             = true
    kms_key_id            = "alias/aws/ebs"
    delete_on_termination = true
  }
  run_tags        = var.build_tags
  run_volume_tags = var.build_tags
  tags            = var.candidate_tags
  snapshot_tags   = var.candidate_tags
  aws_polling {
    delay_seconds = 15
    max_attempts  = 120
  }
}

build {
  sources = ["source.amazon-ebs.kernel"]
  provisioner "file" {
    source      = var.recipe_directory
    destination = "/tmp/ami-example-recipe"
  }
  provisioner "shell" {
    inline = [
      "sudo mv /tmp/ami-example-recipe /opt/ami-example-recipe",
      "sudo env STOCK_ONLY=${var.stock_only} bash /opt/ami-example-recipe/images/xenomai-cobalt/provision.sh"
    ]
  }
  provisioner "file" {
    direction   = "download"
    source      = "/var/lib/ami-example/parent-inventory.json"
    destination = "${var.output_directory}/parent-inventory.json"
  }
  # Stock qualification intentionally creates no snapshot or custom kernel.
  dynamic "provisioner" {
    for_each = var.stock_only ? [] : [1]
    labels   = ["file"]
    content {
      direction   = "download"
      source      = "/etc/ami-example.json"
      destination = "${var.output_directory}/image-manifest.json"
    }
  }
  dynamic "provisioner" {
    for_each = var.stock_only ? [] : [1]
    labels   = ["file"]
    content {
      direction   = "download"
      source      = "/tmp/ami-example-inputs.tar"
      destination = "${var.output_directory}/inputs.tar"
    }
  }
  dynamic "provisioner" {
    for_each = var.stock_only ? [] : [1]
    labels   = ["shell"]
    content {
      inline = ["sudo bash /opt/ami-example-recipe/images/common/finalize-image.sh"]
    }
  }
  post-processor "manifest" {
    output     = "${var.output_directory}/packer-manifest.json"
    strip_path = true
  }
}
