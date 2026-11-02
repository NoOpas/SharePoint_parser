import re
import html
import json
import logging
import os
import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlparse, parse_qs, unquote
from queue import Queue
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from bs4 import BeautifulSoup, NavigableString
import psycopg2
import requests
from requests_ntlm import HttpNtlmAuth
import urllib3

# ============================================================================
# ЗАГРУЗКА .ENV ФАЙЛА
# ============================================================================
# Ищем .env в текущей директории или в корне проекта
_env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=_env_path)

# ============================================================================
# КОНФИГУРАЦИЯ ИЗ ПЕРЕМЕННЫХ ОКРУЖЕНИЯ
# ============================================================================
USERNAME = os.getenv("SP_USERNAME")
PASSWORD = os.getenv("SP_PASSWORD")
BASE_DOMAIN = os.getenv("SP_BASE_DOMAIN", "sp.giprovostokneft.ru")

DB_NAME = os.getenv("DB_NAME", "Chatbot_db")
DB_USER = os.getenv("DB_USER", "my_user")
DB_PASSWORD = os.getenv("DB_PASSWORD", "my_password")
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", "5432"))

# Валидация обязательных переменных
_missing = [
    name for name, value in {
        "SP_USERNAME": USERNAME,
        "SP_PASSWORD": PASSWORD,
    }.items() if not value
]
if _missing:
    raise RuntimeError(
        f"Missing required environment variables in .env: {', '.join(_missing)}"
    )

# Подавляем предупреждения о невалидных SSL-сертификатах
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- Конфигурация логирования с timestamp ---
os.makedirs("./logs", exist_ok=True)
timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
log_filename = f"./logs/parse_test_{timestamp}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(log_filename, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)

# --- Подключение к базе данных ---
conn = psycopg2.connect(
    dbname=DB_NAME,
    user=DB_USER,
    password=DB_PASSWORD,
    host=DB_HOST,
    port=DB_PORT,
)
cur = conn.cursor()

# --- Автоматическая очистка таблиц перед запуском ---
logging.info("Dropping existing tables...")
cur.execute("DROP TABLE IF EXISTS parsed_pages_cleaned_test;")
cur.execute("DROP TABLE IF EXISTS errors_test;")
cur.execute("DROP TABLE IF EXISTS empty_pages_test;")
conn.commit()
logging.info("Tables dropped successfully.")

# --- Создание таблиц ---
cur.execute(
    """
    CREATE TABLE IF NOT EXISTS parsed_pages_cleaned_test (
        id SERIAL PRIMARY KEY,
        url TEXT UNIQUE,
        content TEXT);
    """
)
cur.execute(
    """
    CREATE TABLE IF NOT EXISTS errors_test (
        id SERIAL PRIMARY KEY,
        url TEXT UNIQUE,
        error TEXT);
    """
)
cur.execute(
    """
    CREATE TABLE IF NOT EXISTS empty_pages_test (
        id SERIAL PRIMARY KEY,
        url TEXT UNIQUE,
        reason TEXT,
        raw_length INT
    );
    """
)
conn.commit()
logging.info("Fresh tables created successfully.")

# --- Основные настройки ---
START_URL = f"http://{BASE_DOMAIN}/educationalhelpcenter"
VERIFY_SSL = False

visited_urls = set()
session = requests.Session()
session.auth = HttpNtlmAuth(USERNAME, PASSWORD)
session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
session.verify = VERIFY_SSL

ALLOWED_DOMAINS = ["sp.giprovostokneft.ru/educationalhelpcenter"]

