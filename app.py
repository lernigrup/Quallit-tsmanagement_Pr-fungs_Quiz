
import json
import os
from pathlib import Path
from datetime import date, datetime
import random
import hashlib
import streamlit as st
import sqlite3
import csv
import io

try:
    from reportlab.lib.pagesizes import A4  # type: ignore
    from reportlab.pdfgen import canvas  # type: ignore
except Exception:
    A4 = None
    canvas = None

try:
    from supabase import create_client  # type: ignore
except Exception:
    create_client = None

BASE_DIR = Path(__file__).parent
QUESTIONS_FILE = BASE_DIR / "questions.json"
CUSTOM_FILE = BASE_DIR / "custom_questions.json"
PROGRESS_DIR = BASE_DIR / "progress"
PROGRESS_DIR.mkdir(exist_ok=True)

"""Leaderboards

If you deploy the app (e.g., Streamlit Community Cloud) and want a shared leaderboard
between friends, configure Supabase credentials in Streamlit secrets.

Fallback: local SQLite leaderboard (works only for users on the same machine).
"""

LEADERBOARD_DB = BASE_DIR / "leaderboard.db"  # local fallback

LB_TABLE = "quiz_scores_daily"  # Supabase table name

def supabase_client():
    """Create a Supabase client if secrets are configured, else return None."""
    if create_client is None:
        return None
    try:
        url = st.secrets.get("SUPABASE_URL", "").strip()
        key = st.secrets.get("SUPABASE_SERVICE_KEY", "").strip()
    except Exception:
        return None
    if not url or not key:
        return None
    return create_client(url, key)

def lb_connect():
    """Local fallback DB for leaderboard when Supabase isn't configured."""
    conn = sqlite3.connect(str(LEADERBOARD_DB))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS scores_daily (
          player TEXT NOT NULL,
          day TEXT NOT NULL,
          correct INTEGER NOT NULL DEFAULT 0,
          wrong INTEGER NOT NULL DEFAULT 0,
          skipped INTEGER NOT NULL DEFAULT 0,
          updated_at TEXT NOT NULL,
          PRIMARY KEY (player, day)
        )
        """
    )
    return conn

def lb_upsert_daily(player: str, day: str, delta_correct=0, delta_wrong=0, delta_skipped=0):
    """Upsert daily score (shared via Supabase if configured; else local sqlite)."""
    player = player.strip()
    if not player:
        return
    now = datetime.now().isoformat(timespec="seconds")

    sb = supabase_client()
    if sb is not None:
        # Upsert into Supabase table quiz_scores_daily with PK (player, day)
        payload = {
            "player": player,
            "day": day,
            "correct": int(delta_correct),
            "wrong": int(delta_wrong),
            "skipped": int(delta_skipped),
            "updated_at": now,
        }
        # We need to add deltas, not overwrite. Fetch row first.
        try:
            existing = sb.table(LB_TABLE).select("correct,wrong,skipped").eq("player", player).eq("day", day).execute()
            rows = getattr(existing, "data", None) or []
            if rows:
                c = int(rows[0].get("correct", 0)) + int(delta_correct)
                w = int(rows[0].get("wrong", 0)) + int(delta_wrong)
                s = int(rows[0].get("skipped", 0)) + int(delta_skipped)
                sb.table(LB_TABLE).update({"correct": c, "wrong": w, "skipped": s, "updated_at": now}).eq("player", player).eq("day", day).execute()
            else:
                sb.table(LB_TABLE).insert(payload).execute()
        except Exception:
            # If anything goes wrong, fail silently (quiz should still work)
            return
        return

    # Local fallback
    conn = lb_connect()
    cur = conn.cursor()
    cur.execute("SELECT correct, wrong, skipped FROM scores_daily WHERE player=? AND day=?", (player, day))
    row = cur.fetchone()
    if row:
        c, w, s = row
        c += int(delta_correct)
        w += int(delta_wrong)
        s += int(delta_skipped)
        cur.execute(
            "UPDATE scores_daily SET correct=?, wrong=?, skipped=?, updated_at=? WHERE player=? AND day=?",
            (c, w, s, now, player, day),
        )
    else:
        cur.execute(
            "INSERT INTO scores_daily(player, day, correct, wrong, skipped, updated_at) VALUES (?,?,?,?,?,?)",
            (player, day, int(delta_correct), int(delta_wrong), int(delta_skipped), now),
        )
    conn.commit()
    conn.close()


def lb_get_leaderboards(day: str, n: int = 20):
    """Return (today_rows, total_rows).

    Each row: {player, correct, wrong, skipped, updated_at}
    """
    sb = supabase_client()
    if sb is not None:
        try:
            # Today
            today_resp = sb.table(LB_TABLE).select("player,correct,wrong,skipped,updated_at").eq("day", day).execute()
            today = getattr(today_resp, "data", None) or []
            today.sort(key=lambda r: (-int(r.get("correct", 0)), int(r.get("wrong", 0)), int(r.get("skipped", 0))))
            today = today[: int(n)]

            # Total: fetch all and aggregate per player (simple & fine for small groups)
            all_resp = sb.table(LB_TABLE).select("player,correct,wrong,skipped").execute()
            rows = getattr(all_resp, "data", None) or []
            agg = {}
            for r in rows:
                p = r.get("player")
                if not p:
                    continue
                a = agg.setdefault(p, {"player": p, "correct": 0, "wrong": 0, "skipped": 0, "updated_at": ""})
                a["correct"] += int(r.get("correct", 0))
                a["wrong"] += int(r.get("wrong", 0))
                a["skipped"] += int(r.get("skipped", 0))
            total = list(agg.values())
            total.sort(key=lambda r: (-r["correct"], r["wrong"], r["skipped"]))
            total = total[: int(n)]
            return today, total
        except Exception:
            return [], []

    # Local fallback
    conn = lb_connect()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT player, correct, wrong, skipped, updated_at
        FROM scores_daily
        WHERE day=?
        ORDER BY correct DESC, wrong ASC, skipped ASC
        LIMIT ?
        """,
        (day, int(n)),
    )
    today = [
        {"player": r[0], "correct": r[1], "wrong": r[2], "skipped": r[3], "updated_at": r[4]}
        for r in cur.fetchall()
    ]

    cur.execute(
        """
        SELECT player,
               SUM(correct) AS correct,
               SUM(wrong) AS wrong,
               SUM(skipped) AS skipped
        FROM scores_daily
        GROUP BY player
        ORDER BY correct DESC, wrong ASC, skipped ASC
        LIMIT ?
        """,
        (int(n),),
    )
    total = [
        {"player": r[0], "correct": r[1], "wrong": r[2], "skipped": r[3], "updated_at": ""}
        for r in cur.fetchall()
    ]
    conn.close()
    return today, total

