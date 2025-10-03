# apps/rhid_runner/automator.py
from __future__ import annotations

import os
import time
import logging
import tempfile
import shutil
from datetime import datetime, timezone
from typing import Optional, Iterable, Tuple

from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.action_chains import ActionChains
from selenium.common.exceptions import (
    TimeoutException,
    WebDriverException,
    SessionNotCreatedException,
    StaleElementReferenceException,
    ElementClickInterceptedException,
    ElementNotInteractableException,
)

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
load_dotenv()

log = logging.getLogger(__name__)
logging.basicConfig(
    level=os.environ.get("LOGLEVEL", "INFO").upper(),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

HEADLESS = os.environ.get("HEADLESS", "true").lower() == "true"
TIMEOUT_SECONDS = int(os.environ.get("TIMEOUT_SECONDS", "35"))
POST_LOGIN_SELECTOR = os.environ.get("POST_LOGIN_SELECTOR", "")
CHROME_BINARY = os.environ.get("CHROME_BINARY", "/usr/bin/google-chrome")
PUNCH_DRY_RUN = os.environ.get("PUNCH_DRY_RUN", "false").lower() == "true"
SCREENSHOT_ON_ERROR = os.environ.get("SCREENSHOT_ON_ERROR", "0") == "1"

CHROME_USER_DATA_BASE = os.environ.get("CHROME_USER_DATA_BASE", "/tmp/rhid-chrome")
os.makedirs(CHROME_USER_DATA_BASE, exist_ok=True)
RHID_USE_USER_DATA = os.environ.get("RHID_USE_USER_DATA", "0") == "1"

# Localização fixa (empresa) para compor a mensagem do Discord
COMPANY_LAT = os.environ.get("COMPANY_LAT")
COMPANY_LON = os.environ.get("COMPANY_LON")

# -----------------------------------------------------------------------------
# Seletores flexíveis
# -----------------------------------------------------------------------------
EMAIL_SELECTORS = [
    (By.ID, "email"),
    (By.CSS_SELECTOR, "input#email.form-control.m-input"),
]
PASSWORD_SELECTORS = [
    (By.ID, "password"),
    (By.CSS_SELECTOR, "input#password.form-control.m-input"),
]
LOGIN_BTN_SELECTORS = [
    (By.XPATH, "//button[normalize-space()='Entrar']"),
    (By.CSS_SELECTOR, "button[type='submit']"),
    (By.CSS_SELECTOR, "button.btn.m-btn.m-btn--custom"),
]

# Overlays/Spinners comuns que podem interceptar clique
OVERLAY_SELECTORS = [
    ".swal2-container", ".swal2-shown",
    ".modal.in", ".modal-backdrop",
    ".blockUI", ".block-overlay", ".loading", ".spinner", ".overlay",
]

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def _mktemp_under_base(prefix: str) -> str:
    return tempfile.mkdtemp(prefix=prefix, dir=CHROME_USER_DATA_BASE)

def _cleanup_tmp_list(paths: list[str]) -> None:
    for d in paths:
        try:
            shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass

def _dump_small_html(driver: webdriver.Chrome, max_len: int = 2000) -> str:
    try:
        html = driver.page_source or ""
        compact = " ".join(html.split())
        return compact[:max_len]
    except Exception:
        return "<no html>"

def _screenshot(driver: webdriver.Chrome, path="/tmp/rhid-last.png"):
    if not SCREENSHOT_ON_ERROR:
        return
    try:
        driver.save_screenshot(path)
        log.info("Screenshot salvo em: %s", path)
    except Exception:
        pass

def wait_dom_ready(driver: webdriver.Chrome, timeout: int = TIMEOUT_SECONDS):
    WebDriverWait(driver, timeout).until(
        lambda d: d.execute_script("return document.readyState") in ("interactive", "complete")
    )

def wait_visible(driver: webdriver.Chrome, locator: Tuple[str, str], timeout: int = TIMEOUT_SECONDS):
    return WebDriverWait(driver, timeout).until(EC.visibility_of_element_located(locator))

def find_first_visible(driver: webdriver.Chrome, locators: Iterable[Tuple[str, str]], timeout_each: int = 6):
    last_exc = None
    for by, sel in locators:
        try:
            el = wait_visible(driver, (by, sel), timeout=timeout_each)
            log.info("Elemento encontrado por %s: %s", by, sel)
            return el
        except Exception as e:
            last_exc = e
    if last_exc:
        raise last_exc
    raise TimeoutException("Nenhum seletor visível encontrado.")

def maybe_already_logged(driver: webdriver.Chrome) -> bool:
    if not POST_LOGIN_SELECTOR:
        return False
    try:
        WebDriverWait(driver, 3).until(EC.presence_of_element_located((By.CSS_SELECTOR, POST_LOGIN_SELECTOR)))
        log.info("Detecção de sessão já logada pelo seletor: %s", POST_LOGIN_SELECTOR)
        return True
    except Exception:
        return False

def type_with_retry(driver, locator, text, attempts=3, timeout_each=10):
    last_exc = None
    for _ in range(attempts):
        try:
            el = WebDriverWait(driver, timeout_each).until(EC.element_to_be_clickable(locator))
            try:
                el.click(); time.sleep(0.05); el.clear()
            except StaleElementReferenceException:
                el = WebDriverWait(driver, timeout_each).until(EC.element_to_be_clickable(locator))
                el.click(); time.sleep(0.05); el.clear()
            el.send_keys(text)
            return
        except StaleElementReferenceException as e:
            last_exc = e; time.sleep(0.2); continue
        except Exception as e:
            last_exc = e; break
    try:
        el = WebDriverWait(driver, timeout_each).until(EC.presence_of_element_located(locator))
        driver.execute_script(
            "arguments[0].value = arguments[1];"
            "arguments[0].dispatchEvent(new Event('input', {bubbles:true}));"
            "arguments[0].dispatchEvent(new Event('change', {bubbles:true}));",
            el, text
        )
        return
    except Exception as e:
        raise last_exc or e

def _wait_no_overlays(driver: webdriver.Chrome, timeout: int = 8) -> None:
    """Espera sumirem overlays que possam interceptar cliques."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            present = False
            for sel in OVERLAY_SELECTORS:
                nodes = driver.find_elements(By.CSS_SELECTOR, sel)
                for n in nodes:
                    if n.is_displayed() and n.value_of_css_property("visibility") != "hidden":
                        present = True
                        break
                if present:
                    break
            if not present:
                return
        except Exception:
            pass
        time.sleep(0.15)

def _point_hits_element(driver: webdriver.Chrome, el) -> bool:
    """Confere se o ponto central do elemento atinge o próprio elemento (não está coberto)."""
    try:
        rect = driver.execute_script("""
            const r = arguments[0].getBoundingClientRect();
            return {x: Math.floor(r.left + r.width/2), y: Math.floor(r.top + r.height/2), w:r.width, h:r.height};
        """, el)
        if rect["w"] <= 0 or rect["h"] <= 0:
            return False
        hit = driver.execute_script("""
            const x = arguments[0], y = arguments[1], el = arguments[2];
            const e = document.elementFromPoint(x, y);
            return e === el || (e && el.contains(e));
        """, rect["x"], rect["y"], el)
        return bool(hit)
    except Exception:
        return False

def robust_click(driver: webdriver.Chrome, el, *, timeout: int = 10) -> None:
    """
    Clique robusto:
    - scroll até o centro,
    - espera sumirem overlays,
    - testa se ponto central atinge o elemento,
    - tenta .click(), depois Actions, por fim JS click com MouseEvent.
    """
    end = time.time() + timeout
    last_exc = None

    while time.time() < end:
        try:
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
            _wait_no_overlays(driver, timeout=2)
            time.sleep(0.05)

            if (not el.is_displayed()) or (not el.is_enabled()):
                time.sleep(0.2)
                continue

            if not _point_hits_element(driver, el):
                # nudge leve para forçar repaint
                ActionChains(driver).move_to_element_with_offset(el, 1, 1).perform()
                time.sleep(0.1)
                if not _point_hits_element(driver, el):
                    time.sleep(0.15)
                    continue

            # 1) clique padrão
            try:
                el.click()
                return
            except (ElementClickInterceptedException, ElementNotInteractableException) as e:
                last_exc = e

            # 2) Actions
            try:
                ActionChains(driver).move_to_element(el).pause(0.05).click().perform()
                return
            except Exception as e:
                last_exc = e

            # 3) JS click
            try:
                driver.execute_script("""
                    const el = arguments[0];
                    el.focus({preventScroll:true});
                    const evt = new MouseEvent('click', {bubbles:true, cancelable:true, view:window});
                    el.dispatchEvent(evt);
                """, el)
                return
            except Exception as e:
                last_exc = e

        except StaleElementReferenceException as e:
            last_exc = e
        except Exception as e:
            last_exc = e

        time.sleep(0.2)

    raise last_exc or ElementNotInteractableException("Não foi possível clicar no elemento dentro do timeout.")

def click_with_retry(driver, locators: Iterable[Tuple[str, str]], attempts=3, timeout_each=10):
    """Mantido para cliques genéricos (não-críticos)."""
    last_exc = None
    for _ in range(attempts):
        for by, sel in locators:
            try:
                el = WebDriverWait(driver, timeout_each).until(EC.element_to_be_clickable((by, sel)))
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                time.sleep(0.05)
                el.click()
                return
            except (StaleElementReferenceException, ElementClickInterceptedException) as e:
                last_exc = e; time.sleep(0.2); continue
            except Exception as e:
                last_exc = e; continue
    raise last_exc or RuntimeError("Falha ao clicar no elemento após múltiplas tentativas.")

def _now_human() -> str:
    """Retorna horário local bonito: 03/10/2025 13:52:33 BRT"""
    try:
        return datetime.now().astimezone().strftime("%d/%m/%Y %H:%M:%S %Z")
    except Exception:
        # fallback UTC
        return datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M:%S UTC")

def _location_human() -> tuple[str, Optional[str]]:
    """Monta string de localização e link do Maps a partir de COMPANY_LAT/LON (se houver)."""
    if not COMPANY_LAT or not COMPANY_LON:
        return ("Indefinida", None)
    lat = COMPANY_LAT.strip()
    lon = COMPANY_LON.strip()
    maps = f"https://maps.google.com/?q={lat},{lon}"
    return (f"{lat}, {lon} (Empresa)", maps)

# -----------------------------------------------------------------------------
# Chrome Driver
# -----------------------------------------------------------------------------
def _make_chrome_options(*, data_path: str, cache_dir: str, user_data_dir: str | None) -> Options:
    opts = Options()
    if HEADLESS:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--disable-extensions")
    opts.add_argument("--disable-infobars")
    opts.add_argument("--disable-notifications")
    opts.add_argument("--window-size=1280,960")
    opts.add_argument("--incognito")
    opts.add_argument("--guest")
    opts.add_argument(f"--data-path={data_path}")
    opts.add_argument(f"--disk-cache-dir={cache_dir}")
    if RHID_USE_USER_DATA and user_data_dir:
        opts.add_argument(f"--user-data-dir={user_data_dir}")
        opts.add_argument("--profile-directory=Default")
    opts.add_argument("--remote-debugging-port=0")
    opts.add_argument("--no-first-run")
    opts.add_argument("--no-default-browser-check")
    opts.add_argument("--disable-background-networking")
    opts.add_argument("--disable-features=TranslateUI,AutomationControlled")
    opts.add_argument("--disable-client-side-phishing-detection")
    opts.add_argument("--disable-component-update")
    opts.add_argument("--disable-default-apps")
    opts.add_argument("--disable-sync")
    opts.add_argument("--metrics-recording-only")
    opts.add_argument("--password-store=basic")
    opts.add_argument("--use-mock-keychain")
    if CHROME_BINARY and os.path.exists(CHROME_BINARY):
        opts.binary_location = CHROME_BINARY
    return opts

def _build_driver() -> webdriver.Chrome:
    data_path = _mktemp_under_base("data-")
    cache_dir = _mktemp_under_base("cache-")
    user_data_dir: Optional[str] = _mktemp_under_base("profile-") if RHID_USE_USER_DATA else None

    last_err: Optional[Exception] = None
    for attempt in (1, 2):
        try:
            log.info(
                "Inicializando Chrome (tentativa %s) data_path=%s cache_dir=%s user_data_dir=%s",
                attempt, data_path, cache_dir, user_data_dir,
            )
            opts = _make_chrome_options(data_path=data_path, cache_dir=cache_dir, user_data_dir=user_data_dir)
            service = Service(ChromeDriverManager().install())
            driver = webdriver.Chrome(service=service, options=opts)
            driver._tmp_dirs = [data_path, cache_dir] + ([user_data_dir] if user_data_dir else [])
            return driver
        except (SessionNotCreatedException, WebDriverException) as e:
            last_err = e
            log.warning("Falha ao criar sessão do Chrome (tentativa %s): %s", attempt, e)
            _cleanup_tmp_list([data_path, cache_dir] + ([user_data_dir] if user_data_dir else []))
            data_path = _mktemp_under_base("data-")
            cache_dir = _mktemp_under_base("cache-")
            user_data_dir = _mktemp_under_base("profile-") if RHID_USE_USER_DATA else None
            time.sleep(0.8)
        except Exception as e:
            last_err = e
            log.exception("Erro inesperado ao criar Chrome (tentativa %s):", attempt)
            _cleanup_tmp_list([data_path, cache_dir] + ([user_data_dir] if user_data_dir else []))
            break
    assert last_err is not None
    raise last_err

# -----------------------------------------------------------------------------
# Fluxo RHID
# -----------------------------------------------------------------------------
def _wait_url_contains(driver: webdriver.Chrome, needle: str, timeout: int = TIMEOUT_SECONDS) -> bool:
    try:
        WebDriverWait(driver, timeout).until(lambda d: needle.lower() in (d.current_url or "").lower())
        log.info("URL atual contém '%s': %s", needle, driver.current_url)
        return True
    except TimeoutException:
        log.warning("URL NÃO ficou como esperado ('%s'). URL atual: %s", needle, driver.current_url)
        return False

def _login(driver: webdriver.Chrome, email: str, senha: str) -> None:
    url = os.environ.get("RHID_LOGIN_URL") or os.environ.get("RHID_URL")
    if not url:
        raise RuntimeError("Defina RHID_URL (ou RHID_LOGIN_URL) no .env para a página de login.")

    driver.get(url)
    wait_dom_ready(driver)
    time.sleep(0.5)

    if maybe_already_logged(driver):
        log.info("Já autenticado; URL atual: %s", driver.current_url)
        return

    _ = find_first_visible(driver, EMAIL_SELECTORS, timeout_each=10)
    _ = find_first_visible(driver, PASSWORD_SELECTORS, timeout_each=10)
    _ = find_first_visible(driver, LOGIN_BTN_SELECTORS, timeout_each=10)

    type_with_retry(driver, (By.ID, "email"), email)
    log.info("Preencheu campo email.")
    type_with_retry(driver, (By.ID, "password"), senha)
    log.info("Preencheu campo senha.")

    click_with_retry(driver, LOGIN_BTN_SELECTORS, attempts=3, timeout_each=10)
    log.info("Clique no botão 'Entrar' efetuado. Aguardando redirecionamento para /#/dashboard ...")

    try:
        WebDriverWait(driver, TIMEOUT_SECONDS).until(
            lambda d: "#/dashboard" in (d.current_url or "").lower()
                      or d.find_elements(By.XPATH, "//button[contains(@ng-click,'marcacao_ponto') and contains(normalize-space(.),'Registrar Ponto')]")
        )
        log.info("Pós-login OK. URL atual: %s", driver.current_url)
    except TimeoutException:
        log.warning("Não confirmei /#/dashboard explicitamente (prosseguindo). URL atual: %s", driver.current_url)

def _registrar_ponto(driver: webdriver.Chrome) -> str:
    """
    1) Dashboard: clicar 'Registrar Ponto' e ir para /#/marcacao_ponto
    2) /#/marcacao_ponto: esperar botão habilitar e clicar
    """
    def _visible_or_none(by, sel, t=4):
        try:
            return WebDriverWait(driver, t).until(EC.visibility_of_element_located((by, sel)))
        except Exception:
            return None

    # ---------- ETAPA 1 (DASHBOARD) ----------
    log.info("Procurando botão 'Registrar Ponto' na DASHBOARD ...")
    dash_btns = [
        (By.XPATH, "//button[contains(@ng-click,'marcacao_ponto') and contains(normalize-space(.),'Registrar Ponto')]"),
        (By.XPATH, "//button[contains(@ng-click,'redirect') and contains(normalize-space(.),'Registrar Ponto')]"),
        (By.XPATH, "//button[contains(normalize-space(.),'Registrar Ponto') and contains(@class,'btn-primary')]"),
    ]
    dash_btn = None
    for by, sel in dash_btns:
        el = _visible_or_none(by, sel, t=6)
        if el:
            dash_btn = el
            log.info("Botão da dashboard encontrado por %s: %s", by, sel)
            break
    if dash_btn:
        try:
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", dash_btn)
            time.sleep(0.05)
            dash_btn.click()
            log.info("CLIQUE executado no botão 'Registrar Ponto' da DASHBOARD.")
        except Exception:
            click_with_retry(driver, dash_btns, attempts=2, timeout_each=6)
            log.info("CLIQUE (retry) executado no botão 'Registrar Ponto' da DASHBOARD.")
    else:
        log.warning("Botão 'Registrar Ponto' da DASHBOARD não encontrado. URL: %s", driver.current_url)

    log.info("Aguardando redirecionamento para /#/marcacao_ponto ...")
    _wait_url_contains(driver, "#/marcacao_ponto", timeout=TIMEOUT_SECONDS)

    # ---------- ETAPA 2 (MARCAÇÃO) ----------
    log.info("Na tela de MARCAÇÃO: procurando botão final 'Registrar Ponto' ...")
    final_locators = [
        (By.XPATH, "//button[contains(@ng-click,'registraPonto') and contains(normalize-space(.),'Registrar Ponto')]"),
        (By.XPATH, "//div[contains(.,'Registre seu ponto') or contains(.,'2 - Registre seu ponto')]//button[contains(normalize-space(.),'Registrar Ponto')]"),
        (By.XPATH, "//button[contains(normalize-space(.),'Registrar Ponto') and contains(@class,'btn-primary')]"),
        (By.XPATH, "//button[contains(normalize-space(.),'Registrar Ponto')]"),
    ]

    # localizar (mesmo desabilitado)
    final_btn = None
    deadline = time.time() + TIMEOUT_SECONDS
    while time.time() < deadline and final_btn is None:
        for by, sel in final_locators:
            try:
                el = WebDriverWait(driver, 2).until(EC.presence_of_element_located((by, sel)))
                final_btn = el
                log.info("Botão FINAL localizado (pode estar desabilitado) por %s: %s", by, sel)
                break
            except Exception:
                pass
        if final_btn is None:
            time.sleep(0.3)

    if not final_btn:
        html = _dump_small_html(driver)
        _screenshot(driver)
        raise RuntimeError("Não encontrei o botão final 'Registrar Ponto'. HTML compacto: " + html[:800])

    # esperar habilitar (ng-disabled removido)
    log.info("Aguardando botão FINAL habilitar (ng-disabled ficar falso) ...")
    def _enabled():
        try:
            btn = None
            for by, sel in final_locators:
                try:
                    btn = driver.find_element(by, sel); break
                except Exception:
                    continue
            if not btn:
                return False
            disabled_attr = btn.get_attribute("disabled")
            return btn.is_enabled() and (disabled_attr is None)
        except StaleElementReferenceException:
            return False

    try:
        WebDriverWait(driver, TIMEOUT_SECONDS).until(lambda d: _enabled())
        log.info("Botão FINAL habilitado. Pronto para clicar.")
    except TimeoutException:
        _screenshot(driver)
        raise RuntimeError("Botão FINAL não habilitou dentro do timeout. Verifique geolocalização/permite GPS.")

    # DRY RUN: não clicar no final
    if PUNCH_DRY_RUN:
        log.info("DRY RUN ativo — não clicarei no botão FINAL. URL: %s", driver.current_url)
        return "DRY RUN: cheguei ao botão final habilitado; clique final não executado."

    # 2.3 clicar (robusto)
    try:
        # re-obter o botão mais fresco (evita stale após Angular re-render)
        for by, sel in final_locators:
            try:
                final_btn = driver.find_element(by, sel)
                break
            except Exception:
                pass

        robust_click(driver, final_btn, timeout=10)
        log.info("CLIQUE executado no botão FINAL 'Registrar Ponto'.")
    except Exception:
        try:
            for by, sel in final_locators:
                el = WebDriverWait(driver, 3).until(EC.presence_of_element_located((by, sel)))
                robust_click(driver, el, timeout=6)
                log.info("CLIQUE (fallback) executado no botão FINAL.")
                break
        except Exception as e:
            _screenshot(driver)
            raise

    # confirmação
    try:
        WebDriverWait(driver, 8).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "#modalPontoRegistrado, .swal2-popup, .toast, .alert-success"))
        )
        log.info("Confirmação visível (modal/toast).")
    except TimeoutException:
        try:
            WebDriverWait(driver, 6).until(
                EC.presence_of_element_located(
                    (By.XPATH, "//*[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'registrado') or "
                               "contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'realizado') or "
                               "contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'sucesso')]")
                )
            )
            log.info("Confirmação inferida por texto.")
        except TimeoutException:
            log.warning("Não vi confirmação explícita; seguindo.")

    return "Ponto registrado."

# -----------------------------------------------------------------------------
# Entry point usado pelo bot do Discord
# -----------------------------------------------------------------------------
def run_rhid_punch(trigger: str = "manual", discord_user: Optional[dict] = None):
    """
    Retorna um dict pronto para o Discord:
      {"content": "", "embeds": [ ... ]}
    """
    driver: Optional[webdriver.Chrome] = None
    started_at = datetime.now(timezone.utc)

    try:
        driver = _build_driver()
        rhid_email = os.environ.get("RHID_EMAIL", "")
        rhid_senha = os.environ.get("RHID_PASSWORD", "")
        if not rhid_email or not rhid_senha:
            raise RuntimeError("Credenciais RHID não configuradas (RHID_EMAIL/RHID_PASSWORD).")

        _login(driver, rhid_email, rhid_senha)
        resultado = _registrar_ponto(driver)

        took = (datetime.now(timezone.utc) - started_at).total_seconds()

        # -------- dados para o embed --------
        hora = _now_human()
        loc_str, maps_link = _location_human()
        modo = "DRY RUN (simulação)" if PUNCH_DRY_RUN else "Produção"
        etapas = "login → dashboard → marcação → botão final habilitado ✔"

        color = 0x2ECC71  # verde sucesso

        fields = [
            {"name": "Horário", "value": hora, "inline": True},
            {"name": "Modo", "value": modo, "inline": True},
            {"name": "E-mail", "value": f"`{rhid_email}`", "inline": False},
            {
                "name": "Localização",
                "value": (f"{loc_str}" + (f" • [Abrir no Maps]({maps_link})" if maps_link else "")),
                "inline": False
            },
            {"name": "Etapas", "value": etapas, "inline": False},
            {"name": "Resultado", "value": resultado, "inline": False},
            {"name": "Duração", "value": f"{took:.1f}s", "inline": True},
        ]

        embed = {
            "title": "✅ RHID",
            "description": "Rotina de marcação de ponto",
            "color": color,
            "fields": fields,
            "footer": { "text": f"Trigger: {trigger}" },
            "timestamp": datetime.now(timezone.utc).isoformat()
        }

        return {"content": "", "embeds": [embed], "maps_url": maps_link}

    except TimeoutException as e:
        log.exception("Timeout no fluxo RHID")
        raise RuntimeError(f"Falha ao registrar ponto: Timeout ({e})") from e
    except Exception as e:
        log.exception("Falha no fluxo RHID")
        raise RuntimeError(f"Falha ao registrar ponto: {e}") from e
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
            tmp_dirs = getattr(driver, "_tmp_dirs", [])
            _cleanup_tmp_list(tmp_dirs)
