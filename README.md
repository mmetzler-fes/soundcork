# soundcork
Intercept API for Bose SoundTouch after they turn off the servers

## Overview

soundcork is a self-hosted replacement for the Bose cloud services that were shut down in February 2026. It replaces the Marge server, the BMX registry server, and related endpoints that SoundTouch devices depend on for basic network functionality, TuneIn radio, custom radio streams, and SiriusXM.

This fork adds two deployment improvements over the original:

- **Docker container deployment** — soundcork and an nginx reverse proxy run as Docker containers. The image is published at `ghcr.io/mmetzler-fes/soundcork:main` and supports both `linux/amd64` and `linux/arm64` (Raspberry Pi, NAS). Configuration and data are mounted as volumes so the container can be updated without losing data.

- **Connection relay (port 30034)** — The SoundTouch firmware routes certain HTTPS requests through a local proxy at `127.0.0.1:30034`. soundcork installs a persistent `nc` relay on the speaker (via `/mnt/nv/rc.local`) that forwards all connections on that port to the soundcork server. This makes BMX and media artwork requests work without any modifications to the speaker's certificate trust store.

Read [SECURITY.md](SECURITY.md) carefully. This should only be run inside your home network, behind a firewall. (If you have a router at home, it probably has a firewall on it.) Don't put it on an open network.

## Background

