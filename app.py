
import json
import os
import random
import sqlite3
from datetime import date
from io import BytesIO
from typing import Dict, Any, List, Optional

import streamlit as st

# Supabase: python client (supabase)
try:
    from supabase import create_client
except Exception:
    create_client = None

# PDF Export (reportlab)
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.pdfgen import canvas


# -----------------------------
# Konfiguration
# -----------------------------
QUESTIONS_FILE = "questions.json"
SQLITE_FILE = "local_progress.sqlite3"


# -----------------------------
# Helpers: Laden / Identität
# -----------------------------
def load_questions(path: str) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError("questions.json muss eine LISTE von Fragen sein.")
    # Erwartetes Minimalformat:
    # {
    #   "id": "q1",
    #   "question": "…",
    #   "options": ["A", "B", "C", "D"],
    #   "answer_index": 2,
    #   "explanation": "…"
    # }
    return data


def qid(q: Dict[str, Any], idx_fallback: int) -> str:
    # stabile ID – wenn keine vorhanden ist, fallback
    return str(q.get("id") or f"idx_{idx_fallback}")


# -----------------------------
# SQLite Fallback (nur informativ)
# -----------------------------
def sqlite_init():
    con = sqlite3.connect(SQLITE_FILE)
    cur = con.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS progress (
            player TEXT,
            qid TEXT,
            status TEXT,
            selected_index INTEGER,
            updated_at TEXT,
            PRIMARY KEY (player, qid)
        )
        """
    )
    con.commit()
    con.close()


def sqlite_upsert(player: str, qid_: str, status: str, selected_index: Optional[int]):
    con = sqlite3.connect(SQLITE_FILE)
    cur = con.cursor()
    cur.execute(
        """
        INSERT INTO progress(player, qid, status, selected_index, updated_at)
        VALUES(?,?,?,?, datetime('now'))
        ON CONFLICT(player, qid) DO UPDATE SET
            status=excluded.status,
            selected_index=excluded.selected_index,
            updated_at=datetime('now')
        """,
        (player, qid_, status, selected_index if selected_index is not None else None),
    )
    con.commit()
    con.close()


# -----------------------------
# Supabase: Scores daily
# -----------------------------
def supabase_client():
    # Streamlit Secrets:
    # SUPABASE_URL
    # SUPABASE_SERVICE_KEY
    url = st.secrets.get("SUPABASE_URL", None)
    key = st.secrets.get("SUPABASE_SERVICE_KEY", None)
    if not url or not key or create_client is None:
        return None
    try:
        return create_client(url, key)
    except Exception:
        return None


def supabase_upsert_daily_score(player: str, score: int, total: int):
    sb = supabase_client()
    if sb is None:
        return False, "Supabase nicht konfiguriert/erreichbar."
    day = date.today().isoformat()
    try:
        # Tabelle: quiz_scores_daily
        # PK: (player, day)
        payload = {
            "player": player,
            "day": day,
            "score": score,
            "total": total,
        }
        # upsert (conflict target hängt vom Schema ab; Supabase python client upsert nutzt on_conflict optional)
        # Wir versuchen robust:
        res = sb.table("quiz_scores_daily").upsert(payload, on_conflict="player,day").execute()
        _ = res  # silence
        return True, "Score gespeichert."
    except Exception as e:
        return False, f"Supabase Fehler: {e}"


def supabase_fetch_leaderboard(limit: int = 20):
    sb = supabase_client()
    if sb is None:
        return None
    day = date.today().isoformat()
    try:
        res = (
            sb.table("quiz_scores_daily")
            .select("player,score,total,day")
            .eq("day", day)
            .order("score", desc=True)
            .limit(limit)
            .execute()
        )
        return res.data
    except Exception:
        return None


# -----------------------------
# Export: CSV / PDF
# -----------------------------
def build_export_rows(questions: List[Dict[str, Any]], answers: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = []
    for i, q in enumerate(questions):
        q_id = qid(q, i)
        a = answers.get(q_id)
        if not a:
            continue
        if a["status"] not in ("falsch", "unsicher", "nicht gewusst"):
            continue
        rows.append(
            {
                "qid": q_id,
                "question": q.get("question", ""),
                "your_status": a["status"],
                "your_answer": a.get("selected_text") or "",
                "correct_answer": q["options"][q["answer_index"]] if "answer_index" in q else "",
                "explanation": q.get("explanation", ""),
            }
        )
    return rows


def export_csv_bytes(rows: List[Dict[str, Any]]) -> bytes:
    # einfache CSV-Erzeugung ohne pandas
    import csv

    buf = BytesIO()
    # excel-friendly: utf-8-sig
    buf.write("\ufeff".encode("utf-8"))
    writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()) if rows else ["qid"])
    writer.writeheader()
    for r in rows:
        writer.writerow(r)
    return buf.getvalue()


def export_pdf_bytes(rows: List[Dict[str, Any]], title: str = "Falsche/Unsichere Fragen Export") -> bytes:
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4

    x = 2 * cm
    y = height - 2 * cm

    c.setFont("Helvetica-Bold", 14)
    c.drawString(x, y, title)
    y -= 1.0 * cm

    c.setFont("Helvetica", 10)
    c.drawString(x, y, f"Datum: {date.today().isoformat()}")
    y -= 1.0 * cm

    if not rows:
        c.drawString(x, y, "Keine falschen/unsicheren Fragen vorhanden. ✅")
        c.showPage()
        c.save()
        return buf.getvalue()

    def draw_wrapped(text: str, x0: float, y0: float, max_width: float, line_height: float) -> float:
        # sehr simples wrapping
        import textwrap

        # Näherung: 95 Zeichen ~ Zeilenbreite (je nach Font)
        wrapped = textwrap.wrap(text, width=100)
        y_curr = y0
        for line in wrapped:
            if y_curr < 2 * cm:
                c.showPage()
                c.setFont("Helvetica", 10)
                y_curr = height - 2 * cm
            c.drawString(x0, y_curr, line)
            y_curr -= line_height
        return y_curr

    for idx, r in enumerate(rows, start=1):
        if y < 3 * cm:
            c.showPage()
            y = height - 2 * cm

        c.setFont("Helvetica-Bold", 11)
        c.drawString(x, y, f"{idx}. {r['your_status'].upper()}")
        y -= 0.6 * cm

        c.setFont("Helvetica-Bold", 10)
        c.drawString(x, y, "Frage:")
        y -= 0.5 * cm
        c.setFont("Helvetica", 10)
        y = draw_wrapped(r["question"], x, y, width - 4 * cm, 0.45 * cm)
        y -= 0.2 * cm

        c.setFont("Helvetica-Bold", 10)
        c.drawString(x, y, f"Deine Antwort: {r['your_answer']}")
        y -= 0.5 * cm

        c.setFont("Helvetica-Bold", 10)
        c.drawString(x, y, f"Richtige Antwort: {r['correct_answer']}")
        y -= 0.6 * cm

        if r.get("explanation"):
            c.setFont("Helvetica-Bold", 10)
            c.drawString(x, y, "Erklärung:")
            y -= 0.5 * cm
            c.setFont("Helvetica", 10)
            y = draw_wrapped(r["explanation"], x, y, width - 4 * cm, 0.45 * cm)
            y -= 0.4 * cm

        # Trennlinie
        c.line(x, y, width - 2 * cm, y)
        y -= 0.6 * cm

    c.showPage()
    c.save()
    return buf.getvalue()


# -----------------------------
# Session-State Setup (KRITISCH für Bugfix)
# -----------------------------
def init_state():
    sqlite_init()

    if "player" not in st.session_state:
        st.session_state.player = ""

    # Antworten: qid -> dict(status, selected_index, selected_text, locked)
    if "answers" not in st.session_state:
        st.session_state.answers = {}

    if "q_idx" not in st.session_state:
        st.session_state.q_idx = 0

    # Stabiler Fragenpool + stabile Reihenfolge: NUR EINMAL mischen
    if "questions_order" not in st.session_state:
        qs = load_questions(QUESTIONS_FILE)
        random.shuffle(qs)
        st.session_state.questions_order = qs

    # Fokus-Mode
    if "focus_mode" not in st.session_state:
        st.session_state.focus_mode = False

    if "focus_ids" not in st.session_state:
        st.session_state.focus_ids = []

    # Erklärung-UI pro Frage (toggle)
    if "show_expl" not in st.session_state:
        st.session_state.show_expl = {}  # qid -> bool

    # Abschlussansicht
    if "finished" not in st.session_state:
        st.session_state.finished = False


def get_active_questions() -> List[Dict[str, Any]]:
    qs = st.session_state.questions_order
    if not st.session_state.focus_mode:
        return qs

    # Fokus: nur markierte IDs
    ids = set(st.session_state.focus_ids)
    filtered = []
    for i, q in enumerate(qs):
        if qid(q, i) in ids:
            filtered.append(q)
    return filtered


def current_question() -> Optional[Dict[str, Any]]:
    qs = get_active_questions()
    if st.session_state.q_idx < 0:
        st.session_state.q_idx = 0
    if st.session_state.q_idx >= len(qs):
        return None
    return qs[st.session_state.q_idx]


def mark_for_focus(q_id: str, status: str):
    if status in ("falsch", "unsicher", "nicht gewusst"):
        if q_id not in st.session_state.focus_ids:
            st.session_state.focus_ids.append(q_id)


def compute_score_all() -> (int, int):
    # Score nur über "richtig"
    qs = st.session_state.questions_order
    total = len(qs)
    correct = 0
    for i, q in enumerate(qs):
        q_id = qid(q, i)
        a = st.session_state.answers.get(q_id)
        if a and a.get("status") == "richtig":
            correct += 1
    return correct, total


def reset_all_questions_new_shuffle():
    qs = load_questions(QUESTIONS_FILE)
    random.shuffle(qs)
    st.session_state.questions_order = qs

    st.session_state.answers = {}
    st.session_state.q_idx = 0
    st.session_state.focus_mode = False
    st.session_state.focus_ids = []
    st.session_state.show_expl = {}
    st.session_state.finished = False


def start_focus_mode_from_marked():
    st.session_state.focus_mode = True
    st.session_state.q_idx = 0
    st.session_state.finished = False


def exit_focus_mode():
    st.session_state.focus_mode = False
    st.session_state.q_idx = 0
    st.session_state.finished = False


# -----------------------------
# UI
# -----------------------------
st.set_page_config(page_title="Lernquiz", layout="centered")
init_state()

st.title("📘 Lernquiz")

with st.sidebar:
    st.subheader("Spieler")
    st.session_state.player = st.text_input("Name", value=st.session_state.player, placeholder="z.B. Max")

    st.divider()
    st.subheader("Modus")
    if not st.session_state.focus_mode:
        st.caption("Normalmodus: alle Fragen")
        if st.button("🎯 Fokus-Modus: nur falsch/unsicher üben", use_container_width=True):
            # Fokus-IDs müssen existieren, sonst macht’s keinen Sinn – aber wir lassen es zu, endet dann schnell.
            start_focus_mode_from_marked()
            st.rerun()
    else:
        st.caption("Fokus-Modus aktiv")
        if st.button("↩️ Zurück zum Normalmodus", use_container_width=True):
            exit_focus_mode()
            st.rerun()

    st.divider()
    st.subheader("Rangliste (heute)")
    lb = supabase_fetch_leaderboard(limit=15)
    if lb:
        for row in lb:
            st.write(f"**{row['player']}**: {row['score']}/{row['total']}")
    else:
        st.caption("Keine Rangliste (Supabase nicht aktiv oder keine Daten).")

    st.divider()
    if st.button("🔄 Alles neu starten (neu mischen)", use_container_width=True):
        reset_all_questions_new_shuffle()
        st.rerun()


# -----------------------------
# Abschlussansicht
# -----------------------------
qs_active = get_active_questions()
q = current_question()

if q is None:
    # Ende des aktuellen Modus erreicht
    st.session_state.finished = True

if st.session_state.finished:
    st.success("Wow, du bist durch ✅")

    # Score speichern (nur wenn Playername gesetzt)
    score, total = compute_score_all()
    if st.session_state.player.strip():
        ok, msg = supabase_upsert_daily_score(st.session_state.player.strip(), score, total)
        st.caption(msg if ok else msg)

    st.write(f"Dein Score (gesamt): **{score}/{total}**")

    # Export falsch/unsicher/nicht gewusst
    export_rows = build_export_rows(st.session_state.questions_order, st.session_state.answers)

    colA, colB = st.columns(2)
    with colA:
        if st.button("🎯 Nur falsche/unsichere Fragen üben", use_container_width=True):
            # Fokus-IDs aus allen Fragen generieren (falls noch nicht vollständig)
            st.session_state.focus_ids = [r["qid"] for r in export_rows]
            start_focus_mode_from_marked()
            st.rerun()
    with colB:
        if st.button("🔁 Alle Fragen von vorne (neu gemischt)", use_container_width=True):
            reset_all_questions_new_shuffle()
            st.rerun()

    st.divider()
    st.subheader("Export (falsch/unsicher/nicht gewusst)")

    if export_rows:
        csv_bytes = export_csv_bytes(export_rows)
        st.download_button(
            "⬇️ CSV herunterladen",
            data=csv_bytes,
            file_name="lernquiz_export.csv",
            mime="text/csv",
            use_container_width=True,
        )

        pdf_bytes = export_pdf_bytes(export_rows, title="Lernquiz – Falsche/Unsichere Fragen")
        st.download_button(
            "⬇️ PDF herunterladen",
            data=pdf_bytes,
            file_name="lernquiz_export.pdf",
            mime="application/pdf",
            use_container_width=True,
        )
    else:
        st.info("Es gibt aktuell nichts zu exportieren.")

    st.stop()


# -----------------------------
# Quiz-Ansicht
# -----------------------------
# current question id
# Wichtig: qid muss zur ORIGINAL-Reihenfolge stabil sein.
# Wir erzeugen qid anhand des Original-Questions-Orders Index:
# Dazu suchen wir q im originalen Pool, damit idx_fallback stabil bleibt.
orig_list = st.session_state.questions_order
orig_index = None
for i, qq in enumerate(orig_list):
    if qq is q:  # gleiche Objektinstanz
        orig_index = i
        break
if orig_index is None:
    # Fallback: per content match (notfalls)
    for i, qq in enumerate(orig_list):
        if qq.get("question") == q.get("question"):
            orig_index = i
            break
if orig_index is None:
    orig_index = 0

q_id = qid(q, orig_index)

# progress
st.caption(
    f"Modus: **{'Fokus' if st.session_state.focus_mode else 'Normal'}**  |  "
    f"Frage **{st.session_state.q_idx + 1}/{len(qs_active)}**"
)

st.subheader(q.get("question", ""))

options = q.get("options", [])
correct_index = q.get("answer_index", None)
explanation = q.get("explanation", "")

# Antwortstatus aus State
a = st.session_state.answers.get(q_id)
locked = bool(a.get("locked")) if a else False
selected_index = a.get("selected_index") if a else None
status = a.get("status") if a else None

# Anzeige der Optionen (als Radio), aber gesperrt, wenn schon beantwortet (locked)
# Streamlit radio braucht einen key, der pro Frage einzigartig ist, damit selection stabil bleibt.
radio_key = f"radio_{q_id}"

# Initialwert für Radio:
# Wenn schon beantwortet: die gewählte Option
# sonst: None -> wir nutzen index=0 nicht, sondern erzwingen keine Vorauswahl über workaround
# Streamlit radio erlaubt kein None-index sauber; wir bieten deshalb zusätzlich Buttons an.
st.write("Wähle eine Antwort:")

# Darstellung als Buttons (stabil, besser steuerbar)
cols = st.columns(2) if len(options) <= 6 else st.columns(1)

# wir zeigen Markierung ✅/❌ neben den Optionen, wenn beantwortet
def option_label(i: int, text: str) -> str:
    if not a:
        return text
    # Wenn beantwortet: markiere gewählte + richtige
    if correct_index is not None and i == correct_index:
        return f"{text} ✅"
    if selected_index is not None and i == selected_index and status != "richtig":
        return f"{text} ❌"
    return text


# Antwort-Handler (KRITISCH: hier darf KEIN q_idx=0 passieren!)
def submit_answer(chosen_index: int, mode_status: str):
    # mode_status: "richtig"/"falsch"/"unsicher"
    # Sperren, damit beim Zurückgehen keine neue Wertung entsteht
    chosen_text = options[chosen_index] if 0 <= chosen_index < len(options) else ""
    st.session_state.answers[q_id] = {
        "status": mode_status,
        "selected_index": chosen_index,
        "selected_text": chosen_text,
        "locked": True,
    }
    sqlite_upsert(st.session_state.player.strip() or "anon", q_id, mode_status, chosen_index)
    mark_for_focus(q_id, mode_status)

    # ✅ Bugfix: NICHT auf 0 setzen, sondern sauber weiter
    st.session_state.q_idx = min(st.session_state.q_idx + 1, len(get_active_questions()))
    st.rerun()


def submit_dont_know():
    st.session_state.answers[q_id] = {
        "status": "nicht gewusst",
        "selected_index": None,
        "selected_text": "Ich weiß nicht",
        "locked": True,
    }
    sqlite_upsert(st.session_state.player.strip() or "anon", q_id, "nicht gewusst", None)
    mark_for_focus(q_id, "nicht gewusst")

    # ✅ Bugfix: sauber weiter, kein Reset
    st.session_state.q_idx = min(st.session_state.q_idx + 1, len(get_active_questions()))
    st.rerun()


# Optionen + Buttons
for i, opt in enumerate(options):
    # pro option eine Zeile: Button
    # Wenn locked: Buttons deaktivieren
    if st.button(option_label(i, opt), disabled=locked, key=f"opt_{q_id}_{i}", use_container_width=True):
        # wenn unsicher-Mode: user kann "unsicher" separat wählen
        # Standard: wir prüfen richtig/falsch
        if correct_index is None:
            submit_answer(i, "richtig")  # falls keine korrekte Lösung hinterlegt
        else:
            submit_answer(i, "richtig" if i == correct_index else "falsch")

st.divider()

# Extra-Buttons: "unsicher" nur, wenn noch nicht locked und eine Option gewählt werden soll:
# Da wir via Buttons wählen, ist "unsicher" am besten als eigener Modus:
# -> User klickt erst Option? Das wäre zwei Schritte.
# Wir machen es einfacher: "Ich bin mir nicht sicher" markiert die Frage als unsicher
# ohne Antwortwertung (oder mit der zuletzt gewählten?). Da wir keinen Radiowert haben,
# erlauben wir "unsicher" als Status ohne konkrete Option.
col1, col2, col3 = st.columns(3)

with col1:
    if st.button("🤷 Ich weiß nicht", disabled=locked, use_container_width=True):
        submit_dont_know()

with col2:
    if st.button("😬 Ich bin mir nicht sicher", disabled=locked, use_container_width=True):
        # Status unsicher (ohne selected_index)
        st.session_state.answers[q_id] = {
            "status": "unsicher",
            "selected_index": None,
            "selected_text": "unsicher",
            "locked": True,
        }
        sqlite_upsert(st.session_state.player.strip() or "anon", q_id, "unsicher", None)
        mark_for_focus(q_id, "unsicher")
        st.session_state.q_idx = min(st.session_state.q_idx + 1, len(get_active_questions()))
        st.rerun()

with col3:
    # Erklärung jederzeit einblendbar (auch wenn locked)
    show = st.session_state.show_expl.get(q_id, False)
    label = "📌 Erklärung ausblenden" if show else "📌 Erklärung anzeigen"
    if st.button(label, use_container_width=True):
        st.session_state.show_expl[q_id] = not show
        st.rerun()

if st.session_state.show_expl.get(q_id, False) and explanation:
    st.info(explanation)

# Navigation unten: Zurück / Weiter
nav1, nav2 = st.columns(2)

with nav1:
    if st.button("⬅️ Zurück", disabled=(st.session_state.q_idx <= 0), use_container_width=True):
        # Beim Zurückgehen: keine Neubewertung, weil locked True bleibt.
        st.session_state.q_idx = max(0, st.session_state.q_idx - 1)
        st.rerun()

with nav2:
    if st.button("➡️ Weiter", disabled=(st.session_state.q_idx >= len(qs_active) - 1), use_container_width=True):
        st.session_state.q_idx = min(st.session_state.q_idx + 1, len(qs_active) - 1)
        st.rerun()
