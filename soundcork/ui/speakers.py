import logging
import re
import urllib.request
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

from bosesoundtouchapi.soundtouchclient import (  # type: ignore
    ContentItem as BCContentItem,
    SoundTouchClient,
    SoundTouchDevice,
)
from bosesoundtouchapi.soundtouchdiscovery import SoundTouchDiscovery  # type: ignore
from pydantic import BaseModel

from soundcork.config import Settings
from soundcork.datastore import DataStore
from soundcork.model import ContentItem

logger = logging.getLogger(__name__)

BOSE_HTTP_PORT = 8090


def _get_speaker_status(ip: str) -> tuple[bool, str]:
    """Check if a speaker responds on port 8090 and return (reachable, margeURL)."""
    if not ip:
        logger.debug("_get_speaker_status: empty ip")
        return False, ""
    try:
        url = f"http://{ip}:{BOSE_HTTP_PORT}/info"
        logger.debug(f"_get_speaker_status: fetching {url}")
        with urllib.request.urlopen(url, timeout=5) as resp:
            if resp.status != 200:
                logger.debug(f"_get_speaker_status: status {resp.status}")
                return False, ""
            xml = resp.read()
        root = ET.fromstring(xml)
        marge_url = root.findtext("margeURL") or ""
        logger.info(
            f"_get_speaker_status: ip={ip} reachable=True margeURL={marge_url!r}"
        )
        return True, marge_url
    except Exception as e:
        logger.warning(f"_get_speaker_status: ip={ip} failed: {e!r}")
        return False, ""


def _is_speaker_reachable(ip: str) -> bool:
    reachable, _ = _get_speaker_status(ip)
    return reachable


class CombinedDevice(BaseModel):
    """Device: either detected, configured, or both

    A Device that's at least one of:
    - A physical SoundTouch speaker detected on the network
    - A configured DeviceInfo block stored in the datastore.

    Property:
    - id: Bose-issued unique speaker ID from DeviceInfo
    - ip: The speaker's IP address
    - name: Human-readable speaker name
    - online: Discoverable on the network as of last-update to this object. Not updated on disconnect.
    - account: Account ID
    - in_soundcork: In the soundcork datastore
    - marge_server: API this speaker uses for Marge: (ie. Bose, or this Soundcork instance)
    - reachable:  Has been configured (ie. with a USB key) to have shell-access available.
    - st_device: SoundTouchDevice instance as discovered by BoseSoundTouchApi
    """

    id: str
    ip: str
    name: str
    online: bool
    account: str
    in_soundcork: bool
    marge_server: str
    reachable: bool
    st_device: SoundTouchDevice | None

    class Config:
        arbitrary_types_allowed = True