def lb_top_total(n=20):
    """Return Top N by total correct (sum over all days)."""
    sb = supabase_client()
    if sb is not None:
        try:
            res = sb.table(LB_TABLE).select("player,correct,wrong,skipped").execute()
            rows = getattr(res, "data", None) or []
            agg = {}
            for r in rows:
                p = r.get("player")
                if not p:
                    continue
                a = agg.setdefault(p, {"player": p, "correct": 0, "wrong": 0, "skipped": 0})
                a["correct"] += int(r.get("correct", 0))
                a["wrong"] += int(r.get("wrong", 0))
                a["skipped"] += int(r.get("skipped", 0))
            out = list(agg.values())
            out.sort(key=lambda x: (-x["correct"], x["wrong"], x["skipped"], x["player"].lower()))
            return out[: int(n)]
        except Exception:
            return []

    conn = lb_connect()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT player,
               SUM(correct) AS correct,
               SUM(wrong)   AS wrong,
               SUM(skipped) AS skipped,
               MAX(updated_at) AS updated_at
        FROM scores_daily
        GROUP BY player
        ORDER BY correct DESC, wrong ASC, skipped ASC
        LIMIT ?
        """,
        (int(n),),
    )
    rows = cur.fetchall()
    conn.close()
    return [
        {"player": r[0], "correct": r[1], "wrong": r[2], "skipped": r[3], "updated_at": r[4]}
        for r in rows
    ]

def lb_top_today(day: str, n=20):
    """Return Top N for a specific day."""
    sb = supabase_client()
    if sb is not None:
        try:
            res = sb.table(LB_TABLE).select("player,correct,wrong,skipped,updated_at").eq("day", day).execute()
            rows = getattr(res, "data", None) or []
            rows.sort(key=lambda r: (-int(r.get("correct", 0)), int(r.get("wrong", 0)), int(r.get("skipped", 0)), (r.get("player") or "").lower()))
            return rows[: int(n)]
        except Exception:
            return []

    conn = lb_connect()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT player, correct, wrong, skipped, updated_at
        FROM scores_daily
        WHERE day=?
        ORDER BY correct DESC, wrong ASC, skipped ASC
        LIMIT ?
        """,
        (day, int(n)),
    )
    rows = cur.fetchall()
    conn.close()
    return [
        {"player": r[0], "correct": r[1], "wrong": r[2], "skipped": r[3], "updated_at": r[4]}
        for r in rows
    ]

def safe_explanation(q: dict) -> str:
    """Return a user-friendly explanation. If none exists, provide a placeholder."""
    exp = (q.get("explanation") or "").strip()
    if exp:
        return exp
    # Placeholder: still useful, but makes it explicit the explanation is missing.
    if q.get("type") == "mc":
        correct_idxs = q.get("correct", [])
        opts = q.get("options", [])
        if correct_idxs and opts:
            labels = []
            for i in correct_idxs:
                if 0 <= i < len(opts):
                    labels.append(opts[i])
            if labels:
                return "Noch keine ausführliche Erklärung hinterlegt. Richtige Antwort(en): " + "; ".join(labels)
    sol = (q.get("solution") or "").strip()
    if sol:
        return "Noch keine ausführliche Erklärung hinterlegt. Lösungsvorschlag: " + sol
    return "Noch keine Erklärung hinterlegt. (Du kannst unten im Bereich „Neue Frage hinzufügen“ eine Erklärung ergänzen.)"

