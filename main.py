#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
import datetime
import random
import concurrent.futures
import signal
from pathlib import Path
from threading import Lock, Event
from colorama import Fore, init

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.service import Service as ChromeService
from webdriver_manager.chrome import ChromeDriverManager
from selenium.common.exceptions import (
    TimeoutException,
    WebDriverException,
    StaleElementReferenceException,
)

init(autoreset=True)

# ==========================
# НАСТРОЙКИ
# ==========================
PASSWORD_LENGTH = 12
CACHE_AVAIL = "checked_cache.txt"
CACHE_REP = "reputation_cache.txt"

SUPPORTED_DOMAINS = {
    "yahoo.com": "https://login.yahoo.com/account/create?lang=en-US",
    "aol.com":   "https://login.aol.com/account/create?lang=en-US",
}

MAILS_FILE_DEFAULT = "mail.txt"
DEFAULT_BATCH_SIZE = 50

REPUTATION_URL = "https://mailmeteor.com/tools/email-reputation"

# ===== Availability (100% рабочие настройки против "спама") =====
AVAIL_VALIDATION_TIMEOUT = 2      # максимум ждём 25 секунд
AVAIL_STABLE_OK_SECONDS = 1.5      # нужно стабильное состояние минимум 4.5 сек
AVAIL_IGNORE_ERROR_INITIAL = 1.5   # игнорируем первые 1.5 сек ошибки
AVAIL_POLL_INTERVAL = 0.25         # проверяем каждые 250 мс
AVAIL_AFTER_INPUT_DELAY = 0.6

# ===== Reputation =====
REP_MAX_ATTEMPTS = 3
REP_WAIT_SECONDS = 180
REP_RETRY_BACKOFF = (5, 10)
REP_REQUIRE_NONZERO = True
REP_AFTER_CLICK_DELAY = 3.0
UNABLE_MAX_HITS = 2

stop_event = Event()

# ==========================
# CTRL+C: нормальная остановка
# ==========================
def _sigint_handler(signum, frame):
    stop_event.set()
    raise KeyboardInterrupt

signal.signal(signal.SIGINT, _sigint_handler)

# ==========================
# КАСТОМНЫЕ ОШИБКИ
# ==========================
class UnableToCheckEmail(Exception):
    pass

# ==========================
# УТИЛИТЫ / КЭШ
# ==========================
def load_lines(filename):
    try:
        with open(filename, "r", encoding="utf-8") as f:
            return [x.strip() for x in f if x.strip()]
    except:
        return []

def write_lines(filename, lines):
    with open(filename, "w", encoding="utf-8") as f:
        for line in lines:
            f.write(line + "\n")

def load_cache_set(path):
    if not os.path.exists(path):
        return set()
    with open(path, "r", encoding="utf-8") as f:
        return set(x.strip() for x in f if x.strip())

def save_cache_line(path, line):
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")

def load_rep_cache():
    if not os.path.exists(CACHE_REP):
        return {}
    d = {}
    with open(CACHE_REP, "r", encoding="utf-8") as f:
        for l in f:
            if ":" in l:
                k, v = l.strip().split(":", 1)
                d[k] = v
    return d

def save_rep_cache(email, score):
    with open(CACHE_REP, "a", encoding="utf-8") as f:
        f.write(f"{email}:{score}\n")

# ==========================
# SELENIUM
# ==========================
def make_driver():
    opt = webdriver.ChromeOptions()
    opt.add_argument("--disable-blink-features=AutomationControlled")
    opt.add_argument("--start-maximized")
    return webdriver.Chrome(
        service=ChromeService(ChromeDriverManager().install()),
        options=opt
    )

# ==========================
# УЧЁТ ПРОВЕРЕННЫХ ЛОГИНОВ (для удаления из mail.txt)
# ==========================
def mark_login_done(login_done_map, login, domain, lock):
    with lock:
        s = login_done_map.get(login)
        if s is None:
            s = set()
            login_done_map[login] = s
        s.add(domain)

def get_fully_done_logins(login_done_map):
    need = len(SUPPORTED_DOMAINS)
    return {login for login, done_domains in login_done_map.items() if len(done_domains) >= need}

