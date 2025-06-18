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
import hashlib
import time
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import NoSuchElementException
from selenium.webdriver.firefox.options import Options as FirefoxOptions


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
    "Должникarrow_upward": "debtor",
    "Дата исполнительного производстваarrow_upward": "execution_date",
    "Орган исполнительного пр-ва, судебный исполнитель": "enforcement_body",
    "Орган, выдавший исполнительный документarrow_upward": "issuing_authority",
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
        logger.info("WebDriver initialised (Firefox, headless=%s)", headless)

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
    # def _create_default_driver(*, headless: bool):
    #     opts = webdriver.SafariOptions()
    #     if headless:
    #         opts.add_argument("--headless")
    #     return webdriver.Safari(options=opts)
    def _create_default_driver(*, headless: bool):
        opts = FirefoxOptions()

        # Без этого аргумента Gecko открывает окно даже при opts.headless = True
        if headless:
            opts.add_argument("-headless")  # ключ с одиночным «-»

        # --желательно, но не обязательно--: задаём размер виртуального экрана,
        # иначе некоторые сайты «падают» из-за ширины 0 px.
        opts.add_argument("--width=1920")
        opts.add_argument("--height=1080")

        # Если geckodriver не лежит в PATH, укажите executable_path
        # return webdriver.Firefox(executable_path="/usr/local/bin/geckodriver", options=opts)
        return webdriver.Firefox(options=opts)

    def _navigate(self, url: str):
        self._driver.get(url)

    def _submit_iin(self, iin: str, page: _PageSpec) -> None:
        """
        Вводит ИИН, жмёт «Поиск», ждёт появления новой таблицы
        ИЛИ всплытия snackbar «По запросу ничего не найдено».
        После обработки snackbar сразу скрывается, чтобы не мешать
        следующим итерациям.
        """
        wait = WebDriverWait(self._driver, 25)
        tbody_sel = f".{page.table_wrapper_cls} tbody"
        snackbar_sel = ".v-snack__wrapper.v-sheet.warning"
        snackbar_btn_sel = ".v-snack__btn"
        # ---------- 1. Сохраняем подпись "старой" таблицы ----------
        try:
            old_hash = hashlib.md5(
                self._driver.find_element(By.CSS_SELECTOR, tbody_sel)
                .get_attribute("innerHTML")
                .encode()  # type: ignore
            ).hexdigest()
        except Exception:
            old_hash = None  # таблицы ещё не было

        # ---------- 2. Очищаем поле и вводим ИИН ----------
        inp = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, page.input_sel)))
        self._driver.execute_script(
            "arguments[0].value=''; arguments[0].dispatchEvent(new Event('input',{bubbles:true}));",
            inp,
        )

        # ---------- 5. Если snackbar ещё видим – закрываем ----------
        try:
            snack = self._driver.find_element(By.CSS_SELECTOR, snackbar_sel)
            if snack.is_displayed():
                # Пытаемся нажать «Закрыть»
                try:
                    snack.find_element(By.CSS_SELECTOR, snackbar_btn_sel).click()
                except Exception:
                    # Если кнопка не нашлась, скрываем через JS
                    self._driver.execute_script(
                        "arguments[0].style.display='none';", snack
                    )
        except Exception:
            pass  # snackbar не найден – всё ок

        inp.send_keys(iin)
        time.sleep(0.15)  # debounce для Vue-маски

        # ---------- 3. Кликаем «Поиск» ----------
        wait.until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, page.submit_sel))
        ).click()

        # ---------- 4. Ждём новую таблицу ИЛИ snackbar ----------
        def tbody_changed(driver):
            # 4.1 – новый хэш таблицы
            if page.url == ARRESTS_SPEC.url:
                time.sleep(0.5)
                return True

            try:
                new_hash = hashlib.md5(
                    driver.find_element(By.CSS_SELECTOR, tbody_sel)
                    .get_attribute("innerHTML")
                    .encode()
                ).hexdigest()
            except Exception:
                new_hash = None  # tbody ещё не появился

            # 4.2 – виден ли snackbar «ничего не найдено»
            try:
                snack = driver.find_element(By.CSS_SELECTOR, snackbar_sel)
                snack_visible = (
                    snack.is_displayed()
                    and "display: none" not in snack.get_attribute("style")
                )
            except Exception:
                snack_visible = False

            changed = (old_hash is None and new_hash is not None) or (
                new_hash and new_hash != old_hash
            )
            return changed or snack_visible

        wait.until(tbody_changed)

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
