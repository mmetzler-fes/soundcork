"""
Endpoints for a miniapp UI.
"""

import json
import logging
import urllib.request
import urllib.parse
from typing import TYPE_CHECKING
from urllib.parse import quote, unquote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from soundcork.config import Settings
from soundcork.constants import DEFAULT_DEVICE_IMAGE, DEVICE_IMAGE_MAP
from soundcork.datastore import DataStore
from soundcork.ui.speakers import Speakers

if TYPE_CHECKING:
    from soundcork.model import Preset

logger = logging.getLogger(__name__)

# radio-browser.info API base — uses one of the community DNS-round-robin servers
_RADIO_BROWSER_API = "https://de1.api.radio-browser.info/json"


def encode_cookie_value(value: object) -> str:
    """Encode text for Set-Cookie's latin-1 constrained header value."""
    return quote(str(value), safe="")


def decode_cookie_value(value: str | None, default: str | None = None) -> str | None:
    if value is None:
        return default
    return unquote(value)


def get_device_image(product_code: str) -> str:
    """Map product code to device image file."""
    return DEVICE_IMAGE_MAP.get(product_code.lower(), DEFAULT_DEVICE_IMAGE)


def get_miniapp_router(datastore: DataStore, speakers: Speakers, settings: Settings | None = None):
    import os as _os
    templates = Jinja2Templates(directory=_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "templates"))

    router = APIRouter(tags=["miniapp"])

    @router.get("/miniapp", response_class=HTMLResponse)
    async def main_page(request: Request):
        """Redirect to login or dashboard based on session."""
        account_id = request.cookies.get("soundcork_account_id")
        if account_id and datastore.account_exists(account_id):
            return RedirectResponse(url="/miniapp/dashboard", status_code=303)
        else:
            return RedirectResponse(url="/miniapp/login", status_code=303)

    @router.get("/miniapp/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        """Display login page with account selection."""
        try:
            account_ids = datastore.list_accounts()
            accounts_data = {}

            for account_id in account_ids:
                if account_id:
                    try:
                        label = datastore.get_account_info(account_id)
                        device_count = len(datastore.list_devices(account_id))
                        accounts_data[account_id] = {
                            "label": label,
                            "device_count": device_count,
                        }
                    except Exception as e:
                        logger.error(
                            f"Error getting info for account {account_id}: {e}"
                        )
                        continue

            logger.info(f"Rendering login with {len(accounts_data)} accounts")
            return templates.TemplateResponse(
                request=request,
                name="login.html",
                context={"accounts": accounts_data, "error": None},
            )
        except Exception as e:
            logger.error(f"Error rendering login page: {e}")
            return templates.TemplateResponse(
                request=request,
                name="login.html",
                context={"accounts": {}, "error": "Error loading accounts"},
            )

    @router.post("/miniapp/login")
    async def login_submit(request: Request):
        """Handle account selection and set cookie."""
        try:
            form_data = await request.form()
            account_id_raw = form_data.get("account_id")

            if not account_id_raw or not isinstance(account_id_raw, str):
                return RedirectResponse(
                    url="/miniapp/login?error=No account selected", status_code=303
                )

            account_id: str = account_id_raw

            # Verify account exists
            if not datastore.account_exists(account_id):
                return RedirectResponse(
                    url="/miniapp/login?error=Invalid account", status_code=303
                )

            # Get account label
            account_label = datastore.get_account_info(account_id)

            # Create response with redirect
            response = RedirectResponse(url="/miniapp/dashboard", status_code=303)

            # Set cookies for account
            response.set_cookie(
                key="soundcork_account_id",
                value=account_id,
                max_age=86400 * 30,  # 30 days
                httponly=True,
                samesite="strict",
            )
            response.set_cookie(
                key="soundcork_account_label",
                value=encode_cookie_value(account_label),
                max_age=86400 * 30,
                httponly=False,  # Allow JS to read for display
                samesite="strict",
            )

            logger.info(f"User logged in to account {account_id}")
            return response

        except Exception as e:
            logger.error(f"Error during login: {e}")
            return RedirectResponse(
                url="/miniapp/login?error=Login failed", status_code=303
            )

    @router.get("/miniapp/dashboard", response_class=HTMLResponse)
    async def dashboard_page(request: Request):
        """Display dashboard with devices and presets."""
        account_id = ""
        try:
            # Get account from cookie
            account_id = request.cookies.get("soundcork_account_id", "")
            account_label = decode_cookie_value(
                request.cookies.get("soundcork_account_label"), "Unknown Account"
            )

            if not account_id:
                return RedirectResponse(url="/miniapp/login", status_code=303)

            # Verify account still exists
            if not datastore.account_exists(account_id):
                response = RedirectResponse(url="/miniapp/login", status_code=303)
                response.delete_cookie("soundcork_account_id")
                response.delete_cookie("soundcork_account_label")
                return response

            # Get devices and speakers for this account
            combined_devices = speakers.all_devices()
            my_combined_devices = {
                device_id: cd
                for device_id, cd in combined_devices.items()
                if cd.account == account_id
            }

            devices: list[dict[str, str]] = []
            presets: list["Preset"] = []

            for device_id in my_combined_devices.keys():
                try:
                    ready = "offline"
                    cd = my_combined_devices[device_id]
                    device_info = datastore.get_device_info(account_id, device_id)
                    if (
                        cd.online
                        and cd.in_soundcork
                        and cd.marge_server.startswith("Soundcork")
                    ):
                        ready = "online"
                    devices.append(
                        {
                            "name": device_info.name,
                            "product_code": device_info.product_code,
                            "device_id": device_info.device_id,
                            "status": ready,
                            "image_file": get_device_image(device_info.product_code),
                        }
                    )

                    if not presets:
                        try:
                            presets = datastore.get_presets(account_id)
                        except Exception as e:
                            logger.warning(
                                f"Error getting presets for device {device_id}: {e}"
                            )

                except Exception as e:
                    logger.error(f"Error getting device info for {device_id}: {e}")
                    continue

            logger.info(
                f"Rendering dashboard for account {account_id} with {len(devices)} devices and {len(presets)} presets"
            )

            # Get selected content_item and device from cookies
            selected_content_item = decode_cookie_value(
                request.cookies.get("soundcork_selected_content_item_name")
            )
            selected_device = decode_cookie_value(
                request.cookies.get("soundcork_selected_device")
            )
            selected_device_id = request.cookies.get("soundcork_selected_device_id")
            is_playing = request.cookies.get("soundcork_is_playing", "false")
            ma_url = getattr(settings, "music_assistant_url", "") if settings else ""

            return templates.TemplateResponse(
                request=request,
                name="dashboard.html",
                context={
                    "account_id": account_id,
                    "account_label": account_label,
                    "devices": devices,
                    "presets": presets,
                    "selected_content_item": selected_content_item,
                    "selected_device": selected_device,
                    "selected_device_id": selected_device_id,
                    "is_playing": is_playing,
                    "music_assistant_url": ma_url,
                    "error": None,
                },
            )

        except Exception as e:
            logger.error(f"Error rendering dashboard: {e}")

            # Still try to get selected content_item/device from cookies
            selected_content_item = decode_cookie_value(
                request.cookies.get("soundcork_selected_content_item_name")
            )
            selected_device = decode_cookie_value(
                request.cookies.get("soundcork_selected_device")
            )
            selected_device_id = request.cookies.get("soundcork_selected_device_id")
            is_playing = request.cookies.get("soundcork_is_playing", "false")
            ma_url = getattr(settings, "music_assistant_url", "") if settings else ""

            return templates.TemplateResponse(
                request=request,
                name="dashboard.html",
                context={
                    "account_id": account_id,
                    "account_label": "Unknown",
                    "devices": [],
                    "presets": [],
                    "selected_content_item": selected_content_item,
                    "selected_device": selected_device,
                    "selected_device_id": selected_device_id,
                    "is_playing": is_playing,
                    "music_assistant_url": ma_url,
                    "error": "Error loading dashboard data",
                },
            )

    @router.post("/miniapp/select-content-item")
    async def select_content_item(request: Request):
        """Handle content_item selection and set cookie."""
        try:
            form_data = await request.form()
            content_item_id = form_data.get("content_item_id")
            content_item_name = form_data.get("content_item_name")

            if (
                not isinstance(content_item_id, str)
                or not isinstance(content_item_name, str)
                or not content_item_id
                or not content_item_name
            ):
                return RedirectResponse(url="/miniapp/dashboard", status_code=303)

            response = RedirectResponse(url="/miniapp/dashboard", status_code=303)
            response.set_cookie(
                key="soundcork_selected_content_item_name",
                value=encode_cookie_value(content_item_name),
                max_age=86400 * 30,  # 30 days
                httponly=False,
                samesite="strict",
            )
            response.set_cookie(
                key="soundcork_selected_content_item_id",
                value=content_item_id,
                max_age=86400 * 30,  # 30 days
                httponly=False,
                samesite="strict",
            )

            selected_device_id = request.cookies.get("soundcork_selected_device_id")
            if selected_device_id:
                success = speakers.play_content_item(
                    selected_device_id, content_item_id
                )
                response.set_cookie(
                    key="soundcork_is_playing",
                    value="true" if success else "false",
                    max_age=86400 * 30,
                    httponly=False,
                    samesite="strict",
                )
                if success:
                    logger.info(
                        f"Started playback from preset click: content_item {content_item_id} on device {selected_device_id}"
                    )
                else:
                    logger.error("Failed to start playback from preset click")

            return response

        except Exception as e:
            logger.error(f"Error selecting content_item: {e}")
            return RedirectResponse(url="/miniapp/dashboard", status_code=303)

    @router.post("/miniapp/select-device")
    async def select_device(request: Request):
        """Handle device selection and set cookie."""
        try:
            form_data = await request.form()
            device_id = form_data.get("device_id")
            device_name = form_data.get("device_name")

            if (
                not isinstance(device_id, str)
                or not isinstance(device_name, str)
                or not device_id
                or not device_name
            ):
                return RedirectResponse(url="/miniapp/dashboard", status_code=303)

            response = RedirectResponse(url="/miniapp/dashboard", status_code=303)
            response.set_cookie(
                key="soundcork_selected_device",
                value=encode_cookie_value(device_name),
                max_age=86400 * 30,  # 30 days
                httponly=False,
                samesite="strict",
            )
            # Also store device_id for future use
            response.set_cookie(
                key="soundcork_selected_device_id",
                value=device_id,
                max_age=86400 * 30,
                httponly=True,
                samesite="strict",
            )
            logger.info(f"Device selected: {device_name} ({device_id})")
            return response

        except Exception as e:
            logger.error(f"Error selecting device: {e}")
            return RedirectResponse(url="/miniapp/dashboard", status_code=303)

    @router.post("/miniapp/play")
    async def play(request: Request):
        """Play the selected content_item on the selected device."""
        try:
            # Get content_item and device from cookies
            selected_content_item = decode_cookie_value(
                request.cookies.get("soundcork_selected_content_item_name")
            )
            selected_content_item_id = request.cookies.get(
                "soundcork_selected_content_item_id"
            )
            selected_device_id = request.cookies.get("soundcork_selected_device_id")

            if not selected_content_item or not selected_device_id:
                logger.warning("Cannot play: content_item or device not selected")
                return RedirectResponse(url="/miniapp/dashboard", status_code=303)

            logger.info(
                f"content_item: {selected_content_item}, {selected_content_item_id}"
            )

            # Play the content_item
            success = speakers.play_content_item(
                selected_device_id, str(selected_content_item_id)
            )

            response = RedirectResponse(url="/miniapp/dashboard", status_code=303)
            if success:
                response.set_cookie(
                    key="soundcork_is_playing",
                    value="true",
                    max_age=86400 * 30,
                    httponly=False,
                    samesite="strict",
                )
                logger.info(
                    f"Started playback: content_item {selected_content_item_id} on device {selected_device_id}"
                )
            else:
                logger.error("Failed to start playback")

            return response

        except Exception as e:
            logger.error(f"Error in play endpoint: {e}")
            return RedirectResponse(url="/miniapp/dashboard", status_code=303)

    @router.post("/miniapp/play-preset")
    async def play_preset(request: Request):
        """Select a preset and immediately play it if a device is already selected."""
        try:
            form_data = await request.form()
            content_item_id_raw = form_data.get("content_item_id")
            content_item_name_raw = form_data.get("content_item_name")

            if not content_item_id_raw or not content_item_name_raw:
                return RedirectResponse(url="/miniapp/dashboard", status_code=303)

            content_item_id = str(content_item_id_raw)
            content_item_name = str(content_item_name_raw)

            selected_device_id = request.cookies.get("soundcork_selected_device_id")

            response = RedirectResponse(url="/miniapp/dashboard", status_code=303)
            # Always store the selected content item
            response.set_cookie(
                key="soundcork_selected_content_item_name",
                value=content_item_name,
                max_age=86400 * 30,
                httponly=False,
                samesite="strict",
            )
            response.set_cookie(
                key="soundcork_selected_content_item_id",
                value=content_item_id,
                max_age=86400 * 30,
                httponly=False,
                samesite="strict",
            )

            # If a device is already selected, play immediately
            if selected_device_id:
                success = speakers.play_content_item(selected_device_id, content_item_id)
                if success:
                    response.set_cookie(
                        key="soundcork_is_playing",
                        value="true",
                        max_age=86400 * 30,
                        httponly=False,
                        samesite="strict",
                    )
                    logger.info(
                        f"Preset {content_item_name} ({content_item_id}) started on device {selected_device_id}"
                    )
                else:
                    logger.error(
                        f"Failed to play preset {content_item_id} on device {selected_device_id}"
                    )
            else:
                # Auto-select the only online device if there is exactly one
                all_devs = speakers.all_devices()
                online_devs = [
                    (did, cd)
                    for did, cd in all_devs.items()
                    if cd.online and cd.in_soundcork and cd.marge_server.startswith("Soundcork")
                ]
                if len(online_devs) == 1:
                    auto_device_id, auto_cd = online_devs[0]
                    response.set_cookie(
                        key="soundcork_selected_device",
                        value=auto_cd.name,
                        max_age=86400 * 30,
                        httponly=False,
                        samesite="strict",
                    )
                    response.set_cookie(
                        key="soundcork_selected_device_id",
                        value=auto_device_id,
                        max_age=86400 * 30,
                        httponly=True,
                        samesite="strict",
                    )
                    success = speakers.play_content_item(auto_device_id, content_item_id)
                    if success:
                        response.set_cookie(
                            key="soundcork_is_playing",
                            value="true",
                            max_age=86400 * 30,
                            httponly=False,
                            samesite="strict",
                        )
                        logger.info(
                            f"Preset {content_item_name} auto-started on only online device {auto_device_id}"
                        )
                    else:
                        logger.error(
                            f"Failed to play preset {content_item_id} on auto-selected device {auto_device_id}"
                        )
                else:
                    logger.info(
                        f"Preset {content_item_name} selected; no device chosen yet – skipping playback"
                    )

            return response

        except Exception as e:
            logger.error(f"Error in play-preset endpoint: {e}")
            return RedirectResponse(url="/miniapp/dashboard", status_code=303)

    @router.post("/miniapp/stop")
    async def stop(request: Request):
        """Stop playback on the selected device."""
        try:
            selected_device_id = request.cookies.get("soundcork_selected_device_id")

            if not selected_device_id:
                logger.warning("Cannot stop: device not selected")
                return RedirectResponse(url="/miniapp/dashboard", status_code=303)

            # Stop playback
            success = speakers.stop_playback(selected_device_id)

            response = RedirectResponse(url="/miniapp/dashboard", status_code=303)
            if success:
                response.set_cookie(
                    key="soundcork_is_playing",
                    value="false",
                    max_age=86400 * 30,
                    httponly=False,
                    samesite="strict",
                )
                logger.info(f"Stopped playback on device {selected_device_id}")
            else:
                logger.error("Failed to stop playback")

            return response

        except Exception as e:
            logger.error(f"Error in stop endpoint: {e}")
            return RedirectResponse(url="/miniapp/dashboard", status_code=303)

    @router.post("/miniapp/logout")
    async def logout(request: Request):
        """Clear session and redirect to login."""
        response = RedirectResponse(url="/miniapp/login", status_code=303)
        response.delete_cookie("soundcork_account_id")
        response.delete_cookie("soundcork_account_label")
        response.delete_cookie("soundcork_selected_content_item_name")
        response.delete_cookie("soundcork_selected_content_item_id")
        response.delete_cookie("soundcork_selected_device")
        response.delete_cookie("soundcork_selected_device_id")
        response.delete_cookie("soundcork_is_playing")
        logger.info("User logged out")
        return response

    # ------------------------------------------------------------------
    # Volume
    # ------------------------------------------------------------------

    @router.get("/miniapp/volume")
    async def get_volume(request: Request):
        """Return the current volume (0-100) of the selected device as JSON."""
        device_id = request.cookies.get("soundcork_selected_device_id")
        if not device_id:
            return JSONResponse({"error": "no device selected"}, status_code=400)
        level = speakers.get_volume(device_id)
        if level is None:
            return JSONResponse({"error": "device unreachable"}, status_code=503)
        return JSONResponse({"volume": level})

    @router.post("/miniapp/volume")
    async def set_volume(request: Request):
        """Set the volume of the selected device. Body: JSON {\"volume\": 0-100}."""
        device_id = request.cookies.get("soundcork_selected_device_id")
        if not device_id:
            return JSONResponse({"error": "no device selected"}, status_code=400)
        try:
            body = await request.json()
            level = int(body.get("volume", 50))
        except Exception:
            return JSONResponse({"error": "invalid body"}, status_code=400)
        success = speakers.set_volume(device_id, level)
        if success:
            return JSONResponse({"ok": True, "volume": level})
        return JSONResponse({"error": "failed to set volume"}, status_code=503)

    # ------------------------------------------------------------------
    # Radio Browser
    # ------------------------------------------------------------------

    @router.get("/miniapp/radio")
    async def radio_search(request: Request, q: str = ""):
        """Search radio-browser.info and return matching stations as JSON."""
        stations: list[dict] = []
        try:
            if q.strip():
                api_url = (
                    f"{_RADIO_BROWSER_API}/stations/search"
                    f"?name={urllib.parse.quote_plus(q)}&limit=40&hidebroken=true"
                    f"&order=clickcount&reverse=true"
                )
            else:
                # Return the 40 most popular stations when no query is given
                api_url = (
                    f"{_RADIO_BROWSER_API}/stations/search"
                    f"?limit=40&hidebroken=true&order=clickcount&reverse=true"
                )
            req = urllib.request.Request(
                api_url,
                headers={"User-Agent": "soundcork/1.0"},
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode())
            for s in data:
                stations.append(
                    {
                        "uuid": s.get("stationuuid", ""),
                        "name": s.get("name", ""),
                        "url": s.get("url_resolved") or s.get("url", ""),
                        "favicon": s.get("favicon", ""),
                        "country": s.get("countrycode", ""),
                        "tags": s.get("tags", ""),
                        "bitrate": s.get("bitrate", 0),
                    }
                )
        except Exception as e:
            logger.error(f"radio-browser.info query failed: {e}")
            return JSONResponse({"error": "radio-browser.info unreachable"}, status_code=503)
        return JSONResponse({"stations": stations})

    @router.post("/miniapp/play-radio")
    async def play_radio(request: Request):
        """Play a radio-browser.info station on the selected device."""
        device_id = request.cookies.get("soundcork_selected_device_id")
        if not device_id:
            return JSONResponse({"error": "no device selected"}, status_code=400)
        try:
            body = await request.json()
            stream_url = str(body.get("url", "")).strip()
            name = str(body.get("name", "Internet Radio")).strip()
        except Exception:
            return JSONResponse({"error": "invalid body"}, status_code=400)
        if not stream_url:
            return JSONResponse({"error": "missing url"}, status_code=400)
        success = speakers.play_radio_station(device_id, stream_url, name)
        if success:
            return JSONResponse({"ok": True})
        return JSONResponse({"error": "playback failed"}, status_code=503)

    # ------------------------------------------------------------------
    # Music Assistant
    # ------------------------------------------------------------------

    @router.get("/miniapp/music-assistant-url")
    async def music_assistant_url(request: Request):
        """Return the configured Music Assistant URL."""
        ma_url = getattr(settings, "music_assistant_url", "") if settings else ""
        return JSONResponse({"url": ma_url or ""})

    @router.post("/miniapp/play-music-assistant")
    async def play_music_assistant(request: Request):
        """Play the Music Assistant stream on the selected SoundTouch device."""
        device_id = request.cookies.get("soundcork_selected_device_id")
        if not device_id:
            return JSONResponse({"error": "no device selected"}, status_code=400)
        ma_url = getattr(settings, "music_assistant_url", "") if settings else ""
        ma_stream_url = getattr(settings, "music_assistant_stream_url", "") if settings else ""
        if not ma_stream_url:
            return JSONResponse(
                {"error": "music_assistant_stream_url not configured"},
                status_code=503,
            )
        success = speakers.play_radio_station(device_id, ma_stream_url, "Music Assistant")
        if success:
            return JSONResponse({"ok": True, "ma_url": ma_url})
        return JSONResponse({"error": "playback failed"}, status_code=503)

    return router