# ==========================
# Детектор BUSY по тексту (включая Yahoo new UI)
# ==========================
def _is_busy_message(txt: str) -> bool:
    if not txt:
        return False
    t = txt.lower()

    # Полные варианты сообщений
    busy_phrases = [
        "not available for sign up",
        "this email address is not available",
        "that email address is not available",
        "already taken",
        "unavailable",
        "isn't available",
        "is not available",
        "email not available. try entering a different one.",
    ]

    # Ключевые куски (устойчивые маркеры)
    busy_keywords = [
        "email not available",
        "try something else",
        "try entering a different one",
        "taken",
        "занят",          # русский
        "недоступен",     # русский
        "déjà utilisée",  # французский
        "nicht verfügbar" # немецкий
    ]

    # Проверка: либо совпадает целая фраза, либо встречаются ключевые слова
    if any(p in t for p in busy_phrases):
        return True
    if any(k in t for k in busy_keywords):
        return True

    return False

# ==========================
# YAHOO/AOL: поиск поля логина для разных дизайнов
# ==========================
def find_username_input(driver, domain: str):
    if domain == "yahoo.com":
        selectors = [
            (By.ID, "reg-userId"),
            (By.ID, "usernamereg-userId"),
            (By.CSS_SELECTOR, "input[name='userId']"),
            (By.CSS_SELECTOR, "input[id*='userId']"),
        ]
    else:
        selectors = [
            (By.ID, "reg-userId"),
            (By.CSS_SELECTOR, "input[name='userId']"),
            (By.CSS_SELECTOR, "input[id*='userId']"),
        ]

    for by, sel in selectors:
        try:
            el = driver.find_element(by, sel)
            if el.is_displayed():
                return el
        except:
            continue
    return None

def _extract_error_text_multi(driver, input_el):
    try:
        # старый дизайн
        try:
            el = driver.find_element(By.ID, "reg-userId-error")
            txt = (el.text or "").strip()
            if txt and _is_busy_message(txt):
                return txt
        except:
            pass

        # рядом с input (fieldset)
        try:
            container = input_el.find_element(By.XPATH, "./ancestor::fieldset[1]")
            candidates = container.find_elements(By.XPATH, ".//p|.//span|.//div")
            for c in candidates:
                if not c.is_displayed():
                    continue
                t = (c.text or "").strip()
                if t and len(t) > 2 and _is_busy_message(t):
                    return t
        except:
            pass

        # fallback
        try:
            candidates = driver.find_elements(By.CSS_SELECTOR, "[class*='error'], [class*='invalid']")
            for c in candidates:
                if not c.is_displayed():
                    continue
                t = (c.text or "").strip()
                if t and len(t) > 2 and _is_busy_message(t):
                    return t
        except:
            pass

        return ""
    except StaleElementReferenceException:
        return ""
    except:
        return ""

# ==========================
# Не TAB: blur кликом/JS, чтобы не прыгать в пароль
# ==========================
def _blur_without_tab(driver, input_el):
    try:
        driver.execute_script("arguments[0].blur();", input_el)
    except:
        pass
    try:
        driver.execute_script("document.body.click();")
    except:
        pass

# ==========================
# Availability: ожидание "busy/free" без спама
# ==========================
def _wait_busy_or_free(driver, input_el,
                       timeout=AVAIL_VALIDATION_TIMEOUT,
                       stable_ok=AVAIL_STABLE_OK_SECONDS,
                       ignore_err_initial=AVAIL_IGNORE_ERROR_INITIAL):
    start = time.time()
    ok_since = None
    busy_since = None
    last_err = ""

    while True:
        if stop_event.is_set():
            raise KeyboardInterrupt

        now = time.time()
        err = _extract_error_text_multi(driver, input_el)

        if err:  # сообщение есть
            last_err = err
            if busy_since is None:
                busy_since = now
            elif (now - busy_since) >= stable_ok:
                # приоритет: если сообщение держится стабильно → занят
                return "busy", last_err
            ok_since = None
        else:  # сообщения нет
            if ok_since is None:
                ok_since = now
            elif (now - ok_since) >= stable_ok:
                # свободен только если НИ РАЗУ не было устойчивого busy
                if busy_since is None:
                    return "free", None
                # если busy уже был → считаем занят
                return "busy", last_err
            busy_since = None

        if (now - start) >= timeout:
            # если таймаут и хоть раз видели ошибку → занят
            if busy_since is not None or last_err:
                return "busy", last_err
            return "unknown", None

        time.sleep(AVAIL_POLL_INTERVAL)


