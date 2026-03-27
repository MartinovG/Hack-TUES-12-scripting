import asyncio
import base64
import datetime
import os
import platform
import shutil
import subprocess
import sys
import time
import uuid

import psutil
import socketio

BACKEND_URL = os.getenv("HIVE_BACKEND_URL", "").strip()
SOCKET_PATH = os.getenv("HIVE_SOCKET_PATH", "/computer-socket")
CONNECTION_TOKEN = os.getenv("HIVE_CONNECTION_TOKEN", "").strip()
HEARTBEAT_INTERVAL = int(os.getenv("HIVE_HEARTBEAT_INTERVAL", "30"))
RECONNECT_DELAY_SECONDS = int(os.getenv("HIVE_RECONNECT_DELAY", "5"))

current_vm_id = None
heartbeat_task = None

sio = socketio.AsyncClient(
    reconnection=True,
    reconnection_attempts=0,
    reconnection_delay=RECONNECT_DELAY_SECONDS,
    reconnection_delay_max=RECONNECT_DELAY_SECONDS,
)


async def run_command_in_vm(command: str) -> dict:
    """Run a single command inside the Vagrant VM and return a structured result."""
    try:
        result = subprocess.run(
            ["vagrant", "ssh", "-c", command],
            capture_output=True,
            text=True,
            check=True,
        )
        return {
            "success": True,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        }
    except subprocess.CalledProcessError as error:
        return {
            "success": False,
            "stdout": error.stdout.strip() if error.stdout else "",
            "stderr": error.stderr.strip() if error.stderr else "",
            "code": error.returncode,
        }
    except Exception as error:
        return {"success": False, "stdout": "", "stderr": str(error)}


def _os_options_map(is_arm: bool):
    if is_arm:
        return {
            "1": "generic/alpine316",
            "2": "perk/ubuntu-2204-arm64",
            "3": "bento/debian-12-arm64",
        }
    return {
        "1": "generic/alpine316",
        "2": "gusztavvargadr/windows-10",
        "3": "ubuntu/jammy64",
    }


def _resolve_box_choice(choice, is_arm: bool):
    if isinstance(choice, int):
        choice = str(choice)
    if not choice:
        return None
    mapping = _os_options_map(is_arm)
    if isinstance(choice, str) and choice in mapping:
        return mapping[choice]
    if isinstance(choice, str) and "/" in choice:
        return choice
    return None


def get_vagrant_ssh_info():
    """Extract VM SSH credentials dynamically via vagrant ssh-config."""
    info = {
        "ip_address": "127.0.0.1",
        "ssh_port": 2222,
        "ssh_username": "vagrant",
        "ssh_private_key_path": "",
    }
    try:
        result = subprocess.run(
            ["vagrant", "ssh-config"],
            capture_output=True,
            text=True,
            check=True,
        )
        for line in result.stdout.split("\n"):
            line = line.strip()
            if line.startswith("HostName "):
                info["ip_address"] = line.split()[1]
            elif line.startswith("Port "):
                info["ssh_port"] = int(line.split()[1])
            elif line.startswith("User "):
                info["ssh_username"] = line.split()[1]
            elif line.startswith("IdentityFile "):
                info["ssh_private_key_path"] = line.split(maxsplit=1)[1]
    except Exception:
        pass
    return info


def get_system_info():
    os_type = platform.system()
    arch = platform.machine().lower()
    is_arm = "arm" in arch or "aarch64" in arch
    return os_type, is_arm


def get_hardware_profile():
    total_ram_gb = psutil.virtual_memory().total / (1024**3)
    cpu_cores = psutil.cpu_count(logical=False) or 1

    if total_ram_gb >= 16:
        vm_mem, vm_cpu = 4096, max(1, cpu_cores - 2)
    else:
        vm_mem, vm_cpu = 1024, max(1, cpu_cores - 1)

    print(f"[*] Hardware Profile: Allocated {vm_mem}MB RAM and {vm_cpu} CPU core(s) to the VM.")
    return {"memory": vm_mem, "cpus": vm_cpu}


def build_capabilities():
    return {
        "cpu_cores": psutil.cpu_count(logical=False) or 1,
        "ram_gb": max(1, int(psutil.virtual_memory().total / (1024**3))),
        "storage_gb": max(1, int(shutil.disk_usage("/").free / (1024**3))),
        "os": platform.system(),
    }


def resolve_connection_token():
    token = CONNECTION_TOKEN

    if len(sys.argv) > 1 and sys.argv[1].strip():
        token = sys.argv[1].strip()

    if token:
        return token

    print("[*] Paste the VM setup key shown in the frontend under Your VMs.")

    while True:
        provided = input("Setup key: ").strip()
        if provided:
            return provided
        print("[!] A setup key is required to register this provider machine.")


