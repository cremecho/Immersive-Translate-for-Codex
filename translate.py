"""Exchange translation segments with the current Codex chat and build a bilingual file."""

import argparse
from collections import Counter
from contextlib import closing
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import time

from immersive import packets


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / ".local-deps"))
ENGINES = {".docx": "word", ".pdf": "pdf_layout", ".md": "plain", ".txt": "plain"}
DB_NAME = "job.sqlite3"
# Bump when new PDF segment ids or their meaning change so automatic resume
# cannot mix a previous extraction with a different layout algorithm.
PACKET_LAYOUT_VERSION = "3"
DELIVERY_DIGEST_VERSION = "2"


def engine(path):
    return importlib.import_module("immersive." + ENGINES[path.suffix.lower()])


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def convert_doc(source, dest):
    if os.name != "nt":
        raise ValueError("此 Word 输入需要 Windows 上安装的 Microsoft Word 来生成常规 .docx 副本。")
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(ROOT / "immersive/legacy_word.ps1"), str(source), str(dest)],
        check=True, timeout=180,
    )


def prepare(source, job, target):
    source = source.resolve(strict=True)
    job = job.resolve()
    suffix = source.suffix.lower()
    if suffix not in {*ENGINES, ".doc"}:
        raise ValueError("支持 Word (.docx/.doc)、TXT、Markdown (.md) 和 PDF。")
    if not re.fullmatch(r"[a-zA-Z]{2,8}(?:-[a-zA-Z0-9]{1,8})*", target):
        raise ValueError("请使用语言标签，例如 zh-CN 或 en。")
    # Exclusive creation keeps concurrent preparations out of each other's workspace.
    job.mkdir(parents=True, exist_ok=False)
    try:
        before = digest(source)
        layout = source
        if suffix == ".doc":
            layout = job / "converted.docx"
            convert_doc(source, layout)
        backend = engine(layout)
        if suffix == ".docx":
            try:
                units = backend.extract(layout)
            except backend.StrictDocument:
                layout = job / "converted.docx"
                convert_doc(source, layout)
                units = backend.extract(layout)
        else:
            units = backend.extract(layout)
        if digest(source) != before:
            raise ValueError("提取期间原文件发生变化，请重新提取。")
        with closing(sqlite3.connect(job / DB_NAME)) as conn, conn:
            conn.execute("CREATE TABLE document(source TEXT, original_hash TEXT, converted INTEGER, layout_hash TEXT, target TEXT)")
            conn.execute("INSERT INTO document VALUES(?,?,?,?,?)",
                         (str(source), before, int(layout != source), before if layout == source else digest(layout), target))
            conn.execute("CREATE TABLE segments(id TEXT PRIMARY KEY, unit TEXT NOT NULL, translation TEXT)")
            conn.executemany("INSERT INTO segments(id, unit) VALUES(?,?)",
                             ((u["id"], json.dumps(u, ensure_ascii=False)) for u in units))
    except BaseException:
        # This process exclusively created this directory above.
        for filename in (DB_NAME + "-journal", DB_NAME, "converted.docx"):
            (job / filename).unlink(missing_ok=True)
        if not any(job.iterdir()):
            job.rmdir()
        raise
    return {"job": str(job), "segments": len(units), "target": target}


def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"JSON 中出现重复的键 {key}")
        result[key] = value
    return result


def pending(conn, limit, chars):
    if min(limit, chars) < 1:
        raise ValueError("批次大小必须大于零。")
    remaining = conn.execute("SELECT count(*) FROM segments WHERE translation IS NULL").fetchone()[0]
    units, used = [], 0
    for row, in conn.execute("SELECT unit FROM segments WHERE translation IS NULL ORDER BY rowid LIMIT ?", (limit,)):
        if units and used + len(row) > chars:
            break
        units.append(json.loads(row))
        used += len(row)
    return {"target": conn.execute("SELECT target FROM document").fetchone()[0],
            "remaining": remaining, "segments": units}


def show(conn, ids):
    records = []
    for key in ids:
        row = conn.execute("SELECT unit, translation FROM segments WHERE id=?", (key,)).fetchone()
        if row is None:
            raise ValueError(f"没有段落 {key}")
        records.append({**json.loads(row[0]), "translation": row[1]})
    return {"segments": records}


