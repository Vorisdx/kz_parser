from __future__ import annotations

"""Adilet register scraper – Safari‑driver, CSS selectors, robust cleaning.

* Поле ИИН очищается через `clear()` + резерв `Cmd/Ctrl+A → Delete`.
* Ждём таблицу до 30 с; пропускаем IIN, если строк нет.
* Убираем строки, где все значения (кроме `iin`) пусты – дубликаты исчезают.
* FutureWarning от `replace()` подавлен явным `astype(bool)`.
"""

from dataclasses import dataclass
import logging
import time
from typing import Iterable, List

import pandas as pd
from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException, TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

import warnings

warnings.filterwarnings("ignore")

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# -----------------------------------------------------------------------------
# Page specification (CSS only)
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class _PageSpec:
    url: str
    input_sel: str = ".v-text-field__slot input[type='text']"  # ИИН field
    submit_sel: str = "button[type='submit'].primary"  # Find button
    table_wrapper_cls: str = "v-data-table__wrapper"  # div around table


ARRESTS_SPEC = _PageSpec(url="https://aisoip.adilet.gov.kz/forCitizens/findArest")
DEBTORS_SPEC = _PageSpec(url="https://aisoip.adilet.gov.kz/debtors")

# -----------------------------------------------------------------------------
# Column rename maps
# -----------------------------------------------------------------------------

ARRESTS_RENAME = {
    "ИИН": "iin",
    "Арест на банковские счета": "bank_account_freeze",
    "Запрет на выезд": "travel_ban",
    "Запрет на регистрационные действия": "ban_on_registration_actions",
    "Е-Нотариат": "e_notary",
    "Арест на имущество": "property_freeze",
    "Арест на транспорт": "vehicle_freeze",
}

DEBTORS_RENAME = {
    "ИИН": "iin",
    "Категория": "category",
    "Номер и дата исполнительного документа": "document_no_date",
    "Сумма долга/основание долга": "debt_amount_or_reason",
    "Дата исполнительного производства": "execution_date",
    "Орган исполнительного пр-ва, судебный исполнитель": "enforcement_body",
    "Орган, выдавший исполнительный документ": "issuing_authority",
    "Наличие запрета на выезд из РК по исполнительным производствам ЧСИ/ГСИ": "travel_ban_status",
}

# -----------------------------------------------------------------------------
# Main parser class
# -----------------------------------------------------------------------------


class AdiletParser:
    """Scrapes arrest & debtor registers via Safari‑driver."""

    def __init__(
        self, *, headless: bool = False, driver: webdriver.Remote | None = None
    ):
        self._driver = driver or self._create_default_driver(headless=headless)
        logger.info("WebDriver initialised (Safari, headless=%s)", headless)

    # -------------------------- public API ---------------------------

    def parse_arrests(self, iins: Iterable[str]) -> pd.DataFrame:
        return self._parse(
            iins, ARRESTS_SPEC, ARRESTS_RENAME, pivot_index="Вид обременения"
        )

    def parse_debtors(self, iins: Iterable[str]) -> pd.DataFrame:
        return self._parse(iins, DEBTORS_SPEC, DEBTORS_RENAME)

    def close(self) -> None:
        if self._driver:
            self._driver.quit()

    # ------------------------- core routine -------------------------

    def _parse(self, iins, page, rename_map, *, pivot_index=None):
        self._navigate(page.url)
        results: List[pd.DataFrame] = []

        for iin in iins:
            logger.info("IIN %s", iin)
            self._submit_iin(iin, page)
            table = self._wait_for_table(page)

            if table is None:
                logger.warning("No data for IIN %s", iin)
                continue  # не добавляем пустые дубликаты

            df = self._build_dataframe(table, rename_map, pivot_index=pivot_index)
            if df.empty:
                logger.warning("Empty table for IIN %s", iin)
                continue

            df.insert(0, "iin", iin)
            results.append(df)

        if not results:
            return pd.DataFrame()

        out = pd.concat(results, ignore_index=True)
        # удаляем строки, где все поля (кроме iin) пусты
        mask_empty = out.drop(columns=["iin"]).replace("", pd.NA).isna().all(axis=1)
        return out[~mask_empty]

    # ------------------------ selenium helpers ----------------------

    @staticmethod
    def _create_default_driver(*, headless: bool):
        opts = webdriver.SafariOptions()
        if headless:
            opts.add_argument("--headless")
        return webdriver.Safari(options=opts)

    def _navigate(self, url: str):
        self._driver.get(url)

    def _submit_iin(self, iin: str, page: _PageSpec):
        wait = WebDriverWait(self._driver, 15)
        input_elem = wait.until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, page.input_sel))
        )

        # очистка
        input_elem.clear()
        if input_elem.get_attribute("value"):
            combo = (
                Keys.COMMAND
                if self._driver.capabilities.get("platformName") == "mac"
                else Keys.CONTROL
            )
            input_elem.send_keys(combo, "a", Keys.DELETE)

        time.sleep(0.05)
        input_elem.send_keys(iin)
        time.sleep(0.1)  # Vue debounce

        submit_btn = wait.until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, page.submit_sel))
        )
        submit_btn.click()

    def _wait_for_table(self, page: _PageSpec):
        try:
            wait = WebDriverWait(self._driver, 30)
            wrapper = wait.until(
                EC.presence_of_element_located((By.CLASS_NAME, page.table_wrapper_cls))
            )
            wait.until(
                lambda d: len(
                    d.find_element(
                        By.CSS_SELECTOR, f".{page.table_wrapper_cls} tbody"
                    ).find_elements(By.TAG_NAME, "tr")
                )
                > 0
            )
            rows = wrapper.find_element(By.TAG_NAME, "tbody").find_elements(
                By.TAG_NAME, "tr"
            )
            return wrapper.find_element(By.TAG_NAME, "table") if rows else None
        except (TimeoutException, NoSuchElementException):
            return None

    @staticmethod
    def _build_dataframe(table, rename_map, *, pivot_index=None):
        headers = [
            h.text.strip()
            for h in table.find_element(By.TAG_NAME, "thead").find_elements(
                By.TAG_NAME, "th"
            )
        ]
        rows = table.find_element(By.TAG_NAME, "tbody").find_elements(By.TAG_NAME, "tr")
        data = [
            [td.text for td in row.find_elements(By.TAG_NAME, "td")]
            for row in rows
            if len(row.find_elements(By.TAG_NAME, "td")) == len(headers)
        ]
        if not data:
            return pd.DataFrame()

        df = pd.DataFrame(data, columns=headers)

        if pivot_index and pivot_index in df.columns:
            df = (
                df.set_index(pivot_index)[df.columns.difference([pivot_index])]
                .replace({"Нет": False, "Да": True})
                .astype(bool)  # подавляем FutureWarning
                .T.reset_index(drop=True)
            )

        df.rename(columns=rename_map, inplace=True)
        df.drop_duplicates(inplace=True)

        return df
