#!/usr/bin/env python3
import os
import subprocess
import sys
import distro  # Install 'python3-distro' via apt if needed, or remove this check for simplicity

SERVICE_PATH = "/etc/systemd/system/feeding.service"

def check_package_manager():
    if distro.id() in ['ubuntu', 'debian']:
        return ["apt-get", "install", "-y"]
    else:
        print(f"Unsupported distribution: {distro.id()}. This script requires a Debian/Ubuntu system.")
        sys.exit(1)

def run_command(cmd_list, description=None):
    if description:
        print(f"\n=== {description} ===")
    print("Running:", " ".join(cmd_list))
    subprocess.run(cmd_list, check=True)

def main():
    # 1) Must run as root
    if os.geteuid() != 0:
        print("Please run this script with sudo or as root.")
        sys.exit(1)

    # Get the original user who ran sudo
    user = os.environ.get('SUDO_USER', 'nobody')
    if user == 'nobody':
        print("Error: This script must be run with sudo, not as root directly.")
        sys.exit(1)
    home_dir = f"/home/{user}"
    auto_feed_dir = f"{home_dir}/Auto-Feeding-System"  # Matches your directory name
    venv_dir = f"{auto_feed_dir}/venv"
    requirements_file = f"{auto_feed_dir}/requirements.txt"

    # Check if directory and wsgi.py exist
    if not os.path.isdir(auto_feed_dir):
        print(f"Error: Directory {auto_feed_dir} does not exist.")
        sys.exit(1)
    if not os.path.isfile(f"{auto_feed_dir}/wsgi.py"):
        print(f"Error: wsgi.py not found in {auto_feed_dir}.")
        sys.exit(1)

    SERVICE_CONTENT = f"""[Unit]
Description=pH Gunicorn Service
After=network.target

[Service]
User={user}
WorkingDirectory={auto_feed_dir}
ExecStart={venv_dir}/bin/gunicorn -w 1 -k eventlet wsgi:app --bind 0.0.0.0:8001 --log-level=debug
Restart=always

[Install]
WantedBy=multi-user.target
"""

    try:
        # 2) Update & upgrade
        run_command(["apt-get", "update", "-y"], "apt-get update")  # Removed invalid options for update
        run_command(["apt-get", "upgrade", "-y], "apt-get upgrade")

        # 3) Install needed packages
        pkg_install = check_package_manager()
        run_command(pkg_install + ["git", "python3", "python3-venv", "python3-pip", "python3-dev", "libevent-dev", "avahi-utils", "python3-distro"],
                    "Install Git, Python 3, venv, pip, dev libraries, avahi-utils for mDNS, and distro")

        # 4) Create & activate a virtual environment
        if not os.path.isdir(venv_dir):
            run_command(["sudo", "-u", user, "python3", "-m", "venv", venv_dir],
                        f"Create Python venv in {venv_dir}")
        else:
            print("\n=== venv already exists. Skipping creation. ===")

        # 5) Upgrade pip & install requirements
        run_command(["sudo", "-u", user, f"{venv_dir}/bin/pip", "install", "--upgrade", "pip"],
                    "Upgrade pip in the venv")
        if os.path.isfile(requirements_file):
            run_command(["sudo", "-u", user, f"{venv_dir}/bin/pip", "install", "-r", requirements_file],
                        "Install Python dependencies from requirements.txt")
        else:
            print(f"\n=== {requirements_file} not found! Skipping pip install -r. ===")

        # 6) Enable and start avahi-daemon for mDNS
        run_command(["systemctl", "enable", "avahi-daemon"], "Enable avahi-daemon for mDNS")
        run_command(["systemctl", "start", "avahi-daemon"], "Start avahi-daemon for mDNS")

        # 7) Check and configure ufw for mDNS traffic
        ufw_check = subprocess.run(["which", "ufw"], capture_output=True)
        print(f"ufw check return code: {ufw_check.returncode}")  # Debug output
        if ufw_check.returncode == 0:
            run_command(["ufw", "allow", "5353/udp"], "Allow mDNS traffic through firewall (if ufw is active)")
        else:
            print("ufw not installed, skipping firewall configuration for mDNS.")

        # 8) Create the systemd service file
        print(f"\n=== Creating systemd service at {SERVICE_PATH} ===")
        with open(SERVICE_PATH, "w") as f:
            f.write(SERVICE_CONTENT)

        # 9) Reload systemd
        run_command(["systemctl", "daemon-reload"], "Reload systemd")

        # 10) Enable and start the Feeding service
        run_command(["systemctl", "enable", "feeding.service"], "Enable feeding.service on startup")
        run_command(["systemctl", "start", "feeding.service"], "Start feeding.service now")

        print("\n=== Setup complete! ===")
        print("You can check logs with:  journalctl -u feeding.service -f")
        print("You can check status with: systemctl status feeding.service")

    except subprocess.CalledProcessError as e:
        print(f"Error: Command {' '.join(e.cmd)} failed with exit code {e.returncode}")
        print("Setup failed. Please check logs and try again.")
        sys.exit(1)

if __name__ == "__main__":
    main()