# ============================================================================
# ПРАВИЛА ИСКЛЮЧЕНИЙ
# ============================================================================
EXCLUDED_PATTERNS = [
    re.compile(r"\.avi$", re.I),
    re.compile(r"BusinessDataSynchroniser", re.I),
    re.compile(r"\.cde$", re.I),
    re.compile(r"\.csv$", re.I),
    re.compile(r"\.djv$", re.I),
    re.compile(r"\.docx?$", re.I),
    re.compile(r"\.dwg$", re.I),
    re.compile(r"EmailDetails", re.I),
    re.compile(r"groups", re.I),
    re.compile(r"\.hsc$", re.I),
    re.compile(r"\.jpe?g$", re.I),
    re.compile(r"\.lay$", re.I),
    re.compile(r"listfeed", re.I),
    re.compile(r"masterpage", re.I),
    re.compile(r"MembershipGroupId", re.I),
    re.compile(r"\.mht$", re.I),
    re.compile(r"\.mp4$", re.I),
    re.compile(r"\.pdf$", re.I),
    re.compile(r"people", re.I),
    re.compile(r"\.pia$", re.I),
    re.compile(r"\.png$", re.I),
    re.compile(r"\.pptx?$", re.I),
    re.compile(r"\.ppsx$", re.I),
    re.compile(r"RecentWikiPages", re.I),
    re.compile(r"\.rfa$", re.I),
    re.compile(r"RootFolder", re.I),
    re.compile(r"\.rte$", re.I),
    re.compile(r"\.rtf$", re.I),
    re.compile(r"SubNew", re.I),
    re.compile(r"\.tpl$", re.I),
    re.compile(r"\.txt$", re.I),
    re.compile(r"userdisp", re.I),
    re.compile(r"useredit", re.I),
    re.compile(r"viewlsts", re.I),
    re.compile(r"\.xlsx?$", re.I),
    re.compile(r"\.xltx$", re.I),
    re.compile(r"Workflow", re.I),
    re.compile(r"\.zip$", re.I),
    re.compile(r"\.7z$", re.I),
]

# Безопасные фильтры: точное совпадение всей строки (не подстроки!)
EXCLUDED_SYMBOL_COMBOS = [
    re.compile(r"^$"),
    re.compile(r"^\s+$"),
    re.compile(r"^Включить специальные возможности$"),
    re.compile(r"^Выключить специальные возможности$"),
    re.compile(r"^Пропустить команды ленты$"),
    re.compile(r"^Пропустить до основного контента$"),
    re.compile(r"^Отключить анимацию$"),
    re.compile(r"^Включить анимацию$"),
    re.compile(r"^Поиск на этом сайте$"),
    re.compile(r"^Найти$"),
    re.compile(r"^Открыть меню$"),
    re.compile(r"^Выбранный в данный момент$"),
    re.compile(r"^Вопросы и ответы$"),
    re.compile(r"^Видеоматериалы$"),
    re.compile(r"^Презентации$"),
    re.compile(r"^Документация$"),
    re.compile(r"^Форум$"),
    re.compile(r"^Поддержка$"),
    re.compile(r"^Новости$"),
    re.compile(r"^Инструкции$"),
    re.compile(r"^Главная$"),
    re.compile(r"^Обзор$"),
    re.compile(r"^Страница$"),
    re.compile(r"^Элементы$"),
    re.compile(r"^Список$"),
    re.compile(r"^Подписаться$"),
    re.compile(r"^Синхр\.$"),
    re.compile(r"^Параметры$"),
    re.compile(r"^Справка$"),
    re.compile(r"^Контент сайта$"),
    re.compile(r"^Содержимое сайта$"),
    re.compile(r"^Лента новостей$"),
    re.compile(r"^OneDrive$"),
    re.compile(r"^Сайты$"),
    re.compile(r"^Мои параметры$"),
    re.compile(r"^Обо мне$"),
    re.compile(r"^Выход$"),
    re.compile(r"^Войти в качестве другого пользователя$"),
    re.compile(r"^Совместно с\.\.\.$"),
    # Служебные данные SharePoint
    re.compile(r"^false,false,\d+$"),
    re.compile(r"^true,false,\d+$"),
    re.compile(r"^false,true,\d+$"),
    re.compile(r"^true,true,\d+$"),
    re.compile(r"^false$"),
    re.compile(r"^true$"),
    re.compile(r"^null$"),
    re.compile(r"^none$"),
    re.compile(r"^Browse$"),
    re.compile(r"^Design$"),
    # Короткие числовые ID и GUID
    re.compile(r"^\d{1,3}$"),
    re.compile(r"^0x[0-9A-Fa-f]+$"),
    re.compile(r"^\{[0-9A-Fa-f\-]{36}\}$"),
]

