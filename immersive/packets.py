"""Prepare independent, restartable text packets for translation in the desktop chat."""

from collections import defaultdict
import hashlib
import json
from pathlib import Path
import re
import tempfile


def write_json(path, data):
    # Stage in the same directory so another worker never reads a half-written packet.
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=".packet-", delete=False) as stream:
        temp = Path(stream.name)
        try:
            json.dump(data, stream, ensure_ascii=False, separators=(",", ":"))
        except BaseException:
            stream.close()
            temp.unlink(missing_ok=True)
            raise
    try:
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def _context(unit):
    context = unit.get("context", "")
    if unit["id"].startswith("p"):
        # The PDF id itself already identifies the page.
        return re.sub(r"^PDF 第 \d+ 页[。 ]*", "", context)
    return context


def _repeat_key(unit):
    text = unit["text"]
    context = re.sub(r"^第 \d+(?: 至 \d+)? 行[，。]?", "", _context(unit))
    # Only reuse long identical passages, or layout-identified running matter.
    # A short repeated label like "Scale" can mean different things.
    if len(text) >= 64 or unit.get("repeat_key"):
        return text, context, unit.get("repeat_key")
    return unit["id"]


_SPLIT_FORMULA = "独立的公式已保留原位，只翻译 text 中的片段。原行："
_SHARED_MATH = "正文与行内数学共用文字层，请保留完整算式、变量和上下标。"
_MATH_ALPHA = r"[\U0001d400-\U0001d7ff]"
_IDENTIFIER = re.compile(
    r"(?:https?://[A-Za-z0-9.-]+(?::\d+)?(?:[/?#][A-Za-z0-9._~:/?#@!$&'()*+,;=%-]*)?|"
    r"10\.\d{4,9}/[A-Za-z0-9._;()/:+-]+|"
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[a-zA-Z]{2,}|"
    r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})"
)


