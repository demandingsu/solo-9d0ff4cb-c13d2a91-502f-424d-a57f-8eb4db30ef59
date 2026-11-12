"""套件 / 器件双向核对。

判定口径：
- 缺记录：套件列出了某器件编号，但完整抓取后目录中不存在该编号的器件详情。
- 归属不符：器件标注的所属套件与“列入它的套件”对不上（含所属套件未收录、
  套件未列入该器件、器件未注明所属套件等）。
- 编号不符：套件清单直接链接到某器件详情，而该详情页实际编号与清单编号不符。
  这是两个页面可以直接证明的硬问题，即使存在编号冲突也照常报告。
- 待核实（编号冲突）：两个或以上器件详情共用同一馆藏编号且互相矛盾
  （如标注不同所属套件）。此时仅靠编号推断的归属结论不可信，一律标为
  待核实、不计入一致数量，并列出冲突的全部详情页来源；只有“链接直达且
  链接页实际编号与清单编号不符”这种两页直接证明的问题仍判编号不符。
- 未被套件列入：器件既未注明所属套件，也没有任何套件将其列入。
- 无法确认：存在抓取失败 / 抓取截断时，未找到的器件编号不直接判为缺记录。
"""
from __future__ import annotations

from datetime import datetime

from models import CrawlResult, ItemRecord, SuiteRecord


