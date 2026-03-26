import os
import sys
import shutil
import platform
import psutil
import subprocess

def get_system_info():
    os_type = platform.system()
    arch = platform.machine().lower()
    is_arm = 'arm' in arch or 'aarch64' in arch
    return os_type, is_arm

def select_os_menu():
    os_type, is_arm = get_system_info()
    
    if is_arm:
        print(f"[!] Detected {os_type} (ARM64). Using ARM-compatible boxes.")
        os_options = {
            "1": {"name": "Ubuntu 22.04 (ARM64)", "box": "perk/ubuntu-2204-arm64"},
            "2": {"name": "Debian 12 (ARM64)", "box": "bento/debian-12-arm64"}, 
            "3": {"name": "Alpine Linux (ARM64)", "box": "generic/alpine316"}
        }
    else:
        print(f"[!] Detected {os_type} (x86_64). Using standard boxes.")
        os_options = {
            "1": {"name": "Ubuntu 22.04", "box": "ubuntu/jammy64"},
            "2": {"name": "Debian 12", "box": "debian/bookworm64"},
            "3": {"name": "Alpine Linux", "box": "generic/alpine316"}
        }

    print("\n" + "="*30)
    print(" HIVE PROVIDER: OS SELECTION ")
    print("="*30)
    for key, value in os_options.items():
        print(f"[{key}] {value['name']}")
    
    choice = input("\nSelect the OS: ").strip()
    selected = os_options.get(choice, os_options["1"])
    return selected["box"]

def get_hardware_profile():
    total_ram_gb = psutil.virtual_memory().total / (1024**3)
    cpu_cores = psutil.cpu_count(logical=False)
    
    if total_ram_gb >= 16:
        vm_mem, vm_cpu = 4096, max(1, cpu_cores - 2) 
    else:
        vm_mem, vm_cpu = 1024, 1

    return {"memory": vm_mem, "cpus": vm_cpu}

def run_vagrant(specs, box_name):
    os_type, is_arm = get_system_info()
    
    env = os.environ.copy()
    env["HIVE_VM_MEM"] = str(specs["memory"])
    env["HIVE_VM_CPU"] = str(specs["cpus"])
    env["HIVE_VM_BOX"] = box_name 
    
    provider = "qemu" if is_arm else "virtualbox"
    vagrant_cmd = shutil.which("vagrant") or "vagrant"
    
    try:
        print(f"[*] Provisioning {box_name} using {provider} on {os_type}...")
        subprocess.run([vagrant_cmd, "up", f"--provider={provider}"], env=env, check=True)
        print("[+] VM is live.")
    except Exception as e:
        print(f"[!] Error: {e}")
        if os_type == "Windows":
            print("[i] Hint: Ensure Vagrant is installed and available in your PATH environment variable.")

if __name__ == "__main__":
    selected_box = select_os_menu()
    hardware = get_hardware_profile()
    run_vagrant(hardware, selected_box)