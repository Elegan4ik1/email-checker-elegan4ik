#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys

DEV_BANNER = r"""
=================================================
   ███████╗██╗     ███████╗ ██████╗  █████╗ ███╗   ██╗██╗  ██╗██╗██╗  ██╗
   ██╔════╝██║     ██╔════╝██╔════╝ ██╔══██╗████╗  ██║██║  ██║██║██║ ██╔╝
   █████╗  ██║     █████╗  ██║  ███╗███████║██╔██╗ ██║███████║██║█████╔╝ 
   ██╔══╝  ██║     ██╔══╝  ██║   ██║██╔══██║██║╚██╗██║██╔══██║██║██╔═██╗ 
   ███████╗███████╗███████╗╚██████╔╝██║  ██║██║ ╚████║██║  ██║██║██║  ██╗
   ╚══════╝╚══════╝╚══════╝ ╚═════╝ ╚═╝  ╚═╝╚═╝  ╚═══╝╚═╝  ╚═╝╚═╝╚═╝  ╚═╝

                     Developer: Elegan4ik
=================================================
"""

APP_TITLE = "Email Checker and Reputation — Elegan4ik"

def set_console_title(title: str) -> None:
    if os.name == "nt":
        os.system(f"title {title}")
    else:
        sys.stdout.write(f"\33]0;{title}\7")
        sys.stdout.flush()

# дальше идут твои остальные импорты как обычно, каждый с новой строки:
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
AVAIL_VALIDATION_TIMEOUT = 25      # максимум ждём 25 секунд
AVAIL_STABLE_OK_SECONDS = 7.5      # нужно стабильное состояние минимум 4.5 сек
AVAIL_IGNORE_ERROR_INITIAL = 2.5   # игнорируем первые 1.5 сек ошибки
AVAIL_POLL_INTERVAL = 0.25         # проверяем каждые 250 мс
AVAIL_AFTER_INPUT_DELAY = 0.6

