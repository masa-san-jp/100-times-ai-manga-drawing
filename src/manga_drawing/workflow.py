"""Read-only production overview. Never starts generation or changes review state."""

from __future__ import annotations

import json


def summarize(db, project, counts):
    pid = project["id"]
    total = sum(counts.values())
    accepted = counts.get("accepted", 0)
    character = db.execute(
        """SELECT COUNT(*) total,COALESCE(SUM(CASE WHEN
      s.source_revision_id=r.current_revision_id AND
      (s.candidate_id IS NULL OR c.availability='available') THEN 1 ELSE 0 END),0) ready
      FROM reference_sets r LEFT JOIN character_reference_selections s ON s.reference_id=r.id
      LEFT JOIN candidates c ON c.id=s.candidate_id WHERE r.project_id=? AND r.kind='character'""",
        (pid,),
    ).fetchone()
    active = db.execute(
        """SELECT COUNT(*) FROM jobs j JOIN rounds r ON r.id=j.round_id
      JOIN pages p ON p.id=r.page_id WHERE p.project_id=? AND j.state IN ('queued','running','retry_wait','unknown')""",
        (pid,),
    ).fetchone()[0]
    latest = db.execute(
        "SELECT * FROM exports WHERE project_id=? ORDER BY created_at DESC LIMIT 1", (pid,)
    ).fetchone()
    completed = db.execute(
        "SELECT * FROM exports WHERE project_id=? AND state='complete' ORDER BY created_at DESC LIMIT 1",
        (pid,),
    ).fetchone()
    adopted = [
        dict(r)
        for r in db.execute(
            """SELECT p.number,c.id candidate_id,c.asset_hash,c.raw_asset_hash,
       j.round_id,json_extract(r.snapshot_json,'$.settings.provider') provider
       FROM pages p JOIN candidates c ON c.id=p.accepted_candidate_id JOIN jobs j ON j.id=c.job_id
       JOIN rounds r ON r.id=j.round_id WHERE p.project_id=? ORDER BY p.number""",
            (pid,),
        )
    ]
    missing = [
        r[0]
        for r in db.execute(
            "SELECT number FROM pages WHERE project_id=? AND accepted_candidate_id IS NULL ORDER BY number",
            (pid,),
        )
    ]
    required = set()
    for row in db.execute(
        """SELECT p.draft_json FROM pages p LEFT JOIN rounds r ON r.id=p.latest_round_id
      WHERE p.project_id=? AND p.accepted_candidate_id IS NULL
      AND (p.latest_round_id IS NULL OR r.review_state='rejected')""",
        (pid,),
    ):
        required.update(json.loads(row[0]).get("characters", []))
    available = {
        r[0]
        for r in db.execute(
            """SELECT r.stable_key FROM reference_sets r
      JOIN character_reference_selections s ON s.reference_id=r.id AND s.source_revision_id=r.current_revision_id
      LEFT JOIN candidates c ON c.id=s.candidate_id WHERE r.project_id=? AND r.kind='character'
      AND (s.candidate_id IS NULL OR c.availability='available')""",
            (pid,),
        )
    }
    missing_required = bool(required - available)
    settings = json.loads(project["settings_json"])
    current = False
    if completed:
        manifest = json.loads(completed["manifest_json"])
        current = (
            adopted
            == [
                {
                    k: item.get(k)
                    for k in (
                        "number",
                        "candidate_id",
                        "asset_hash",
                        "raw_asset_hash",
                        "round_id",
                        "provider",
                    )
                }
                for item in manifest["pages"]
            ]
            and manifest.get("missing_pages") == missing
            and manifest.get("width") == settings.get("width", 1410)
            and manifest.get("height") == settings.get("height", 1920)
            and manifest.get("title") == project["title"]
            and manifest.get("work_id") == project["work_id"]
            and manifest.get("work_title")
            == db.execute("SELECT title FROM works WHERE id=?", (project["work_id"],)).fetchone()[0]
        )
    export_status = "未書き出し"
    if completed:
        export_status = "採用画像と一致" if current else "採用・出力設定の変更あり"
    if latest and latest["state"] in ("queued", "running"):
        export_status = "書き出し待ち" if latest["state"] == "queued" else "書き出し中"
    elif latest and latest["state"] == "failed":
        export_status = "書き出し失敗"
    if counts.get("unknown", 0) or counts.get("error", 0):
        phase, message, label, url = (
            "recovery",
            "結果不明または生成失敗のページがあります。",
            "復旧対象を確認",
            "#generation-section",
        )
    elif counts.get("review", 0):
        phase, message, label, url = (
            "review",
            f"採否未判定が{counts['review']}ページあります。"
            + (f"ほかに{active}件の生成を処理中です。" if active else ""),
            "採否未判定のページを確認",
            "#page-grid",
        )
    elif active:
        phase, message, label, url = (
            "processing",
            f"{active}件を処理中です。生成状況を確認できます。",
            "生成状況を確認",
            "#generation-section",
        )
    elif latest and latest["state"] in ("queued", "running"):
        phase, message, label, url = (
            "exporting",
            "採用画像を書き出しています。",
            "書き出し状況を確認",
            "#export-section",
        )
    elif accepted == total and total:
        if current:
            phase, message, label, url = (
                "complete",
                "全ページ採用済みです。採用画像の書き出しも最新です。",
                "書き出し結果を確認",
                "#export-section",
            )
        else:
            phase, message, label, url = (
                "export",
                "全ページ採用済みです。採用画像を書き出せます。",
                "書き出しへ",
                "#export-section",
            )
    elif counts.get("rejected", 0):
        if missing_required:
            phase, message, label, url = (
                "characters",
                "再生成に必要なキャラクターリファレンスを採用してください。",
                "キャラクター資料へ",
                f"/projects/{pid}/characters",
            )
        else:
            phase, message, label, url = (
                "revision",
                f"要修正が{counts['rejected']}ページあります。修正指示を保存して再生成できます。",
                "要修正ページを確認",
                "#page-grid",
            )
    elif missing_required:
        phase, message, label, url = (
            "characters",
            "ページ作画に使用するキャラクターリファレンスを採用してください。",
            "キャラクター資料へ",
            f"/projects/{pid}/characters",
        )
    else:
        phase, message, label, url = (
            "generation",
            "未生成ページの作画を開始できます。",
            "生成対象を確認",
            "#generation-controls",
        )
    return {
        "phase": phase,
        "message": message,
        "action_label": label,
        "action_url": url,
        "characters": {"accepted": character["ready"], "total": character["total"]},
        "pages": {
            "accepted": accepted,
            "total": total,
            "unreviewed": counts.get("review", 0),
            "rejected": counts.get("rejected", 0),
            "not_requested": counts.get("new", 0),
        },
        "export": {"status": export_status, "current": current, "id": completed["id"] if completed else None},
    }