def cross_check(result: CrawlResult) -> dict:
    suites: list[SuiteRecord] = result.suites
    items: list[ItemRecord] = result.items
    incomplete = bool(result.failures) or bool(result.notes)
    failed_urls = {f.url for f in result.failures}

    # —— 编号冲突识别：同一编号出现在多个详情页 ——
    item_pages_by_no: dict[str, list[ItemRecord]] = {}
    items_by_url: dict[str, ItemRecord] = {}
    for it in items:
        item_pages_by_no.setdefault(it.number, []).append(it)
        items_by_url.setdefault(it.url, it)

    conflict_numbers: set[str] = {no for no, pages in item_pages_by_no.items() if len(pages) > 1}
    number_conflicts: list[dict] = [
        {"number": no, "kind": "器件", "pages": pages}
        for no, pages in sorted(item_pages_by_no.items())
        if len(pages) > 1
    ]

    suite_pages_by_no: dict[str, list[SuiteRecord]] = {}
    for s in suites:
        suite_pages_by_no.setdefault(s.number, []).append(s)
    for no, pages in sorted(suite_pages_by_no.items()):
        if len(pages) > 1:
            number_conflicts.append({"number": no, "kind": "套件", "pages": pages})

    missing: list[dict] = []        # 缺记录
    mismatches: list[dict] = []     # 归属不符 / 编号不符（硬问题）
    unverifiable: list[dict] = []   # 无法确认（因抓取失败）
    pending: list[dict] = []        # 待核实（编号冲突，编号推断不再可信）
    orphans: list[ItemRecord] = []  # 未被套件列入
    listed_by: dict[str, list[SuiteRecord]] = {it.number: [] for it in items}
    member_status: dict[tuple[str, str], tuple[str, ItemRecord | None, list[ItemRecord]]] = {}
    consistent_pairs = 0

    # —— 正向：以套件列出的器件编号为纲，逐一核对 ——
    for s in suites:
        for m in s.members:
            key = (s.url, m.accession)
            if m.link and m.link in failed_urls:
                unverifiable.append(
                    {"suite": s, "accession": m.accession, "link": m.link,
                     "reason": "器件详情页抓取失败，无法核对"}
                )
                member_status[key] = ("无法确认", None, [])
                continue

            linked = items_by_url.get(m.link) if m.link else None

            # 1) 清单链接直达详情，且两页直接证明编号不符：硬问题，照常报告，
            #    不被编号冲突降级。
            if linked is not None and linked.number != m.accession:
                mismatches.append(
                    {"problem": f"套件所列编号「{m.accession}」与详情页实际编号「{linked.number}」不一致",
                     "suite": s, "item": linked, "caveat": False}
                )
                listed_by.setdefault(linked.number, []).append(s)
                member_status[key] = ("编号不符", linked, [])
                continue

            # 2) 按编号定位详情（链接缺失，或链接页本身也声明同一编号）。
            target = linked
            if target is None:
                candidates = item_pages_by_no.get(m.accession, [])
                target = candidates[0] if candidates else None

            if target is None:
                if incomplete:
                    unverifiable.append(
                        {"suite": s, "accession": m.accession, "link": m.link,
                         "reason": "已抓取页面中未找到该编号；因存在抓取失败/截断，无法确认是否漏录"}
                    )
                    member_status[key] = ("无法确认", None, [])
                else:
                    missing.append({"suite": s, "accession": m.accession})
                    member_status[key] = ("缺记录", None, [])
                continue

            listed_by.setdefault(target.number, []).append(s)
            conflict_pages = item_pages_by_no.get(target.number, [])

            # 3) 该编号存在多页冲突：仅靠编号（含同编号链接）推断的归属不可信，
            #    标为待核实并列出全部冲突来源页，不计入一致。
            if target.number in conflict_numbers:
                pending.append(
                    {"suite": s, "accession": m.accession, "item": target,
                     "pages": conflict_pages,
                     "reason": "同一馆藏编号出现在多个器件详情页且归属标注不一致，"
                               "按编号推断的套件归属待人工核实"}
                )
                member_status[key] = ("待核实", target, conflict_pages)
                continue

            if target.suite_no is None:
                mismatches.append(
                    {"problem": "器件未注明所属套件，但该套件将其列入",
                     "suite": s, "item": target, "caveat": False}
                )
                member_status[key] = ("归属不符", target, [])
            elif target.suite_no != s.number:
                mismatches.append(
                    {"problem": f"器件标注所属套件为「{target.suite_no}」，与列入它的套件不符",
                     "suite": s, "item": target, "caveat": False}
                )
                member_status[key] = ("归属不符", target, [])
            else:
                consistent_pairs += 1
                member_status[key] = ("一致", target, [])

    # —— 反向：以器件标注的所属套件为纲，逐一核对 ——
    for it in items:
        # 编号冲突的器件，正向核对中已整体标为待核实，此处不再下硬结论
        if it.number in conflict_numbers:
            continue
        if it.suite_no:
            suite_pages = suite_pages_by_no.get(it.suite_no, [])
            s = suite_pages[0] if suite_pages else None
            if s is None:
                mismatches.append(
                    {"problem": f"器件标注所属套件为「{it.suite_no}」，但目录中未抓取到该套件",
                     "suite": None, "item": it, "caveat": incomplete}
                )
            elif not any(
                mem.accession == it.number or (mem.link and mem.link == it.url)
                for mem in s.members
            ):
                mismatches.append(
                    {"problem": f"器件标注所属「{it.suite_no}」，但该套件未将其列入",
                     "suite": s, "item": it, "caveat": False}
                )
        if not listed_by.get(it.number) and it.suite_no is None:
            orphans.append(it)

    mismatch_item_urls = {row["item"].url for row in mismatches if row.get("item")}
    orphan_urls = {it.url for it in orphans}

    suite_rows = []
    for s in suites:
        members = []
        for m in s.members:
            status, target, conflict_pages = member_status.get(
                (s.url, m.accession), ("—", None, [])
            )
            members.append(
                {"acc": m.accession, "link": m.link, "status": status,
                 "item": target, "conflict_pages": conflict_pages}
            )
        suite_rows.append({"suite": s, "members": members})

    item_rows = []
    for it in items:
        lbs = listed_by.get(it.number, [])
        if it.number in conflict_numbers:
            status = "待核实"
        elif it.url in orphan_urls:
            status = "未被套件列入"
        elif it.url in mismatch_item_urls:
            status = "归属不符"
        elif lbs and it.suite_no and any(s.number == it.suite_no for s in lbs):
            status = "一致"
        else:
            status = "—"
        item_rows.append(
            {"item": it, "listed_by": lbs, "status": status,
             "conflict_pages": item_pages_by_no.get(it.number, [])
             if it.number in conflict_numbers else []}
        )

    return {
        "start_url": result.start_url,
        "elapsed": result.elapsed,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "incomplete": incomplete,
        "notes": result.notes,
        "failures": result.failures,
        "missing": missing,
        "mismatches": mismatches,
        "orphans": orphans,
        "unverifiable": unverifiable,
        "pending": pending,
        "duplicates": result.duplicates,
        "number_conflicts": number_conflicts,
        "suite_rows": suite_rows,
        "item_rows": item_rows,
        "list_pages": result.list_pages,
        "stats": {
            "list_pages": len(result.list_pages),
            "suites": len(suites),
            "items": len(items),
            "declared": sum(len(s.members) for s in suites),
            "consistent": consistent_pairs,
            "pending": len(pending),
        },
    }