# ВАЖНО: ms-core-pageTitle УБРАН - это заголовок страницы, он нужен для контента
EXCLUDED_CLASSES = [
    "ms-core-suiteLink",
    "ms-core-menu-root",
    "ms-cui-topBar2",
    "ms-srch-sb ms-srch-sb-border",
    "ms-bottompaging",
    "ms-viewheadertr ms-vhltr",
    "ms-InlineSearch-DivBaseline",
    "noindex",
    "ms-dialogHidden",
    "s4-notdlg",
    "ms-cui-tt-span",
    "ms-promotedActionButton-text",
    "ms-siteactions-imgspan",
    "ms-menu-item-text",
    "ms-hidden",
]

# ============================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================================
def safe_normalize(url):
    """Нормализует URL для внутреннего отслеживания."""
    try:
        from url_normalize import url_normalize
        res = url_normalize(url)
        return res if res else url
    except Exception:
        return url


def is_visited(url):
    """Проверяет, был ли URL уже обработан."""
    norm_url = safe_normalize(url)
    if norm_url is None:
        return True
    if norm_url in visited_urls:
        return True
    return False


def save_page(url, content):
    """Сохраняет содержимое страницы в БД."""
    if not content or not content.strip():
        return
    cur.execute(
        "INSERT INTO parsed_pages_cleaned_test (url, content) VALUES (%s, %s) ON CONFLICT (url) DO NOTHING;",
        (url, content),
    )
    conn.commit()


def save_error(url, error):
    """Сохраняет ошибку в БД."""
    cur.execute(
        "INSERT INTO errors_test (url, error) VALUES (%s, %s) ON CONFLICT (url) DO NOTHING;",
        (url, str(error)),
    )
    conn.commit()


def save_empty_page(url, reason, raw_length=0):
    """Фиксирует пропущенную пустую страницу с указанием причины."""
    cur.execute(
        "INSERT INTO empty_pages_test (url, reason, raw_length) VALUES (%s, %s, %s) ON CONFLICT (url) DO NOTHING;",
        (url, reason, raw_length),
    )
    conn.commit()


# ============================================================================
# ОБРАБОТКА MDS (Minimal Download Strategy)
# ============================================================================
def resolve_mds_url(url, html_text=""):
    """
    Преобразует MDS-обёртки SharePoint в реальные URL.
    ВАЖНО: Сохраняет базовый путь сайта (например, /educationalhelpcenter/MagiCAD).
    """
    parsed = urlparse(url)
    if "/_layouts/15/start.aspx" not in parsed.path:
        return url

    # Извлекаем базовый путь сайта (всё до /_layouts/15/start.aspx)
    site_base_path = parsed.path.split("/_layouts/15/start.aspx")[0]
    # Убираем trailing slash, если есть
    site_base_path = site_base_path.rstrip("/")

    # Приоритет 1: Извлекаем из hash-фрагмента URL
    fragment = parsed.fragment
    if fragment:
        # Убираем ведущие слеши из фрагмента
        real_path = fragment.lstrip("/")
        # Формируем полный URL с сохранением базового пути сайта
        resolved = f"{parsed.scheme}://{parsed.netloc}{site_base_path}/{real_path}"
        logging.info(f"MDS resolved from hash: {url[:80]}... -> {resolved[:80]}...")
        return resolved

    # Приоритет 2: Извлекаем из g_initUrl в HTML
    if html_text:
        match = re.search(r"g_initUrl\s*=\s*['\"]([^'\"]+)['\"]", html_text)
        if match:
            real_path = match.group(1).replace("\\u002f", "/").lstrip("/")
            resolved = f"{parsed.scheme}://{parsed.netloc}{site_base_path}/{real_path}"
            logging.info(
                f"MDS resolved from g_initUrl: {url[:80]}... -> {resolved[:80]}..."
            )
            return resolved

    return url


