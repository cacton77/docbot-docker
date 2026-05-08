#!/bin/bash

set -e  # Exit on any error

echo "========================================="
echo "ROS2 Docker Template Installation Script"
echo "========================================="
echo ""

# Get the directory where this script is located
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

# Source .env so values like ROS_DOMAIN_ID flow into the firmware build flags
# below (-DMICROROS_DOMAIN_ID=...). Default to 0 to match rcl's own default
# when nothing is set.
if [ -f "$SCRIPT_DIR/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    source "$SCRIPT_DIR/.env"
    set +a
fi
ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-0}"

# Get container name from folder name (sanitize for docker: lowercase, no spaces)
CONTAINER_NAME=$(basename "$SCRIPT_DIR" | tr '[:upper:]' '[:lower:]' | tr ' ' '-')
echo "Container name: $CONTAINER_NAME"


# Install pip3 if not installed
if ! command -v pip3 &> /dev/null; then
    echo ""
    echo "pip3 not found. Installing pip3..."
    sudo apt-get update && sudo apt-get install -y python3-pip
    echo "✓ pip3 installed"
fi

# Install vcstool if not installed
if ! command -v vcs &> /dev/null; then
    echo ""
    echo "vcstool not found. Installing vcstool..."
    pip3 install --break-system-packages vcstool
    echo "✓ vcstool installed"
else
    echo ""
    echo "✓ vcstool is already installed"
fi

# Install vcstool if not installed                                                               
if ! command -v vcs &> /dev/null; then                                                           
    echo ""                                                                                      
    echo "vcstool not found. Installing vcstool..."                                              
    sudo apt install vcstool
    export PATH="$PATH:$HOME/.local/bin"                                                         
    echo "✓ vcstool installed"                                                                   
else                                                                                             
    echo ""                                                                                      
    echo "✓ vcstool is already installed"                                                        
fi

# Import dependencies into source directory
SRC_DIR="$SCRIPT_DIR/src"
cd "$SRC_DIR"
echo "Importing dependencies..."
vcs import < ./src.repos
echo "✓ Dependencies imported"

# If git-lfs is not installed, Install git-lfs
if ! command -v git-lfs &> /dev/null; then
    echo ""
    echo "Git LFS not found. Installing Git LFS..."
    curl -s https://packagecloud.io/install/repositories/github/git-lfs/script.deb.sh | sudo bash
    sudo apt-get install git-lfs
    git lfs install
    echo "✓ Git LFS installed"
else
    echo ""
    echo "✓ Git LFS is already installed"
fi