[Bose shut down the servers for the SoundTouch system in February 2026.](https://www.bose.com/soundtouch-end-of-life) soundcork reverse-engineers those servers so that users can continue to use the full set of SoundTouch functionality.

The SoundTouch speaker stores the URLs of all Bose cloud services in `/opt/Bose/etc/SoundTouchSdkPrivateCfg.xml`. By placing an override file at `/mnt/nv/OverrideSdkPrivateCfg.xml`, all those URLs can be redirected to a local soundcork server without touching the original file (which, if corrupted, causes a reboot loop requiring a firmware update). See [Ueberbose API](https://github.com/julius-d/ueberboese-api) for background.

---

## Step-by-Step: Preparing a SoundTouch Speaker for soundcork

### Step 1 — Connect the Speaker to LAN via Ethernet

For the initial setup the speaker must be reachable over a wired Ethernet connection so that you can determine its IP address and connect to it via SSH.

1. Connect an Ethernet cable between the speaker and your router or switch.
2. Look up the IP address assigned to the speaker in your router's DHCP table (or use a network scanner such as `nmap -sn 192.168.1.0/24`). Note this address — it is referred to as `SPEAKER_IP` throughout this guide.

---

### Step 2 — Enable SSH Access on the Speaker

The speaker's SSH server can only be reached after the speaker reads a special trigger file from a USB drive on boot.

1. Take a USB drive formatted as FAT32.
2. Create an **empty** file named `remote_services` in the root of the drive:
   ```sh
   touch /media/your-usb/remote_services
   ```
3. Safely eject the drive and plug it into the USB port on the back of the SoundTouch speaker.
4. Power-cycle the speaker (unplug the power cord and plug it back in). The speaker reads the USB drive during boot and activates the SSH server.
5. Verify SSH access — the speaker runs a legacy SSH server, so you need to allow old RSA keys:
   ```sh
   ssh -o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedAlgorithms=+ssh-rsa root@SPEAKER_IP
   ```
   Log in as `root`; there is no password. If you reach a shell prompt, SSH access is working.

> **Tip:** Set up key-based authentication now so that `setup-speaker.sh` can connect without a password prompt:
> ```sh
> ssh-copy-id -o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedAlgorithms=+ssh-rsa root@SPEAKER_IP
> ```

---

### Step 3 — Prepare the soundcork Server

The soundcork server runs as a Docker container. It must be running and reachable from the speaker **before** the speaker is configured to use it.

#### 3.1 — Install Docker

Follow the [official Docker installation guide](https://docs.docker.com/engine/install/) for your system. On Debian/Ubuntu:
```sh
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER   # log out and back in afterwards
```

#### 3.2 — Copy the required files to your server

Copy the following files from this repository to a working directory on your server (e.g. `/opt/soundcork`). Note that `docker-compose.nas.yml` is renamed to `docker-compose.yml` so that Docker Compose picks it up automatically:

```sh
mkdir -p /opt/soundcork/soundcork/resources
scp docker-compose.nas.yml  user@server:/opt/soundcork/docker-compose.yml
scp nginx-ETag.conf          user@server:/opt/soundcork/
scp .env.example             user@server:/opt/soundcork/
scp setup-speaker.sh         user@server:/opt/soundcork/
scp soundcork/resources/OverrideSdkPrivateCfg.xml.template \
    user@server:/opt/soundcork/soundcork/resources/
```

The resulting layout before first run:

```
/opt/soundcork/
├── docker-compose.yml
├── nginx-ETag.conf
├── .env.example
├── setup-speaker.sh
└── soundcork/
    └── resources/
        └── OverrideSdkPrivateCfg.xml.template
```

#### 3.3 — Create the configuration file

```sh
cd /opt/soundcork
cp .env.example .env
```

Edit `.env` and fill in both values:

```
# IP and port of this server, as reachable by the speaker over LAN.
# Use port 8001 (nginx-ETag proxy) for the speaker-facing URL.
BASE_URL=http://192.168.1.100:8001

# IP of the SoundTouch speaker (needed by setup-speaker.sh)
SPEAKER_IP=192.168.1.200
```

> Use the **LAN (Ethernet) IP** of your server for `BASE_URL` at this stage. You will update it to the WiFi IP in a later step if needed.

#### 3.4 — Create data and log directories

```sh
mkdir -p /opt/soundcork/data /opt/soundcork/logs
```

#### 3.5 — Start the soundcork containers

```sh
docker compose up -d
```

This starts two containers:
- **soundcork** — the main API server on port 8000
- **nginx-ETag** — a reverse proxy on port 8001 that adds the `ETag` response header in the casing required by the SoundTouch firmware

Verify that the server is responding:
```sh
curl http://localhost:8000/
# Expected: {"Bose":"Can't Brick Us"}
```

---

### Step 4 — Configure the Speaker

`setup-speaker.sh` is a host-side script — it runs directly on the server, **not** inside the Docker container. It only needs `bash`, `ssh`, `sed`, and `curl`, which are standard Linux tools. No Python or full repository clone is required; the two files already copied in step 3.2 (`setup-speaker.sh` and `soundcork/resources/OverrideSdkPrivateCfg.xml.template`) are sufficient.

Before running the script, the `/mnt/nv` partition on the speaker must be remounted with write access — it is read-only by default. Connect via SSH and run:

```sh
ssh -o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedAlgorithms=+ssh-rsa root@SPEAKER_IP \
    "mount -o remount,rw /mnt/nv"
```

> **Note:** This remount is temporary and resets to read-only after a reboot. `setup-speaker.sh` performs the remount automatically each time it runs, so you only need to do this manually if you are editing files on the speaker by hand.

Run it once from the deploy directory. It reads `SPEAKER_IP` and `BASE_URL` from `.env` and performs all speaker configuration automatically:

```sh
cd /opt/soundcork
chmod +x setup-speaker.sh
./setup-speaker.sh
```

The script does the following:

1. **Remounts `/mnt/nv` read-write** on the speaker so that configuration files can be written.

2. **Writes `/mnt/nv/OverrideSdkPrivateCfg.xml`** on the speaker — this redirects all Bose cloud service URLs (Marge, BMX, swUpdate, stats) to your soundcork server. The template used is `soundcork/resources/OverrideSdkPrivateCfg.xml.template`. The file will contain entries like:
   ```xml
   <SoundTouchSdkPrivateCfg>
       <margeServerUrl>http://192.168.1.100:8001/marge</margeServerUrl>
       <bmxRegistryUrl>http://192.168.1.100:8001/bmx/registry/v1/services</bmxRegistryUrl>
       ...
   </SoundTouchSdkPrivateCfg>
   ```

3. **Writes `/mnt/nv/rc.local`** on the speaker — this installs a persistent `nc` relay that listens on `127.0.0.1:30034` and forwards all connections to `SERVER_IP:8000`. The file survives reboots because `/mnt/nv/` is on a persistent partition. On every boot the speaker executes `rc.local`, which starts the relay in the background.

4. **Starts the relay immediately** by executing `rc.local` over SSH (no reboot needed for the relay itself).

5. **Reboots the speaker** if the XML config was changed, so that the new server URLs take effect.

6. **Waits for the speaker to come back online** (typically 60–90 seconds) and reports success.

---

### Step 5 — Populate the soundcork Data Store

soundcork needs a copy of the speaker's Presets, Recents, Sources, and DeviceInfo to serve them back to the speaker. Retrieve the device ID and account ID first:

```sh
curl http://SPEAKER_IP:8090/info
```

Example response:
```xml
<info deviceID="A0B1C2D3E4F5">
  <name>Living Room</name>
  <type>SoundTouch 20</type>
  <margeAccountUUID>1234567</margeAccountUUID>
  ...
</info>
```

Create the directory structure in `data/` (replace values from the response above):
```sh
mkdir -p /opt/soundcork/data/1234567/devices/A0B1C2D3E4F5
```

Fetch `Presets.xml`, `Recents.xml`, and `DeviceInfo.xml` from the speaker's built-in HTTP server and save them to the correct locations:
```sh
DATA=/opt/soundcork/data/1234567
curl http://SPEAKER_IP:8090/presets > $DATA/Presets.xml
curl http://SPEAKER_IP:8090/recents > $DATA/Recents.xml
curl http://SPEAKER_IP:8090/info    > $DATA/devices/A0B1C2D3E4F5/DeviceInfo.xml
```

`Sources.xml` is not exposed via the HTTP server, so it must be copied from the speaker directly over SSH:
```sh
ssh -o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedAlgorithms=+ssh-rsa root@SPEAKER_IP \
    "cat /mnt/nv/BoseApp-Persistence/1/Sources.xml" > $DATA/Sources.xml
```

> **Note on `Sources.xml`:** Each `<source>` element needs a unique `id` attribute. soundcork will assign IDs automatically if they are missing, but it is safer to assign them explicitly:
> ```xml
> <source displayName="AUX IN" id="100001" secret="">
> ```

After completing all the steps above, the working directory and data store should look like this (with your actual `AccountUUID` and `deviceID` in place of the placeholders):

```
soundcork-deploy/
├── docker-compose.yml
├── .env
├── .env.example
├── nginx-ETag.conf
├── logs/
└── data/
    ├── Accounts.json
    ├── AccountUUID/
    │   ├── Presets.xml
    │   ├── Recents.xml
    │   ├── Sources.xml
    │   └── devices/
    │       └── deviceID/
    │           └── DeviceInfo.xml
    └── devices/
        └── deviceID/
            └── PowerOn.xml
```

`Accounts.json` and `PowerOn.xml` are created automatically by soundcork on first contact with the speaker — you do not need to create them manually.

---

### Step 6 — Verify LAN Operation

After the speaker has rebooted, check that it is connecting to soundcork:

1. Open the soundcork admin UI in a browser: `http://SERVER_IP:8001/admin`
2. The speaker should appear in the list. The "Marge" column should show that it is connected to your soundcork instance, not to `streaming.bose.com`.
3. Check the Docker logs for incoming requests from the speaker:
   ```sh
   docker compose logs -f soundcork
   ```
4. Test basic playback — switch between presets and verify that audio plays.

---

### Step 7 — Switch to WiFi (Optional)

If your server is connected via Ethernet and the speaker will eventually run over WiFi (e.g. you want to remove the Ethernet cable from the speaker), the server's IP address may change depending on whether the server itself uses Ethernet or WiFi. If `BASE_URL` needs to change (e.g. the server now has a different IP on WiFi), update it and re-run the setup script:

1. Update `BASE_URL` in `/opt/soundcork/.env` to the new IP of the server:
   ```
   BASE_URL=http://192.168.1.110:8001
   ```
2. Re-run the setup script — it detects that the XML on the speaker has changed, updates it, and reboots the speaker:
   ```sh
   ./setup-speaker.sh
   ```
3. Connect the speaker to WiFi via the Bose SoundTouch app if it is not already configured for WiFi, then remove the Ethernet cable.
4. After the speaker reconnects over WiFi, verify operation as described in Step 6.

> **Files changed by `setup-speaker.sh`:**
> - `.env` — edit `BASE_URL` manually before re-running the script.
> - `/mnt/nv/OverrideSdkPrivateCfg.xml` on the speaker — updated with the new server URL.
> - `/mnt/nv/rc.local` on the speaker — updated with the new relay target IP.

---

## Updating soundcork

To pull the latest container image and restart:

```sh
docker compose pull
docker compose up -d
```

---

## More Information

- [Deployment Guide](docs/deployment.md) — all deployment options (Docker, Kubernetes, bare metal)
- [API Specification](docs/API_Spec.md)
- [Developer Wiki](https://github.com/deborahgu/soundcork/wiki/)
- [Contributing](CONTRIBUTING.md)
- [Security Policy](SECURITY.md)