def prepare_url_for_request(url):
    """
    Добавляет ?AjaxDelta=0 к URL, чтобы SharePoint вернул полную HTML-страницу.
    Проверяет, чтобы параметр не добавлялся дважды.
    """
    if "AjaxDelta=0" in url:
        return url  # Уже есть, не добавляем повторно
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}AjaxDelta=0"


def _is_mds_wrapper(html_text):
    """Проверяет, является ли HTML MDS-обёрткой."""
    soup = BeautifulSoup(html_text, "lxml")
    main = soup.find(id="DeltaPlaceHolderMain")
    if main and not main.get_text(strip=True):
        if "g_initUrl" in html_text and "start.aspx" in html_text:
            return True
    return False


def extract_links(soup, base_url):
    """Извлекает ссылки со страницы для очереди обхода."""
    links = set()
    for a in soup.find_all("a", href=True):
        full_link = urljoin(base_url, a["href"])
        # Разрешаем MDS-ссылки ПЕРЕД проверкой исключений
        full_link = resolve_mds_url(full_link)
        parsed = urlparse(full_link)
        if any(p.search(parsed.path) for p in EXCLUDED_PATTERNS):
            continue
        if (
            parsed.netloc == ALLOWED_DOMAINS[0][:21]
            and "educationalhelpcenter" in parsed.path
        ):
            links.add(full_link)
    return links


# ============================================================================
# РАБОТА СО СПИСКАМИ ЧЕРЕЗ REST API
# ============================================================================
def is_news_dispform_url(url):
    """Проверяет, является ли URL страницей элемента списка."""
    u = url.lower()
    return ("dispform.aspx" in u or "listform.aspx" in u) and ("id=" in u)


def is_forum_url(url):
    """Проверяет, является ли URL страницей форума Community Discussion."""
    u = url.lower()
    return "flat.aspx" in u or "threaded.aspx" in u or "community discussion" in u


def normalize_guid(guid):
    if not guid:
        return None
    return guid.strip().replace("{", "").replace("}", "").lower()


def extract_site_base_url(url):
    """Извлекает базовый URL сайта."""
    parsed = urlparse(url)
    path = parsed.path
    for marker in ["/_layouts", "/SitePages", "/Lists"]:
        idx = path.find(marker)
        if idx != -1:
            path = path[:idx]
            break
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def extract_news_params(url):
    """Извлекает ListId и ID из URL страницы элемента списка."""
    p = urlparse(url)
    params = parse_qs(p.query)
    if not params and p.fragment and "?" in p.fragment:
        params = parse_qs(p.fragment.split("?", 1)[1])
    list_id = params.get("ListId", [None])[0]
    item_id = params.get("ID", [None])[0]
    if list_id:
        list_id = unquote(list_id)
    return normalize_guid(list_id), item_id


def extract_list_guid_from_html(html_text):
    """Извлекает GUID списка из HTML, если его нет в URL."""
    match = re.search(r'ctx\.listName\s*=\s*"\{([0-9A-Fa-f-]+)\}"', html_text)
    if match:
        return normalize_guid(match.group(1))
    return None