# ==========================
# Фильтрация (ВАЖНО): не запускаем браузеры, если всё уже в кэше
# ==========================
def _login_fully_cached(login: str, checked_cache: set) -> bool:
    for dom in SUPPORTED_DOMAINS:
        if f"{login}@{dom}" not in checked_cache:
            return False
    return True

def _logins_need_domain(domain: str, logins: list, checked_cache: set) -> list:
    out = []
    for login in logins:
        if f"{login}@{domain}" not in checked_cache:
            out.append(login)
    return out

# ==========================
# ШАГ 1: ДОСТУПНОСТЬ (yahoo/aol)
# ==========================
def process_domain(domain, logins, checked_cache, cache_lock,
                   avail_f, busy_f, login_done_map, done_lock):
    # если нечего проверять по этому домену — не открываем браузер вообще
    if not logins:
        print(Fore.CYAN + f"[{domain}] Нечего проверять (всё в кэше) — браузер не запускаю")
        return

    driver = None
    try:
        driver = make_driver()
        driver.get(SUPPORTED_DOMAINS[domain])
        print(Fore.CYAN + f"[{domain}] Браузер запущен (логинов: {len(logins)})")

        def ensure_input():
            t0 = time.time()
            while True:
                if stop_event.is_set():
                    raise KeyboardInterrupt
                el = find_username_input(driver, domain)
                if el:
                    return el
                if time.time() - t0 > 25:
                    raise TimeoutException(f"[{domain}] Не найдено поле логина (дизайн не распознан).")
                time.sleep(0.3)

        input_el = ensure_input()

        for login in logins:
            if stop_event.is_set():
                break

            email = f"{login}@{domain}"

            with cache_lock:
                if email in checked_cache:
                    mark_login_done(login_done_map, login, domain, done_lock)
                    continue

            # 2 попытки на один логин (если DOM сломался)
            per_login_attempts = 2
            for attempt in range(1, per_login_attempts + 1):
                try:
                    input_el = ensure_input()

                    # фокус и ввод
                    try:
                        input_el.click()
                    except:
                        pass

                    try:
                        input_el.clear()
                    except:
                        input_el.send_keys(Keys.CONTROL, "a")
                        input_el.send_keys(Keys.BACKSPACE)

                    input_el.send_keys(login)

                    # микро-действие (важно!)
                    input_el.send_keys(" ")
                    input_el.send_keys(Keys.BACKSPACE)

                    # blur без TAB
                    _blur_without_tab(driver, input_el)

                    time.sleep(AVAIL_AFTER_INPUT_DELAY)

                    status, err_text = _wait_busy_or_free(driver, input_el)

                    if status == "busy":
                        busy_f.write(email + "\n")
                        with cache_lock:
                            save_cache_line(CACHE_AVAIL, email)
                            checked_cache.add(email)
                        print(Fore.RED + f"{email} — ЗАНЯТ | {err_text}")

                    elif status == "free":
                        pwd = "".join(random.choice(
                            "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
                        ) for _ in range(PASSWORD_LENGTH))
                        avail_f.write(f"{email}:{pwd}\n")
                        with cache_lock:
                            save_cache_line(CACHE_AVAIL, email)
                            checked_cache.add(email)
                        print(Fore.GREEN + f"{email} — СВОБОДЕН")

                    else:
                        busy_f.write(email + "\n")
                        with cache_lock:
                            save_cache_line(CACHE_AVAIL, email)
                            checked_cache.add(email)
                        print(Fore.YELLOW + f"{email} — НЕЯСНО (timeout). Записал как ЗАНЯТ (безопасно).")

                    mark_login_done(login_done_map, login, domain, done_lock)
                    time.sleep(0.25)
                    break

                except KeyboardInterrupt:
                    stop_event.set()
                    break

                except (TimeoutException, StaleElementReferenceException, WebDriverException) as e:
                    if attempt >= per_login_attempts:
                        print(Fore.YELLOW + f"[{domain}] {email} — ошибка DOM/валидации: {e}. Записал как ЗАНЯТ.")
                        busy_f.write(email + "\n")
                        with cache_lock:
                            save_cache_line(CACHE_AVAIL, email)
                            checked_cache.add(email)
                        mark_login_done(login_done_map, login, domain, done_lock)
                        break

                    print(Fore.MAGENTA + f"[{domain}] Перезапуск браузера (причина: {e})...")
                    try:
                        driver.quit()
                    except:
                        pass
                    driver = make_driver()
                    driver.get(SUPPORTED_DOMAINS[domain])
                    input_el = ensure_input()

                except Exception as e:
                    if attempt >= per_login_attempts:
                        print(Fore.YELLOW + f"[{domain}] {email} — неизвестная ошибка: {e}. Записал как ЗАНЯТ.")
                        busy_f.write(email + "\n")
                        with cache_lock:
                            save_cache_line(CACHE_AVAIL, email)
                            checked_cache.add(email)
                        mark_login_done(login_done_map, login, domain, done_lock)
                        break

                    print(Fore.MAGENTA + f"[{domain}] Перезапуск браузера (unknown err: {e})...")
                    try:
                        driver.quit()
                    except:
                        pass
                    driver = make_driver()
                    driver.get(SUPPORTED_DOMAINS[domain])
                    input_el = ensure_input()

    finally:
        try:
            if driver:
                driver.quit()
        except:
            pass
        print(Fore.CYAN + f"[{domain}] Закрыт")