# ===== Reputation =====
REP_MAX_ATTEMPTS = 4          # было 2
REP_WAIT_SECONDS = 180        # оставь
REP_RETRY_BACKOFF = (10, 20, 40, 80)  # было (5, 10)
REP_REQUIRE_NONZERO = False   # важно: 0 считаем ошибкой в логике, а не как валидный результат
REP_AFTER_CLICK_DELAY = 6.0   # было 2.0
UNABLE_MAX_HITS = 3           # было 2

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

        for item in logins:
            if stop_event.is_set():
                break

            # item может быть как "login", так и "login@domain"
            raw = (item or "").strip()
            if "@" in raw:
                login_part, dom_part = raw.split("@", 1)
                login = login_part
                email = f"{login_part}@{dom_part}".lower()
            else:
                login = raw
                email = f"{raw}@{domain}".lower()

            # Если вдруг передали не тот домен — не ломаем цикл, но и не проверяем "не свой" email
            if "@" in raw:
                try:
                    dom_part = raw.split("@", 1)[1].lower()
                    if dom_part != domain.lower():
                        print(Fore.YELLOW + f"[{domain}] Пропуск (домен не совпал): {raw}")
                        mark_login_done(login_done_map, email, domain, done_lock)
                        continue
                except:
                    pass

            with cache_lock:
                if email in checked_cache:
                    mark_login_done(login_done_map, email, domain, done_lock)
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

                    # ВАЖНО: в поле вводим только часть до "@"
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

                    # В этом режиме "единица работы" — полный email
                    mark_login_done(login_done_map, email, domain, done_lock)
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
                        mark_login_done(login_done_map, email, domain, done_lock)
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
                        mark_login_done(login_done_map, email, domain, done_lock)
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

        # Если score ещё не распарсился — просто ждём дальше
        if score is not None:
            # Если требуем ненулевой, а пришёл 0 — считаем, что "ещё не готово"
            if require_nonzero and score == 0:
                stable_score = None
                stable_since = None
            else:
                # Считаем значение "готовым", когда оно стабильно держится 2 секунды
                if stable_score != score:
                    stable_score = score
                    stable_since = time.time()
                else:
                    if stable_since is not None and (time.time() - stable_since) >= 2.0:
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
                require_nonzero=False
            )

            # Если получили 0 — это не "рейтинг", а страница/JS/лимит не успели.
            # На слабом ПК лучше не читать сразу второй раз, а дать паузу и повторить попытку заново.
            if score is None or int(score) == 0:
                print(Fore.MAGENTA + f"{email} — результат 0/пусто, ждём и пробуем ещё раз...")
                time.sleep(random.uniform(8, 15))
                try:
                    driver.refresh()
                except:
                    pass
                continue

            # Успех — сбрасываем счетчик Unable
            unable_hits = 0
            return int(score)

        except UnableToCheckEmail:
            unable_hits += 1
            print(Fore.MAGENTA + f"{email} — Unable... ({unable_hits}/{UNABLE_MAX_HITS})")

            # Дай странице/ПК отдышаться
            time.sleep(random.uniform(8, 15))

            # refresh вместо мгновенного нового get
            try:
                driver.refresh()
            except:
                pass

            if unable_hits >= UNABLE_MAX_HITS:
                print(Fore.RED + f"{email} — SKIP: Unable... слишком часто, репутация НЕ получена")
                return None

            continue

        except KeyboardInterrupt:
            stop_event.set()
            return None

        except (TimeoutException, WebDriverException, Exception) as e:
            if attempt >= REP_MAX_ATTEMPTS:
                print(Fore.RED + f"{email} — репутация НЕ получена: {e}")
                return None

            # backoff из твоего массива + небольшой jitter
            backoff = REP_RETRY_BACKOFF[min(attempt - 1, len(REP_RETRY_BACKOFF) - 1)]
            backoff = backoff + random.uniform(0.5, 3.0)

            print(Fore.MAGENTA + f"{email} — retry через {backoff:.1f}s (причина: {e})")
            time.sleep(backoff)

            # Если много проблем подряд — перезапускаем драйвер на 3-й и 5-й попытке
            if attempt in (3, 5):
                try:
                    driver.quit()
                except:
                    pass
                try:
                    driver = make_driver()
                except:
                    return None

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

            # кэш читаем, но 0 игнорируем
            if email in rep_cache and str(rep_cache[email]).strip().isdigit():
                cached = int(rep_cache[email])
                if cached > 0:
                    score = cached
                    print(Fore.CYAN + f"{email} — репутация из кэша: {score}")

            if score is None:
                try:
                    score = _get_reputation_with_retry(driver, email)
                except Exception as e:
                    errf.write(f"{email} | EXC | {repr(e)}\n")
                    score = None

                # кэшируем ТОЛЬКО если score > 0
                if score is not None and int(score) > 0:
                    save_rep_cache(email, score)

            # если не получили score — в retry_failed
            if score is None or int(score) <= 0:
                fail.write(email + "\n")
                continue

            # раскладываем по файлам
            if score >= 71:
                good.write(f"{email}:{score}\n")
            elif score >= 31:
                mid.write(f"{email}:{score}\n")
            else:
                bad.write(f"{email}:{score}\n")

            print(Fore.GREEN + f"{email} — репутация {score}")

            # было 1.5 — ставим человеческую паузу, чтобы меньше ловить лимит/Unable
            time.sleep(random.uniform(6, 14))

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
    set_console_title(APP_TITLE)
    print(DEV_BANNER)
    ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    out_dir = Path(f"results_{ts}")
    out_dir.mkdir(exist_ok=True)
    print(Fore.CYAN + f"[RESULTS] {out_dir}")

    # В availability-кэше храним и сравниваем emails в lower(), чтобы не зависеть от регистра в mail.txt
    checked_cache = {x.lower() for x in load_cache_set(CACHE_AVAIL)}
    print(Fore.CYAN + f"[CACHE availability] {len(checked_cache)}")

    mails_file = input(f"Файл логинов ({MAILS_FILE_DEFAULT}): ").strip() or MAILS_FILE_DEFAULT

    original_file_lines = load_lines(mails_file)
    if not original_file_lines:
        print(Fore.RED + f"Файл {mails_file} пустой или отсутствует")
        return

    # 1) фильтрация: берём только yahoo.com и aol.com
    filtered = []
    for line in original_file_lines:
        raw = (line or "").strip()
        if "@" not in raw:
            continue
        local, dom = raw.rsplit("@", 1)
        if not local:
            continue
        dom_l = dom.strip().lower()
        if dom_l in ("yahoo.com", "aol.com"):
            filtered.append(f"{local.strip()}@{dom_l}".lower())

    if not filtered:
        print(Fore.YELLOW + "После фильтрации по yahoo.com/aol.com — нечего проверять.")
        print(Fore.CYAN + "\nГотово.")
        return

    # 2) сортировка: сначала все Yahoo, потом все AOL (порядок внутри домена сохраняется как в файле)
    yahoo_emails = [e for e in filtered if e.endswith("@yahoo.com")]
    aol_emails   = [e for e in filtered if e.endswith("@aol.com")]
    process_emails_sorted = yahoo_emails + aol_emails

    limit = int(input("Сколько логинов проверить? (0 = все): ") or "0")
    process_emails = process_emails_sorted[:limit] if limit > 0 else list(process_emails_sorted)

    # 3) убираем те, что уже в кэше availability
    cached = [e for e in process_emails if e in checked_cache]
    process_emails = [e for e in process_emails if e not in set(cached)]

    if cached:
        print(Fore.CYAN + f"[SKIP] Уже в кэше availability: {len(cached)} email(ов)")

    if not process_emails:
        print(Fore.YELLOW + "Нечего проверять: все выбранные email уже есть в checked_cache.txt")
        # удаляем из mail.txt только те Yahoo/AOL email, которые попали в выбранный лимит и уже в кэше
        cached_set = set(cached)
        remaining = []
        for x in original_file_lines:
            rx = (x or "").strip()
            if "@" in rx:
                loc, dom = rx.rsplit("@", 1)
                norm = f"{loc.strip()}@{dom.strip().lower()}".lower()
                if norm in cached_set:
                    continue
            remaining.append(rx)
        write_lines(mails_file, remaining)
        print(Fore.CYAN + f"[MAIL FILE] Обновил {mails_file}. Осталось: {len(remaining)}")
        print(Fore.CYAN + "\nГотово.")
        return

    batch = int(input(f"Размер пакета ({DEFAULT_BATCH_SIZE}): ") or DEFAULT_BATCH_SIZE)

    avail_path = out_dir / "available.txt"
    busy_path  = out_dir / "busy.txt"

    cache_lock = Lock()
    done_lock = Lock()

    remaining_lines = list(original_file_lines)

    try:
        with open(avail_path, "w", encoding="utf-8") as af, open(busy_path, "w", encoding="utf-8") as bf:
            for i in range(0, len(process_emails), batch):
                if stop_event.is_set():
                    break

                chunk = process_emails[i:i + batch]
                print(Fore.MAGENTA + f"\n=== Пакет {i // batch + 1} ({len(chunk)}) ===")

                # раскладываем по доменам (передаём ПОЛНЫЕ email-адреса)
                emails_by_domain = {
                    "yahoo.com": [e for e in chunk if e.endswith("@yahoo.com") and e not in checked_cache],
                    "aol.com":   [e for e in chunk if e.endswith("@aol.com")   and e not in checked_cache],
                }

                login_done_map = {}

                # Сначала Yahoo, потом AOL
                for dom in ["yahoo.com", "aol.com"]:
                    process_domain(
                        dom,
                        emails_by_domain[dom],
                        checked_cache,
                        cache_lock,
                        af,
                        bf,
                        login_done_map,
                        done_lock
                    )

                # В этом режиме ключи в login_done_map — это full email
                done_emails = set(login_done_map.keys())
                if done_emails:
                    new_remaining = []
                    for x in remaining_lines:
                        rx = (x or "").strip()
                        if "@" in rx:
                            loc, dom = rx.rsplit("@", 1)
                            norm = f"{loc.strip()}@{dom.strip().lower()}".lower()
                            if norm in done_emails:
                                continue
                        new_remaining.append(rx)
                    remaining_lines = new_remaining
                    write_lines(mails_file, remaining_lines)
                    print(Fore.CYAN + f"[MAIL FILE] Удалено из {mails_file}: {len(done_emails)} email(ов). Осталось: {len(remaining_lines)}")

    except KeyboardInterrupt:
        stop_event.set()
        print(Fore.YELLOW + "\nОстановка пользователем (Ctrl+C).")

    if not stop_event.is_set():
        emails = [l.split(":")[0] for l in load_lines(avail_path)]
        if emails:
            print(Fore.CYAN + "\nПереход к проверке репутации (Mailmeteor) — ждём результат, затем следующий email")
            check_reputation(emails, out_dir)

    print(Fore.CYAN + "\nГотово.")

if __name__ == "__main__":
    main()