def fetch_list_item_content_enhanced(site_url, list_guid, item_id, session):
    """
    Запрашивает содержимое элемента списка через REST API.
    Исключает метаданные (автор, даты, ID), которые не несут ценности для чат-бота.
    """
    api = f"{site_url}/_api/web/lists(guid'{list_guid}')/items({item_id})"
    headers = {"Accept": "application/atom+xml"}
    try:
        r = session.get(api, headers=headers, verify=VERIFY_SSL, timeout=15)
        r.raise_for_status()

        root = ET.fromstring(r.content)
        ns = {
            "d": "http://schemas.microsoft.com/ado/2007/08/dataservices",
            "m": "http://schemas.microsoft.com/ado/2007/08/dataservices/metadata",
        }
        props = root.find(".//m:properties", ns)
        if props is None:
            return None

        def get_val(name):
            e = props.find(f"d:{name}", ns)
            return e.text if e is not None and e.text else None

        title = get_val("Title") or get_val("FileLeafRef") or "Без названия"

        # Проверяем несколько возможных полей с содержимым
        body_enc = (
            get_val("Body")
            or get_val("Description")
            or get_val("WikiField")
            or get_val("Comments")
        )

        body_text = ""
        if body_enc:
            body_clean = html.unescape(body_enc)
            body_soup = BeautifulSoup(body_clean, "lxml")
            body_text = body_soup.get_text(separator="\n", strip=True)

        # FALLBACK: Если содержимого нет, извлекаем метаданные
        # НО ИСКЛЮЧАЕМ ненужные поля (автор, даты, ID)
        if not body_text.strip():
            metadata_parts = []
            if title and title != "Без названия":
                metadata_parts.append(f"Заголовок: {title}")

            # Поля, которые НЕ НУЖНО сохранять (метаданные, системные поля)
            excluded_fields = {
                "ID",
                "ContentType",
                "FileSystemObjectType",
                "GUID",
                "PermMask",
                "Title",
                "FileLeafRef",
                "Body",
                "Description",
                "WikiField",
                "Comments",
                "OData__UIVersionString",
                "AttachmentFiles",
                # Метаданные автора и дат
                "AuthorId",
                "EditorId",
                "Author",
                "Editor",
                "Created",
                "Modified",
                "Created_x0020_Date",
                "Modified_x0020_Date",
                "Created.FriendlyDisplay",
                "Modified.FriendlyDisplay",
                "Created_x0020_Date.ifnew",
                "Modified_x0020_Date.ifnew",
                # Системные поля
                "ContentTypeId",
                "File_x0020_Type",
                "FileRef",
                "HTML_x0020_File_x0020_Type",
                "ServerRedirectedEmbedUrl",
                "CheckoutUser",
                "CheckedOutUserId",
                "IsCheckedoutToLocal",
                # Поля с URL/ссылками (часто мусор)
                "_x0412__x0438__x0434__x0435__x04",
                "File_x0020_Type.url",
                "File_x0020_Type.progid",
                # Поля с permission masks
                "PermMask",
                "BaseName",
            }

            for child in props:
                tag_name = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                # Пропускаем исключённые поля
                if tag_name in excluded_fields:
                    continue
                # Пропускаем поля, начинающиеся с "_" (системные)
                if tag_name.startswith("_"):
                    continue
                # Пропускаем поля с "x0020" (закодированные пробелы в именах)
                if "x0020" in tag_name:
                    continue
                # Сохраняем только если есть текст
                if child.text and child.text.strip():
                    metadata_parts.append(f"{tag_name}: {child.text.strip()}")

            if len(metadata_parts) > 1 or (
                len(metadata_parts) == 1 and "Заголовок" in metadata_parts[0]
            ):
                return "\n".join(metadata_parts).strip()
            return None

        return f"{title}\n\n{body_text}".strip()
    except Exception as e:
        logging.error(
            f"REST API fetch failed for item {item_id} in list {list_guid}: {e}"
        )
        return None