# ==========================
# ШАГ 2: РЕПУТАЦИЯ (Mailmeteor)
# ==========================
def _mailmeteor_unable_message(driver) -> bool:
    try:
        html = (driver.page_source or "").lower()
        return ("unable to check this email" in html) and ("please try again" in html)
    except:
        return False

def _parse_meter_score(driver):
    try:
        meter = driver.find_element(By.CSS_SELECTOR, "[role='meter']")
        val = meter.get_attribute("aria-valuenow")
        if val is None:
            return None
        val = str(val).strip()
        if not val.isdigit():
            return None
        score = int(val)
        if 0 <= score <= 100:
            return score
        return None
    except:
        return None

def _wait_for_form_ready(driver, timeout_seconds: int = 90):
    start = time.time()
    while True:
        if stop_event.is_set():
            raise KeyboardInterrupt
        try:
            driver.find_element(By.NAME, "email-reputation-checker-input")
            driver.find_element(By.CSS_SELECTOR, "button[type='submit']")
            return
        except:
            pass
        if time.time() - start > timeout_seconds:
            raise TimeoutException("Mailmeteor form not ready (maybe Cloudflare/block).")
        time.sleep(0.5)

def _submit_email_for_reputation(driver, email: str):
    inp = driver.find_element(By.NAME, "email-reputation-checker-input")
    inp.clear()
    inp.send_keys(email)
    driver.find_element(By.CSS_SELECTOR, "button[type='submit']").click()

def _wait_for_ready_score(driver, timeout_seconds: int, require_nonzero: bool = True) -> int:
    start = time.time()
    stable_score = None
    stable_since = None

    while True:
        if stop_event.is_set():
            raise KeyboardInterrupt

        if _mailmeteor_unable_message(driver):
            raise UnableToCheckEmail("Unable to check this email. Please try again.")

        score = _parse_meter_score(driver)

        if score is not None:
            if require_nonzero and score == 0:
                # ждём пока появится ненулевой рейтинг
                pass
            else:
                if stable_score != score:
                    stable_score = score
                    stable_since = time.time()
                elif (time.time() - stable_since) >= 2.0:  # устойчиво 2 сек
                    return stable_score

        if time.time() - start > timeout_seconds:
            raise TimeoutException("Score not ready (still 0/None).")

        time.sleep(0.5)