# Import data repos into data directory
DATA_DIR="$SCRIPT_DIR/data"
cd "$DATA_DIR"
echo "Importing data repositories..."
vcs import < ./data.repos
# Enter each folder under DATA_DIR
for dir in "$DATA_DIR"/*/; do
    if [ -d "$dir/.git" ]; then
        cd "$dir"
        git lfs install
        git lfs pull
        git lfs fetch --all
        echo "✓ Data repository updated: $dir"
        cd "$DATA_DIR"
    fi
done

# Import model repositories
MODELS_DIR="$SCRIPT_DIR/models"
cd "$MODELS_DIR"
echo ""
echo "Importing model repositories..."
vcs import < ./models.repos
echo "✓ Model repositories imported"

# Install arduino-cli if not installed
# If arduino directory doesn't exist, create it and install arduino-cli there to avoid polluting user PATH with arduino-cli
ARDUINO_DIR="$SCRIPT_DIR/arduino"
ARDUINO_CLI="$ARDUINO_DIR/bin/arduino-cli"
ARDUINO_CONFIG="$ARDUINO_DIR/arduino-cli.yaml"

if [ ! -x "$ARDUINO_CLI" ]; then
    echo ""
    echo "Arduino CLI not found. Installing Arduino CLI..."
    mkdir -p "$ARDUINO_DIR"
    cd "$ARDUINO_DIR"
    curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh | sh
    cd "$SCRIPT_DIR"
    echo "✓ Arduino CLI installed"
else
    echo ""
    echo "✓ Arduino CLI is already installed"
fi

# Ensure PyYAML is available (used to parse each firmware repo's firmware.yaml)
if ! python3 -c "import yaml" 2>/dev/null; then
    echo ""
    echo "PyYAML not found. Installing python3-yaml..."
    sudo apt-get install -y python3-yaml
    echo "✓ python3-yaml installed"
fi

# Allow installing libraries from git URLs (e.g. micro_ros_arduino)
"$ARDUINO_CLI" --config-file "$ARDUINO_CONFIG" config set library.enable_unsafe_install true >/dev/null

# YAML helpers --------------------------------------------------------------
yaml_get() {
    # yaml_get <file> <key> [default]
    python3 - "$1" "$2" "${3-}" <<'PY'
import sys, yaml
path, key, default = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path) as f:
    data = yaml.safe_load(f) or {}
val = data.get(key)
print(val if val is not None else default)
PY
}

yaml_extra_libs() {
    # Emit one "<git_url>#<version>" (or just "<git_url>") per line.
    python3 - "$1" <<'PY'
import sys, yaml
with open(sys.argv[1]) as f:
    data = yaml.safe_load(f) or {}
for lib in data.get('extra_libraries') or []:
    url = lib.get('git_url', '')
    ver = lib.get('version', '')
    if url:
        print(f"{url}#{ver}" if ver else url)
PY
}

sketch_yaml_field() {
    # sketch_yaml_field <file> <field>
    # Extracts deps from the first profile (or default_profile if set).
    # Fields: fqbn, platform, platform_url, libraries.
    # Format: "platform" -> "name@version", "libraries" -> one "name@version" per line.
    python3 - "$1" "$2" <<'PY'
import sys, yaml, re
path, field = sys.argv[1], sys.argv[2]
with open(path) as f:
    data = yaml.safe_load(f) or {}
profiles = data.get('profiles') or {}
default = data.get('default_profile')
if default and default in profiles:
    profile = profiles[default]
elif profiles:
    profile = next(iter(profiles.values()))
else:
    profile = {}

def split_versioned(s):
    m = re.match(r'^(.+?)\s*\(([^)]*)\)\s*$', s)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return s.strip(), ''

if field == 'fqbn':
    print(profile.get('fqbn', ''))
elif field == 'platform':
    plats = profile.get('platforms') or []
    if plats:
        name, ver = split_versioned(plats[0].get('platform', ''))
        print(f"{name}@{ver}" if ver else name)
elif field == 'platform_url':
    plats = profile.get('platforms') or []
    if plats:
        print(plats[0].get('platform_index_url', ''))
elif field == 'libraries':
    for lib in profile.get('libraries') or []:
        name, ver = split_versioned(lib)
        print(f"{name}@{ver}" if ver else name)
PY
}

# Flashing helpers ----------------------------------------------------------
find_uf2_drive() {
    # find_uf2_drive <label> -> sets UF2_DRIVE on success
    local label="$1"
    UF2_DRIVE=$(mount | grep -i "$label" | awk '{print $3}')
    if [ -n "$UF2_DRIVE" ]; then
        return 0
    fi
    local dev
    dev=$(lsblk -o NAME,LABEL -rn 2>/dev/null | grep -i "$label" | awk '{print $1}')
    if [ -n "$dev" ]; then
        UF2_DRIVE="/mnt/${label,,}"
        sudo mkdir -p "$UF2_DRIVE"
        sudo mount "/dev/$dev" "$UF2_DRIVE"
        echo "Mounted /dev/$dev at $UF2_DRIVE"
        return 0
    fi
    UF2_DRIVE=""
    return 1
}

flash_uf2() {
    local sketch_dir="$1"
    local meta="$2"
    local sketch_name; sketch_name=$(basename "$sketch_dir")
    local label;       label=$(yaml_get "$meta" bootloader_label "RPI-RP2")
    local serial_glob; serial_glob=$(yaml_get "$meta" serial_glob "/dev/ttyACM*")

    echo "Checking for $label bootloader drive..."
    if ! find_uf2_drive "$label"; then
        local port; port=$(ls $serial_glob 2>/dev/null | head -n 1)
        if [ -z "$port" ]; then
            echo "WARNING: $sketch_name: no $label drive and no serial port matching $serial_glob."
            echo "  Connect the board (or hold BOOTSEL while plugging in) and re-run."
            return 1
        fi
        echo "Resetting $port into bootloader (1200-baud touch)..."
        # arduino-pico's BOOTSEL trigger fires only when baudrate==1200 AND
        # DTR is de-asserted. pyserial defaults DTR to True on open, which
        # silently defeats the touch — explicitly set it False after opening.
        if ! python3 -c "
import serial, time
s = serial.Serial('$port')
s.baudrate = 1200
s.dtr = False
time.sleep(0.1)
s.close()
"; then
            echo "  (Python touch failed; falling back to stty)"
            sudo stty -F "$port" 1200
            exec 3<>"$port"; sleep 0.1; exec 3>&-
        fi
        echo "Waiting for $label bootloader drive..."
        local found=false
        for i in $(seq 1 30); do
            if find_uf2_drive "$label"; then found=true; break; fi
            sleep 1
        done
        if [ "$found" != true ]; then
            echo "WARNING: $label drive did not appear after reset."
            return 1
        fi
    fi

    local uf2
    uf2=$(find "$HOME/.cache/arduino/sketches" -name "${sketch_name}.ino.uf2" 2>/dev/null | head -n 1)
    if [ -z "$uf2" ]; then
        uf2=$(find "$sketch_dir" -name "*.uf2" 2>/dev/null | head -n 1)
    fi
    if [ -z "$uf2" ]; then
        echo "WARNING: $sketch_name: could not find compiled .uf2 file."
        return 1
    fi
    echo "Copying $uf2 to $UF2_DRIVE..."
    sudo cp "$uf2" "$UF2_DRIVE/"
    sync
    echo "✓ $sketch_name: firmware uploaded"
    [[ "$UF2_DRIVE" == /mnt/* ]] && sudo umount "$UF2_DRIVE" 2>/dev/null || true
}

flash_serial() {
    local sketch_dir="$1"
    local meta="$2"
    local sketch_name;  sketch_name=$(basename "$sketch_dir")
    local serial_glob;  serial_glob=$(yaml_get "$meta" serial_glob "/dev/ttyUSB*")
    local port;         port=$(ls $serial_glob 2>/dev/null | head -n 1)
    if [ -z "$port" ]; then
        echo "WARNING: $sketch_name: no serial port matching $serial_glob."
        return 1
    fi
    echo "Uploading $sketch_name via $port..."
    "$ARDUINO_CLI" --config-file "$ARDUINO_CONFIG" upload \
        --port "$port" "$sketch_dir"
    echo "✓ $sketch_name: firmware uploaded"
}

# Stop the service AND any running containers so they release /dev/ttyACM* etc.
# before we flash firmware. Containers started outside systemd (e.g. via
# launch_container.sh or a manual `docker compose up`) hold the serial devices
# via the /dev bind mount, so a `systemctl stop` alone isn't enough.
# The service will be restarted at the end of install.sh.
echo ""
echo "Stopping service and containers so firmware flashing can claim serial ports..."
"$SCRIPT_DIR/stop.sh"

# Discover firmware sketches: any directory under src/ containing a sketch.yaml.
# Each must also contain a firmware.yaml describing how to flash.
mapfile -t SKETCHES < <(find "$SRC_DIR" -path "*/.git" -prune -o -name sketch.yaml -print | xargs -I {} dirname {} | sort -u)

