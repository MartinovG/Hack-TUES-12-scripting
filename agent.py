import requests
import time
import subprocess
import json

config = json.load(open("config.json"))

def start_vm():
    subprocess.run(["vagrant", "up"], cwd="vm/")

def start_ssh_tunnel():
    subprocess.Popen([
        "ssh",
        "-i", "ssh/provider_key",
        "-o", "StrictHostKeyChecking=yes",
        "-R", "2222:localhost:22",
        f"{config['backend_user']}@{config['backend_ip']}"
    ])

def heartbeat():
    headers = {"Authorization": f"Bearer {config['api_key']}"}
    requests.post(config["backend_url"] + "/heartbeat", headers=headers)

if __name__ == "__main__":
    start_vm()
    start_ssh_tunnel()

    while True:
        heartbeat()
        time.sleep(10)