def queue_all_list_items_via_api(html_text, url, queue, session):
    """
    Сканирует HTML на предмет встроенных списков SharePoint (ctx-блоки),
    запрашивает через REST API все элементы списка (обходя пагинацию)
    и добавляет их URL в очередь.
    """
    pattern = re.compile(
        r'ctx\.listName\s*=\s*"\{([0-9A-Fa-f-]+)\}".*?ctx\.displayFormUrl\s*=\s*"([^"]+)"',
        re.DOTALL,
    )
    matches = pattern.findall(html_text)
    if not matches:
        return

    site_url = extract_site_base_url(url)
    for guid, form_url in matches:
        api_url = f"{site_url}/_api/web/lists(guid'{guid}')/items?$top=5000&$select=ID"
        headers = {"Accept": "application/json;odata=verbose"}
        try:
            r = session.get(api_url, headers=headers, verify=VERIFY_SSL, timeout=15)
            if r.status_code != 200:
                logging.warning(
                    f"REST API returned status {r.status_code} for list {guid}"
                )
                continue
            try:
                data = r.json()
                items = data.get("d", {}).get("results", [])
                logging.info(f"Queued {len(items)} items for list {guid}")
                for item in items:
                    item_id = item.get("ID")
                    if item_id:
                        sep = "&" if "?" in form_url else "?"
                        full_url = f"{form_url}{sep}ID={item_id}"
                        queue.put(full_url)
            except ValueError as json_err:
                logging.error(f"JSON parse failed for list {guid}: {json_err}")
                save_error(
                    api_url, f"JSON parse error: {json_err} | Response: {r.text[:200]}"
                )
        except Exception as e:
            logging.error(f"Failed to queue list items for {guid}: {e}")
            save_error(api_url, str(e))


# ============================================================================
# ПАРСИНГ ФОРУМОВ SHAREPOINT (УПРОЩЁННАЯ ВЕРСИЯ)
# ============================================================================
def extract_community_discussion_topics(html_text):
    """
    Извлекает темы обсуждений из WPQ*ListData для Community Discussion.
    Возвращает список тем (только Title + Body) и URL для Flat.aspx (чтобы получить ответы).
    """
    topics = []
    flat_urls = []

    pattern = r"var\s+(WPQ\d+ListData)\s*=\s*(\{.+?\});"
    matches = re.findall(pattern, html_text, re.DOTALL)

    if not matches:
        return topics, flat_urls

    for var_name, json_str in matches:
        try:
            data = json.loads(json_str)
            if "Row" not in data or not isinstance(data["Row"], list):
                continue

            # Проверяем, что это действительно Community Discussion
            is_discussion = False
            for row in data["Row"]:
                if row.get("ContentType") == "Обсуждение" or (
                    row.get("FSObjType") == "1" and ("Body" in row or "Title" in row)
                ):
                    is_discussion = True
                    break

            if not is_discussion:
                continue

            logging.info(
                f"Found Community Discussion in {var_name}: {len(data['Row'])} topics"
            )

            for row in data["Row"]:
                topic_parts = []
                # Заголовок темы
                title = row.get("Title") or row.get("FileLeafRef")
                if not title:
                    continue
                topic_parts.append(f"Тема: {title}")

                # Тело сообщения (основной контент)
                body_enc = row.get("Body")
                if body_enc:
                    body_clean = html.unescape(body_enc)
                    body_soup = BeautifulSoup(body_clean, "lxml")
                    body_text = body_soup.get_text(separator="\n", strip=True)
                    if body_text.strip():
                        topic_parts.append(f"\n{body_text}")

                if len(topic_parts) > 1:  # Есть хотя бы заголовок и текст
                    topics.append("\n".join(topic_parts))

                # Формируем URL для Flat.aspx (чтобы получить ответы)
                file_ref = row.get("FileRef")
                item_id = row.get("ID")
                if file_ref and item_id:
                    flat_url = f"http://{BASE_DOMAIN}{file_ref}/Flat.aspx?ID={item_id}"
                    flat_urls.append(flat_url)

        except json.JSONDecodeError as e:
            logging.debug(f"Failed to parse {var_name}: {e}")
            continue
        except Exception as e:
            logging.debug(f"Error processing {var_name}: {e}")
            continue

    return topics, flat_urls