if [ ${#SKETCHES[@]} -eq 0 ]; then
    echo ""
    echo "✓ No firmware sketches found under $SRC_DIR — skipping firmware section"
else

echo ""
echo "Updating Arduino core index..."
# arduino-cli.yaml uses relative paths for data/downloads/user, so cwd must be
# $ARDUINO_DIR for cores and libraries to land in the right place.
cd "$ARDUINO_DIR"
# /tmp is tmpfs and too small for git clones of large libraries (micro_ros_arduino).
# Redirect arduino-cli's temp dir to disk for the duration of the firmware section.
mkdir -p "$ARDUINO_DIR/tmp"
export TMPDIR="$ARDUINO_DIR/tmp"
"$ARDUINO_CLI" --config-file "$ARDUINO_CONFIG" core update-index

EXTRA_LIBS_DIR="$ARDUINO_DIR/extra-libraries"
mkdir -p "$EXTRA_LIBS_DIR"

for sketch_dir in "${SKETCHES[@]}"; do
    sketch_name=$(basename "$sketch_dir")
    sketch_yaml="$sketch_dir/sketch.yaml"
    meta="$sketch_dir/firmware.yaml"

    echo ""
    echo "=== $sketch_name ==="

    if [ ! -f "$meta" ]; then
        echo "✗ $sketch_name: missing firmware.yaml next to sketch.yaml"
        exit 1
    fi

    # Parse the sketch.yaml profile ourselves rather than letting arduino-cli
    # activate sketch-profile mode — profiles silently ignore --libraries,
    # which we need to surface git-cloned libs (e.g. micro_ros_arduino).
    fqbn=$(sketch_yaml_field "$sketch_yaml" fqbn)
    platform_spec=$(sketch_yaml_field "$sketch_yaml" platform)
    platform_url=$(sketch_yaml_field "$sketch_yaml" platform_url)

    if [ -z "$fqbn" ]; then
        echo "✗ $sketch_name: sketch.yaml has no fqbn"
        exit 1
    fi

    if [ -n "$platform_spec" ]; then
        core_args=(core install "$platform_spec")
        [ -n "$platform_url" ] && core_args+=(--additional-urls "$platform_url")
        "$ARDUINO_CLI" --config-file "$ARDUINO_CONFIG" "${core_args[@]}"
    fi

    while IFS= read -r lib; do
        [ -z "$lib" ] && continue
        "$ARDUINO_CLI" --config-file "$ARDUINO_CONFIG" lib install "$lib"
    done < <(sketch_yaml_field "$sketch_yaml" libraries)

    # Non-registry libraries (git URLs) live in an extra-libraries collection
    # passed to compile via --libraries.
    while IFS= read -r entry; do
        [ -z "$entry" ] && continue
        url="${entry%%#*}"
        ver=""
        if [[ "$entry" == *"#"* ]]; then
            ver="${entry##*#}"
        fi
        libname=$(basename "$url" .git)
        target="$EXTRA_LIBS_DIR/$libname"

        if [ -d "$target" ] && [ ! -d "$target/.git" ]; then
            echo "Removing incomplete clone at $target..."
            rm -rf "$target"
        fi

        if [ -d "$target/.git" ]; then
            echo "Extra library already present: $libname"
        else
            echo "Cloning extra library: $url${ver:+ (branch $ver)}"
            if [ -n "$ver" ]; then
                git clone --depth 1 --branch "$ver" "$url" "$target"
            else
                git clone --depth 1 "$url" "$target"
            fi
        fi
    done < <(yaml_extra_libs "$meta")

    # Generate a header in the sketch dir so .env's ROS_DOMAIN_ID flows into
    # the firmware. Each firmware's config.h includes this via __has_include
    # and falls back to MICROROS_DOMAIN_ID=0 when absent. Done this way (vs
    # --build-property compiler.cpp.extra_flags=...) because that property is
    # already set by some platforms (ESP32 sets it to "-MMD -c"); replacing
    # it breaks the build, and arduino-cli has no append semantics for it.
    cat > "$sketch_dir/domain_id.h" <<EOF
// AUTO-GENERATED by install.sh from .env's ROS_DOMAIN_ID. Do not edit.
#pragma once
#define MICROROS_DOMAIN_ID $ROS_DOMAIN_ID
EOF

    echo "Compiling $sketch_name (MICROROS_DOMAIN_ID=$ROS_DOMAIN_ID)..."
    "$ARDUINO_CLI" --config-file "$ARDUINO_CONFIG" \
        compile --fqbn "$fqbn" --libraries "$EXTRA_LIBS_DIR" "$sketch_dir"
    echo "✓ $sketch_name: compiled"

    flash_method=$(yaml_get "$meta" flash_method)
    case "$flash_method" in
        uf2)    flash_uf2    "$sketch_dir" "$meta" || true ;;
        serial) flash_serial "$sketch_dir" "$meta" || true ;;
        *)      echo "✗ $sketch_name: unknown flash_method '$flash_method'"; exit 1 ;;
    esac