def _get_reputation_with_retry(driver, email: str):
    unable_hits = 0

    for attempt in range(1, REP_MAX_ATTEMPTS + 1):
        if stop_event.is_set():
            return None

        try:
            driver.get(REPUTATION_URL)
            _wait_for_form_ready(driver, timeout_seconds=REP_WAIT_SECONDS)

            _submit_email_for_reputation(driver, email)
            print(Fore.YELLOW + f"{email} — попытка {attempt}/{REP_MAX_ATTEMPTS}: если есть Cloudflare, реши вручную")

            time.sleep(REP_AFTER_CLICK_DELAY)

            score = _wait_for_ready_score(
                driver,
                timeout_seconds=REP_WAIT_SECONDS,
                require_nonzero=False  # ⚡ снимаем ограничение
            )

            # 🔹 Дополнительная проверка: если score == 0 → пробуем ещё раз
            if score == 0:
                print(Fore.MAGENTA + f"{email} — результат 0, повторная проверка...")
                time.sleep(5)  # пауза перед повтором
                score_retry = _wait_for_ready_score(
                    driver,
                    timeout_seconds=REP_WAIT_SECONDS,
                    require_nonzero=False
                )
                if score_retry and score_retry > 0:
                    return score_retry

            return score

        except UnableToCheckEmail:
            unable_hits += 1
            print(Fore.MAGENTA + f"{email} — Unable... ({unable_hits}/{UNABLE_MAX_HITS})")
            if unable_hits >= UNABLE_MAX_HITS:
                print(Fore.RED + f"{email} — SKIP (Unable... два раза). score=0")
                return 0
            time.sleep(2)
            continue

        except KeyboardInterrupt:
            stop_event.set()
            return None

        except (TimeoutException, WebDriverException, Exception) as e:
            if attempt >= REP_MAX_ATTEMPTS:
                print(Fore.RED + f"{email} — репутация НЕ получена: {e}")
                return None
            backoff = REP_RETRY_BACKOFF[min(attempt - 1, len(REP_RETRY_BACKOFF) - 1)]
            print(Fore.MAGENTA + f"{email} — retry через {backoff}s (причина: {e})")
            time.sleep(backoff)

    return None

def check_reputation(emails, out_dir: Path):
    rep_cache = load_rep_cache()
    driver = make_driver()

    good = open(out_dir / "reputation_good.txt", "w", encoding="utf-8")
    mid  = open(out_dir / "reputation_medium.txt", "w", encoding="utf-8")
    bad  = open(out_dir / "reputation_bad.txt", "w", encoding="utf-8")
    fail = open(out_dir / "reputation_retry_failed.txt", "w", encoding="utf-8")
    errf = open(out_dir / "reputation_errors.txt", "w", encoding="utf-8")

    try:
        for email in emails:
            if stop_event.is_set():
                break

            score = None

            if email in rep_cache and str(rep_cache[email]).strip().isdigit():
                cached = int(rep_cache[email])
                if cached != 0:
                    score = cached
                    print(Fore.CYAN + f"{email} — репутация из кэша: {score}")

            if score is None:
                try:
                    score = _get_reputation_with_retry(driver, email)
                except Exception as e:
                    errf.write(f"{email} | EXC | {repr(e)}\n")
                    score = None

                if score is not None:
                    save_rep_cache(email, score)

            if score is None:
                fail.write(email + "\n")
                continue

            if score >= 71:
                good.write(f"{email}:{score}\n")
            elif score >= 31:
                mid.write(f"{email}:{score}\n")
            else:
                bad.write(f"{email}:{score}\n")

            print(Fore.GREEN + f"{email} — репутация {score}")
            time.sleep(1.5)

    except KeyboardInterrupt:
        stop_event.set()
        print(Fore.YELLOW + "\nОстановка репутации по Ctrl+C...")

    finally:
        good.close(); mid.close(); bad.close(); fail.close(); errf.close()
        try:
            driver.quit()
        except:
            pass

