"""
Robô da Academia — Walton Family Whole Health & Fitness (Club Automation)
v1.1 — login mais robusto (campos #login/#password e botão #loginButton)

O que faz, a cada execução:
  1. Entra no portal com a conta do Jaime.
  2. Para cada aula configurada em config.json, abre a página da aula e olha
     as datas dentro do horizonte (horizon_days) no dia da semana e horário certos.
  3. Se a data está aberta ("Sign Up") ou já "Registered", abre a janela de
     inscrição e garante que todos os membros (Jaime e Ana Elisa) estejam inscritos.
  4. Para toda data em que os membros estão inscritos, garante que exista um
     evento no calendário "Academia" do iCloud, com a Ana Elisa convidada.
  5. Avisa no WhatsApp (CallMeBot) quando fizer uma reserva nova.

Variáveis de ambiente (GitHub Secrets):
  CA_USERNAME, CA_PASSWORD           login do portal
  ICLOUD_EMAIL, ICLOUD_APP_PASSWORD  Apple ID + senha de app (calendário)
  ANA_EMAIL                          e-mail da Ana Elisa para o convite
  WA_PHONE_JAIME, WA_APIKEY_JAIME    CallMeBot do Jaime
  WA_PHONE_ANA, WA_APIKEY_ANA        CallMeBot da Ana Elisa
Opcionais:
  DRY_RUN=true        só verifica e relata, não reserva nem cria eventos
  SKIP_CALENDAR=true  não mexe no calendário
  NOTIFY_ERRORS=true  envia erro pelo WhatsApp (o workflow liga isso nas execuções manuais)
"""

import json
import os
import re
import sys
import time
import traceback
import urllib.parse
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

ROOT = Path(__file__).parent
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
TZ = ZoneInfo(CFG["timezone"])
BASE = CFG["base_url"]
DEBUG_DIR = ROOT / "debug"
DEBUG_DIR.mkdir(exist_ok=True)

DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
SKIP_CALENDAR = os.getenv("SKIP_CALENDAR", "false").lower() == "true"
NOTIFY_ERRORS = os.getenv("NOTIFY_ERRORS", "false").lower() == "true"

WEEKDAYS_PT = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]
MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], start=1)}


def log(msg):
    print(f"[{datetime.now(TZ):%H:%M:%S}] {msg}", flush=True)


# ---------------------------------------------------------------- WhatsApp
def whatsapp(text, only_jaime=False):
    targets = [("JAIME", os.getenv("WA_PHONE_JAIME"), os.getenv("WA_APIKEY_JAIME"))]
    if not only_jaime:
        targets.append(("ANA", os.getenv("WA_PHONE_ANA"), os.getenv("WA_APIKEY_ANA")))
    for who, phone, key in targets:
        if not phone or not key:
            log(f"WhatsApp {who}: sem credenciais, pulando")
            continue
        url = ("https://api.callmebot.com/whatsapp.php?phone="
               + urllib.parse.quote(phone) + "&text=" + urllib.parse.quote(text)
               + "&apikey=" + urllib.parse.quote(key))
        try:
            r = requests.get(url, timeout=30)
            log(f"WhatsApp {who}: HTTP {r.status_code}")
        except Exception as e:  # noqa
            log(f"WhatsApp {who}: falhou ({e})")
        time.sleep(2)


# ---------------------------------------------------------------- helpers
def parse_row_date(text, today):
    """'Wednesday, October 28 04:30pm - 05:15pm ...' -> (date, '04:30pm', '05:15pm')"""
    m = re.search(r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+"
                  r"([A-Za-z]+)\s+(\d{1,2})\s+(\d{1,2}:\d{2}[ap]m)\s*-\s*(\d{1,2}:\d{2}[ap]m)", text)
    if not m:
        return None
    _, month, day, start, end = m.groups()
    mo, d = MONTHS[month], int(day)
    year = today.year
    candidate = date(year, mo, d)
    if candidate < today - timedelta(days=60):  # virada de ano
        candidate = date(year + 1, mo, d)
    return candidate, start.lower(), end.lower()


def to_dt(d, hhmm_ampm):
    t = datetime.strptime(hhmm_ampm, "%I:%M%p").time()
    return datetime.combine(d, t, tzinfo=TZ)


def fmt_pt(d, start):
    return f"{WEEKDAYS_PT[d.weekday()]} {d:%d/%m} {start}"