def add(conn, answers, replace=False):
    values = json.loads(answers.read_text(encoding="utf-8-sig"), object_pairs_hook=unique_keys)
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        save_values(conn, values, replace)
    remaining = conn.execute("SELECT count(*) FROM segments WHERE translation IS NULL").fetchone()[0]
    return {"accepted": len(values), "remaining": remaining}


def save_values(conn, values, replace=False):
    if not isinstance(values, dict) or not values:
        raise ValueError('译文文件应是非空 JSON 对象，如 {"段落ID":"译文"}。')
    for key, value in values.items():
        row = conn.execute("SELECT unit, translation FROM segments WHERE id=?", (key,)).fetchone()
        if not row or not isinstance(value, str) or not value.strip():
            raise ValueError(f"{key} 不是有效段号或译文为空。")
        if row[1] is not None and row[1] != value and not replace:
            raise ValueError(f"{key} 已有不同译文。校订时请使用 --replace。")
        original = json.loads(row[0])["text"]
        if key.startswith(("md-", "p")) or ".xml#" in key:
            marks = r"⟦(?:/?W*\d+/?|[mf]+\d+)⟧"
            if Counter(re.findall(marks, original)) != Counter(re.findall(marks, value)):
                raise ValueError(f"{key} 的格式标记有遗漏或重复，请对照原文。")
        conn.execute("UPDATE segments SET translation=? WHERE id=?", (value, key))


def start(source, job, target, chars):
    began = time.perf_counter()
    source = source.resolve(strict=True)
    source_hash = digest(source)
    if job is None:
        def job_for(version):
            key = hashlib.sha256((version + str(source) + source_hash + target).encode()).hexdigest()[:16]
            return Path.cwd() / ".translate-work" / ("document-" + key)

        previous = job_for("2")
        job = job_for(PACKET_LAYOUT_VERSION) if source.suffix.lower() == ".pdf" else previous
        if job != previous and not job.exists() and (previous / DB_NAME).is_file():
            with closing(sqlite3.connect(previous / DB_NAME)) as old:
                saved = old.execute("SELECT 1 FROM segments WHERE translation IS NOT NULL LIMIT 1").fetchone()
            if saved or any((previous / "packets").glob("*.answer.json")):
                job = previous
    job = job.resolve()
    if not (job / DB_NAME).is_file():
        prepare(source, job, target)
    with closing(sqlite3.connect(job / DB_NAME)) as conn:
        meta = conn.execute("SELECT source, original_hash, target FROM document").fetchone()
        if meta != (str(source), source_hash, target):
            raise ValueError("任务与当前原件或目标语言不匹配，请选择新目录。")
        packets.plan(conn, chars)
        packets.import_ready(conn, job, save_values, unique_keys)
        work = packets.pending_packets(conn, job)
        count = conn.execute("SELECT count(*) FROM segments").fetchone()[0]
        last = conn.execute("SELECT output FROM deliveries ORDER BY rowid DESC LIMIT 1").fetchone()
    result = {"job": str(job), "segments": count, "target": target, "packets": work,
              "prepare_seconds": round(time.perf_counter() - began, 3)}
    if not work and last:
        result["previous_output"] = last[0]
    return result


def finish(conn, job, output, *, auto_output=False, corrections=None):
    began = time.perf_counter()
    if not packets.has_plan(conn):
        raise ValueError("此任务尚未生成翻译批次。请用 start 指向同一原稿与任务目录。")
    packets.import_ready(conn, job, save_values, unique_keys)
    if corrections is not None:
        add(conn, corrections, replace=True)
    missing = conn.execute("SELECT count(*) FROM segments WHERE translation IS NULL").fetchone()[0]
    if missing:
        work = packets.pending_packets(conn, job)
        raise ValueError(f"还有 {missing} 段待译。待完成的答案文件：" + ", ".join(Path(b["answer"]).name for b in work))
    try:
        result = build(conn, job, output, checked=True, auto_output=auto_output)
    except ValueError as error:
        source = Path(conn.execute("SELECT source FROM document").fetchone()[0])
        if not auto_output or source.suffix.lower() != ".pdf" or not isinstance(error, engine(source).PdfFitError):
            raise
        items = []
        for line in str(error).splitlines():
            match = re.match(r"(p\d+-[bgt]\d+) (.*)", line)
            if match:
                raw, target = conn.execute("SELECT unit,translation FROM segments WHERE id=?", (match[1],)).fetchone()
                items.append(dict(id=match[1], source=json.loads(raw)["text"], current=target, reason=match[2]))
        if not items:
            raise
        result = {"delivery_status": "shorten", "items": items}
    result["finish_seconds"] = round(time.perf_counter() - began, 3)
    return result