def resolve_backend_url():
    if BACKEND_URL:
        return BACKEND_URL.rstrip("/")

    print("[*] Enter the backend URL for your hosted Share A Comp API.")

    while True:
        provided = input("Backend URL: ").strip()
        if provided:
            return provided.rstrip("/")
        print("[!] A backend URL is required, for example: https://api.your-domain.com")


async def heartbeat_loop():
    """Send periodic heartbeats while connected."""
    global current_vm_id
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL)
        if not sio.connected:
            continue

        payload = {
            "action": "heartbeat",
            "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
            "status": "healthy",
            "active_vms": [current_vm_id] if current_vm_id else [],
        }

        try:
            await sio.emit("heartbeat", payload)
            print("[*] Heartbeat sent.")
        except Exception as error:
            print(f"[!] Heartbeat error: {error}")


def check_and_install_dependencies():
    os_type, is_arm = get_system_info()
    print(f"[*] Performing dependency checks for {os_type}...")

    if not shutil.which("vagrant"):
        print("[!] Vagrant is not installed. Provisioning commands will fail until it is installed.")

    if is_arm:
        if not shutil.which("qemu-system-aarch64") and not shutil.which("qemu-system-arm"):
            print("[!] QEMU is not installed. ARM-based Vagrant providers may fail.")
    else:
        if os_type == "Linux" and not shutil.which("virsh"):
            print("[!] libvirt is not installed. Linux provisioning may fail until it is installed.")
        elif os_type == "Darwin":
            if not shutil.which("vboxmanage") and not shutil.which("VBoxManage"):
                print("[!] VirtualBox is not installed. macOS provisioning may fail.")


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
    except subprocess.CalledProcessError as error:
        print(f"[!] Vagrant process returned non-zero exit status: {error.returncode}")
        return False
    except Exception as error:
        print(f"[!] Target error: {error}")
        if os_type == "Windows":
            print("[i] Hint: You may need to restart your terminal or run it as Administrator.")
        return False


@sio.event
async def connect():
    global heartbeat_task
    print("[+] Connected to NestJS backend.")

    await sio.emit(
        "client_connected",
        {
            "action": "client_connected",
            "hostname": platform.node(),
            "connection_token": CONNECTION_TOKEN,
            "capabilities": build_capabilities(),
        },
    )

    if heartbeat_task is None or heartbeat_task.done():
        heartbeat_task = asyncio.create_task(heartbeat_loop())


@sio.event
def disconnect():
    print("[!] Disconnected from NestJS backend.")


@sio.on("connection_acknowledged")
def on_connection_acknowledged(payload):
    print(f"[+] Registration acknowledged: {payload}")


@sio.on("error")
def on_error(payload):
    print(f"[!] Backend error event: {payload}")


@sio.on("provision_vm")
async def on_provision_vm(data):
    global current_vm_id
    current_vm_id = data.get("vm_id")
    print(f"[*] Received provision_vm event: {data}")

    await sio.emit(
        "vm_provisioning_started",
        {
            "action": "vm_provisioning_started",
            "vm_id": current_vm_id,
            "status": "building",
            "message": "Starting Vagrant up...",
        },
    )

    is_arm = "arm" in platform.machine().lower() or "aarch64" in platform.machine().lower()
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
        await sio.emit(
            "vm_provisioned",
            {
                "action": "vm_provisioned",
                "vm_id": current_vm_id,
                "status": "running",
                "vm_info": ssh_info,
            },
        )
    else:
        await sio.emit(
            "vm_provisioning_failed",
            {
                "action": "vm_provisioning_failed",
                "vm_id": current_vm_id,
                "status": "failed",
                "error": "Vagrant provisioning returned exit code 1",
            },
        )


@sio.on("execute_file")
async def on_execute_file(data):
    print(f"[*] Received execute_file event: {data}")
    vm_id = data.get("vm_id")
    job_id = data.get("job_id")
    filename = data.get("exec_filename", "payload.bin")
    content_b64 = data.get("exec_file", "")
    exec_command = data.get(
        "exec_command",
        f"chmod +x /home/vagrant/{filename} && /home/vagrant/{filename}",
    )

    await sio.emit(
        "execution_started",
        {
            "action": "execution_started",
            "job_id": job_id,
            "vm_id": vm_id,
            "status": "running",
            "message": f"File uploaded, preparing to execute {filename}...",
        },
    )

    try:
        file_content = base64.b64decode(content_b64)
        local_path = os.path.join(os.getcwd(), filename)
        remote_path = data.get("working_directory", "/home/vagrant") + f"/{filename}"

        with open(local_path, "wb") as file:
            file.write(file_content)

        subprocess.run(
            ["vagrant", "upload", local_path, remote_path],
            capture_output=True,
            text=True,
            check=False,
        )

        start_time = time.time()
        result = await asyncio.to_thread(run_command_in_vm, exec_command)
        exec_time = time.time() - start_time

        if os.path.exists(local_path):
            os.remove(local_path)

        if result.get("success"):
            await sio.emit(
                "execution_completed",
                {
                    "action": "execution_completed",
                    "job_id": job_id,
                    "vm_id": vm_id,
                    "status": "completed",
                    "exit_code": 0,
                    "stdout": result.get("stdout", ""),
                    "stderr": result.get("stderr", ""),
                    "execution_time": round(exec_time, 2),
                },
            )
        else:
            await sio.emit(
                "execution_failed",
                {
                    "action": "execution_failed",
                    "job_id": job_id,
                    "vm_id": vm_id,
                    "status": "failed",
                    "error": "Command failed",
                    "exit_code": result.get("code", 1),
                    "stderr": result.get("stderr", ""),
                },
            )
    except Exception as error:
        await sio.emit(
            "execution_failed",
            {
                "action": "execution_failed",
                "job_id": job_id,
                "vm_id": vm_id,
                "status": "failed",
                "error": str(error),
                "exit_code": -1,
                "stderr": "",
            },
        )