class Speakers:
    """
    This class contains methods used to interact with speakers, primarily through the
    bosesoundtouchapi package (https://github.com/thlucas1/bosesoundtouchapi)
    """

    def __init__(self, datastore: DataStore, settings: Settings) -> None:
        self._st_discovery = SoundTouchDiscovery(areDevicesVerified=True)
        self._st_discovery.DiscoverDevices(timeout=1)
        self._datastore = datastore
        self._settings = settings

    def soundtouch_devices(self) -> dict:
        self._st_discovery.DiscoverDevices(timeout=2)
        return self._st_discovery.VerifiedDevices

    def clear_device(self, device_id: str):
        cd = self.all_devices().get(device_id)
        if cd:
            st = cd.st_device
            if st:
                self._st_discovery.VerifiedDevices.pop(f"{st.Host}:8090")
                self._st_discovery.DiscoveredDeviceNames.pop(f"{st.Host}:8090")

    def device_by_id(self, ip_port: str) -> SoundTouchDevice:
        logger.debug(f"Getting device by id: {ip_port}")
        return self._st_discovery.VerifiedDevices.get(ip_port)

    def all_devices(self) -> dict[str, CombinedDevice]:
        """
        Returns a combination of all devices seen on the network and
        all devices configured in soundcork as a dict with the device
        id as the key
        """
        combined_devices = {}
        account_ids = self._datastore.list_accounts()
        print(f"account_ids {account_ids}")
        for account_id in account_ids:
            if account_id:
                for device_id in self._datastore.list_devices(account_id):
                    if device_id:
                        device_info = self._datastore.get_device_info(
                            account_id, device_id
                        )
                        reachable, marge_url = _get_speaker_status(
                            device_info.ip_address
                        )
                        logger.info(
                            f"all_devices: device={device_id} ip={device_info.ip_address!r} "
                            f"reachable={reachable} marge_url={marge_url!r} "
                            f"base_url={self._settings.base_url!r}"
                        )
                        if marge_url == "https://streaming.bose.com":
                            marge_server = "Bose"
                        elif (
                            self._settings.base_url
                            and urlparse(marge_url).hostname
                            == urlparse(self._settings.base_url).hostname
                            and marge_url.rstrip("/").endswith("/marge")
                        ):
                            marge_server = "Soundcork"
                        elif marge_url:
                            marge_server = marge_url
                        else:
                            marge_server = "Unknown"
                        logger.info(
                            f"all_devices: device={device_id} marge_server={marge_server!r}"
                        )
                        cd = CombinedDevice(
                            # If the IP changes on a device reboot, it would have made a `/power_on`
                            # call to Soundcork, which will have already updated the datastore.
                            id=device_id,
                            ip=device_info.ip_address,
                            name=device_info.name,
                            online=reachable,
                            account=account_id,
                            in_soundcork=True,
                            marge_server=marge_server,
                            reachable=False,
                            st_device=None,
                        )
                        combined_devices[device_id] = cd
                        logger.debug(
                            f"cd for {device_id} = {combined_devices[device_id]}"
                        )

        verified = self.soundtouch_devices()
        for key in verified.keys():
            st_device = verified[key]
            id = st_device.DeviceId
            sc_device = combined_devices.get(id, None)

            if sc_device:
                sc_device.online = True
                sc_device.st_device = st_device
            else:
                new_cd = CombinedDevice(
                    id=id,
                    ip=st_device.Host,
                    name=st_device.DeviceName,
                    online=True,
                    account=st_device.StreamingAccountUUID,
                    in_soundcork=False,
                    marge_server=st_device.StreamingUrl,
                    reachable=False,
                    st_device=st_device,
                )
                combined_devices[id] = new_cd
                sc_device = new_cd
            if st_device.StreamingUrl == "https://streaming.bose.com":
                sc_device.marge_server = "Bose"
            elif (
                self._settings.base_url
                and urlparse(st_device.StreamingUrl).hostname
                == urlparse(self._settings.base_url).hostname
                and st_device.StreamingUrl.rstrip("/").endswith("/marge")
            ):
                sc_device.marge_server = "Soundcork"
            elif not self._settings.base_url and st_device.StreamingUrl.rstrip(
                "/"
            ).endswith("/marge"):
                logger.warning(
                    f"Device {st_device.DeviceId} points to {st_device.StreamingUrl} "
                    "which looks like a Soundcork instance, but base_url is not configured. "
                    "Set base_url in .env.shared to enable proper detection."
                )
                sc_device.marge_server = f"Soundcork? (base_url not set)"
            else:
                sc_device.marge_server = f"Unknown ({st_device.StreamingUrl})"

        return combined_devices

    def _resolve_to_internet_radio(self, ci: ContentItem) -> BCContentItem | None:
        """For TUNEIN sources, resolve the actual stream URL and return a
        LOCAL_INTERNET_RADIO ContentItem using the Soundcork Orion endpoint.

        The device fetches the Orion URL directly from Soundcork to get the
        BmxPlaybackResponse, which contains the proxy stream URL.  This avoids
        all dependency on the offline Bose BMX cloud and works even after Bose
        removed TUNEIN/INTERNET_RADIO from the firmware.
        Returns None if resolution fails.
        """
        if ci.source == "TUNEIN":
            # location is like /v1/playback/station/s24896
            match = re.search(r"/v1/playback/station/(\w+)$", ci.location or "")
            if not match:
                return None
            station_id = match.group(1)
            try:
                import base64
                import json as _json

                from soundcork.bmx import tunein_playback

                resp = tunein_playback(station_id)
                if not resp.audio or not resp.audio.streamUrl:
                    return None
                base = (
                    self._settings.base_url.rstrip("/")
                    if self._settings.base_url
                    else ""
                )
                if not base:
                    logger.warning(
                        "base_url is not configured; cannot build LOCAL_INTERNET_RADIO "
                        f"URL for TUNEIN station {station_id}"
                    )
                    return None
                # Use the Soundcork stream proxy as the actual stream URL so
                # that all upstream connections originate from the server IP.
                proxy_url = f"{base}/stream/{station_id}"
                station_data = base64.urlsafe_b64encode(
                    _json.dumps(
                        {"name": ci.name, "imageUrl": "", "streamUrl": proxy_url}
                    ).encode()
                ).decode()
                orion_url = (
                    f"{base}/core02/svc-bmx-adapter-orion/prod/orion"
                    f"/station?data={station_data}"
                )
                logger.info(
                    f"Resolved TUNEIN station {station_id} to LOCAL_INTERNET_RADIO "
                    f"orion URL (proxy stream: {proxy_url})"
                )
                return BCContentItem(
                    name=ci.name,
                    source="LOCAL_INTERNET_RADIO",
                    typeValue="stationurl",
                    location=orion_url,
                    sourceAccount="",
                    isPresetable=False,
                )
            except Exception as e:
                logger.error(f"Failed to resolve TUNEIN station {station_id}: {e}")
                return None
        return None

    def _content_item_to_soundtouchclient(self, ci: ContentItem) -> BCContentItem:
        """Maps our ContentItem to a SoundTouchClient ContentItem."""
        return BCContentItem(
            name=ci.name,
            source=ci.source,
            typeValue=ci.type,
            location=ci.location,
            sourceAccount=ci.source_account,
            isPresetable=ci.is_presetable,
        )

    def play_content_item(self, device_id: str, content_item_id: str) -> bool:
        """Play a content_item on a specific device.

        Args:
            device_id: The device ID to play on
            content_item: The content item ID to play

        Returns:
            True if successful, False otherwise
        """
        cd = self.all_devices().get(device_id)
        if not cd or not cd.st_device:
            logger.error(f"Device {device_id} not found or not online")
            return False

        content_item = self._datastore.get_content_item(
            account=cd.account,
            device_id=cd.id,
            ci_id=content_item_id,
        )
        if not content_item:
            logger.error(f"{content_item_id} is not a defined ContentItem")
            return False

        logger.info(
            f"Attempting playback of content item {content_item_id} on device {device_id}"
        )
        # For cloud-dependent sources (e.g. TUNEIN), resolve the stream URL
        # locally so the device doesn't need to contact the offline Bose BMX servers.
        bose_content_item = self._resolve_to_internet_radio(content_item)
        if bose_content_item is None:
            bose_content_item = self._content_item_to_soundtouchclient(content_item)
        client = SoundTouchClient(cd.st_device)
        client.PlayContentItem(bose_content_item)

        return True

    def stop_playback(self, device_id: str) -> bool:
        """Stop playback on a specific device.

        Args:
            device_id: The device ID to stop

        Returns:
            True if successful, False otherwise
        """
        cd = self.all_devices().get(device_id)
        if not cd or not cd.st_device:
            logger.error(f"Device {device_id} not found or not online")
            return False

        client = SoundTouchClient(cd.st_device)
        try:
            client.MediaStop()
            logger.info(f"Stopped playback on device {device_id}")
            return True
        except Exception as e:
            logger.error(f"Error stopping playback on device {device_id}: {e}")
            return False

    def get_volume(self, device_id: str) -> int | None:
        """Get the current volume of a device (0-100), or None on failure."""
        cd = self.all_devices().get(device_id)
        if not cd or not cd.st_device:
            logger.error(f"Device {device_id} not found or not online")
            return None
        try:
            client = SoundTouchClient(cd.st_device)
            vol = client.GetVolume()
            return int(vol.ActualVolume)
        except Exception as e:
            logger.error(f"Error getting volume for device {device_id}: {e}")
            return None

    def set_volume(self, device_id: str, level: int) -> bool:
        """Set the volume of a device to level (0-100).

        Returns True on success, False otherwise.
        """
        level = max(0, min(100, level))
        cd = self.all_devices().get(device_id)
        if not cd or not cd.st_device:
            logger.error(f"Device {device_id} not found or not online")
            return False
        try:
            client = SoundTouchClient(cd.st_device)
            client.SetVolumeLevel(level)
            logger.info(f"Set volume to {level} on device {device_id}")
            return True
        except Exception as e:
            logger.error(f"Error setting volume for device {device_id}: {e}")
            return False

    def play_radio_station(self, device_id: str, stream_url: str, name: str) -> bool:
        """Play an internet radio stream URL directly on a device.

        Uses the LOCAL_INTERNET_RADIO source type via the Soundcork Orion proxy
        so that the device receives a well-formed playback response.

        Args:
            device_id: The device ID to play on
            stream_url: Direct HTTP audio stream URL
            name: Human-readable station name

        Returns:
            True if successful, False otherwise
        """
        import base64 as _b64
        import json as _json

        cd = self.all_devices().get(device_id)
        if not cd or not cd.st_device:
            logger.error(f"Device {device_id} not found or not online")
            return False

        base = self._settings.base_url.rstrip("/") if self._settings.base_url else ""
        if not base:
            logger.warning(
                "base_url is not configured; cannot build LOCAL_INTERNET_RADIO URL"
            )
            return False

        station_data = _b64.urlsafe_b64encode(
            _json.dumps(
                {"name": name, "imageUrl": "", "streamUrl": stream_url}
            ).encode()
        ).decode()
        orion_url = (
            f"{base}/core02/svc-bmx-adapter-orion/prod/orion"
            f"/station?data={station_data}"
        )
        try:
            bose_ci = BCContentItem(
                name=name,
                source="LOCAL_INTERNET_RADIO",
                typeValue="stationurl",
                location=orion_url,
                sourceAccount="",
                isPresetable=False,
            )
            client = SoundTouchClient(cd.st_device)
            client.PlayContentItem(bose_ci)
            logger.info(f"Started radio station '{name}' on device {device_id}")
            return True
        except Exception as e:
            logger.error(f"Error playing radio station on device {device_id}: {e}")
            return False