def _native_text(unit):
    """Only unambiguous machine notation; ordinary words and acronyms go to the chat."""
    if not re.match(r"^p\d+-", unit["id"]):
        # In a PDF, an identity answer retains the original glyphs exactly.
        # Editable formats instead append an additional translated paragraph.
        return False
    text = unit["text"].strip()
    if _IDENTIFIER.fullmatch(text):
        return True
    context = _context(unit)
    if not context.startswith(_SPLIT_FORMULA):
        return False
    # A PDF math font by itself can also style prose. Require the original
    # compound formula around this fragment as independent evidence.
    line = context[len(_SPLIT_FORMULA):].split("。", 1)[0]
    if not re.search(_MATH_ALPHA, line) or text not in line:
        return False
    fragment = re.escape(text)
    if re.fullmatch(_MATH_ALPHA + "+", text):
        return bool(re.search(_MATH_ALPHA + fragment + "|" + fragment + _MATH_ALPHA, line))
    return bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*\(", text) and re.search(fragment + _MATH_ALPHA, line))


def _prompt_context(key, context):
    if key.startswith("p"):
        context = context.replace("。" + _SHARED_MATH, "").removeprefix(_SHARED_MATH)
        context = context.removeprefix(_SPLIT_FORMULA)
        return context.replace("。保留公式标记：", "；").removeprefix("保留公式标记：")
    if key.startswith(("md-", "txt-")):
        context = re.sub(r"^第 \d+(?: 至 \d+)? 行[，。]?", "", context)
        return "" if context == "p" else context.removeprefix("p。")
    return context


def _source_row(raw):
    unit = json.loads(raw)
    row = [unit["id"], unit["text"]]
    if context := _context(unit):
        row.append(context)
    return row


def _packet_key(target, rows):
    data = json.dumps([target, rows], separators=(",", ":"), ensure_ascii=False).encode()
    return "@" + hashlib.sha256(data).hexdigest()[:16]


def _prompt(target, rows, is_pdf):
    # Original IDs stay in the public source packet. The worker
    # consumes a positional view and only needs to reproduce one batch key.
    items, contexts = [], {}
    for key, text, *notes in rows:
        hint = _prompt_context(key, notes[0]) if notes else ""
        if hint:
            reference = contexts.setdefault(hint, len(contexts) + 1)
            items.append([text, reference])
        else:
            items.append(text)
    result = {"key": _packet_key(target, rows), "target": target}
    if contexts:
        result["contexts"] = {number: hint for hint, number in contexts.items()}
    if is_pdf:
        result["pdf"] = "原位保留图内标签或乱码用 null；保留公式、变量和上下标。"
    result["items"] = items
    return result


def plan(conn, char_budget):
    if char_budget < 1:
        raise ValueError("批量字符数必须大于零。")
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='packet_items'").fetchone():
            return
        conn.execute("CREATE TABLE packet_items(batch INTEGER, leader TEXT, id TEXT PRIMARY KEY)")
        conn.execute("CREATE INDEX packets_by_batch ON packet_items(batch)")
        conn.execute("CREATE TABLE packet_receipts(batch INTEGER PRIMARY KEY, digest TEXT NOT NULL)")
        conn.execute("CREATE TABLE deliveries(output TEXT PRIMARY KEY, state TEXT, digest TEXT, report TEXT)")
        seen = {}
        batch, count, chars = 1, 0, 0
        rows = conn.execute("SELECT id,unit FROM segments WHERE translation IS NULL ORDER BY rowid")
        for key, raw in rows:
            unit = json.loads(raw)
            if _native_text(unit):
                conn.execute("UPDATE segments SET translation=? WHERE id=?", (unit["text"], key))
                continue
            repeat = _repeat_key(unit)
            assigned = seen.get(repeat)
            if assigned is None:
                cost = len(unit["text"]) + len(_prompt_context(key, _context(unit))) + 4
                if count and (chars + cost > char_budget or count >= 128):
                    batch += 1
                    count, chars = 0, 0
                assigned = seen[repeat] = batch, key
                count += 1
                chars += cost
            conn.execute("INSERT INTO packet_items VALUES(?,?,?)", (*assigned, key))


def _groups(conn):
    groups = defaultdict(list)
    rows = conn.execute("SELECT batch,leader,id FROM packet_items ORDER BY rowid")
    for batch, leader, key in rows:
        groups[batch].append((leader, key))
    return groups


def pending_packets(conn, job):
    target, document = conn.execute("SELECT target, source FROM document").fetchone()
    is_pdf = Path(document).suffix.lower() == ".pdf"
    folder = job / "packets"
    folder.mkdir(exist_ok=True)
    work = []
    rows = conn.execute("""WITH incomplete AS (
                             SELECT DISTINCT p.batch FROM packet_items p JOIN segments s ON p.id=s.id
                             WHERE s.translation IS NULL
                           )
                           SELECT p.batch, s.unit FROM packet_items p JOIN segments s ON s.id=p.leader
                           WHERE p.id=p.leader AND p.batch IN (SELECT batch FROM incomplete)
                           ORDER BY p.rowid""")
    source = defaultdict(list)
    for batch, raw in rows:
        source[batch].append(_source_row(raw))
    for batch, rows in source.items():
        src = folder / f"{batch:03}.source.json"
        prompt = folder / f"{batch:03}.prompt.json"
        dest = folder / f"{batch:03}.answer.json"
        packet = {"target": target, "items": rows}
        if is_pdf:
            packet["figure_policy"] = "翻译正文、独立表格和图注。若混入插图内部标签或图中乱码，该段答案填 JSON null，程序会原位保留图形。"
        write_json(src, packet)
        write_json(prompt, _prompt(target, rows, is_pdf))
        work.append({"source": str(src), "prompt": str(prompt), "answer": str(dest), "units": len(rows),
                     "chars": sum(len(row[1]) for row in rows)})
    # Balance long prose and short table entries by characters, not by row count.
    loads = [0] * min(4, len(work))
    for packet in sorted(work, key=lambda b: -b["chars"]):
        lane = loads.index(min(loads))
        packet["lane"] = lane + 1
        loads[lane] += packet["chars"]
    return work


def import_ready(conn, job, save_values, unique_keys):
    issues = []
    source_file, target = conn.execute("SELECT source,target FROM document").fetchone()
    is_pdf = Path(source_file).suffix.lower() == ".pdf"
    for batch, group in _groups(conn).items():
        answer = job / "packets" / f"{batch:03}.answer.json"
        if not answer.is_file():
            continue
        content = answer.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                receipt = conn.execute("SELECT digest FROM packet_receipts WHERE batch=?", (batch,)).fetchone()
                if receipt and receipt[0] == digest:
                    continue
                data = json.loads(content.decode("utf-8-sig"), object_pairs_hook=unique_keys)
                leaders = {first for first, _ in group}
                positions = {}
                if isinstance(data, list):
                    rows = [_source_row(unit) for unit, in conn.execute(
                        """SELECT s.unit FROM packet_items p JOIN segments s ON s.id=p.leader
                           WHERE p.batch=? AND p.id=p.leader ORDER BY p.rowid""", (batch,))]
                    if not data or data[0] != _packet_key(target, rows):
                        raise ValueError("首项应为此 prompt 的原始 key，请核对答案包。")
                    if len(data) != len(rows) + 1:
                        raise ValueError(f"此包需要 {len(rows)} 项译文，收到 {len(data) - 1} 项。")
                    positions = {row[0]: index for index, row in enumerate(rows, 1)}
                    data = {row[0]: value for row, value in zip(rows, data[1:])}
                if not isinstance(data, dict):
                    raise ValueError("答案应为带包 key 的有序数组，或段号到译文的 JSON 对象。")
                if data.keys() != leaders:
                    missing = sorted(leaders - data.keys())
                    extra = sorted(data.keys() - leaders)
                    raise ValueError(f"缺少 {len(missing)} 个段号 {missing[:6]}，多出 {len(extra)} 个段号 {extra[:6]}。")
                values = {key: data[leader] for leader, key in group}
                if is_pdf:
                    for key, value in values.items():
                        if value is None:
                            # Use the exact source run so the renderer leaves the
                            # corresponding native glyphs and artwork untouched.
                            raw, = conn.execute("SELECT unit FROM segments WHERE id=?", (key,)).fetchone()
                            values[key] = json.loads(raw)["text"]
                try:
                    save_values(conn, values, replace=bool(receipt))
                except ValueError as exc:
                    key = str(exc).split(" ", 1)[0]
                    leader = next((leader for leader, member in group if member == key), None)
                    if leader in positions:
                        raise ValueError(f"第 {positions[leader]} 项译文 — {exc}") from exc
                    raise
                conn.execute("INSERT OR REPLACE INTO packet_receipts VALUES(?,?)", (batch, digest))
        except (ValueError, UnicodeError) as exc:
            issues.append(f"{answer.name} — {exc}")
    if issues:
        raise ValueError("\n".join(issues))


def has_plan(conn):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE name='packet_items' AND type='table'").fetchone())