@sio.on("stop_vm")
async def on_stop_vm(data):
    vm_id = data.get("vm_id")
    print(f"[*] Stopping VM {vm_id}...")
    subprocess.run(["vagrant", "halt"], check=False)
    await sio.emit(
        "vm_stopped",
        {"action": "vm_stopped", "vm_id": vm_id, "status": "stopped"},
    )


@sio.on("destroy_vm")
async def on_destroy_vm(data):
    global current_vm_id
    vm_id = data.get("vm_id")
    print(f"[*] Destroying VM {vm_id}...")
    subprocess.run(["vagrant", "destroy", "-f"], check=False)
    current_vm_id = None
    await sio.emit(
        "vm_destroyed",
        {"action": "vm_destroyed", "vm_id": vm_id, "status": "destroyed"},
    )


@sio.on("upload_file_to_vm")
async def on_upload_file_to_vm(data):
    vm_id = data.get("vm_id")
    file_id = data.get("file_id")
    content_b64 = data.get("file_content", "")
    dest_path = data.get("destination_path", "/home/vagrant/uploaded_file")

    try:
        local_tmp = f"tmp_upload_{uuid.uuid4().hex}"
        with open(local_tmp, "wb") as file:
            file.write(base64.b64decode(content_b64))
        subprocess.run(["vagrant", "upload", local_tmp, dest_path], check=True)
        os.remove(local_tmp)

        perms = data.get("permissions")
        if perms:
            await asyncio.to_thread(run_command_in_vm, f"chmod {perms} {dest_path}")

        await sio.emit(
            "file_uploaded",
            {
                "file_id": file_id,
                "vm_id": vm_id,
                "status": "success",
                "path": dest_path,
            },
        )
    except Exception as error:
        await sio.emit(
            "error_occurred",
            {
                "action": "error_occurred",
                "error_type": "upload_failed",
                "vm_id": vm_id,
                "message": str(error),
                "recoverable": True,
            },
        )


@sio.on("download_file_from_vm")
async def on_download_file_from_vm(data):
    vm_id = data.get("vm_id")
    file_id = data.get("file_id")
    source_path = data.get("source_path")
    result = await asyncio.to_thread(run_command_in_vm, f"base64 {source_path}")

    if result.get("success"):
        chunk = result["stdout"].replace("\n", "").replace("\r", "")
        await sio.emit(
            "file_downloaded",
            {
                "file_id": file_id,
                "vm_id": vm_id,
                "file_content": chunk,
                "file_size": len(base64.b64decode(chunk)),
            },
        )
    else:
        await sio.emit(
            "error_occurred",
            {
                "action": "error_occurred",
                "error_type": "download_failed",
                "vm_id": vm_id,
                "message": result.get("stderr", "Unknown error"),
                "recoverable": True,
            },
        )


async def connect_to_backend():
    while True:
        try:
            print(f"[*] Attempting to connect to {BACKEND_URL} using path {SOCKET_PATH}...")
            await sio.connect(
                BACKEND_URL,
                socketio_path=SOCKET_PATH.lstrip("/"),
                transports=["websocket"],
            )
            await sio.wait()
        except Exception as error:
            print(f"[!] Connection failed: {error}. Retrying in {RECONNECT_DELAY_SECONDS} seconds...")
            await asyncio.sleep(RECONNECT_DELAY_SECONDS)


if __name__ == "__main__":
    try:
        CONNECTION_TOKEN = resolve_connection_token()
        BACKEND_URL = resolve_backend_url()
        check_and_install_dependencies()
        print("[*] Starting remote Hive Agent...")
        print(f"[*] Backend URL: {BACKEND_URL}")
        print(f"[*] Socket path: {SOCKET_PATH}")
        print(f"[*] Using setup key: {CONNECTION_TOKEN}")
        print("[*] Environment initialized. Proceeding to websocket layer...")
        asyncio.run(connect_to_backend())
    except KeyboardInterrupt:
        print("\n[!] Operation cancelled by user.")
        sys.exit(0)
