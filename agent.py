import os
import sys
import shutil
import platform
import subprocess
import time
import json
import base64
import datetime
import uuid

import asyncio
import websockets

try:
    import psutil
except ImportError:
    print("[*] Missing 'psutil' library. Installing it automatically...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "psutil"])
    import psutil

BACKEND_URL = "ws://localhost:8765"
current_vm_id = None

async def run_command_in_vm(command: str) -> dict:
    """Run a single command inside the Vagrant VM and return a structured result."""
    try:
        result = subprocess.run(
            ["vagrant", "ssh", "-c", command],
            capture_output=True,
            text=True,
            check=True
        )
        return {"success": True, "stdout": result.stdout.strip(), "stderr": result.stderr.strip()}
    except subprocess.CalledProcessError as e:
        return {"success": False, "stdout": e.stdout.strip() if e.stdout else "", "stderr": e.stderr.strip() if e.stderr else "", "code": e.returncode}
    except Exception as exc:
        return {"success": False, "stdout": "", "stderr": str(exc)}


def _os_options_map(is_arm: bool):
    if is_arm:
        return {
            "1": "generic/alpine316",
            "2": "perk/ubuntu-2204-arm64",
            "3": "bento/debian-12-arm64"
        }
    else:
        return {
            "1": "generic/alpine316",
            "2": "gusztavvargadr/windows-10",
            "3": "ubuntu/jammy64"
        }


def _resolve_box_choice(choice, is_arm: bool):
    # Accept either an index string/number or a direct box name
    if isinstance(choice, int):
        choice = str(choice)
    if not choice:
        return None
    mapping = _os_options_map(is_arm)
    # If user sent an index
    if isinstance(choice, str) and choice in mapping:
        return mapping[choice]
    # If looks like a vagrant box name, return as-is
    if isinstance(choice, str) and "/" in choice:
        return choice
    return None

def get_vagrant_ssh_info():
    """Extract VM SSH credentials dynamically via vagrant ssh-config."""
    info = {
        "ip_address": "127.0.0.1",
        "ssh_port": 2222,
        "ssh_username": "vagrant",
        "ssh_private_key_path": ""
    }
    try:
        result = subprocess.run(["vagrant", "ssh-config"], capture_output=True, text=True, check=True)
        for line in result.stdout.split('\n'):
            line = line.strip()
            if line.startswith("HostName "): info["ip_address"] = line.split()[1]
            elif line.startswith("Port "): info["ssh_port"] = int(line.split()[1])
            elif line.startswith("User "): info["ssh_username"] = line.split()[1]
            elif line.startswith("IdentityFile "): info["ssh_private_key_path"] = line.split()[1]
    except Exception:
        pass
    return info

async def heartbeat_loop(websocket):
    """Sends a heartbeat every 30 seconds back to the server."""
    global current_vm_id
    while True:
        await asyncio.sleep(30)
        try:
            payload = {
                "action": "heartbeat",
                "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
                "status": "healthy",
                "active_vms": [current_vm_id] if current_vm_id else []
            }
            await websocket.send(json.dumps(payload))
        except websockets.exceptions.ConnectionClosed:
            break
        except Exception as e:
            print(f"[!] Heartbeat error: {e}")

async def connect_to_websocket():
    global current_vm_id
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

                # Retrieve real system metrics
                capabilities = {
                    "cpu_cores": psutil.cpu_count(logical=False),
                    "ram_gb": round(psutil.virtual_memory().total / (1024**3), 2),
                    "storage_gb": round(shutil.disk_usage('/').free / (1024**3), 2),
                    "os": platform.system()
                }

                # 1. Connection & Registration
                await websocket.send(json.dumps({
                    "action": "client_connected",
                    "hostname": platform.node(),
                    "connection_token": "agent-default-token",
                    "capabilities": capabilities
                }))

                # Start heartbeat in background
                asyncio.create_task(heartbeat_loop(websocket))

                async for message in websocket:
                    print(f"\n[*] Message from server: {message}")
                    try:
                        data = json.loads(message)
                    except Exception:
                        continue

                    action = data.get("action")
                    vm_id = data.get("vm_id")

                    if action == "provision_vm":
                        current_vm_id = vm_id
                        await websocket.send(json.dumps({
                            "action": "vm_provisioning_started",
                            "vm_id": vm_id,
                            "status": "building",
                            "message": "Starting Vagrant up..."
                        }))

                        is_arm = ('arm' in platform.machine().lower() or 'aarch64' in platform.machine().lower())
                        box_name = _resolve_box_choice(data.get("os_choice"), is_arm)

                        specs = data.get("specs", {})
                        if not specs.get("memory"):
                            host_specs = get_hardware_profile()
                            specs["memory"] = host_specs["memory"]
                            specs["cpus"] = host_specs["cpus"]

                        loop = asyncio.get_running_loop()
                        success = await loop.run_in_executor(None, run_vagrant, specs, box_name)

                        if success:
                            ssh_info = await asyncio.to_thread(get_vagrant_ssh_info)
                            await websocket.send(json.dumps({
                                "action": "vm_provisioned",
                                "vm_id": vm_id,
                                "status": "running",
                                "vm_info": ssh_info
                            }))
                        else:
                            await websocket.send(json.dumps({
                                "action": "vm_provisioning_failed",
                                "vm_id": vm_id,
                                "status": "failed",
                                "error": "Vagrant provisioning returned exit code 1"
                            }))

                    elif action == "execute_file":
                        job_id = data.get("job_id")
                        filename = data.get("exec_filename", "payload.bin")
                        content_b64 = data.get("exec_file", "")
                        exec_command = data.get("exec_command", f"chmod +x /home/vagrant/{filename} && /home/vagrant/{filename}")
                        
                        await websocket.send(json.dumps({
                            "action": "execution_started",
                            "job_id": job_id,
                            "vm_id": vm_id,
                            "status": "running",
                            "message": f"File uploaded, preparing to execute {filename}..."
                        }))

                        try:
                            file_content = base64.b64decode(content_b64)
                            local_path = os.path.join(os.getcwd(), filename)
                            remote_path = data.get("working_directory", "/home/vagrant") + f"/{filename}"
                            
                            with open(local_path, "wb") as f:
                                f.write(file_content)
                                
                            upload_proc = subprocess.run(["vagrant", "upload", local_path, remote_path], capture_output=True, text=True)
                            
                            start_time = time.time()
                            result = await asyncio.to_thread(run_command_in_vm, exec_command)
                            exec_time = time.time() - start_time

                            if os.path.exists(local_path):
                                os.remove(local_path)
                                
                            if result.get("success"):
                                await websocket.send(json.dumps({
                                    "action": "execution_completed",
                                    "job_id": job_id,
                                    "vm_id": vm_id,
                                    "status": "completed",
                                    "exit_code": 0,
                                    "stdout": result.get("stdout", ""),
                                    "stderr": result.get("stderr", ""),
                                    "execution_time": round(exec_time, 2)
                                }))
                            else:
                                await websocket.send(json.dumps({
                                    "action": "execution_failed",
                                    "job_id": job_id,
                                    "vm_id": vm_id,
                                    "status": "failed",
                                    "error": "Command failed",
                                    "exit_code": result.get("code", 1),
                                    "stderr": result.get("stderr", "")
                                }))
                        except Exception as e:
                            await websocket.send(json.dumps({
                                "action": "execution_failed",
                                "job_id": job_id,
                                "vm_id": vm_id,
                                "status": "failed",
                                "error": str(e),
                                "exit_code": -1,
                                "stderr": ""
                            }))
                            
                    elif action == "stop_vm":
                        print(f"[*] Stopping VM {vm_id}...")
                        subprocess.run(["vagrant", "halt"], check=False)
                        await websocket.send(json.dumps({
                            "action": "vm_stopped",
                            "vm_id": vm_id,
                            "status": "stopped"
                        }))
                        
                    elif action == "destroy_vm":
                        print(f"[*] Destroying VM {vm_id}...")
                        subprocess.run(["vagrant", "destroy", "-f"], check=False)
                        current_vm_id = None
                        await websocket.send(json.dumps({
                            "action": "vm_destroyed",
                            "vm_id": vm_id,
                            "status": "destroyed"
                        }))

                    elif action == "upload_file_to_vm":
                        file_id = data.get("file_id")
                        content_b64 = data.get("file_content", "")
                        dest_path = data.get("destination_path", "/home/vagrant/uploaded_file")
                        
                        try:
                            local_tmp = f"tmp_upload_{uuid.uuid4().hex}"
                            with open(local_tmp, "wb") as f:
                                f.write(base64.b64decode(content_b64))
                            subprocess.run(["vagrant", "upload", local_tmp, dest_path], check=True)
                            os.remove(local_tmp)
                            
                            # Optional permissions set
                            perms = data.get("permissions")
                            if perms:
                                await asyncio.to_thread(run_command_in_vm, f"chmod {perms} {dest_path}")
                                
                            await websocket.send(json.dumps({
                                "action": "file_uploaded",
                                "file_id": file_id,
                                "vm_id": vm_id,
                                "status": "success",
                                "path": dest_path
                            }))
                        except Exception as e:
                            await websocket.send(json.dumps({"action": "error_occurred", "error_type": "upload_failed", "vm_id": vm_id, "message": str(e), "recoverable": True}))

                    elif action == "download_file_from_vm":
                        file_id = data.get("file_id")
                        source_path = data.get("source_path")
                        # Read file as base64 right from the VM via SSH
                        result = await asyncio.to_thread(run_command_in_vm, f"base64 {source_path}")
                        if result.get("success"):
                            chunk = result["stdout"].replace("\n", "").replace("\r", "")
                            await websocket.send(json.dumps({
                                "action": "file_downloaded",
                                "file_id": file_id,
                                "vm_id": vm_id,
                                "file_content": chunk,
                                "file_size": len(base64.b64decode(chunk))
                            }))
                        else:
                            await websocket.send(json.dumps({"action": "error_occurred", "error_type": "download_failed", "vm_id": vm_id, "message": result.get("stderr", "Unknown error"), "recoverable": True}))

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