done

unset TMPDIR
fi  # end firmware sketches block

cd "$SCRIPT_DIR"

# Parse command line arguments
COMPOSE_PROFILE=""
for arg in "$@"; do
    case $arg in
        --profile=*)
            COMPOSE_PROFILE="${arg#*=}"
            shift
            ;;
        --jetson)
            COMPOSE_PROFILE="jetson"
            shift
            ;;
        --gpu)
            COMPOSE_PROFILE="linux-gpu"
            shift
            ;;
        --rpi)
            COMPOSE_PROFILE="rpi"
            shift
            ;;
        --linux)
            COMPOSE_PROFILE="linux"
            shift
            ;;
        *)
            ;;
    esac
done

# Auto-detect platform if not specified via flag
if [ -z "$COMPOSE_PROFILE" ]; then
    echo "Auto-detecting platform..."
    if [ -f /proc/device-tree/compatible ] && grep -qi "nvidia,tegra" /proc/device-tree/compatible 2>/dev/null; then
        COMPOSE_PROFILE="jetson"
        echo "✓ NVIDIA Jetson detected → profile: jetson"
    elif command -v nvidia-smi &> /dev/null && nvidia-smi &> /dev/null; then
        COMPOSE_PROFILE="linux-gpu"
        echo "✓ Discrete NVIDIA GPU detected → profile: linux-gpu"
    elif [ "$(uname -m)" = "aarch64" ]; then
        COMPOSE_PROFILE="rpi"
        echo "✓ ARM64 platform detected → profile: rpi"
    else
        COMPOSE_PROFILE="linux"
        echo "✓ x86_64 platform detected → profile: linux"
    fi
