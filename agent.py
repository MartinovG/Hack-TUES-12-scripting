import os
import sys
import shutil
import platform
import subprocess
import time

import asyncio
import websockets

try:
    import psutil
except ImportError:
    print("[*] Missing 'psutil' library. Installing it automatically...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "psutil"])
    import psutil

BACKEND_URL = "ws://localhost:8765"

async def connect_to_websocket():
    while True:
        try:
            print(f"[*] Attempting to connect to {BACKEND_URL}...")
            async with websockets.connect(
                BACKEND_URL,
                ping_interval=20,
                ping_timeout=20,
                close_timeout=10
            ) as websocket:
                print("[+] Connected to server.")
                
                await websocket.send(f"Client Node: {platform.node()} is active")

                while True:
                    try:
                        response = await websocket.recv()
                        print(f"[*] Message from server: {response}")
                    except websockets.exceptions.ConnectionClosed:
                        print("[!] Connection closed by server. Retrying...")
                        break 
        
        except (OSError, websockets.exceptions.InvalidURI, Exception) as e:
            print(f"[!] Connection failed: {e}. Retrying in 5 seconds...")
            await asyncio.sleep(5) 

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
            "1": {"name": "Alpine Linux (ARM64)", "box": "generic/alpine316"},
            "2": {"name": "Ubuntu (ARM64)", "box": "perk/ubuntu-2204-arm64"}, 
            "3": {"name": "Debian 12 (ARM64)", "box": "bento/debian-12-arm64"}
        }
    else:
        print(f"[!] Detected {os_type} (x86_64). Using standard boxes.")
        os_options = {
            "1": {"name": "Alpine Linux", "box": "generic/alpine316"},
            "2": {"name": "Windows 10", "box": "gusztavvargadr/windows-10"},
            "3": {"name": "Linux (Ubuntu)", "box": "ubuntu/jammy64"}
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
        vm_mem, vm_cpu = 1024, max(1, cpu_cores - 1)

    print(f"[*] Hardware Profile: Allocated {vm_mem}MB RAM and {vm_cpu} CPU core(s) to the VM.")
    return {"memory": vm_mem, "cpus": vm_cpu}

def check_and_install_dependencies():
    os_type, is_arm = get_system_info()
    print(f"[*] Performing dependency checks for {os_type}...")
    
    if not shutil.which("vagrant"):
        print("[*] Vagrant is missing. Attempting to install...")
        if os_type == "Darwin":
            subprocess.run(["brew", "install", "hashicorp/tap/hashicorp-vagrant"], check=False)
        elif os_type == "Linux":
            subprocess.run(["sudo", "apt-get", "update"], check=False)
            subprocess.run(["sudo", "apt-get", "install", "-y", "vagrant"], check=False)
        elif os_type == "Windows":
            print("[i] Attempting to install Vagrant via winget (Admin prompt will appear)...")
            subprocess.run(["powershell", "-Command", "Start-Process winget -ArgumentList 'install Hashicorp.Vagrant --accept-package-agreements --accept-source-agreements' -Verb RunAs -Wait"], check=False)
    
    if is_arm:
        if not shutil.which("qemu-system-aarch64") and not shutil.which("qemu-system-arm"):
            print("[*] QEMU is missing. Attempting to install...")
            if os_type == "Darwin":
                subprocess.run(["brew", "install", "qemu"], check=False)
            elif os_type == "Linux":
                subprocess.run(["sudo", "apt-get", "install", "-y", "qemu-system", "qemu-utils"], check=False)
        
        if shutil.which("vagrant"):
            try:
                plugins = subprocess.check_output(["vagrant", "plugin", "list"]).decode(errors="ignore")
                if "vagrant-qemu" not in plugins:
                    print("[*] Installing 'vagrant-qemu' plugin...")
                    subprocess.run(["vagrant", "plugin", "install", "vagrant-qemu"], check=False)
            except Exception as e:
                print(f"[!] Could not check/install vagrant-qemu plugin: {e}")
    else:
        if os_type == "Windows":
            print("[i] Ensuring Windows Hyper-V is enabled for optimal performance...")
            subprocess.run(["powershell", "-Command", "Start-Process powershell -ArgumentList 'Enable-WindowsOptionalFeature -Online -FeatureName Microsoft-Hyper-V -All -NoRestart' -Verb RunAs -Wait"], check=False)
        elif os_type == "Linux":
            if not shutil.which("virsh"):
                print("[*] KVM/Libvirt is missing. Attempting to install for optimal performance...")
                subprocess.run(["sudo", "apt-get", "install", "-y", "qemu-kvm", "libvirt-daemon-system", "libvirt-clients", "bridge-utils"], check=False)
            
            if shutil.which("vagrant"):
                try:
                    plugins = subprocess.check_output(["vagrant", "plugin", "list"]).decode(errors="ignore")
                    if "vagrant-libvirt" not in plugins:
                        print("[*] Installing 'vagrant-libvirt' plugin...")
                        subprocess.run(["vagrant", "plugin", "install", "vagrant-libvirt"], check=False)
                except Exception as e:
                    print(f"[!] Could not check/install vagrant-libvirt plugin: {e}")
        elif os_type == "Darwin":
            vbox_cmd = "vboxmanage"
            if not shutil.which(vbox_cmd) and not shutil.which("VBoxManage"):
                print("[*] VirtualBox is missing. Attempting to install...")
                subprocess.run(["brew", "install", "--cask", "virtualbox"], check=False)

def run_vagrant(specs, box_name):
    os_type, is_arm = get_system_info()
    
    env = os.environ.copy()
    env["HIVE_VM_MEM"] = str(specs["memory"])
    env["HIVE_VM_CPU"] = str(specs["cpus"])
    env["HIVE_VM_BOX"] = box_name 
    
    if is_arm:
        provider = "qemu"
    else:
        if os_type == "Windows":
            provider = "hyperv"
        elif os_type == "Linux":
            provider = "libvirt"
        else:
            provider = "virtualbox"  
            
    vagrant_cmd = shutil.which("vagrant") or "vagrant"
    
    try:
        status_output = subprocess.check_output([vagrant_cmd, "status"], text=True)
        if "running" in status_output:
            print("[*] VM is already running. Skipping provision...")
            print("[i] Hint: You can use 'vagrant ssh' to connect to the VM.")
            return True
            
        print(f"[*] Provisioning {box_name} using {provider} on {os_type}...")
        subprocess.run([vagrant_cmd, "up", f"--provider={provider}"], env=env, check=True)
        print("[+] VM is live. You can connect using: vagrant ssh")
        return True
    except subprocess.CalledProcessError as e:
        print(f"[!] Vagrant process returned non-zero exit status: {e.returncode}")
        return False
    except Exception as e:
        print(f"[!] Target error: {e}")
        if os_type == "Windows":
            print("[i] Hint: You may need to restart your terminal or run it as Administrator.")
        return False

def wait_for_activation():
    flag_file = "state.txt"
    print(f"[*] Polling for active state... (Waiting for a file named '{flag_file}' to contain 'active')")
    while True:
        if os.path.exists(flag_file):
            try:
                with open(flag_file, "r") as f:
                    content = f.read().strip().lower()
                if content == "active":
                    print("\n[+] Active state flag detected! Proceeding to OS selection...")
                    break
            except Exception:
                pass
        time.sleep(2)

if __name__ == "__main__":
    try:
        check_and_install_dependencies()
        wait_for_activation()
        os_opts = select_os_menu()
        hw_specs = get_hardware_profile()
        
        success = run_vagrant(hw_specs, os_opts)
        
        if not success:
            print("[!] VM boot failed/was interrupted. Proceeding to websocket layer anyway...")
        else:
            print("[+] Provisioning complete. Keeping websocket connection active...")
            
        asyncio.run(connect_to_websocket())
    except KeyboardInterrupt:
        print("\n[!] Operation cancelled by user.")
        sys.exit(0)