def shot(page, name):
    try:
        page.screenshot(path=str(DEBUG_DIR / f"{datetime.now():%H%M%S}-{name}.png"), full_page=True)
    except Exception:
        pass


def dismiss_popups(page):
    for sel in ['text=SNOOZE', 'text=Snooze', '.ui-dialog-titlebar-close:visible',
                'button:has-text("Close"):visible', '[aria-label="Close"]:visible']:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=800):
                loc.click(timeout=2000)
                time.sleep(0.5)
        except Exception:
            pass


# ---------------------------------------------------------------- portal
def login(page):
    log("Fazendo login…")
    page.goto(BASE + "/", wait_until="domcontentloaded")
    time.sleep(2)
    dismiss_popups(page)
    if page.locator("text=Log Out").first.is_visible(timeout=1500):
        log("Já logado")
        return
    page.wait_for_selector("#caSignInLoginForm #login", state="attached", timeout=20000)
    dismiss_popups(page)
    # preenche direto nos campos do formulário (funciona mesmo com aviso por cima)
    page.fill("#caSignInLoginForm #login", os.environ["CA_USERNAME"].strip(), force=True)
    page.fill("#caSignInLoginForm #password", os.environ["CA_PASSWORD"].strip(), force=True)
    dismiss_popups(page)
    try:
        page.click("#loginButton", timeout=10000)
    except Exception:
        page.evaluate("document.getElementById('loginButton').click()")
    try:
        page.wait_for_function(
            "() => /\\/member/.test(location.pathname) || /Log Out/.test(document.body.innerText)",
            timeout=40000)
    except PWTimeout:
        shot(page, "login-falhou")
        msg = page.evaluate("""() => [...document.querySelectorAll('.error, .alert, .message, [class*=error], [class*=Error]')]
            .filter(e => e.offsetParent !== null).map(e => e.innerText.trim()).filter(Boolean).join(' | ').slice(0, 300)""")
        log(f"URL após login: {page.url.split('?')[0]}")
        raise RuntimeError("Login não confirmou" + (f" — site disse: {msg}" if msg else " (sem mensagem de erro na tela)"))
    page.wait_for_load_state("domcontentloaded")
    dismiss_popups(page)
    log("Login OK")


def open_event(page, event_id):
    page.goto(f"{BASE}/calendar/event-info?id={event_id}", wait_until="domcontentloaded")
    page.wait_for_selector("button.register_button", timeout=30000)
    time.sleep(1)


def read_rows(page):
    return page.evaluate("""() => [...document.querySelectorAll('button.register_button')].map((b, i) => {
        const row = b.closest('.col-md-12') || b.parentElement;
        return {i, cls: b.className, label: b.innerText.trim(),
                text: (row ? row.innerText : '').replace(/\\s+/g, ' ')};
    })""")


def get_description(page):
    try:
        return page.evaluate("""() => {
            const el = [...document.querySelectorAll('div,p,span,td')].find(e =>
                e.children.length === 0 && e.offsetParent !== null &&
                e.innerText.trim().length > 40 && !/Registered|Open|Sign Up/.test(e.innerText) &&
                !e.closest('.register_button') && e.getBoundingClientRect().top < 900);
            return el ? el.innerText.trim() : '';
        }""") or ""
    except Exception:
        return ""


def ensure_members(page, button_index, label_for_log):
    """Abre a janela da data, inscreve os membros que faltam. Retorna (novos, todos_inscritos)."""
    btn = page.locator("button.register_button").nth(button_index)
    btn.scroll_into_view_if_needed()
    btn.click()
    page.wait_for_selector("div.select-member:visible", timeout=20000)
    time.sleep(1)

    newly = []
    for name in CFG["members"]:
        item = page.locator("div.select-member:visible").filter(
            has_text=re.compile(rf"^\s*{re.escape(name)}\b")).first
        if item.count() == 0:
            raise RuntimeError(f"Perfil '{name}' não apareceu na janela de inscrição")
        cls = item.get_attribute("class") or ""
        if "registered" in cls.split():
            log(f"  {name}: já inscrito")
            continue
        if DRY_RUN:
            log(f"  {name}: seria inscrito (DRY_RUN)")
            newly.append(name)
            continue
        item.click()
        page.wait_for_load_state("networkidle")
        time.sleep(1.5)
        cls_after = item.get_attribute("class") or ""
        if "unselected" in cls_after.split():
            item.click()  # tenta de novo uma vez
            page.wait_for_load_state("networkidle")
            time.sleep(1.5)
            cls_after = item.get_attribute("class") or ""
            if "unselected" in cls_after.split():
                shot(page, "selecao-falhou")
                raise RuntimeError(f"Não consegui selecionar '{name}' em {label_for_log}")
        newly.append(name)

    if not newly:
        page.locator("a:visible", has_text=re.compile(r"^\s*Cancel\s*$")).first.click()
        time.sleep(1)
        return [], True

    if DRY_RUN:
        page.locator("a:visible", has_text=re.compile(r"^\s*Cancel\s*$")).first.click()
        time.sleep(1)
        return newly, False

    page.locator("a.button-round:visible", has_text=re.compile(r"^\s*Register\s*$")).first.click()
    try:
        page.wait_for_selector("text=/registration is complete/i", timeout=30000)
    except PWTimeout:
        shot(page, "confirmacao-ausente")
        raise RuntimeError(f"Sem confirmação de inscrição em {label_for_log}")
    try:
        page.locator("a:visible, button:visible", has_text=re.compile(r"^\s*OK\s*$")).first.click(timeout=5000)
    except Exception:
        pass
    time.sleep(1)
    return newly, True