else
    echo "Platform profile set via flag: $COMPOSE_PROFILE"
fi

COMPOSE_SERVICE="$COMPOSE_PROFILE"

# Change to the container directory
echo "Changing to directory: $SCRIPT_DIR"
cd "$SCRIPT_DIR" || exit 1

# Build the Docker image
echo ""
echo "Building Docker image (profile: $COMPOSE_PROFILE)..."
docker compose --profile "$COMPOSE_PROFILE" build "$COMPOSE_SERVICE"

if [ $? -eq 0 ]; then
    echo "✓ Docker image built successfully"
else
    echo "✗ Docker build failed"
    exit 1
fi

# Create applications directory if it doesn't exist
APPS_DIR="$HOME/.local/share/applications/$SCRIPT_DIR"
echo ""
echo "Ensuring applications directory exists: $APPS_DIR"
if [ -d "$APPS_DIR" ]; then
    rm -rf "$APPS_DIR"
fi
mkdir -p "$APPS_DIR"

# Update .env file with CONTAINER_NAME and USE_GPU
ENV_FILE=".env"
if [ -f "$SCRIPT_DIR/$ENV_FILE" ]; then
    # Update CONTAINER_NAME
    if grep -q "^CONTAINER_NAME=" "$SCRIPT_DIR/$ENV_FILE"; then
        sed -i "s/^CONTAINER_NAME=.*/CONTAINER_NAME=$CONTAINER_NAME/" "$SCRIPT_DIR/$ENV_FILE"
    else
        echo "CONTAINER_NAME=$CONTAINER_NAME" >> "$SCRIPT_DIR/$ENV_FILE"
    fi
    # Update COMPOSE_PROFILE
    if grep -q "^COMPOSE_PROFILE=" "$SCRIPT_DIR/$ENV_FILE"; then
        sed -i "s/^COMPOSE_PROFILE=.*/COMPOSE_PROFILE=$COMPOSE_PROFILE/" "$SCRIPT_DIR/$ENV_FILE"
    else
        echo "COMPOSE_PROFILE=$COMPOSE_PROFILE" >> "$SCRIPT_DIR/$ENV_FILE"
    fi
    # Update COMPOSE_SERVICE
    if grep -q "^COMPOSE_SERVICE=" "$SCRIPT_DIR/$ENV_FILE"; then
        sed -i "s/^COMPOSE_SERVICE=.*/COMPOSE_SERVICE=$COMPOSE_SERVICE/" "$SCRIPT_DIR/$ENV_FILE"
    else
        echo "COMPOSE_SERVICE=$COMPOSE_SERVICE" >> "$SCRIPT_DIR/$ENV_FILE"
    fi
    # Copy .env file to apps directory
    echo "Copying .env file to $APPS_DIR..."
    cp "$SCRIPT_DIR/$ENV_FILE" "$APPS_DIR/"
    echo "✓ .env file installed (CONTAINER_NAME=$CONTAINER_NAME, COMPOSE_PROFILE=$COMPOSE_PROFILE)"