# ==========================
# MAIN
# ==========================
def main():
    ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    out_dir = Path(f"results_{ts}")
    out_dir.mkdir(exist_ok=True)
    print(Fore.CYAN + f"[RESULTS] {out_dir}")

    checked_cache = load_cache_set(CACHE_AVAIL)
    print(Fore.CYAN + f"[CACHE availability] {len(checked_cache)}")

    mails_file = input(f"Файл email ({MAILS_FILE_DEFAULT}): ").strip() or MAILS_FILE_DEFAULT
    raw_emails = load_lines(mails_file)

    if not raw_emails:
        print(Fore.RED + f"Файл {mails_file} пустой или отсутствует")
        return
    # ==========================
    # ВЫБОР РЕЖИМА РАБОТЫ
    # ==========================
    print("\nВыбери режим работы:")
    print("1 — Проверять занятость логинов + репутацию")
    print("2 — Проверять ТОЛЬКО репутацию (без проверки занятости)")

    mode = input("Твой выбор (1/2): ").strip()

    if mode not in ("1", "2"):
        print("Неверный выбор")
        return

    # ===== ТОЛЬКО РЕПУТАЦІЯ =====
    if mode == "2":
        emails = [e for e in raw_emails if "@" in e]

        if not emails:
            print(Fore.RED + "Нет валидных email для проверки репутации")
            return

        print(Fore.CYAN + f"Проверка ТОЛЬКО репутации ({len(emails)})")
        check_reputation(emails, out_dir)
        print(Fore.CYAN + "\nГотово.")
        return
    # ==========================
    # НОВОЕ: фильтрация email
    # ==========================
    yahoo_logins = []
    aol_logins = []

    for email in raw_emails:
        if "@" not in email:
            continue

        login, domain = email.rsplit("@", 1)
        domain = domain.lower()

        if domain == "yahoo.com":
            yahoo_logins.append(login)
        elif domain == "aol.com":
            aol_logins.append(login)

    # СНАЧАЛА YAHOO, ПОТОМ AOL
    process_logins = yahoo_logins + aol_logins

    if not process_logins:
        print(Fore.YELLOW + "Нет email с доменами yahoo.com или aol.com")
        return

    limit = int(input("Сколько логинов проверить? (0 = все): ") or "0")
    if limit > 0:
        process_logins = process_logins[:limit]

    # Убираем полностью закэшированные
    fully_cached = [lg for lg in process_logins if _login_fully_cached(lg, checked_cache)]
    process_logins = [lg for lg in process_logins if lg not in fully_cached]

    if fully_cached:
        print(Fore.CYAN + f"[SKIP] Уже в кэше (yahoo+aol): {len(fully_cached)}")

    if not process_logins:
        print(Fore.YELLOW + "Нечего проверять — всё уже в кэше")
        return

    batch = int(input(f"Размер пакета ({DEFAULT_BATCH_SIZE}): ") or DEFAULT_BATCH_SIZE)

    avail_path = out_dir / "available.txt"
    busy_path  = out_dir / "busy.txt"

    cache_lock = Lock()
    done_lock = Lock()

    try:
        with open(avail_path, "w", encoding="utf-8") as af, open(busy_path, "w", encoding="utf-8") as bf:
            for i in range(0, len(process_logins), batch):
                if stop_event.is_set():
                    break

                chunk = process_logins[i:i + batch]
                print(Fore.MAGENTA + f"\n=== Пакет {i // batch + 1} ({len(chunk)}) ===")

                logins_by_domain = {
                    dom: _logins_need_domain(dom, chunk, checked_cache)
                    for dom in SUPPORTED_DOMAINS
                }

                login_done_map = {}

                # ВАЖНО: сначала Yahoo, потом AOL
                for dom in ["yahoo.com", "aol.com"]:
                    process_domain(
                        dom,
                        logins_by_domain[dom],
                        checked_cache,
                        cache_lock,
                        af,
                        bf,
                        login_done_map,
                        done_lock
                    )

    except KeyboardInterrupt:
        stop_event.set()
        print(Fore.YELLOW + "\nОстановка пользователем (Ctrl+C).")

    if not stop_event.is_set():
        emails = [l.split(":")[0] for l in load_lines(avail_path)]
        if emails:
            print(Fore.CYAN + "\nПереход к проверке репутации")
            check_reputation(emails, out_dir)

    print(Fore.CYAN + "\nГотово.")

if __name__ == "__main__":
    main()