# ---------------------------------------------------------------- calendário
class Calendar:
    def __init__(self):
        import caldav
        self.caldav = caldav
        client = caldav.DAVClient(url="https://caldav.icloud.com",
                                  username=os.environ["ICLOUD_EMAIL"],
                                  password=os.environ["ICLOUD_APP_PASSWORD"])
        self.principal = client.principal()
        cals = [c for c in self.principal.calendars() if (c.name or "").strip() == CFG["calendar_name"]]
        if not cals:
            raise RuntimeError(f"Calendário '{CFG['calendar_name']}' não encontrado no iCloud")
        self.cal = cals[0]
        try:
            self.organizer = str(self.principal.get_vcal_address()).replace("MAILTO:", "mailto:")
        except Exception:
            self.organizer = "mailto:" + os.environ["ICLOUD_EMAIL"]
        log(f"Calendário '{CFG['calendar_name']}' conectado")

    def exists(self, uid, start, end, title):
        try:
            self.cal.event_by_uid(uid)
            return True
        except Exception:
            pass
        try:
            found = self.cal.search(start=start - timedelta(minutes=1), end=end + timedelta(minutes=1),
                                    event=True, expand=False)
            for ev in found:
                data = ev.data or ""
                base_name = title.split(" @ ")[0].split(" (")[0]
                if re.search(r"SUMMARY[^:]*:.*" + re.escape(base_name), data, re.I):
                    return True
        except Exception as e:
            log(f"  busca no calendário falhou ({e}); vou criar mesmo assim")
        return False

    def create(self, uid, start, end, title, description):
        def esc(s):
            return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")
        utc = ZoneInfo("UTC")
        stamp = datetime.now(utc).strftime("%Y%m%dT%H%M%SZ")
        s = start.astimezone(utc).strftime("%Y%m%dT%H%M%SZ")
        e = end.astimezone(utc).strftime("%Y%m%dT%H%M%SZ")
        desc = CFG["phone"] + ("\n\n" + description if description else "")
        ana = os.getenv("ANA_EMAIL", "").strip()
        lines = [
            "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//academia-bot//PT", "CALSCALE:GREGORIAN",
            "BEGIN:VEVENT",
            f"UID:{uid}", f"DTSTAMP:{stamp}", f"DTSTART:{s}", f"DTEND:{e}",
            f"SUMMARY:{esc(title)}",
            f"LOCATION:{esc(CFG['location'])}",
            f"DESCRIPTION:{esc(desc)}",
            "X-APPLE-TRAVEL-ADVISORY-BEHAVIOR:AUTOMATIC",
            f"ORGANIZER;CN=Jaime:{self.organizer}",
        ]
        if ana:
            lines.append(f"ATTENDEE;CN=Ana Elisa;CUTYPE=INDIVIDUAL;ROLE=REQ-PARTICIPANT;"
                         f"PARTSTAT=NEEDS-ACTION;RSVP=TRUE:mailto:{ana}")
        lines += [
            "BEGIN:VALARM", "ACTION:DISPLAY", "DESCRIPTION:Lembrete",
            f"TRIGGER:-PT{CFG['alert_minutes_before']}M", "END:VALARM",
            "END:VEVENT", "END:VCALENDAR",
        ]
        self.cal.save_event("\r\n".join(lines))