else
    echo "✗ .env file not found: $SCRIPT_DIR/$ENV_FILE"
    exit 1
fi

# Copy docker-compose files
echo "Copying docker-compose files to $APPS_DIR..."
cp "$SCRIPT_DIR/docker-compose.yaml" "$APPS_DIR/"
echo "✓ Docker compose file installed"

# Copy main script
MAIN_SCRIPT="connect.sh"
if [ -f "$SCRIPT_DIR/$MAIN_SCRIPT" ]; then
    echo "Copying main script to $APPS_DIR..."
    cp "$SCRIPT_DIR/$MAIN_SCRIPT" "$APPS_DIR/"
    chmod +x "$APPS_DIR/$MAIN_SCRIPT"
    echo "✓ Main script installed"
else
    echo "✗ Main script not found: $SCRIPT_DIR/$MAIN_SCRIPT"
    exit 1
fi

# Copy assets
ASSETS_DIR="$APPS_DIR/assets"
echo ""
echo "Ensuring assets directory exists: $ASSETS_DIR"
mkdir -p "$ASSETS_DIR"

# Copy asset files
for asset in "$SCRIPT_DIR/assets/"*; do
    if [ -f "$asset" ]; then
        echo "Copying asset file to $ASSETS_DIR..."
        cp "$asset" "$ASSETS_DIR/"
    fi
done

# Copy desktop file
BRINGUP_DESKTOP_FILE="bringup.desktop"
if [ -f "$SCRIPT_DIR/$BRINGUP_DESKTOP_FILE" ]; then
    echo "Copying bringup desktop file to $APPS_DIR..."
    cp "$SCRIPT_DIR/$BRINGUP_DESKTOP_FILE" "$APPS_DIR/"
    chmod +x "$APPS_DIR/$BRINGUP_DESKTOP_FILE"
    echo "✓ Bringup desktop file installed"
    echo "Icon=$ASSETS_DIR/bringup_icon.png" >> "$APPS_DIR/$BRINGUP_DESKTOP_FILE"
    echo "Creating bringup desktop shortcut..."
    # Remove existing shortcut if it exists
    rm -f "$HOME/Desktop/$BRINGUP_DESKTOP_FILE"
    ln -s "$APPS_DIR/$BRINGUP_DESKTOP_FILE" "$HOME/Desktop/"
    echo "✓ Desktop shortcut created"
else
    echo "✗ Bringup desktop file not found: $SCRIPT_DIR/$BRINGUP_DESKTOP_FILE"
    exit 1
fi
# Copy devel desktop file
DEVEL_DESKTOP_FILE="devel.desktop"
if [ -f "$SCRIPT_DIR/$DEVEL_DESKTOP_FILE" ]; then
    echo "Copying devel desktop file to $APPS_DIR..."
    cp "$SCRIPT_DIR/$DEVEL_DESKTOP_FILE" "$APPS_DIR/"
    chmod +x "$APPS_DIR/$DEVEL_DESKTOP_FILE"
    echo "✓ Devel desktop file installed"
    echo "Icon=$ASSETS_DIR/devel_icon.png" >> "$APPS_DIR/$DEVEL_DESKTOP_FILE"
    echo "Creating devel desktop shortcut..."
    # Remove existing shortcut if it exists
    rm -f "$HOME/Desktop/$DEVEL_DESKTOP_FILE"
    ln -s "$APPS_DIR/$DEVEL_DESKTOP_FILE" "$HOME/Desktop/"
    echo "✓ Desktop shortcut created"