def load_json(path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default

def save_json(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")

def load_questions():
    base = load_json(QUESTIONS_FILE, [])
    custom = load_json(CUSTOM_FILE, [])
    # custom questions get ids after base max
    if custom:
        max_id = max(q["id"] for q in base) if base else 0
        normalized = []
        for i,q in enumerate(custom, start=1):
            q2 = dict(q)
            if "id" not in q2:
                q2["id"] = max_id + i
            normalized.append(q2)
        custom = normalized
    return base + custom

def player_file(player: str) -> Path:
    safe = "".join(ch for ch in player.strip() if ch.isalnum() or ch in ("-","_")).strip()
    if not safe:
        safe = "player"
    return PROGRESS_DIR / f"{safe.lower()}.json"

def load_player_state(player: str):
    path = player_file(player)
    return load_json(path, {
        "player": player,
        "cursor": 0,        # position within today's shuffled order
        "order_date": "",   # YYYY-MM-DD
        "order": [],        # list of question IDs in the order shown today
        "answered": {},      # qid -> {"ts": iso, "correct": bool, "selected": ...}
        "daily": {},         # "YYYY-MM-DD" -> {"correct": int, "wrong": int, "skipped": int, "total": int}
    })


def deterministic_shuffle(player: str, day: str, items: list[int]) -> list[int]:
    """Stable shuffle per player+day, so you get variety but can continue where you stopped."""
    seed_bytes = hashlib.sha256((player + "|" + day).encode("utf-8")).digest()
    seed = int.from_bytes(seed_bytes[:8], "big", signed=False)
    rng = random.Random(seed)
    out = list(items)
    rng.shuffle(out)
    return out


def ensure_daily_order(state: dict, player: str, questions: list[dict]):
    """Ensure state has a shuffled order for today.

    IMPORTANT: state['order'] may contain duplicates (spaced repetition).
    We must NOT force len(order) == len(ids) or set(order) == ids_set.
    """
    today = str(date.today())
    ids = [int(q["id"]) for q in questions]
    ids_set = set(ids)

    # A nonce lets us restart "from the beginning" with a different shuffle on the same day.
    nonce = int(state.get("shuffle_nonce", 0) or 0)
    mix_key = f"{today}#{nonce}"

    order = state.get("order") or []
    order_date = state.get("order_date") or ""

    # Regenerate if new day / missing order
    if order_date != mix_key or not order:
        state["order_date"] = mix_key
        state["order"] = deterministic_shuffle(player, mix_key, ids)
        state["cursor"] = 0
        return

    # Normalize stored order to ints
    try:
        order_ints = [int(x) for x in order]
    except Exception:
        order_ints = []

    # If order contains IDs that no longer exist -> regenerate
    if not set(order_ints).issubset(ids_set):
        state["order_date"] = mix_key
        state["order"] = deterministic_shuffle(player, mix_key, ids)
        state["cursor"] = 0
        return

    # If new questions were added since last time, append them (keep existing order!)
    order_set = set(order_ints)
    missing = [i for i in ids if i not in order_set]
    if missing:
        missing_shuffled = deterministic_shuffle(player, mix_key + "|new", missing)
        order_ints = order_ints + missing_shuffled
        state["order"] = order_ints
    else:
        state["order"] = order_ints

    # Keep cursor in range. NOTE: order can be longer than ids because of repeats.
    state["cursor"] = max(0, min(int(state.get("cursor", 0)), len(state["order"])))

def bump_daily(state, correct=None, skipped=False, unsure=False):
    key = str(date.today())
    d = state["daily"].setdefault(key, {"correct": 0, "wrong": 0, "skipped": 0, "unsure": 0, "total": 0})
    d["total"] += 1
    if skipped:
        d["skipped"] += 1
    else:
        if correct is True:
            d["correct"] += 1
        else:
            d["wrong"] += 1
        if unsure:
            d["unsure"] += 1

def format_daily(state):
    key = str(date.today())
    d = state["daily"].get(key, {"correct": 0, "wrong": 0, "skipped": 0, "unsure": 0, "total": 0})
    return d


def is_correct_mc(q, selected):
    correct = set(q.get("correct", []))
    if q.get("answerType", "single") == "multi":
        return set(selected) == correct
    return len(selected) == 1 and selected[0] in correct

HAS_DIALOG = hasattr(st, "dialog")

st.set_page_config(page_title="Lern-Quiz", layout="centered")

st.title("📚 Lern-Quiz (aus deinem Lernzettel)")
st.caption("Speichert deinen Fortschritt pro Spielername lokal im Ordner „quiz_app/progress“.")

questions = load_questions()
if not questions:
    st.error("Keine Fragen gefunden. questions.json fehlt oder ist leer.")
    st.stop()

with st.sidebar:
    st.subheader("Spieler")
    player = st.text_input("Spielername", value=st.session_state.get("player",""))
    if player:
        st.session_state["player"] = player
        state = load_player_state(player)
        # Ensure a deterministic shuffled order for today.
        ensure_daily_order(state, player, questions)
        save_json(player_file(player), state)
    else:
        st.info("Gib einen Spielernamen ein, damit Fortschritt gespeichert werden kann.")
        st.stop()

    d = format_daily(state)
    st.markdown("### Heute")
    st.write(f"✅ richtig: **{d['correct']}**")
    st.write(f"❌ falsch: **{d['wrong']}**")
    st.write(f"🟡 unsicher: **{d.get('unsure',0)}**")
    st.write(f"🤷 nicht gewusst: **{d['skipped']}**")
    st.write(f"🧮 gesamt: **{d['total']}**")
    st.divider()

    show_lb = st.toggle("🏁 Vergleich / Leaderboard", value=False)
    if show_lb:
        st.caption("Für Freunde-Vergleich: deployen + Supabase einrichten. Lokal vergleicht es nur auf diesem PC.")
        today_rows, total_rows = lb_get_leaderboards(str(date.today()), n=20)
        st.write("**Heute (Top 20)**")
        if today_rows:
            st.dataframe(
                [{"Spieler": r["player"], "✅": r["correct"], "❌": r["wrong"], "🤷": r["skipped"], "Update": r.get("updated_at","")} for r in today_rows],
                hide_index=True,
                use_container_width=True,
            )
        else:
            st.info("Noch keine Einträge für heute.")

        st.write("**Gesamt (Top 20)**")
        if total_rows:
            st.dataframe(
                [{"Spieler": r["player"], "✅": r["correct"], "❌": r["wrong"], "🤷": r["skipped"], "Update": r.get("updated_at","")} for r in total_rows],
                hide_index=True,
                use_container_width=True,
            )
        else:
            st.info("Noch keine Gesamteinträge.")

    st.divider()
    if st.button("Fortschritt zurücksetzen (nur Cursor)"):
        state["cursor"] = 0
        save_json(player_file(player), state)
        st.success("Cursor zurückgesetzt. (Antwort-Historie bleibt erhalten.)")
    if st.button("Alles zurücksetzen (Cursor + Historie)"):
        state = {
            "player": player, "cursor": 0, "order_date": "", "order": [], "answered": {}, "daily": {}
        }
        save_json(player_file(player), state)
        st.success("Alles zurückgesetzt.")

order = state.get("order") or [int(q["id"]) for q in questions]
cursor_pos = int(state.get("cursor", 0))
# cursor may be == len(order) to indicate "finished"
cursor_pos = max(0, min(cursor_pos, len(order)))

# --- Focus mode: practice only wrong/unsure/skipped from what you've answered so far ---
if "mode" not in state:
    state["mode"] = "normal"  # normal | focus_wrong

def compute_focus_list():
    """IDs to practice again (wrong OR skipped OR unsure) excluding those already mastered."""
    out = []
    for qid0 in state.get("order") or []:
        a = state.get("answered", {}).get(str(qid0)) or {}
        if a.get("mastered") is True:
            continue
        if a.get("skipped") is True or a.get("correct") is False or a.get("unsure") is True:
            out.append(int(qid0))
    # Also include any items scheduled later in the order (spaced repetition) that match the criteria
    # while keeping uniqueness and preserving order.
    seen = set()
    uniq = []
    for i in out:
        if i not in seen:
            uniq.append(i)
            seen.add(i)
    return uniq

# One bottom button to jump into focus practice from the current progress.
focus_candidates = compute_focus_list()
if state.get("mode") == "normal" and focus_candidates:
    if st.button("🎯 Ab jetzt nur Falsche / 'Ich weiß nicht' / Unsichere üben", use_container_width=True):
        state["mode"] = "focus_wrong"
        state["resume_cursor"] = int(state.get("cursor", 0))
        state["focus_order"] = focus_candidates
        state["focus_cursor"] = 0
        # IMPORTANT: In focus mode, questions must be answerable again.
        # We therefore track focus-run answers separately (do NOT reuse master answered-map).
        state["focus_answered"] = {}
        save_json(player_file(player), state)
        st.rerun()

# If we are in focus mode, override the effective order/cursor for the UI.
if state.get("mode") == "focus_wrong":
    order = state.get("focus_order") or []
    cursor_pos = int(state.get("focus_cursor", 0))
    cursor_pos = max(0, min(cursor_pos, len(order)))

# Which answered-map is active?
# - normal: state['answered'] (master progress)
# - wrong-only practice (end-screen button): state['practice_answered']
# - focus mode (bottom focus button): state['focus_answered']
active_answered = state.get('answered', {})
if state.get('mode') == 'focus_wrong':
    active_answered = state.setdefault('focus_answered', {})
elif state.get('practice_mode') == 'wrong_only':
    active_answered = state.setdefault('practice_answered', {})

# Optional: nur unbeantwortete Fragen üben
# Wichtig: Suche NICHT immer ab Frage 1, sondern ab der aktuellen Position weiter,
# damit du nach "Ich weiß nicht" / "unsicher" nicht wieder nach vorne springst.
only_unanswered = False
if state.get("mode") != "focus_wrong":
    only_unanswered = st.toggle("Nur unbeantwortete Fragen", value=False)
    if only_unanswered:
        start = min(cursor_pos, max(len(order) - 1, 0))
        found = None
        # 1) Vorwärts ab aktueller Position
        for i in range(start, len(order)):
            if str(order[i]) not in active_answered:
                found = i
                break
        # 2) Wenn keine mehr vorne, dann wrap zum Anfang
        if found is None:
            for i in range(0, start):
                if str(order[i]) not in active_answered:
                    found = i
                    break
        if found is not None:
            cursor_pos = found
        else:
            st.success("Du hast alle Fragen einmal beantwortet 🎉")

# Finished screen: show overview and next actions
all_answered = all(str(qid0) in active_answered for qid0 in order)

# Special finish screen for focus mode
if state.get("mode") == "focus_wrong" and (cursor_pos >= len(order) or len(order) == 0):
    st.success("🎯 Fokus-Übung abgeschlossen! Du hast alle aktuell falschen/unsicheren/\"Ich weiß nicht\" Fragen bearbeitet.")
    col1, col2 = st.columns(2)
    with col1:
        if st.button("➡️ Normal weiter machen", use_container_width=True):
            state["mode"] = "normal"
            state["cursor"] = int(state.get("resume_cursor", state.get("cursor", 0)) or 0)
            state.pop("focus_order", None)
            state.pop("focus_cursor", None)
            state.pop("resume_cursor", None)
            state.pop("focus_answered", None)
            save_json(player_file(player), state)
            st.rerun()
    with col2:
        # Rebuild focus list based on current answered status
        if st.button("🔁 Fokus nochmal starten", use_container_width=True):
            state["mode"] = "focus_wrong"
            state["focus_order"] = compute_focus_list()
            state["focus_cursor"] = 0
            save_json(player_file(player), state)
            st.rerun()
    st.stop()

if cursor_pos >= len(order) or all_answered:
    d = format_daily(state)
    if state.get('practice_mode') == 'wrong_only':
        st.success('🎯 Übungsrunde (nur falsche/übersprungene) abgeschlossen.')
        if st.button('↩️ Zurück zum normalen Quiz', use_container_width=True):
            state['practice_mode'] = 'all'
            state.pop('practice_answered', None)
            ensure_daily_order(state, player, questions)
            state['cursor'] = 0
            save_json(player_file(player), state)
            st.rerun()
        st.caption('Hinweis: Tagesstatistik/Leaderboard bleibt unverändert – das ist nur Üben.')
    else:
        st.success("🎉 Wow, du bist durch! Alle Fragen in diesem Durchlauf erledigt.")
    st.markdown(
        f"**Heute:** ✅ {d['correct']}  ·  ❌ {d['wrong']}  ·  🟡 {d.get('unsure',0)}  ·  🤷 {d['skipped']}  ·  🧮 {d['total']}"
    )

    # Collect wrong/unknown questions for a targeted session
    wrong_ids = []
    for qid0 in order:
        a = state.get("answered", {}).get(str(qid0)) or {}
        # Treat skipped as "to practice"; open questions (correct=None) are not counted as wrong
        if a.get("skipped") is True or a.get("correct") is False or a.get("unsure") is True:
            wrong_ids.append(int(qid0))

    # --- Export (CSV / PDF) for wrong questions ---
    by_id_finish = {int(qq["id"]): qq for qq in questions}
    wrong_questions = [by_id_finish.get(int(i)) for i in wrong_ids]
    wrong_questions = [w for w in wrong_questions if isinstance(w, dict)]

    if wrong_questions:
        st.markdown("#### 📤 Export deiner falschen/übersprungenen Fragen")

        # CSV
        csv_buf = io.StringIO()
        writer = csv.writer(csv_buf)
        writer.writerow(["id", "type", "question", "options", "correct", "explanation"])
        for w in wrong_questions:
            opts = w.get("options") or []
            corr = w.get("correct") or []
            writer.writerow([
                w.get("id"),
                w.get("type"),
                w.get("question"),
                " | ".join([str(x) for x in opts]),
                ",".join([str(x) for x in corr]),
                (w.get("explanation") or w.get("solution") or ""),
            ])
        csv_bytes = csv_buf.getvalue().encode("utf-8")
        st.download_button(
            "⬇️ Falsche Fragen als CSV",
            data=csv_bytes,
            file_name=f"falsche_fragen_{date.today().isoformat()}.csv",
            mime="text/csv",
            use_container_width=True,
        )

        # PDF (optional; requires reportlab)
        if canvas is not None and A4 is not None:
            pdf_buffer = io.BytesIO()
            c = canvas.Canvas(pdf_buffer, pagesize=A4)
            width, height = A4
            x = 40
            y = height - 50
            c.setFont("Helvetica-Bold", 14)
            c.drawString(x, y, f"Falsche/übersprungene Fragen – {player}")
            y -= 25
            c.setFont("Helvetica", 10)
            c.drawString(x, y, f"Datum: {date.today().isoformat()}   Anzahl: {len(wrong_questions)}")
            y -= 30

            def write_wrapped(text: str, y_pos: float, font="Helvetica", size=10, max_width=90):
                c.setFont(font, size)
                # Very simple wrapping by words
                words = (text or "").split()
                line = ""
                for w0 in words:
                    test = (line + " " + w0).strip()
                    if len(test) > max_width:
                        c.drawString(x, y_pos, line)
                        y_pos -= 14
                        line = w0
                    else:
                        line = test
                if line:
                    c.drawString(x, y_pos, line)
                    y_pos -= 14
                return y_pos

            for idx, w in enumerate(wrong_questions, start=1):
                if y < 120:
                    c.showPage()
                    y = height - 50
                c.setFont("Helvetica-Bold", 11)
                c.drawString(x, y, f"{idx}. (ID {w.get('id')})")
                y -= 16
                y = write_wrapped(str(w.get("question") or ""), y, font="Helvetica", size=10)

                if w.get("type") == "mc":
                    opts = w.get("options") or []
                    corr = set(w.get("correct") or [])
                    for oi, opt in enumerate(opts):
                        prefix = "✅" if oi in corr else "•"
                        y = write_wrapped(f"{prefix} {opt}", y, font="Helvetica", size=9)
                else:
                    sol = (w.get("solution") or "").strip()
                    if sol:
                        y = write_wrapped(f"Lösung: {sol}", y, font="Helvetica", size=9)

                exp = (w.get("explanation") or "").strip()
                if exp:
                    y = write_wrapped(f"Erklärung: {exp}", y, font="Helvetica", size=9)
                y -= 8

            c.save()
            pdf_bytes = pdf_buffer.getvalue()
            st.download_button(
                "⬇️ Falsche Fragen als PDF",
                data=pdf_bytes,
                file_name=f"falsche_fragen_{date.today().isoformat()}.pdf",
                mime="application/pdf",
                use_container_width=True,
            )
        else:
            st.caption("(PDF-Export nicht verfügbar – reportlab fehlt in requirements.txt.)")

    colA, colB = st.columns(2)
    with colA:
        if st.button("🔁 Nur die Falschen üben", use_container_width=True, disabled=(len(wrong_ids) == 0)):
            # New session order based on wrong questions
            state["practice_mode"] = "wrong_only"
            # Practice run should allow answering again -> separate answered map
            state["practice_answered"] = {}
            state["order"] = deterministic_shuffle(player, state.get("order_date", str(date.today())) + "|wrong", wrong_ids)
            state["cursor"] = 0
            save_json(player_file(player), state)
            st.rerun()
        if len(wrong_ids) == 0:
            st.caption("Keine falschen/übersprungenen Fragen — stark! 💪")

    with colB:
        if st.button("🎲 Alle von vorne (neu gemischt)", use_container_width=True):
            # Komplett neuer Durchlauf:
            # 1) Fortschritt dieses Durchlaufs zurücksetzen (sonst bleibt all_answered=True)
            state["answered"] = {}

            # 2) Heutige Zähler zurücksetzen (damit die Endübersicht nicht sofort wieder erscheint)
            today_key = str(date.today())
            state.setdefault("daily", {})
            state["daily"][today_key] = {"correct": 0, "wrong": 0, "skipped": 0, "unsure": 0, "total": 0}

            # 3) Aus Spezial-Modi raus (falls vorhanden)
            state["mode"] = "normal"
            state["practice_mode"] = "all"
            state.pop("focus_order", None)
            state.pop("focus_cursor", None)
            state.pop("resume_cursor", None)
            state.pop("practice_answered", None)

            # 4) Neue Mischung erzwingen (gleicher Tag -> anderer Shuffle)
            state["shuffle_nonce"] = int(state.get("shuffle_nonce", 0) or 0) + 1
            ensure_daily_order(state, player, questions)
            state["cursor"] = 0

            save_json(player_file(player), state)
            st.rerun()

    st.stop()

# Build quick lookup
by_id = {int(q["id"]): q for q in questions}
qid = int(order[cursor_pos])
q = by_id[qid]

st.progress((cursor_pos+1)/len(order))
nav1, nav2, nav3 = st.columns([1, 4, 1])
with nav1:
    if st.button("⬅ Zurück", disabled=(cursor_pos <= 0)):
        state["cursor"] = max(0, cursor_pos - 1)
        save_json(player_file(player), state)
        st.rerun()
with nav2:
    st.write(f"**Frage {cursor_pos+1} von {len(order)}**  ·  ID: **{qid}**")
with nav3:
    # Small helper button to go forward without changing the answer (useful when reviewing)
    if st.button("Weiter ➡", disabled=(cursor_pos >= len(order)-1)):
        state["cursor"] = min(cursor_pos + 1, len(order))
        save_json(player_file(player), state)
        st.rerun()

st.markdown(f"### {q['question']}")

answered_current = (active_answered or {}).get(str(qid))
if answered_current:
    st.caption("✅ Diese Frage wurde bereits beantwortet. Du kannst die Erklärung erneut anzeigen oder mit \"Weiter\" navigieren.")
    cexp, _ = st.columns([1, 3])
    with cexp:
        if st.button("📌 Erklärung anzeigen", key=f"exp_{qid}"):
            st.session_state["pending"] = {
                "qid": qid,
                "kind": "review",
                "title": "Lösung + Erklärung",
                "no_advance": True,
                # reuse stored payload so selected answers stay consistent
                "payload": answered_current,
            }
            st.rerun()

# Session state per question (to allow explanation popup after submit)
key_prefix = f"q{qid}"
if f"{key_prefix}_done" not in st.session_state:
    st.session_state[f"{key_prefix}_done"] = False
if f"{key_prefix}_result" not in st.session_state:
    st.session_state[f"{key_prefix}_result"] = None

# Pending modal state
if "pending" not in st.session_state:
    st.session_state["pending"] = None

def persist_and_advance(result_dict):
    # Count only the FIRST time a question is answered (prevents double counting when you navigate back).
    # First-time within the CURRENT run (normal or wrong-only practice)
    first_time = str(qid) not in (active_answered or {})
    # First-time overall (master progress) – used for daily/leaderboard counting
    first_time_master = str(qid) not in state.get("answered", {})

    in_focus = state.get("mode") == "focus_wrong"

    # --- Spaced repetition scheduling (normal mode only) ---
    # If a question was wrong OR marked as unsure OR skipped, schedule it to reappear later.
    # We do this only the first time the question is answered to avoid infinite duplication.
    def schedule_repeat_if_needed():
        if not first_time:
            return
        should_repeat = bool(result_dict.get("skipped")) or (result_dict.get("correct") is False) or bool(result_dict.get("unsure"))
        if not should_repeat:
            return

        # Avoid too many repeats of the same question
        repeats = int((result_dict.get("repeats") or 0))
        if repeats >= 2:
            return

        gap = int(state.get("sr_gap", 7) or 7)
        insert_at = min(cursor_pos + gap, len(order))

        # Don't schedule if it's already present ahead in the order
        if qid in order[cursor_pos+1:]:
            return

        state["order"] = order[:insert_at] + [qid] + order[insert_at:]
        # Mark that we scheduled a repeat for this question
        result_dict["repeats"] = repeats + 1

    if not in_focus:
        schedule_repeat_if_needed()

    # Update answered record
    prev = state.get("answered", {}).get(str(qid)) or {}
    merged = {**prev, **result_dict}

    # If we are re-practicing and now got it right (and not unsure), mark as mastered
    if in_focus and merged.get("correct") is True and not merged.get("skipped") and not merged.get("unsure"):
        merged["mastered"] = True

    state["answered"][str(qid)] = merged

    # Track answered inside practice runs so questions are answerable again.
    # - focus mode uses focus_answered
    # - end-screen "Nur die Falschen üben" uses practice_answered
    if in_focus:
        state.setdefault('focus_answered', {})[str(qid)] = merged
    elif state.get('practice_mode') == 'wrong_only':
        state.setdefault('practice_answered', {})[str(qid)] = merged

    # Advance cursor depending on mode
    if in_focus:
        state["focus_cursor"] = min(int(state.get("focus_cursor", cursor_pos)) + 1, len(order))
        # Do NOT change main cursor in focus mode
    else:
        # allow cursor == len(order) to represent "finished"
        state["cursor"] = min(cursor_pos+1, len(order))
    save_json(player_file(player), state)

    in_practice = state.get('practice_mode') == 'wrong_only'

    if first_time_master and (not in_practice) and (not in_focus):
        skipped = bool(result_dict.get("skipped"))
        correct_val = result_dict.get("correct")
        bump_daily(state, correct=correct_val, skipped=skipped, unsure=bool(result_dict.get("unsure")))
        save_json(player_file(player), state)

        # Update shared leaderboard (Supabase if configured; else local sqlite)
        day = str(date.today())
        if skipped:
            lb_upsert_daily(player, day, delta_skipped=1)
        else:
            if correct_val is True:
                lb_upsert_daily(player, day, delta_correct=1)
            elif correct_val is False:
                lb_upsert_daily(player, day, delta_wrong=1)

    st.session_state[f"{key_prefix}_done"] = True
    st.session_state[f"{key_prefix}_result"] = result_dict
    st.session_state["pending"] = None
    st.rerun()


def show_feedback_modal(pending: dict):
    """Modal-style feedback after submitting or clicking 'weiß nicht'."""
    exp_text = safe_explanation(q)

    # Build solution text
    solution_lines = []
    if q.get("type") == "mc":
        opts = q.get("options", [])
        corr = q.get("correct", [])
        if corr and opts:
            for i in corr:
                if 0 <= i < len(opts):
                    solution_lines.append(f"- {opts[i]}")
    else:
        sol = (q.get("solution") or "").strip()
        if sol:
            solution_lines.append(sol)

    title = pending.get("title", "Feedback")

    no_advance = bool(pending.get("no_advance"))

    if HAS_DIALOG:
        @st.dialog(title)
        def _dlg():
            kind = pending.get("kind")
            if kind == "submit":
                if pending.get("correct") is True:
                    st.success("✅ Richtig!")
                else:
                    st.error("❌ Falsch.")
            elif kind == "skip":
                st.warning("🤷 Kein Problem – hier ist die Lösung + Erklärung.")
            else:
                st.info("Gespeichert – hier ist die Lösung + Erklärung.")

            if solution_lines:
                st.markdown("**Lösung:**")
                st.markdown("\n".join(solution_lines))
            else:
                st.markdown("**Lösung:** (nicht hinterlegt)")

            st.markdown("**Erklärung:**")
            st.write(exp_text)

            if no_advance:
                if st.button("Schließen"):
                    st.session_state["pending"] = None
                    st.rerun()
            else:
                if st.button("Weiter"):
                    persist_and_advance(pending["payload"])

        _dlg()
    else:
        # Fallback (older Streamlit): toast + inline explanation
        if pending.get("kind") == "submit":
            st.toast("Richtig ✅" if pending.get("correct") else "Falsch ❌")
        elif pending.get("kind") == "skip":
            st.toast("Ich weiß nicht 🤷 – Lösung angezeigt")
        st.info("Deine Streamlit-Version unterstützt keine echten Pop-ups. Lösung/Erklärung werden unten angezeigt.")
        st.markdown("### ✅ Lösung")
        if solution_lines:
            st.markdown("\n".join(solution_lines))
        st.markdown("### 📌 Erklärung")
        st.write(exp_text)
        if no_advance:
            if st.button("Schließen"):
                st.session_state["pending"] = None
                st.rerun()
        else:
            if st.button("Weiter"):
                persist_and_advance(pending["payload"])


# If a modal is pending for THIS question, render it now and stop.
pending = st.session_state.get("pending")
if isinstance(pending, dict) and pending.get("qid") == qid:
    show_feedback_modal(pending)
    st.stop()

if q["type"] == "mc":
    multi = (q.get("answerType","single") == "multi")
    opts = q.get("options", [])
    if not opts:
        st.warning("Diese Frage hat keine Antwortoptionen (Datenproblem).")
    else:
        prev_selected = []
        if answered_current and isinstance(answered_current.get("selected"), list):
            prev_selected = answered_current.get("selected") or []
        locked = bool(answered_current)

        # When reviewing an already-answered question, visually mark:
        # ✅ correct options, ❌ options the user selected that were wrong.
        correct_set = set(int(i) for i in (q.get("correct") or []) if isinstance(i, int))
        selected_set = set(int(i) for i in (prev_selected or []) if isinstance(i, int))

        def option_label(i: int) -> str:
            base = opts[i]
            if not locked:
                return base
            # Review mode (locked)
            if i in correct_set:
                return f"✅ {base}"
            if i in selected_set and i not in correct_set:
                return f"❌ {base}"
            # Unselected + incorrect: keep neutral (still readable, but no icon)
            return f"   {base}"

        if multi:
            selected = st.multiselect(
                "Wähle alle zutreffenden Antworten:",
                list(range(len(opts))),
                format_func=option_label,
                default=[i for i in prev_selected if isinstance(i, int)],
                disabled=locked,
            )
        else:
            prev_index = prev_selected[0] if (prev_selected and isinstance(prev_selected[0], int)) else None
            selected_one = st.radio(
                "Wähle eine Antwort:",
                list(range(len(opts))),
                format_func=option_label,
                index=prev_index,
                disabled=locked,
            )
            selected = [] if selected_one is None else [selected_one]

        # Optional: mark as "unsicher" (will be scheduled for repetition)
        unsure_flag = st.checkbox("🟡 Ich bin mir unsicher (kommt später nochmal)", value=False, disabled=locked)

        col1, col2, col3 = st.columns([1,1,1])
        with col1:
            if st.button("Antwort abgeben", disabled=(not selected) or locked):
                correct = is_correct_mc(q, selected)
                st.session_state["pending"] = {
                    "qid": qid,
                    "kind": "submit",
                    "title": "Ergebnis",
                    "correct": bool(correct),
                    "payload": {
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "correct": bool(correct),
                    "selected": selected,
                    "unsure": bool(unsure_flag),
                    }
                }
                st.rerun()
        with col2:
            if st.button("Ich weiß nicht 🤷", disabled=locked):
                st.session_state["pending"] = {
                    "qid": qid,
                    "kind": "skip",
                    "title": "Lösung + Erklärung",
                    "payload": {
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "correct": False,
                    "selected": None,
                    "skipped": True,
                    }
                }
                st.rerun()
        with col3:
            st.write("")



elif q["type"] == "open":
    st.caption("Offene Frage: tippe deine Antwort (Stichpunkte reichen). Danach bekommst du Lösung + Hinweise.")
    prev_txt = ""
    if answered_current and answered_current.get("freeText") is not None:
        prev_txt = str(answered_current.get("freeText") or "")
    locked = bool(answered_current)
    user_answer = st.text_area("Deine Antwort", height=140, value=prev_txt, disabled=locked)

    unsure_flag = st.checkbox("🟡 Ich bin mir unsicher (kommt später nochmal)", value=False, disabled=locked)

    col1, col2 = st.columns([1,1])
    with col1:
        if st.button("Antwort speichern & Lösung anzeigen", disabled=locked):
            st.session_state["pending"] = {
                "qid": qid,
                "kind": "open",
                "title": "Lösung + Erklärung",
                "payload": {
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "correct": None,
                    "freeText": user_answer,
                    "unsure": bool(unsure_flag),
                },
            }
            st.rerun()
    with col2:
        if st.button("Ich weiß nicht 🤷", disabled=locked):
            st.session_state["pending"] = {
                "qid": qid,
                "kind": "skip",
                "title": "Lösung + Erklärung",
                "payload": {
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "correct": None,
                    "freeText": None,
                    "skipped": True,
                },
            }
            st.rerun()

else:
    st.warning("Unbekannter Fragetyp im Datensatz.")

st.divider()
st.subheader("➕ Neue Frage hinzufügen")
with st.expander("Neue Frage erstellen (wird dauerhaft gespeichert)"):
    new_type = st.selectbox("Typ", ["mc (Single Choice)", "mc (Multiple Choice)", "open"])
    new_question = st.text_area("Fragentext")
    if new_type.startswith("mc"):
        raw_opts = st.text_area("Antwortoptionen (eine pro Zeile)")
        correct_line = st.text_input("Richtige Option(en) – Indizes (0-basiert), z.B. 2 oder 0,3")
        new_hint = st.text_area("Hinweis (optional)")
        new_exp = st.text_area("Erklärung (optional)")
        if st.button("Speichern"):
            opts = [l.strip() for l in raw_opts.splitlines() if l.strip()]
            if not new_question.strip() or len(opts) < 2:
                st.error("Bitte Fragentext + mindestens 2 Optionen angeben.")
            else:
                try:
                    correct = [int(x.strip()) for x in correct_line.split(",") if x.strip() != ""]
                except Exception:
                    st.error("Konnte richtige Indizes nicht lesen. Beispiel: 2 oder 0,3")
                    st.stop()
                qobj = {
                    "type": "mc",
                    "question": new_question.strip(),
                    "options": opts,
                    "correct": correct,
                    "answerType": "multi" if "Multiple" in new_type else "single",
                    "hint": new_hint.strip(),
                    "explanation": new_exp.strip(),
                    "confidence": "user_added"
                }
                custom = load_json(CUSTOM_FILE, [])
                custom.append(qobj)
                save_json(CUSTOM_FILE, custom)
                st.success("Gespeichert! Starte die App neu oder aktualisiere die Seite.")
    else:
        sol = st.text_area("Lösungsvorschlag (optional)")
        hint = st.text_area("Hinweise (optional)")
        if st.button("Speichern"):
            if not new_question.strip():
                st.error("Bitte Fragentext angeben.")
            else:
                qobj = {
                    "type": "open",
                    "question": new_question.strip(),
                    "options": [],
                    "solution": sol.strip(),
                    "hint": hint.strip(),
                    "source": "user_added"
                }
                custom = load_json(CUSTOM_FILE, [])
                custom.append(qobj)
                save_json(CUSTOM_FILE, custom)
                st.success("Gespeichert! Starte die App neu oder aktualisiere die Seite.")
