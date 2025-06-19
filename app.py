import streamlit as st
import pandas as pd
from parser_refactored import AdiletParser
from selenium.common.exceptions import TimeoutException, WebDriverException

st.set_page_config(page_title="Adilet INN Lookup", page_icon="🔍", layout="wide")

st.title("🔍 Поиск по базе Adilet по ИИН")

inn = st.text_input(
    "Введите ИИН", max_chars=12, help="Только цифры, без пробелов или дефисов"
)

run = st.button("🔍 Найти")

if run:
    inn = inn.strip()
    if not (inn.isdigit() and len(inn) == 12):
        st.error("Некорректный ИИН: должно быть ровно 12 цифр.")
        st.stop()

    st.info(f"Запрашиваем данные для ИИН **{inn}** …")

    try:
        parser = AdiletParser(headless=True)
    except WebDriverException as e:
        st.error(f"Не удалось инициализировать WebDriver: {e}")
        st.stop()

    try:
        with st.spinner("Получаем аресты / обременения …"):
            arrests_df = parser.parse_arrests([inn])
        with st.spinner("Получаем данные о задолженности …"):
            debtors_df = parser.parse_debtors([inn])
    except TimeoutException:
        st.error(
            "Истек тайм-аут ожидания ответа сайта. Попробуйте позже или проверьте соединение."
        )
        arrests_df = pd.DataFrame()
        debtors_df = pd.DataFrame()
    except Exception as e:
        st.exception(e)
        arrests_df = pd.DataFrame()
        debtors_df = pd.DataFrame()

    # -- Render results ----------------------------------------------------
    if arrests_df.empty and debtors_df.empty:
        st.warning("По этому ИИН ничего не найдено ⛔️")
    else:
        if not arrests_df.empty:
            st.subheader("📑 Аресты / Обременения")
            st.dataframe(arrests_df, use_container_width=True)
        else:
            st.info("Аресты / обременения не обнаружены.")

        if not debtors_df.empty:
            st.subheader("💸 Список должников")
            st.dataframe(debtors_df, use_container_width=True)
        else:
            st.info("ИИН не найден в перечне должников.")

        # Общее резюме
        found = []
        if not arrests_df.empty:
            found.append("аресты")
        if not debtors_df.empty:
            found.append("задолженности")
        if found:
            st.success("Найдены " + ", ".join(found) + ".")

else:
    st.write("Введите ИИН слева и нажмите **Найти**.")
