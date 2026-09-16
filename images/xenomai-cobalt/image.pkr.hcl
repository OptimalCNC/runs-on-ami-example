packer {
  required_version = "= 1.16.0"
  required_plugins {
    qemu = {
      source  = "github.com/hashicorp/qemu"
      version = "= 1.1.6"
    }
  }
}

variable "source_url" { type = string }
variable "source_sha256" { type = string }
variable "cpus" { type = number }
variable "memory_mib" { type = number }
variable "accelerator" { type = string }
variable "firmware_code" { type = string }
variable "firmware_vars" { type = string }
variable "seed_iso" { type = string }
variable "ssh_private_key_file" { type = string }
variable "recipe_directory" { type = string }
variable "output_directory" { type = string }
variable "packer_output_directory" { type = string }

source "qemu" "kernel" {
  iso_url              = var.source_url
  iso_checksum         = "sha256:${var.source_sha256}"
  disk_image           = true
  format               = "raw"
  vm_name              = "disk.raw"
  output_directory     = var.packer_output_directory
  disk_size            = "16G"
  disk_additional_size = ["16G"]
  disk_interface       = "virtio"
  disk_discard         = "unmap"
  disk_detect_zeroes   = "unmap"
  headless             = true
  accelerator          = var.accelerator
  cpus                 = var.cpus
  memory               = var.memory_mib
  machine_type         = "q35"
  net_device           = "virtio-net"
  efi_boot             = true
  efi_firmware_code    = var.firmware_code
  efi_firmware_vars    = var.firmware_vars
  efi_drop_efivars     = true
  ssh_username         = "ubuntu"
  ssh_private_key_file = var.ssh_private_key_file
  ssh_timeout          = "15m"
  shutdown_timeout     = "5m"
  # Finalization removes the build SSH key, so shut down in this same session.
  shutdown_command = "sudo bash /opt/ami-example-recipe/images/common/finalize-image.sh && sudo /sbin/shutdown -P now"
  qemuargs = [
    ["-cdrom", var.seed_iso],
    ["-serial", "file:${var.output_directory}/serial.log"],
  ]
}

build {
  sources = ["source.qemu.kernel"]
  provisioner "shell" {
    inline = ["cloud-init status --wait"]
  }
  provisioner "file" {
    source      = var.recipe_directory
    destination = "/tmp/ami-example-recipe"
  }
  provisioner "shell" {
    inline = [
      "sudo mv /tmp/ami-example-recipe /opt/ami-example-recipe",
      "sudo bash /opt/ami-example-recipe/images/xenomai-cobalt/provision.sh"
    ]
  }
  provisioner "file" {
    direction   = "download"
    source      = "/var/lib/ami-example/parent-inventory.json"
    destination = "${var.output_directory}/parent-inventory.json"
  }
  provisioner "file" {
    direction   = "download"
    source      = "/etc/ami-example.json"
    destination = "${var.output_directory}/image-manifest.json"
  }
}