def summarize_warnings(warnings):
    # Retain every id in the report, but surface failed checks ahead of the many
    # identical informational messages a scientific paper can produce.
    sections = {"check": [], "fit": [], "link": [], "math": []}
    for item in warnings:
        if "行内数学与正文共用" in item:
            section = "math"
        elif "译侧锚点" in item:
            section = "link"
        elif "缩至原字号" in item or "保留完整译文" in item:
            section = "fit"
        else:
            section = "check"
        sections[section].append(item)
    sample = [value for items in sections.values() for value in items[:2]]
    return {name: len(items) for name, items in sections.items() if items}, sample


def build(conn, job, output, checked=False, *, auto_output=False):
    rectangles = {}
    source_texts = None
    with conn:
        conn.execute("BEGIN")
        source, original_hash, converted, layout_hash, target = conn.execute("SELECT * FROM document").fetchone()
        if checked and Path(source).suffix.lower() == ".pdf":
            values = {}
            source_texts = {}
            for key, raw, value in conn.execute("SELECT id, unit, translation FROM segments ORDER BY rowid"):
                values[key] = value
                unit = json.loads(raw)
                source_texts[key] = unit["text"]
                if rect := unit.get("_rect"):
                    rectangles[key] = rect
        else:
            values = dict(conn.execute("SELECT id, translation FROM segments ORDER BY rowid"))
    missing = sum(v is None for v in values.values())
    if missing:
        raise ValueError(f"还有 {missing} 段待翻译，可用 pending 继续。")
    source = Path(source)
    layout = (job / "converted.docx") if converted else source
    if digest(source) != original_hash or (layout != source and digest(layout) != layout_hash):
        raise ValueError("原文件或转换副本已更改，请用新任务重新提取。")
    suffix = layout.suffix.lower()
    if output is None:
        folder = Path.cwd() / "output" / suffix[1:] if auto_output and suffix != ".md" else source.parent
        output = folder / f"{source.stem}-{target}-dual{suffix}"
    output = output.resolve()
    if output.suffix.lower() != suffix:
        raise ValueError(f"输出文件应使用 {suffix} 扩展名。")
    if checked:
        # Segment ids, their translations and the layout file identify the build.
        # Keep the original delivery digest format for already completed jobs.
        snapshot = json.dumps([DELIVERY_DIGEST_VERSION, layout_hash, target, values], ensure_ascii=False).encode()
        state = hashlib.sha256(snapshot).hexdigest()
        if auto_output:
            for name, expected, raw in conn.execute(
                    "SELECT output,digest,report FROM deliveries WHERE state=? ORDER BY rowid DESC", (state,)):
                candidate = Path(name)
                if (json.loads(raw).get("delivery_status") != "review" and candidate.is_file()
                        and digest(candidate) == expected):
                    output = candidate
                    break
            else:
                base = output
                version = 2
                while output.exists():
                    output = base.with_name(f"{base.stem}-v{version}{base.suffix}")
                    version += 1
        prior = conn.execute("SELECT state,digest,report FROM deliveries WHERE output=?", (str(output),)).fetchone()
        if prior and prior[0] == state and output.is_file() and digest(output) == prior[1]:
            stored = json.loads(prior[2])
            if "delivery_status" not in stored:
                # Older reports grouped informational renderer messages and
                # verifier findings together. Recheck only ambiguous deliveries.
                review = []
                if stored.get("warning_groups", {}).get("check"):
                    from immersive.verify import verify
                    review = verify(layout, output, values, rectangles=rectangles)["warnings"]
                stored["delivery_status"] = "review" if review else "ready"
                if review:
                    stored["review_findings"] = review
                with conn:
                    conn.execute("UPDATE deliveries SET report=? WHERE output=? AND state=? AND digest=?",
                                 (json.dumps(stored, ensure_ascii=False), str(output), state, prior[1]))
            return {**stored, "reused": True}
    if output.exists():
        raise FileExistsError(f"文件已存在，请另选输出路径：{output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, filename = tempfile.mkstemp(prefix=".bilingual-", suffix=suffix, dir=output.parent)
    os.close(descriptor)
    staging = Path(filename)
    result = {"output": str(output), "segments": len(values)}
    try:
        began = time.perf_counter()
        options = {}
        if source_texts is not None:
            result["page_cache"] = {}
            options = dict(source_texts=source_texts, cache_dir=job / "pdf-cache", source_hash=layout_hash,
                           stats=result["page_cache"])
        warnings = engine(layout).apply(layout, values, staging, **options)
        if checked:
            from immersive.verify import verify
            rendered = time.perf_counter()
            checks = verify(layout, staging, values, rectangles=rectangles)
            review = checks.pop("warnings", [])
            warnings = list(warnings or []) + review
            result["delivery_status"] = "review" if review else "ready"
            if review:
                result["review_findings"] = review
            result.update(render_seconds=round(rendered - began, 3),
                          check_seconds=round(time.perf_counter() - rendered, 3), checks=checks)
            report = job / "verification.json"
            packets.write_json(report, {**result, "warnings": warnings})
            counts, sample = summarize_warnings(warnings)
            result.update(warning_count=len(warnings), warning_groups=counts, report=str(report))
            warnings = sample
        if digest(layout) != layout_hash or (converted and digest(source) != original_hash):
            raise ValueError("排版期间输入文件发生变化，请重新提取。")
        # Publish only a finished file, atomically refusing a competing writer.
        if os.name == "nt":
            staging.rename(output)
        else:
            os.link(staging, output)
    finally:
        staging.unlink(missing_ok=True)
    result["warnings"] = warnings or []
    if checked:
        with conn:
            conn.execute("INSERT OR REPLACE INTO deliveries VALUES(?,?,?,?)",
                         (str(output), state, digest(output), json.dumps(result, ensure_ascii=False)))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    begin = commands.add_parser("start", help="提取原件并生成可并行翻译的文本包，支持续译")
    begin.add_argument("source", type=Path)
    begin.add_argument("--job", type=Path)
    begin.add_argument("--target", default="zh-CN")
    begin.add_argument("--chars", type=int, default=10000)
    begin.add_argument("--compact", action="store_true", help="桌面翻译任务只返回本次需要的包名和分组")
    done = commands.add_parser("finish", help="汇总译文、排版、验证并交付")
    prep = commands.add_parser("prepare", help="创建本地翻译任务")
    prep.add_argument("source", type=Path)
    prep.add_argument("--job", required=True, type=Path)
    prep.add_argument("--target", default="zh-CN")
    read = commands.add_parser("pending", help="读取待译批次")
    read.add_argument("--limit", type=int, default=35)
    read.add_argument("--chars", type=int, default=12000)
    save = commands.add_parser("add", help="提交当前对话翻译的 JSON")
    inspect = commands.add_parser("show", help="按段号查看原文和已有译文")
    write = commands.add_parser("build", help="写入双语文件")
    for cmd in [read, save, inspect, write, done]:
        cmd.add_argument("job", type=Path)
    inspect.add_argument("ids", nargs="+")
    save.add_argument("answers", type=Path)
    save.add_argument("--replace", action="store_true")
    write.add_argument("-o", "--output", type=Path)
    destination = done.add_mutually_exclusive_group()
    destination.add_argument("-o", "--output", type=Path)
    destination.add_argument("--auto-output", action="store_true", help="自动选择交付位置、复用成品并避开已有文件")
    done.add_argument("--corrections", type=Path, help="仅修订指定段号的 JSON 对象")
    args = parser.parse_args()
    if args.command == "start":
        task = start(args.source, args.job, args.target, args.chars)
        if args.compact:
            for i, packet in enumerate(task["packets"]):
                task["packets"][i] = {"lane": packet["lane"],
                                       **{k: str(Path(packet[k]).relative_to(task["job"])) for k in ("prompt", "answer")}}
        return task
    if args.command == "prepare":
        return prepare(args.source, args.job, args.target)
    database = (args.job / DB_NAME).resolve(strict=True)
    with closing(sqlite3.connect(database)) as conn:
        if args.command == "finish":
            return finish(conn, args.job.resolve(), args.output, auto_output=args.auto_output,
                          corrections=args.corrections)
        if args.command == "pending":
            return pending(conn, args.limit, args.chars)
        if args.command == "add":
            return add(conn, args.answers, args.replace)
        if args.command == "show":
            return show(conn, args.ids)
        return build(conn, args.job, args.output)


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    try:
        print(json.dumps(main(), ensure_ascii=False, indent=2))
    except (ValueError, OSError, RuntimeError, ImportError, sqlite3.Error, subprocess.SubprocessError) as error:
        sys.exit(str(error))
