import os
import platform
import psutil
import subprocess

def select_os_menu():
    os_options = {
        "1": {"name": "Ubuntu 20.04 (Standard)", "box": "ubuntu/focal64"},
        "2": {"name": "Debian 11 (Stable)", "box": "debian/bullseye64"},
        "3": {"name": "Alpine Linux (Ultra-Lightweight)", "box": "generic/alpine316"},
        "4": {"name": "CentOS 7 (Enterprise)", "box": "centos/7"}
    }

    print("\n" + "="*30)
    print(" HIVE PROVIDER: OS SELECTION ")
    print("="*30)
    for key, value in os_options.items():
        print(f"[{key}] {value['name']}")
    
    choice = input("\nSelect the OS you want to provide: ").strip()
    
    selected = os_options.get(choice, os_options["1"])
    print(f"[+] Selected: {selected['name']}\n")
    return selected["box"]

def get_hardware_profile():
    total_ram_gb = psutil.virtual_memory().total / (1024**3)
    cpu_cores = psutil.cpu_count(logical=False)
    
    # Allocation Logic
    if total_ram_gb >= 16:
        vm_mem, vm_cpu = 8192, max(1, cpu_cores - 2)
    elif total_ram_gb >= 8:
        vm_mem, vm_cpu = 4096, 2
    else:
        vm_mem, vm_cpu = 1024, 1

    return {"memory": vm_mem, "cpus": vm_cpu}

def run_vagrant(specs, box_name):
    env = os.environ.copy()
    env["HIVE_VM_MEM"] = str(specs["memory"])
    env["HIVE_VM_CPU"] = str(specs["cpus"])
    env["HIVE_VM_BOX"] = box_name 
    
    try:
        print(f"[*] Provisioning {box_name}...")
        subprocess.run(["vagrant", "up"], env=env, check=True)
        print("[+] VM is live.")
    except Exception as e:
        print(f"[!] Error: {e}")

if __name__ == "__main__":
    selected_box = select_os_menu()
    hardware = get_hardware_profile()
    run_vagrant(hardware, selected_box)