def parse_forum_replies(html_text):
    """
    Парсит ответы из страницы Flat.aspx (плоское представление обсуждения).
    Возвращает структурированный текст с ответами.
    """
    soup = BeautifulSoup(html_text, "lxml")
    replies = []

    # Ищем контейнеры с ответами
    message_containers = soup.find_all(
        class_=re.compile(r"ms-disc-body|ms-rtestate-field", re.I)
    )

    if not message_containers:
        thread = soup.find(class_=re.compile(r"ms-disc-thread|ms-disc-root", re.I))
        if thread:
            message_containers = thread.find_all(
                ["div", "td"], class_=re.compile(r"ms-vb|ms-vb2", re.I)
            )

    seen_texts = set()
    reply_count = 0

    for msg in message_containers:
        text = msg.get_text(separator="\n", strip=True)
        if not text or len(text) < 10 or text in seen_texts:
            continue

        # Очищаем от кнопок SharePoint
        text = re.sub(
            r"(Пометить как ответ|Отменить пометку|Ответить|Цитировать|Редактировать).*",
            "",
            text,
            flags=re.I,
        ).strip()
        if len(text) < 10:
            continue

        seen_texts.add(text)

        if reply_count == 0:
            replies.append(f"[Исходное сообщение]:\n{text}")
        else:
            replies.append(f"\n[Ответ {reply_count}]:\n{text}")

        reply_count += 1

    if replies:
        return "\n\n".join(replies)
    return None


# ============================================================================
# ОЧИСТКА HTML
# ============================================================================
def clean_soup(soup):
    """
    Извлекает основной контент и удаляет служебные элементы SharePoint.
    ВАЖНО: НЕ удаляем тег <form>, так как SharePoint оборачивает всю страницу в него.
    """
    main_content = soup.find(id="DeltaPlaceHolderMain")
    if not main_content:
        main_content = soup.find("body")
    if not main_content:
        return None

    # ВАЖНО: "form" УБРАН! SharePoint оборачивает всю страницу в <form id="aspnetForm">,
    # его удаление уничтожает весь контент.
    tags_to_remove = [
        "script",
        "style",
        "noscript",
        "iframe",
        "svg",
        "meta",
        "link",
        "input",
        "hidden",
    ]
    for tag_name in tags_to_remove:
        for tag in main_content.find_all(tag_name):
            tag.decompose()

    # Удаляем скрытые элементы
    for tag in main_content.find_all(
        style=re.compile(r"display\s*:\s*none", re.IGNORECASE)
    ):
        tag.decompose()

    # Удаляем служебные классы SharePoint
    for cls in EXCLUDED_CLASSES:
        for tag in main_content.find_all(class_=cls):
            tag.decompose()

    return main_content


def extract_text_improved(soup, html_text):
    """
    Двухуровневое извлечение текста без жёстких порогов.
    Уровень 1: чистое извлечение из #DeltaPlaceHolderMain.
    Уровень 2: резервное извлечение со всей страницы.
    """
    # Уровень 1: чистое извлечение
    main_content = soup.find(id="DeltaPlaceHolderMain")
    if main_content:
        for tag in main_content(["script", "style", "noscript", "iframe", "svg"]):
            tag.decompose()

        texts = []
        for descendant in main_content.descendants:
            if isinstance(descendant, NavigableString):
                t = descendant.strip()
                if (
                    t
                    and len(t) > 1
                    and not any(r.search(t) for r in EXCLUDED_SYMBOL_COMBOS)
                ):
                    texts.append(t)

        # Возвращаем, если нашли хотя бы 1 осмысленный текст
        if texts:
            return "\n".join(texts)

    # Уровень 2: резервное извлечение со всей страницы
    fallback_soup = BeautifulSoup(html_text, "lxml")
    for cls in EXCLUDED_CLASSES:
        for tag in fallback_soup.find_all(class_=cls):
            tag.decompose()

    text = fallback_soup.get_text(separator="\n", strip=True)
    lines = text.split("\n")

    clean_lines = [
        line.strip()
        for line in lines
        if line.strip()
        and not any(r.search(line) for r in EXCLUDED_SYMBOL_COMBOS)
        and len(line.strip()) > 1
    ]

    final_text = "\n".join(clean_lines)

    # УБРАН ПОРОГ 50 СИМВОЛОВ - сохраняем даже короткие страницы
    return final_text