# ---------------------------------------------------------------- principal
def main():
    today = datetime.now(TZ).date()
    horizon = today + timedelta(days=CFG["horizon_days"])
    log(f"Início — DRY_RUN={DRY_RUN} — horizonte até {horizon:%d/%m/%Y}")

    booked, problems, to_calendar = [], [], []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1300, "height": 1000})
        page.set_default_timeout(30000)
        try:
            login(page)
            for c in CFG["classes"]:
                log(f"Aula: {c['name']} ({c['weekday']} {c['start']})")
                open_event(page, c["event_id"])
                description = get_description(page)
                rows = read_rows(page)
                targets = []
                for r in rows:
                    parsed = parse_row_date(r["text"], today)
                    if not parsed:
                        continue
                    d, start, end = parsed
                    if not r["text"].startswith(c["weekday"]) or start != c["start"].lower():
                        continue
                    if d < today or d > horizon:
                        continue
                    targets.append((d, start, end, r))

                for d, start, end, r in targets:
                    label = f"{c['name']} {fmt_pt(d, start)}"
                    status = r["label"]
                    log(f"  {fmt_pt(d, start)}: {status}")
                    if to_dt(d, start) <= datetime.now(TZ):
                        continue
                    if "register-button-now" in r["cls"] or "register-button-registered" in r["cls"]:
                        open_event(page, c["event_id"])  # recarrega para evitar estado velho
                        rows_now = read_rows(page)
                        idx = next((x["i"] for x in rows_now if x["text"].startswith(r["text"][:40])), None)
                        if idx is None:
                            problems.append(f"{label}: linha sumiu da página")
                            continue
                        try:
                            newly, all_in = ensure_members(page, idx, label)
                        except Exception as e:
                            problems.append(f"{label}: {e}")
                            shot(page, "erro-inscricao")
                            continue
                        if newly:
                            booked.append((label, newly))
                        if all_in or DRY_RUN:
                            to_calendar.append((c, d, start, end, description))
                    elif "register-button-waitlist" in r["cls"] or "full" in r["cls"].lower():
                        log("    lotada / lista de espera — nada a fazer")
                    else:
                        log("    ainda não abriu")
        except Exception as e:
            shot(page, "erro-geral")
            problems.append(f"Erro geral: {e}")
            traceback.print_exc()
        finally:
            browser.close()

    # ----- calendário
    created = []
    if to_calendar and not SKIP_CALENDAR:
        try:
            cal = None if DRY_RUN else Calendar()
            for c, d, start, end, description in to_calendar:
                title = f"{c['name']} @ Walmart"
                s, e = to_dt(d, start), to_dt(d, end)
                uid = f"academia-{c['event_id']}-{d:%Y%m%d}@academia-bot"
                if DRY_RUN:
                    log(f"  calendário (DRY_RUN): {title} {fmt_pt(d, start)}")
                    continue
                if cal.exists(uid, s, e, title):
                    log(f"  calendário: já existe {title} {fmt_pt(d, start)}")
                    continue
                cal.create(uid, s, e, title, description)
                created.append(f"{title} {fmt_pt(d, start)}")
                log(f"  calendário: criado {title} {fmt_pt(d, start)}")
        except Exception as e:
            problems.append(f"Calendário: {e}")
            traceback.print_exc()

    # ----- avisos
    if booked:
        lines = ["🏋️ ACADEMIA — reserva feita" + (" (teste)" if DRY_RUN else "")]
        for label, names in booked:
            lines.append(f"✅ {label} — {' e '.join(names)}")
        if created:
            lines.append("📅 Evento(s) no calendário Academia com convite para a Ana Elisa.")
        whatsapp("\n".join(lines))
    if problems:
        log("PROBLEMAS:\n  " + "\n  ".join(problems))
        now = datetime.now(TZ)
        # execução manual: avisa sempre; automática: no máximo 1 aviso por dia (1ª execução após 8h)
        if NOTIFY_ERRORS or (now.hour == 8 and now.minute < 15):
            whatsapp("⚠️ ACADEMIA — problema no robô:\n" + "\n".join(problems[:5]), only_jaime=True)

    log(f"Fim — reservas novas: {len(booked)}, eventos criados: {len(created)}, problemas: {len(problems)}")
    if problems and NOTIFY_ERRORS:
        sys.exit(1)  # só marca falha nas execuções manuais, para não lotar seu e-mail


if __name__ == "__main__":
    main()
