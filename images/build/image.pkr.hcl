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
variable "source_checksum" { type = string }
variable "root_disk_gib" { type = number }
variable "build_disk_gib" { type = number }
variable "ssh_username" { type = string }
variable "guest_workspace" { type = string }
variable "prepare_command" { type = string }
variable "provision_commands" { type = list(string) }
variable "finalize_command" { type = string }
variable "cpus" { type = number }
variable "memory_mib" { type = number }
variable "accelerator" { type = string }
variable "seed_iso" { type = string }
variable "ssh_private_key_file" { type = string }
variable "recipe_directory" { type = string }
variable "output_directory" { type = string }
variable "packer_output_directory" { type = string }

source "qemu" "image" {
  iso_url              = var.source_url
  iso_checksum         = var.source_checksum
  disk_image           = true
  format               = "raw"
  vm_name              = "disk.raw"
  output_directory     = var.packer_output_directory
  disk_size            = "${var.root_disk_gib}G"
  disk_additional_size = ["${var.build_disk_gib}G"]
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
  efi_firmware_code    = "/usr/share/OVMF/OVMF_CODE_4M.fd"
  efi_firmware_vars    = "/usr/share/OVMF/OVMF_VARS_4M.fd"
  efi_drop_efivars     = true
  ssh_username         = var.ssh_username
  ssh_private_key_file = var.ssh_private_key_file
  ssh_timeout          = "15m"
  shutdown_timeout     = "5m"
  # Finalization removes the build SSH key, so shut down in this same session.
  shutdown_command = "${var.finalize_command} && sudo /sbin/shutdown -P now"
  qemuargs = [
    ["-cdrom", var.seed_iso],
    ["-serial", "file:${var.output_directory}/serial.log"],
  ]
}

build {
  sources = ["source.qemu.image"]
  provisioner "shell" {
    inline = ["cloud-init status --wait", var.prepare_command]
  }
  provisioner "file" {
    source      = "${var.recipe_directory}/"
    destination = var.guest_workspace
  }
  provisioner "shell" {
    inline = var.provision_commands
  }
}