# ============================================================================
# ОСНОВНАЯ ФУНКЦИЯ ПАРСИНГА
# ============================================================================
def fetch_and_parse(url, queue):
    try:
        if "/_layouts/15/start.aspx" in url:
            logging.debug(f"Skipping MDS wrapper URL: {url[:80]}...")
            return "", set(), url

        clean_url = resolve_mds_url(url)
        request_url = prepare_url_for_request(clean_url)

        response = session.get(request_url, timeout=30)
        response.raise_for_status()
        response.encoding = "utf-8"
        html_text = response.text

        if "/_layouts/15/start.aspx" in response.url or _is_mds_wrapper(html_text):
            real_url = resolve_mds_url(response.url, html_text)
            if real_url and real_url != clean_url:
                logging.info(f"MDS detected, re-requesting: {real_url[:80]}...")
                request_url = prepare_url_for_request(real_url)
                response = session.get(request_url, timeout=30)
                response.raise_for_status()
                response.encoding = "utf-8"
                html_text = response.text
                url = real_url

        soup = BeautifulSoup(html_text, "lxml")

        links = extract_links(soup, url)
        queue_all_list_items_via_api(html_text, url, queue, session)

        # Обработка форумов
        if is_forum_url(url) or "Community Discussion" in url:
            if "Flat.aspx" in url or "Threaded.aspx" in url:
                forum_content = parse_forum_replies(html_text)
                if forum_content:
                    return forum_content, links, url
            else:
                topics, flat_urls = extract_community_discussion_topics(html_text)
                for flat_url in flat_urls:
                    if not is_visited(flat_url):
                        queue.put(flat_url)
                if topics:
                    combined_topics = "\n\n---\n\n".join(topics)
                    return combined_topics, links, url

        # Обработка DispForm
        if is_news_dispform_url(url):
            list_guid, item_id = extract_news_params(url)
            if not list_guid:
                list_guid = extract_list_guid_from_html(html_text)

            if list_guid and item_id:
                site_url = extract_site_base_url(url)
                rest_text = fetch_list_item_content_enhanced(
                    site_url, list_guid, item_id, session
                )
                if rest_text and rest_text.strip():
                    return rest_text, links, url

        # Стандартная обработка HTML
        final_text = extract_text_improved(soup, html_text)

        if final_text and final_text.strip():
            return final_text, links, url
        else:
            save_empty_page(
                url, "Page yielded no text after improved extraction", len(html_text)
            )
            return "", links, url

    except Exception as e:
        logging.error(f"Fetch failed for {url}: {e}")
        save_error(url, e)
        return "", set(), url


# ============================================================================
# ГЛАВНЫЙ ЦИКЛ ОБХОДА
# ============================================================================
def parser_page(start_url):
    """Основной цикл: обходит страницы, парсит, сохраняет в PostgreSQL."""
    queue = Queue()
    queue.put(start_url)
    logging.info(f"Starting crawl from: {start_url}")
    logging.info(f"Log file: {log_filename}")

    pages_processed = 0
    pages_saved = 0

    while not queue.empty():
        url = queue.get()
        if is_visited(url):
            continue

        logging.info(f"Processing [{pages_processed + 1}]: {url[:100]}...")
        visited_urls.add(safe_normalize(url))
        pages_processed += 1

        try:
            final_text, new_links, actual_url = fetch_and_parse(url, queue)

            # Добавляем новые ссылки в очередь
            for link in new_links:
                if not is_visited(link):
                    queue.put(link)

            # Сохраняем содержимое, если оно есть
            if final_text and final_text.strip():
                save_page(actual_url, final_text)
                logging.info(f"Saved {len(final_text)} chars: {actual_url[:100]}...")
                pages_saved += 1
            else:
                logging.warning(f"Skipped empty page: {actual_url[:100]}...")

        except Exception as e:
            logging.error(f"Error processing {url[:100]}...: {e}")
            save_error(url, e)

    conn.close()
    logging.info(
        f"Crawler finished. Processed: {pages_processed}, Saved: {pages_saved}"
    )


if __name__ == "__main__":
    parser_page(START_URL)