else
    echo "✗ Devel desktop file not found: $SCRIPT_DIR/$DEVEL_DESKTOP_FILE"
    exit 1
fi

# Make the main scripts executable
MAIN_SCRIPT="connect.sh"
if [ -f "$SCRIPT_DIR/$MAIN_SCRIPT" ]; then
    echo "Making main script executable..."
    chmod +x "$SCRIPT_DIR/$MAIN_SCRIPT"
    echo "✓ Main script is executable"
else
    echo "✗ Main script not found: $SCRIPT_DIR/$MAIN_SCRIPT"
    exit 1
fi

# Make run.sh executable
RUN_SCRIPT="run.sh"
if [ -f "$SCRIPT_DIR/$RUN_SCRIPT" ]; then
    echo "Making run script executable..."
    chmod +x "$SCRIPT_DIR/$RUN_SCRIPT"
    echo "✓ Run script is executable"
else
    echo "✗ Run script not found: $SCRIPT_DIR/$RUN_SCRIPT"
    exit 1
fi

# Update desktop database (optional, helps with immediate icon visibility)
if command -v update-desktop-database &> /dev/null; then
    echo ""
    echo "Updating desktop database..."
    update-desktop-database "$APPS_DIR"
    echo "✓ Desktop database updated"
    echo ""
fi


# Set up systemd service for auto-start on boot
echo ""
echo "Setting up systemd service for auto-start..."
SERVICE_NAME="$CONTAINER_NAME"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

# Create the systemd service file
sudo tee "$SERVICE_FILE" > /dev/null << EOF
[Unit]
Description=$CONTAINER_NAME - ROS2 Docker Container
After=docker.service network-online.target
Wants=network-online.target
Requires=docker.service

[Service]
Type=simple
User=$USER
WorkingDirectory=$SCRIPT_DIR
ExecStart=$SCRIPT_DIR/run.sh
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

# Set kernel socket buffer limit (permanent)
if ! grep -q "net.core.rmem_max=26214400" /etc/sysctl.conf; then
    echo "net.core.rmem_max=26214400" | sudo tee -a /etc/sysctl.conf
fi
if ! grep -q "net.core.wmem_max=26214400" /etc/sysctl.conf; then
    echo "net.core.wmem_max=26214400" | sudo tee -a /etc/sysctl.conf
fi
sudo sysctl -p

# Set kernel socket buffer limit (permanent)
if ! grep -q "net.core.rmem_max=26214400" /etc/sysctl.conf; then
    echo "net.core.rmem_max=26214400" | sudo tee -a /etc/sysctl.conf
fi
if ! grep -q "net.core.wmem_max=26214400" /etc/sysctl.conf; then
    echo "net.core.wmem_max=26214400" | sudo tee -a /etc/sysctl.conf
fi
sudo sysctl -p

# Reload systemd daemon and enable the service
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME.service"
echo "✓ Systemd service '$SERVICE_NAME' created and enabled"

# Restart the service to apply changes
echo "Restarting service to apply changes..."
sudo systemctl restart "$SERVICE_NAME.service"
echo "✓ Service restarted"
echo ""
echo "  To check status: sudo systemctl status $SERVICE_NAME"
echo "  To view logs: sudo journalctl -u $SERVICE_NAME -f"
echo "  To disable auto-start: sudo systemctl disable $SERVICE_NAME"

echo ""
echo "========================================="
echo "Installation complete!"
echo "========================================="
echo ""
echo "Container name: $CONTAINER_NAME"
echo "Platform profile: $COMPOSE_PROFILE"
echo ""
echo "You can now launch the container by running: ./connect.sh"
echo "The container will also auto-start on boot via systemd."
echo ""
