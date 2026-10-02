import asyncio
import base64
import logging
from asyncio import Task
from datetime import timedelta
from io import BytesIO
from pathlib import Path

import requests
import voluptuous as vol
from PIL import Image
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_platform, config_validation as cv
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.template import Template, TemplateError
from urllib3.exceptions import NewConnectionError

from . import Pixoo
from .pixoo64._colors import get_rgb, CSS4_COLORS, render_color
from .const import DOMAIN, VERSION
from .pages._pages import special_pages
from .pixoo64 import FontManager


_LOGGER = logging.getLogger(__name__)

# Snacky: dynamische Angebotsseiten aus der lokalen Timeline-API.
SNACKY_PIXOO_PAGES_URL = "http://192.168.188.167:8099/api/pixoo-ads/pages"
SNACKY_PIXOO_PLAYLIST_URL = "http://192.168.188.167:8099/api/pixoo-playlist"
SNACKY_PIXOO_TIMEOUT = 5



async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities):
    async_add_entities([ Pixoo64(config_entry=config_entry, pixoo=hass.data[DOMAIN][config_entry.entry_id]["pixoo"]) ], True)


class Pixoo64(Entity):

    def __init__(self, pixoo: Pixoo, config_entry: ConfigEntry):
        # self._ip_address = ip_address
        self._pixoo = pixoo
        self._config_entry = config_entry
        self._base_pages = self._config_entry.options.get('pages_data', [])
        self._pages = self._build_pages_with_dynamic_ads([])
        self._scan_interval = timedelta(seconds=int(self._config_entry.options.get('scan_interval', timedelta(seconds=15))))
        self._current_page_index = -1  # Start at -1 so that the first page is 0.
        self._attr_has_entity_name = True
        self._attr_name = 'Current Page'
        self._attr_extra_state_attributes = {'TotalPages': len(self._pages)}
        _LOGGER.debug("All pages for %s: %s", self._pixoo.address, self._pages)
        self._update_task: None | Task = None

    @staticmethod
    def _is_snacky_static_offer_page(page: dict) -> bool:
        """Erkennt die bisherigen statischen ANGEBOT-Seiten, nicht die Seite 'Angebote / Im Snacky'."""
        if str(page.get("page_type", "")).lower() not in ["custom", "components"]:
            return False

        text_contents = [
            str(component.get("content", "")).strip().casefold()
            for component in page.get("components", [])
            if component.get("type") == "text"
        ]
        return "angebot" in text_contents and any(
            content.startswith("spirale ") for content in text_contents
        )

    def _build_pages_with_dynamic_ads(self, dynamic_pages: list) -> list:
        """Ersetzt die alten statischen Angebotsseiten an deren erster Position durch dynamische Seiten."""
        result = []
        inserted = False

        for page in self._base_pages:
            if self._is_snacky_static_offer_page(page):
                if not inserted:
                    result.extend(dynamic_pages)
                    inserted = True
                continue
            result.append(page)

        # Sicherheitsfallback, falls die alten Angebotsseiten später aus der
        # Config entfernt wurden: dynamische Anzeigen ans Ende hängen.
        if dynamic_pages and not inserted:
            result.extend(dynamic_pages)

        return result

    def _load_snacky_dynamic_pages(self) -> list:
        """Lädt aktive Snacky-Anzeigen. Bei Fehlern laufen nur die festen Seiten weiter."""
        response = requests.get(SNACKY_PIXOO_PAGES_URL, timeout=SNACKY_PIXOO_TIMEOUT)
        response.raise_for_status()
        payload = response.json()

        dynamic_pages = []
        for item in payload.get("pages", []):
            page_data = item.get("page_data")
            if isinstance(page_data, dict) and page_data.get("page_type"):
                dynamic_pages.append(page_data)

        return dynamic_pages

    def _load_snacky_playlist(self) -> list:
        """Lädt die konfigurierte Reihenfolge der logischen Pixoo-Karten."""
        response = requests.get(
            SNACKY_PIXOO_PLAYLIST_URL,
            timeout=SNACKY_PIXOO_TIMEOUT
        )
        response.raise_for_status()
        payload = response.json()

        cards = payload.get("cards", [])
        if not isinstance(cards, list):
            return []

        return cards

    def _load_snacky_dynamic_items(self) -> list:
        """Lädt ANGEBOT- und NEU-Seiten inklusive ihrer Metadaten."""
        response = requests.get(
            SNACKY_PIXOO_PAGES_URL,
            timeout=SNACKY_PIXOO_TIMEOUT
        )
        response.raise_for_status()
        payload = response.json()

        items = []
        for item in payload.get("pages", []):
            page_data = item.get("page_data")
            if isinstance(page_data, dict) and page_data.get("page_type"):
                items.append(item)

        return items

    @staticmethod
    def _pages_with_total_duration(pages: list, total_duration) -> list:
        """Verteilt die Dauer einer logischen Karte auf ihre einzelnen Frames."""
        if not pages:
            return []

        try:
            total = float(total_duration)
        except (TypeError, ValueError):
            return [dict(page) for page in pages]

        if total <= 0:
            return [dict(page) for page in pages]

        per_page = total / len(pages)

        result = []
        for page in pages:
            page_copy = dict(page)
            page_copy["duration"] = per_page
            result.append(page_copy)

        return result

    def _build_pages_from_playlist(
        self,
        playlist: list,
        dynamic_items: list
    ) -> list:
        """Baut die reale Pixoo-Seitenfolge aus der logischen Playlist."""

        base = self._base_pages

        fixed_cards = {
            "open_animation": base[0:6],
            "cocacola": base[6:7],
            "snacky_animation": base[7:13],
            "hitschies": base[13:14],
            "offers_intro": base[14:15],
            "redbull": base[16:17],
            "takis": base[18:19],
            "brainlicker": base[19:20],
        }

        offers = []
        returning_products = []
        new_products = []

        for item in dynamic_items:
            page_data = item.get("page_data")
            if not isinstance(page_data, dict):
                continue

            ad_type = str(item.get("ad_type", "")).casefold()

            if ad_type == "offer":
                offers.append(dict(page_data))
            elif ad_type == "returning":
                returning_products.append(dict(page_data))
            elif ad_type == "new":
                new_products.append(dict(page_data))

        result = []

        for card in playlist:
            if not card.get("active", False):
                continue

            key = str(card.get("card_key", ""))
            card_type = str(card.get("card_type", ""))
            duration = card.get("duration_seconds")

            if card_type in (
                "manual_offer",
                "manual_new",
                "manual_layout",
                "manual_text",
                "weather_current",
                "weather_forecast",
            ):
                page_data = card.get("page_data")

                if isinstance(page_data, dict):
                    page = dict(page_data)

                    if duration is not None:
                        page["duration"] = duration

                    result.append(page)

                continue

            if card_type == "custom_image":
                config = card.get("config") or {}
                image_path = config.get("image_path")

                if image_path:
                    page = {
                        "components": [
                            {
                                "type": "image",
                                "image_path": image_path,
                                "x": 0,
                                "y": 0
                            }
                        ]
                    }

                    if duration is not None:
                        page["duration"] = duration

                    result.append(page)

                continue

            if key == "offers":
                # Die Dauer jedes einzelnen Angebots stammt weiterhin aus
                # /api/pixoo-ads/pages.
                result.extend(offers)
                continue

            if key == "returning_products":
                # Für WIEDER DA bestimmt die Playlist die Dauer pro Produkt.
                for page in returning_products:
                    page_copy = dict(page)
                    if duration is not None:
                        page_copy["duration"] = duration
                    result.append(page_copy)
                continue

            if key == "new_products":
                # Für NEU bestimmt die Playlist die Dauer pro Produkt.
                for page in new_products:
                    page_copy = dict(page)
                    if duration is not None:
                        page_copy["duration"] = duration
                    result.append(page_copy)
                continue

            if key == "day_greeting":
                # Tageszeitabhängiger Snacky-Gruß.
                # Zunächst ist das Nachtmotiv als erster realer Test hinterlegt.
                from datetime import datetime
                from zoneinfo import ZoneInfo

                hour = datetime.now(ZoneInfo("Europe/Berlin")).hour

                if 5 <= hour < 11:
                    prefix = "morgen"
                elif 11 <= hour < 17:
                    prefix = "tag"
                elif 17 <= hour < 23:
                    prefix = "abend"
                else:
                    prefix = "nachteulen"

                greeting_pages = [
                    {
                        "page_type": "components",
                        "components": [
                            {
                                "type": "image",
                                "image_path": (
                                    f"/config/www/snacky/pixoo/"
                                    f"{prefix}_{frame:02d}.png"
                                ),
                                "position": [0, 0],
                                "resample_mode": "pixel_art",
                            }
                        ]
                    }
                    for frame in range(1, 7)
                ]

                result.extend(
                    self._pages_with_total_duration(
                        greeting_pages,
                        duration
                    )
                )
                continue

            pages = fixed_cards.get(key, [])
            result.extend(
                self._pages_with_total_duration(pages, duration)
            )

        return result

    async def _async_refresh_snacky_pages(self):
        """Aktualisiert die reale Pixoo-Rotation aus Playlist und dynamischen Seiten."""
        try:
            playlist, dynamic_items = await asyncio.gather(
                self.hass.async_add_executor_job(
                    self._load_snacky_playlist
                ),
                self.hass.async_add_executor_job(
                    self._load_snacky_dynamic_items
                ),
            )

            pages = self._build_pages_from_playlist(
                playlist,
                dynamic_items
            )

            if not pages:
                raise ValueError(
                    "Pixoo-Playlist enthält keine aktiven darstellbaren Seiten"
                )

            self._pages = pages
            self._attr_extra_state_attributes = {
                "TotalPages": len(self._pages)
            }

            offer_count = sum(
                1 for item in dynamic_items
                if str(item.get("ad_type", "")).casefold() == "offer"
            )
            new_count = sum(
                1 for item in dynamic_items
                if str(item.get("ad_type", "")).casefold() == "new"
            )

            _LOGGER.debug(
                "Snacky Pixoo playlist refreshed: %s cards, "
                "%s offers, %s new products, %s physical pages",
                len(playlist),
                offer_count,
                new_count,
                len(self._pages),
            )

        except Exception as exc:
            # Sicherheitsfallback:
            # Bei nicht erreichbarer Playlist/API weiterhin die festen
            # Pixoo-Seiten anzeigen, aber keine möglicherweise veralteten
            # dynamischen ANGEBOT-/NEU-Seiten weiterverwenden.
            self._pages = self._build_pages_with_dynamic_ads([])
            self._attr_extra_state_attributes = {
                "TotalPages": len(self._pages)
            }

            _LOGGER.exception(
                "SNACKY DIAG: Could not load Snacky Pixoo playlist from %s; "
                "continuing with %s fixed pages only",
                SNACKY_PIXOO_PLAYLIST_URL,
                len(self._pages),
            )

    async def async_added_to_hass(self):
        platform = entity_platform.async_get_current_platform()
        # Register the buzz service
        platform.async_register_entity_service(
            'play_buzzer',
            {
                vol.Optional('buzz_cycle_time_millis'): cv.positive_int,
                vol.Optional('idle_cycle_time_millis'): cv.positive_int,
                vol.Optional('total_time'): cv.positive_int
            },
            "async_play_buzzer"
        )

        # Register the page service
        platform.async_register_entity_service(
            "show_message",
            {
                vol.Required('page_data'): dict,
                vol.Optional('duration'): cv.positive_int,
            },
            "async_show_message"
        )

        # Register the preview service
        platform.async_register_entity_service(
            'render_preview',
            {
                vol.Required('page_number'): cv.positive_int,
            },
            "async_render_preview"
        )

        # Register the restart service
        platform.async_register_entity_service(
            'restart',
            {},
            "restart_device"
        )

        # # Register the update page service
        platform.async_register_entity_service(
            'update_page',
            {},
            "update_page"
        )

        # Continue with the setup
        if DOMAIN in self.hass.data:
            self.hass.data[DOMAIN].setdefault(self._config_entry.entry_id, {})['sensor'] =  self
        await self._async_refresh_snacky_pages()
        await self._async_next_page()

    async def async_will_remove_from_hass(self):
        """When entity is being removed from hass."""
        self.cancel_update_task()

    async def async_schedule_next_page(self, wait_time: float):
        _LOGGER.debug("Scheduling next page in %s seconds for %s", wait_time, self._pixoo.address)

        async def task():
            try:
                await asyncio.sleep(wait_time)
                await self._async_next_page()
            except asyncio.CancelledError:
                _LOGGER.debug('Next page timer cancelled for %s', self._pixoo.address)
        # Using HA's async_create_task instead of asyncio.create_task because it's better for HA.
        # (canceled in the async_will_remove_from_hass method of this file)
        self._update_task = self._config_entry.async_create_background_task(self.hass, task(), "pixoo-next-page-timer")

    async def _async_next_page(self):
        if self.hass.data[DOMAIN][self._config_entry.entry_id]['available'] is False:
            _LOGGER.debug("Device is not available. Not updating.")
            self.schedule_update_ha_state()
            await self.async_schedule_next_page(self._scan_interval.total_seconds())
            return
        _LOGGER.debug("Loading next page for %s", self._pixoo.address)

        # Am Ende jeder vollständigen Schleife die aktiven Anzeigen neu laden.
        # Änderungen über den 📺-Button greifen dadurch ohne HA-Neustart.
        if self._current_page_index == -1 or self._current_page_index >= len(self._pages) - 1:
            await self._async_refresh_snacky_pages()

        if len(self._pages) == 0:
            return

        is_enabled = None
        iteration_count = 0
        self._current_page_index = (self._current_page_index + 1) % len(self._pages)
        while not is_enabled:
            if iteration_count >= len(self._pages):
                _LOGGER.info("All pages disabled. Not updating.")
                break

            self.page = self._pages[self._current_page_index]

            try:
                is_enabled = str(Template(str(self.page.get('enabled', 'true')), self.hass).async_render())
                is_enabled = is_enabled.lower() in ['true', 'yes', '1', 'on']
            except TemplateError as e:
                _LOGGER.error(f"Error rendering enable template: {e}")
                is_enabled = False

            if is_enabled:
                try:
                    duration = int(Template(str(self.page.get('duration', self._scan_interval.total_seconds())), self.hass).async_render())
                except TemplateError as e:
                    _LOGGER.error("Template render error: %s", e)
                    duration = self._scan_interval.total_seconds()

                await self.async_schedule_next_page(duration)
                self.schedule_update_ha_state()
                try:
                    await self.hass.async_add_executor_job(self._render_page, self.page)
                except Exception as e:
                    _LOGGER.exception("Error rendering page for %s: %s", self._pixoo.address, e)
            else:
                self._current_page_index = (self._current_page_index + 1) % len(self._pages)
                iteration_count += 1

    def _render_page(self, page: dict, push: bool = True):
        pixoo = self._pixoo
        pixoo.clear()

        font_manager = None
        if self.hass and DOMAIN in self.hass.data:
            font_manager = self.hass.data[DOMAIN].get(self._config_entry.entry_id, {}).get('font_manager')
        if font_manager is None:
            font_manager = FontManager.get_instance()

        page_type = page['page_type'].lower()
        if page_type in special_pages:
            special_pages[page_type](pixoo, self.hass, page, font_manager)
            if push:
                pixoo.push()
        elif page_type == "channel":
            try:
                channel_id = Template(str(page['id']), self.hass).async_render()
            except TemplateError as e:
                _LOGGER.error(f"Error rendering channel id template: {e}")
                channel_id = page['id']
            pixoo.set_custom_page(channel_id)            
        elif page_type == "visualizer":
            try:
                visualizer_id = Template(str(page['id']), self.hass).async_render()
            except TemplateError as e:
                _LOGGER.error(f"Error rendering visualizer id template: {e}")
                visualizer_id = page['id']
            pixoo.set_visualizer(visualizer_id)            
        elif page_type == "clock":
            try:
                clock_id = Template(str(page['id']), self.hass).async_render()
            except TemplateError as e:
                _LOGGER.error(f"Error rendering clock id template: {e}")
                clock_id = page['id']
            pixoo.set_clock(clock_id)
        elif page_type == "gif":
            try:
                gif_url = Template(str(page['gif_url']), self.hass).async_render()
            except TemplateError as e:
                _LOGGER.error(f"Error rendering gif url template: {e}")
                gif_url = page['gif_url']
            pixoo.play_gif(gif_url)
        elif page_type in ["custom", "components"]:
            variables = page.get('variables', {})
            rendered_variables = {}
            for var_name in variables:
                rendered_variables[var_name] = Template(str(variables[var_name]), self.hass).async_render()

            components: list = page['components'].copy()  # Copy the list so we can add new items to it.
            for index, component in enumerate(components):

                if component['type'] == "text":
                    try:
                        if 'content_entity' in component:
                            entity_id = str(component['content_entity'])
                            state = self.hass.states.get(entity_id)

                            if state is None:
                                rendered_text = "?"
                                _LOGGER.error(
                                    "Entity for Pixoo text not found: %s",
                                    entity_id
                                )
                            else:
                                entity_value = state.state
                                format_spec = str(component.get('format', ''))

                                if format_spec:
                                    try:
                                        rendered_text = format(
                                            float(entity_value),
                                            format_spec
                                        )
                                    except (ValueError, TypeError):
                                        _LOGGER.error(
                                            "Could not format Pixoo entity %s "
                                            "with format %s; using raw state.",
                                            entity_id,
                                            format_spec
                                        )
                                        rendered_text = str(entity_value)
                                else:
                                    rendered_text = str(entity_value)

                        else:
                            rendered_text = str(
                                Template(
                                    str(component['content']),
                                    self.hass
                                ).async_render(
                                    variables=rendered_variables
                                )
                            )

                    except TemplateError as e:
                        _LOGGER.error("Template render error: %s", e)
                        rendered_text = "Template Error"

                    font_name = component.get('font', "")
                    font = font_manager.get_font(font_name)

                    rendered_color = render_color(component.get('color'), self.hass, variables=rendered_variables)

                    align = component.get('align', "").lower()

                    pixoo.draw_text(rendered_text.upper(), tuple(component['position']), rendered_color, font, align)

                elif component['type'] == "image":
                    try:
                        if "image_path" in component:
                            # File
                            rendered_image_path = Template(str(component['image_path']), self.hass).async_render(variables=rendered_variables)
                            img = Image.open(rendered_image_path)
                        elif "image_url" in component:
                            # URL/Web
                            rendered_image_path = Template(str(component['image_url']), self.hass).async_render(variables=rendered_variables)
                            response = requests.get(rendered_image_path, timeout=pixoo.timeout)
                            img = Image.open(BytesIO(response.content))
                        elif "image_data" in component:
                            # Base64
                            # Use a website like https://base64.guru/converter/encode/image to encode the image.
                            rendered_image_data = Template(str(component['image_data']), self.hass).async_render(variables=rendered_variables)
                            img = Image.open(BytesIO(base64.b64decode(rendered_image_data)))
                        else:
                            continue

                        # If neither width nor height is set, the image will be displayed in its original size.
                        # (If too big, it's handled in the _pixoo class)

                        # You can "see" the difference here: https://i.stack.imgur.com/bKlzT.png
                        rendered_resample_mode = str(Template(str(component.get('resample_mode', "box")), self.hass).async_render(variables=rendered_variables)).lower()
                        if rendered_resample_mode == "nearest" or rendered_resample_mode == "pixel_art":
                            resample_mode = Image.NEAREST
                        elif rendered_resample_mode == "bilinear":
                            resample_mode = Image.BILINEAR
                        elif rendered_resample_mode == "hamming":
                            resample_mode = Image.HAMMING
                        elif rendered_resample_mode == "bicubic":
                            resample_mode = Image.BICUBIC
                        elif rendered_resample_mode == "antialias" or rendered_resample_mode == "lanczos":
                            resample_mode = Image.LANCZOS
                        else:
                            resample_mode = Image.BOX

                        width = component.get('width')
                        height = component.get('height')

                        if width and height:
                            img = img.resize((width, height), resample_mode)
                        elif width or height:
                            img.thumbnail((100 if not width else width, 100 if not height else height), resample_mode)

                        pixoo.draw_image(img, tuple(component['position']), image_resample_mode=resample_mode)
                    except TemplateError as e:
                        _LOGGER.error("Template render error: %s", e)
                    except NewConnectionError as e:
                        _LOGGER.error("Connection error: %s", e)
                    except TimeoutError as e:
                        _LOGGER.error("Timeout error: %s", e)

                elif component['type'] == "rectangle":
                    try:
                        rendered_color = render_color(component.get('color'), self.hass, variables=rendered_variables)

                        position = [
                            int(Template(str(position), self.hass).async_render(variables=rendered_variables)) for position in
                            component['position']
                        ]
                        size = [
                            int(Template(str(size), self.hass).async_render(variables=rendered_variables)) for size in
                            component['size']
                        ]

                        size = (size[0] - 1, size[1] - 1)

                        rendered_fill = bool(Template(str(component.get('filled', True)), self.hass).async_render(variables=rendered_variables))

                        if rendered_fill:
                            pixoo.draw_filled_rectangle(position, (position[0] + size[0], position[1] + size[1]), rendered_color)
                        else:
                            pixoo.draw_line(position, (position[0] + size[0], position[1]), rendered_color)
                            pixoo.draw_line((position[0] + size[0], position[1]), (position[0] + size[0], position[1] + size[1]), rendered_color)
                            pixoo.draw_line((position[0] + size[0], position[1] + size[1]), (position[0], position[1] + size[1]), rendered_color)
                            pixoo.draw_line((position[0], position[1] + size[1]), position, rendered_color)

                    except TemplateError as e:
                        _LOGGER.error("Template render error: %s", e)
                elif component["type"] == "templatable":
                    try:
                        rendered_list = list(Template(str(component.get("template", [])), self.hass).async_render(variables=rendered_variables))
                        for item in rendered_list[::-1]:  # Reverse the list so that the order is correct.
                            components.insert(index + 1, item)

                    except TemplateError as e:
                        _LOGGER.error("Template render error: %s", e)

            if push:
                pixoo.push()

    def _render_page_to_buffer(self, page: dict) -> bytes:
        """Render a page to RGB bytes without changing the Pixoo display."""
        # Pixoo hält den Zeichenpuffer intern als __buffer.
        # Frühere Snacky-Versionen stellten dafür get_buffer_bytes() /
        # set_buffer_bytes() bereit; die aktuelle Upstream-Version nicht mehr.
        buffer_attr = "_Pixoo__buffer"

        if not hasattr(self._pixoo, buffer_attr):
            raise RuntimeError(
                "Pixoo internal buffer not found; preview renderer "
                "needs adjustment for this Pixoo library version"
            )

        original_buffer = list(getattr(self._pixoo, buffer_attr))

        try:
            self._render_page(page, push=False)
            return bytes(getattr(self._pixoo, buffer_attr))
        finally:
            setattr(self._pixoo, buffer_attr, original_buffer)

    def _render_preview_file(self, page: dict, preview_path: Path):
        """Render a preview page and write it to disk outside the HA event loop."""
        from PIL import Image

        buffer = self._render_page_to_buffer(page)

        preview_path.parent.mkdir(parents=True, exist_ok=True)

        image = Image.frombytes("RGB", (64, 64), buffer)
        image.save(preview_path, format="PNG")

    async def async_render_preview(self, page_number: int):
        """Render a physical playlist page to a PNG without changing the display."""

        # Beim HA-Start kann der Preview-Service bereits erreichbar sein,
        # während _pages noch die initialen Fallback-Seiten enthält.
        if page_number > len(self._pages):
            await self._async_refresh_snacky_pages()

        if page_number < 1 or page_number > len(self._pages):
            raise ValueError(
                f"Invalid page number {page_number}; "
                f"valid range is 1-{len(self._pages)}"
            )

        page = self._pages[page_number - 1]
        preview_path = Path("/config/www/snacky/pixoo-preview.png")

        await self.hass.async_add_executor_job(
            self._render_preview_file,
            page,
            preview_path,
        )

        _LOGGER.debug(
            "Rendered Pixoo preview for physical page %s to %s",
            page_number,
            preview_path,
        )

    # Service to show a message.
    async def async_show_message(self, page_data: dict, duration: int = -1):
        duration = timedelta(seconds=duration if duration >= 0 else self._scan_interval.total_seconds())

        if not page_data or not page_data.get('page_type'):
            _LOGGER.error("No page to render.")
            return

        def draw():
            self._render_page(page_data)

        await self.hass.async_add_executor_job(draw)
        if self._update_task:
            self.cancel_update_task()
            await self.async_schedule_next_page(duration.total_seconds())

    # Service to play the buzzer
    async def async_play_buzzer(self, buzz_cycle_time_millis: int = 500, idle_cycle_time_millis: int = 500, total_time: int = 3000):
        def buzz():
            self._pixoo.play_buzzer(timedelta(milliseconds=buzz_cycle_time_millis), timedelta(milliseconds=idle_cycle_time_millis), timedelta(milliseconds=total_time))

        await self.hass.async_add_executor_job(buzz)

    async def restart_device(self):
        def restart():
            self._pixoo.restart_device()

        await self.hass.async_add_executor_job(restart)

    async def update_page(self):
        def update_current_page():
            self._render_page(self.page)

        await self.hass.async_add_executor_job(update_current_page)

    def cancel_update_task(self):
        if self._update_task:
            self._update_task.cancel()
            _LOGGER.debug("Successfully canceled update task for %s", self._pixoo.address)

    @property
    def state(self):
        return self._current_page_index+1

    @property
    def available(self) -> bool | None:
        return self.hass.data[DOMAIN][self._config_entry.entry_id]['available']

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, str(self._config_entry.entry_id)) if self._config_entry is not None else (DOMAIN, "divoom")},
            name=self._config_entry.title,
            manufacturer="Divoom",
            model="Pixoo",
            sw_version=VERSION,
        )

    @property
    def unique_id(self):
        return "current_page_" + str(self._config_entry.entry_id)
