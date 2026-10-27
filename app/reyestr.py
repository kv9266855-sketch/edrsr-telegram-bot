import asyncio
import logging
import re
from urllib.parse import urljoin

from playwright.async_api import BrowserContext, TimeoutError as PlaywrightTimeoutError

from .models import Document

log = logging.getLogger(__name__)
BASE_URL = "https://reyestr.court.gov.ua/"


class RegistryUnavailable(RuntimeError):
    pass


class EDRSRClient:
    def __init__(self, context: BrowserContext, timeout_ms: int):
        self.context = context
        self.timeout_ms = timeout_ms
        self.lock = asyncio.Lock()

    async def search(self, case_number: str) -> list[Document]:
        async with self.lock:
            page = await self.context.new_page()
            page.set_default_timeout(self.timeout_ms)
            try:
                await page.goto(BASE_URL, wait_until="domcontentloaded")
                await self._ensure_available(page)
                field = page.locator('#CaseNumber, input[name="CaseNumber"], input[id*="CaseNumber" i]').first
                if await field.count() == 0:
                    raise RegistryUnavailable("Поле «Справа №» не знайдено; розмітка ЄДРСР могла змінитися")
                await field.fill(case_number)
                button = page.locator('#btn, button[name="btn"]').first
                if await button.count() == 0:
                    button = page.get_by_role("button", name=re.compile("пошук|перевірити", re.I)).first
                if await button.count() == 0:
                    raise RegistryUnavailable("Кнопка пошуку не знайдена")
                await button.click()
                await page.wait_for_load_state("domcontentloaded")
                await self._ensure_available(page)
                await page.wait_for_timeout(700)
                return await self._parse_results(page, case_number)
            except PlaywrightTimeoutError as exc:
                raise RegistryUnavailable("Таймаут ЄДРСР або інтерактивного захисту") from exc
            finally:
                await page.close()

    async def _ensure_available(self, page) -> None:
        text = (await page.locator("body").inner_text()).lower()
        markers = ("captcha", "cloudflare", "перевірте, що ви людина", "access denied", "занадто багато запитів")
        if any(marker in text for marker in markers):
            raise RegistryUnavailable("ЄДРСР запросив інтерактивну перевірку або обмежив доступ")

    async def _parse_results(self, page, case_number: str) -> list[Document]:
        body_text = (await page.locator("body").inner_text()).lower()
        if "на ваш запит не знайдено жодного документа" in body_text:
            return []

        links = page.locator("a[href]")
        result: list[Document] = []
        for index in range(await links.count()):
            link = links.nth(index)
            href = await link.get_attribute("href")
            title = (await link.inner_text()).strip()
            if not href or not title or not re.search(r"(document|decision|review|рішенн|ухвал)", href + " " + title, re.I):
                continue
            if re.search(r"змінен[іи] назви судів|головна|допомога|правила|контакти", title, re.I):
                continue
            url = urljoin(BASE_URL, href)
            if url.rstrip("/") == BASE_URL.rstrip("/"):
                continue
            row_text = ""
            try:
                row_text = (await link.locator("xpath=ancestor::*[self::tr or contains(@class, 'row')][1]").inner_text()).strip()
            except Exception:
                pass
            registry_id = re.search(r"(?:id|document|doc)[=/](\d+)", url, re.I)
            key = registry_id.group(1) if registry_id else url
            date_match = re.search(r"\b(\d{2}[./]\d{2}[./]\d{4})\b", row_text)
            result.append(Document(key, case_number, date_match.group(1) if date_match else "—", title, title, url, registry_id.group(1) if registry_id else "—"))
        unique = {doc.document_key: doc for doc in result}
        return list(unique.values())
